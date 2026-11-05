// SPDX-License-Identifier: GPL-2.0-only
/* Borrow configured DMAengine channels. Never attach/detach/reset the DSA. */
#include <linux/module.h>
#include <linux/dmaengine.h>
#include <linux/iommu.h>
#include <linux/panic.h>
#include <linux/mm.h>
#include "dsa.h"

struct idxd_device *dsa_idxd_list[DSA_MAX_DEVS];
extern unsigned int dsa_num_devs, dsa_num_groups_per_dev, dsa_num_wqs_per_group;
extern unsigned int dsa_num_engines_per_group;
extern bool disp_using_dma, worker_using_dma;
static unsigned int device_id;
module_param_named(dsa_device_id, device_id, uint, 0444);
static struct device *held;
static struct dma_chan *channels[DSA_MAX_WQS];
static unsigned int groups;
static atomic_t fault = ATOMIC_INIT(0);
static atomic64_t submitted = ATOMIC64_INIT(0), completed = ATOMIC64_INIT(0);
static atomic64_t bytes_done = ATOMIC64_INIT(0), errors = ATOMIC64_INIT(0);
static atomic64_t timeouts = ATOMIC64_INIT(0);

void swarmio_dsa_error(bool timeout)
{
    atomic_set(&fault, 1); atomic64_inc(&errors);
    if (timeout) atomic64_inc(&timeouts);
}
bool swarmio_dsa_faulted(void) { return atomic_read(&fault); }
void swarmio_dsa_submit(void) { atomic64_inc(&submitted); }
void swarmio_dsa_complete(u64 bytes)
{
    atomic64_inc(&completed); atomic64_add(bytes, &bytes_done);
}
static int state_get(char *buf, const struct kernel_param *kp)
{
    unsigned int i, borrowed = 0;
    for (i = 0; i < DSA_MAX_WQS; ++i) borrowed += !!channels[i];
    return scnprintf(buf, PAGE_SIZE,
        "version=1 device=%u borrowed=%u fault=%d submitted=%lld completed=%lld bytes=%lld errors=%lld timeouts=%lld\n",
        device_id, borrowed, atomic_read(&fault), atomic64_read(&submitted),
        atomic64_read(&completed), atomic64_read(&bytes_done), atomic64_read(&errors), atomic64_read(&timeouts));
}
static const struct kernel_param_ops state_ops = { .get = state_get };
module_param_cb(dsa_state, &state_ops, NULL, 0444);

static bool match_channel(struct dma_chan *chan, void *arg)
{
    struct idxd_wq *wq = arg;
    return wq->idxd_chan && chan == &wq->idxd_chan->chan && wq->state == IDXD_WQ_ENABLED;
}
int dsa_set_params(const struct dsa_cfg_params *p)
{
    if (!p || p->num_devs != 1 || !p->num_groups_per_dev || p->num_groups_per_dev > 4 ||
        p->num_wqs_per_group != 2 || p->num_engines_per_group != 1 || !p->wq_sizes ||
        !p->wq_sizes[0] || !p->wq_sizes[1]) return -EINVAL;
    groups = p->num_groups_per_dev;
    dsa_num_devs = 1; dsa_num_groups_per_dev = groups;
    dsa_num_wqs_per_group = 2; dsa_num_engines_per_group = 1;
    return 0;
}
void dsa_disable_devices(void)
{
    unsigned int i;
    for (i = 0; i < DSA_MAX_WQS; ++i) {
        if (channels[i]) dma_release_channel(channels[i]);
        channels[i] = NULL;
    }
    dsa_idxd_list[0] = NULL;
    if (held) put_device(held);
    held = NULL;
    pr_info("SwarmIO: DSA channels released; device/WQ configuration and bindings preserved\n");
}
int dsa_enable_devices(void)
{
    struct idxd_device *idxd;
    dma_cap_mask_t mask;
    char name[16];
    unsigned int i;
    int ret = -EINVAL;
    if (held || !groups || test_taint(TAINT_DIE)) return -EBUSY;
    snprintf(name, sizeof(name), "dsa%u", device_id);
    held = bus_find_device_by_name(&dsa_bus_type, NULL, name);
    if (!held) return -ENODEV;
    idxd = container_of(confdev_to_idxd_dev(held), struct idxd_device, idxd_dev);
    if (!held->driver || strcmp(held->driver->name, "idxd") || idxd->state != IDXD_DEV_ENABLED ||
        idxd->max_wqs < (int)groups * 2 || idxd->max_groups < (int)groups) goto fail;
    /* Only the tested physical-address path: never assume translated/PASID
     * addresses are identical to physical addresses of reserved host RAM. */
    if (iommu_get_domain_for_dev(&idxd->pdev->dev) || device_pasid_enabled(idxd)) {
        pr_err("SwarmIO: DSA baseline requires intel_iommu=off and no kernel PASID\n");
        ret = -EOPNOTSUPP; goto fail;
    }
    dma_cap_zero(mask); dma_cap_set(DMA_MEMCPY, mask);
    for (i = 0; i < groups * 2; ++i) {
        struct idxd_wq *wq;
        if ((i % 2 == 0 && !disp_using_dma) || (i % 2 && !worker_using_dma)) continue;
        wq = idxd->wqs[i];
        if (wq && wq->client_count) { ret = -EBUSY; goto fail; }
        if (!wq || wq->state != IDXD_WQ_ENABLED || !wq_confdev(wq)->driver ||
            strcmp(wq_confdev(wq)->driver->name, "dmaengine") ||
            wq->type != IDXD_WQT_KERNEL || !wq_dedicated(wq) ||
            wq->group != idxd->groups[i / 2] || !wq->idxd_chan || !wq->descs || !wq->portal ||
            wq->size < 2 || wq->max_xfer_bytes < PAGE_SIZE) goto fail;
        channels[i] = dma_request_channel(mask, match_channel, wq);
        if (!channels[i]) { ret = -EBUSY; goto fail; }
    }
    dsa_idxd_list[0] = idxd;
    pr_info("SwarmIO: borrowed dsa%u WQs; no device reconfiguration\n", device_id);
    return 0;
fail:
    dsa_disable_devices();
    return ret;
}
void dsa_assign_wq_by_group_disagg(int team, int local, int *dev, int *wq)
{
    *dev = 0; *wq = 2 * (team % groups) + (local != 0);
}
void dsa_assign_wq_by_group_rr(int team, int local, int *dev, int *wq)
{
    dsa_assign_wq_by_group_disagg(team, local, dev, wq);
}
