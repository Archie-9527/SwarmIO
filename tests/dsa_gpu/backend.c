// SPDX-License-Identifier: GPL-2.0-only
/* v3共享服务器后端：只借用预先配置并启用的DMAengine通道。
 * 不配置硬件，不启停WQ，不解绑/复位整个DSA；退出只释放已借到的通道。
 * payload仍直接构造DSA BATCH，不经过DMAengine memcpy包装层。
 */
#include <linux/device.h>
#include <linux/dmaengine.h>
#include "dsa.h"
#include "backend.h"

struct idxd_device *dsa_idxd_list[DSA_MAX_DEVS];
static struct device *held_device;
static struct dma_chan *channels[DSA_MAX_WQS];
static unsigned int expected_groups, expected_count, expected_size[2];

static bool match_channel(struct dma_chan *chan, void *arg)
{
    struct idxd_wq *wq = arg;
    /* 先比较实际注册的channel指针，不把任意DMA提供者强转为idxd结构。 */
    return wq && wq->idxd_chan && chan == &wq->idxd_chan->chan &&
           wq->state == IDXD_WQ_ENABLED;
}
static int check_wq(struct idxd_device *idxd, unsigned int i)
{
    struct idxd_wq *wq = idxd->wqs[i];
    struct device *dev;
    if (!wq || !wq->group) return -ENODEV;
    dev = wq_confdev(wq);
    if (wq->state != IDXD_WQ_ENABLED || !dev->driver ||
        strcmp(dev->driver->name, "dmaengine") ||
        wq->type != IDXD_WQT_KERNEL || strcmp(wq->name, "dmaengine") ||
        !wq_dedicated(wq) || wq->group != idxd->groups[i / 2] ||
        wq->size != expected_size[i % 2] ||
        wq->max_batch_size < 1024 || wq->max_xfer_bytes < 65536 ||
        !wq->idxd_chan || !wq->portal || !wq->descs) {
        pr_err("dsa_gpu_test: dsa%d/wq%d.%u is not a matching enabled dedicated DMAengine WQ; run prepare_dsa.sh on a healthy, idle device\n",
               idxd->id, idxd->id, i);
        return -EINVAL;
    }
    return 0;
}
int dsa_set_params(const struct dsa_cfg_params *p)
{
    if (!p || p->num_devs != 1 || !p->num_groups_per_dev ||
        p->num_groups_per_dev > 4 || p->num_wqs_per_group != 2 ||
        p->num_engines_per_group != 1 || !p->wq_sizes ||
        !p->wq_sizes[0] || !p->wq_sizes[1]) return -EINVAL;
    expected_groups = p->num_groups_per_dev;
    expected_count = expected_groups * 2;
    expected_size[0] = p->wq_sizes[0]; expected_size[1] = p->wq_sizes[1];
    return 0;
}
bool dsa_test_ready(void)
{
    struct idxd_device *idxd = dsa_idxd_list[0];
    unsigned int i;
    if (!held_device || !idxd || idxd->state != IDXD_DEV_ENABLED) return false;
    for (i = 0; i < expected_count; i++)
        if (!channels[i] || check_wq(idxd, i) ||
            channels[i] != &idxd->wqs[i]->idxd_chan->chan) return false;
    return true;
}
void dsa_disable_devices(void)
{
    unsigned int i;
    /* 只释放成功申请的通道。部分初始化失败也走这里，不能对未知WQ做quiesce。
     * 所有测试DMA/描述符已由run_test排空并释放，才允许归还这些通道。
     */
    for (i = 0; i < DSA_MAX_WQS; i++) {
        if (!channels[i]) continue;
        dma_release_channel(channels[i]);
        channels[i] = NULL;
    }
    dsa_idxd_list[0] = NULL;
    if (held_device) { put_device(held_device); held_device = NULL; }
    pr_info("dsa_gpu_test: channels released; DSA/WQ driver bindings and hardware configuration left intact\n");
}
int dsa_enable_devices(void)
{
    struct idxd_device *idxd;
    dma_cap_mask_t mask;
    char name[16];
    unsigned int i;
    int ret;
    if (!expected_count || held_device) return -EINVAL;
    snprintf(name, sizeof(name), "dsa%u", dg_device_id);
    held_device = bus_find_device_by_name(&dsa_bus_type, NULL, name);
    if (!held_device) return -ENODEV;
    idxd = container_of(confdev_to_idxd_dev(held_device), struct idxd_device, idxd_dev);
    if (!held_device->driver || strcmp(held_device->driver->name, "idxd") ||
        idxd->state != IDXD_DEV_ENABLED || idxd->max_wqs < (int)expected_count) {
        pr_err("dsa_gpu_test: dsa%u must already be enabled and managed by idxd; no automatic device attach/reset is performed\n", dg_device_id);
        ret = -EINVAL; goto fail;
    }
    for (i = 0; i < expected_groups; i++) {
        unsigned int e, count = 0;
        for (e = 0; e < (unsigned int)idxd->max_engines; e++)
            if (idxd->engines[e]->group == idxd->groups[i]) count++;
        if (count != 1) { ret = -EINVAL; goto fail; }
    }
    for (i = 0; i < (unsigned int)idxd->max_wqs; i++)
        if (idxd->wqs[i]->client_count) {
            pr_err("dsa_gpu_test: dsa%u/wq%u.%u has active clients; refusing to take over\n", dg_device_id, dg_device_id, i);
            ret = -EBUSY; goto fail;
        }
    for (i = 0; i < expected_count; i++) {
        ret = check_wq(idxd, i); if (ret) goto fail;
    }
    dma_cap_zero(mask); dma_cap_set(DMA_MEMCPY, mask);
    for (i = 0; i < expected_count; i++) {
        channels[i] = dma_request_channel(mask, match_channel, idxd->wqs[i]);
        if (!channels[i]) {
            pr_err("dsa_gpu_test: cannot exclusively acquire dsa%u/wq%u.%u; rolling back only acquired channels\n",
                   dg_device_id, dg_device_id, i);
            ret = -EBUSY; goto fail;
        }
    }
    dsa_idxd_list[0] = idxd;
    return 0;
fail:
    dsa_disable_devices();
    return ret;
}
