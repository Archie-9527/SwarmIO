// SPDX-License-Identifier: GPL-2.0-only
/* 独立的 DSA↔NVIDIA GPU 搬运测试。参考 SwarmIO single.c/batch.c，
 * 直接使用 idxd 内部接口。需要与运行内核完全匹配的源码和符号表。
 * 与 SwarmIO 的物理地址假设不同，本测试为 DSA pdev 单独建立 P2P 映射。
 */
#include <linux/module.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <linux/delay.h>
#include <linux/ktime.h>
#include <linux/capability.h>
#include <linux/log2.h>
#include <linux/slab.h>
#include <linux/sched.h>
#include <nv-p2p.h>
#include "dsa.h"
#include "uapi.h"

#ifndef NVIDIA_P2P_CAP_GET_PAGES_PERSISTENT_API
#error "本测试要求 NVIDIA persistent P2P API；请使用支持该 API 的匹配驱动和头文件"
#endif
extern struct idxd_device *dsa_idxd_list[];
static DEFINE_MUTEX(run_lock);
static bool exclusive;
module_param(exclusive, bool, 0444);
MODULE_PARM_DESC(exclusive, "确认独占空闲 DSA 设备后设置为 1");
struct region {
    u64 va;
    nvidia_p2p_page_table_t *pages;
    nvidia_p2p_dma_mapping_t *map;
};
struct slot {
    struct idxd_desc *top;
    struct dsa_hw_desc *children;
    struct dsa_completion_record *comps;
    dma_addr_t children_dma, comps_dma;
    unsigned int n;
    bool active;
};

static void unpin(struct pci_dev *pdev, struct region *r)
{
    if (r->map)
        nvidia_p2p_dma_unmap_pages(pdev, r->pages, r->map);
    if (r->pages)
        nvidia_p2p_put_pages_persistent(r->va, r->pages, 0);
    memset(r, 0, sizeof(*r));
}
static int pin(struct pci_dev *pdev, struct region *r, u64 va, u64 bytes)
{
    int ret;
    r->va = va;
    ret = nvidia_p2p_get_pages_persistent(va, bytes, &r->pages, 0);
    if (ret) return ret;
    if (!NVIDIA_P2P_PAGE_TABLE_VERSION_COMPATIBLE(r->pages) ||
        r->pages->page_size != NVIDIA_P2P_PAGE_SIZE_64KB)
        return -EOPNOTSUPP;
    ret = nvidia_p2p_dma_map_pages(pdev, r->pages, &r->map);
    if (ret) return ret;
    if (!NVIDIA_P2P_DMA_MAPPING_VERSION_COMPATIBLE(r->map) ||
        r->map->page_size_type != NVIDIA_P2P_PAGE_SIZE_64KB ||
        r->map->entries < bytes / DG_ALIGN)
        return -EOPNOTSUPP;
    return 0;
}
static dma_addr_t gpu_addr(struct region *r, u64 off)
{
    return r->map->dma_addresses[off / DG_ALIGN] + off % DG_ALIGN;
}
static void prep(struct dsa_hw_desc *d, unsigned int op, u64 dst, u64 src,
                 u32 bytes, u64 completion)
{
    memset(d, 0, sizeof(*d));
    d->opcode = op;
    d->flags = IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR;
    d->src_addr = src; d->dst_addr = dst;
    d->xfer_size = bytes; d->completion_addr = completion;
}
/* 超时只能说明设备尚未完成，不能据此释放正在被 DMA 引用的内存。
 * 这里每 5 秒告警并继续等待，不响应信号；故障机器可能需要重启。
 * 该策略专用于独占硬件测试，避免原实现超时后回收在途资源的风险。
 */
static u8 wait_record(struct dsa_completion_record *c)
{
    unsigned long next = jiffies + 5 * HZ;
    u8 status;
    while (!(status = READ_ONCE(c->status))) {
        cpu_relax();
        if (need_resched()) cond_resched();
        if (time_after(jiffies, next)) {
            pr_err("dsa_gpu_test: DMA still pending; keep buffers pinned, do not unload/reset GPU\n");
            next = jiffies + 5 * HZ;
        }
    }
    dma_rmb();
    return status;
}
static int wait_slot(struct slot *s)
{
    unsigned int i;
    int ret = 0;
    if (!s->active) return 0;
    if (wait_record(s->top->completion) != DSA_COMP_SUCCESS) ret = -EIO;
    /* BATCH 成功后再检查每个子描述符；不能把提交成功当成搬运成功。 */
    if (!ret && s->n)
        for (i = 0; i < s->n; i++)
            if (READ_ONCE(s->comps[i].status) != DSA_COMP_SUCCESS) ret = -EIO;
    s->active = false;
    return ret;
}
static int submit(struct idxd_wq *wq, struct slot *s)
{
    int ret;
    s->top->completion->status = 0;
    dma_wmb();
    ret = idxd_submit_desc(wq, s->top);
    if (!ret) s->active = true;
    return ret;
}
static int copy_sync(struct idxd_wq *wq, struct slot *s, u64 dst, u64 src, u32 bytes)
{
    int ret;
    s->n = 0;
    prep(s->top->hw, DSA_OPCODE_MEMMOVE, dst, src, bytes, s->top->compl_dma);
    ret = submit(wq, s);
    return ret ? ret : wait_slot(s);
}
/* 同一 WQ 上的 DRAIN 在释放内存前等待此前操作退出。
 * 若无法证明 DMA 已停止，保留全部资源并等待管理员处理，禁止带病回收。 */
static void drain(struct idxd_wq *wq, struct slot *s)
{
    s->n = 0;
    prep(s->top->hw, DSA_OPCODE_DRAIN, 0, 0, 0, s->top->compl_dma);
    if (!submit(wq, s) && !wait_slot(s)) return;
    pr_emerg("dsa_gpu_test: DRAIN failed; resources quarantined; reboot required\n");
    for (;;) schedule_timeout_uninterruptible(HZ);
}
static long run(struct dg_run *r)
{
    struct idxd_wq *wq = dsa_idxd_list[0]->wqs[0];
    struct pci_dev *pdev = wq->idxd->pdev;
    struct device *dev = &pdev->dev;
    struct region data = {0}, sq = {0};
    struct slot slots[16] = {0};
    struct dg_cmd *cmds = NULL;
    dma_addr_t host_dma = 0, cmds_dma = 0;
    u64 *host = NULL, begin, stage, pos, n, i, j, blocks;
    unsigned long *touched = NULL;
    unsigned int allocated = 0, count, k;
    int ret = 0, err;
    bool submitted = false;
    if ((r->block_bytes != 512 && r->block_bytes != 4096) ||
        !is_power_of_2(r->data_bytes) || r->data_bytes < DG_ALIGN ||
        r->data_bytes > (64ULL << 20) || !r->operations || r->operations > 1048576 ||
        r->sq_bytes != ALIGN(r->operations * sizeof(*cmds), DG_ALIGN) ||
        (r->gpu_data | r->gpu_sq) % DG_ALIGN || !r->gpu_data || !r->gpu_sq ||
        r->gpu_data > U64_MAX - r->data_bytes || r->gpu_sq > U64_MAX - r->sq_bytes ||
        (r->gpu_data < r->gpu_sq + r->sq_bytes && r->gpu_sq < r->gpu_data + r->data_bytes) ||
        r->mode > 1 || r->direction > 2 || r->depth < 1 || r->depth > 16 ||
        r->batch_size < 2 || r->batch_size > DSA_MAX_BATCH_SIZE)
        return -EINVAL;
    blocks = r->data_bytes / r->block_bytes;
    host = dma_alloc_coherent(dev, r->data_bytes, &host_dma, GFP_KERNEL);
    cmds = dma_alloc_coherent(dev, DG_WINDOW * sizeof(*cmds), &cmds_dma, GFP_KERNEL);
    touched = bitmap_zalloc(blocks, GFP_KERNEL);
    if (!host || !cmds || !touched) { ret = -ENOMEM; goto out; }
    for (i = 0; i < r->data_bytes / 8; i++) {
        bool write = dg_write(i * 8 / r->block_bytes, r->direction);
        host[i] = dg_pattern(i) ^ (write ? U64_MAX : 0);
    }
    for (k = 0; k < r->depth; k++) {
        struct slot *s = &slots[k];
        s->top = idxd_alloc_desc(wq, IDXD_OP_BLOCK);
        if (IS_ERR(s->top)) { ret = PTR_ERR(s->top); s->top = NULL; goto out; }
        allocated++;
        s->children = dma_alloc_coherent(dev, 32 * sizeof(*s->children), &s->children_dma, GFP_KERNEL);
        s->comps = dma_alloc_coherent(dev, 32 * sizeof(*s->comps), &s->comps_dma, GFP_KERNEL);
        if (!s->children || !s->comps) { ret = -ENOMEM; goto out; }
    }
    ret = pin(pdev, &data, r->gpu_data, r->data_bytes);
    if (ret) goto out;
    ret = pin(pdev, &sq, r->gpu_sq, r->sq_bytes);
    if (ret) goto out;
    r->sq_ns = r->payload_ns = r->completed = r->host_errors = 0;
    begin = ktime_get_ns();
    for (pos = 0; pos < r->operations; pos += n) {
        n = min_t(u64, min_t(u64, DG_WINDOW, blocks), r->operations - pos);
        /* 128×64B 窗口不会跨越 64KiB GPU 页；每个窗口先取 SQE 再搬 payload。 */
        stage = ktime_get_ns(); submitted = true;
        if (!r->mode) {
            for (i = 0; i < n && !ret; i++)
                ret = copy_sync(wq, &slots[0], cmds_dma + i * sizeof(*cmds),
                    gpu_addr(&sq, (pos + i) * sizeof(*cmds)), sizeof(*cmds));
        } else {
            /* SwarmIO 风格的连续 SQE 合并获取；获取完成前不解析命令。 */
            ret = copy_sync(wq, &slots[0], cmds_dma, gpu_addr(&sq, pos * sizeof(*cmds)), n * sizeof(*cmds));
        }
        r->sq_ns += ktime_get_ns() - stage;
        if (ret) goto out;
        for (i = 0; i < n; i++) {
            u64 block = dg_block(pos + i, blocks, r->seed);
            /* 不信任 GPU 命令：校验长度、偏移、序号、方向，防止越界 DMA。
             * 同时验证 SQE 真正取回，而不是意外沿用上次窗口的内容。 */
            if (cmds[i].tag != pos + i || cmds[i].offset != block * r->block_bytes ||
                cmds[i].bytes != r->block_bytes || cmds[i].write != dg_write(block, r->direction)) {
                ret = -EBADMSG; goto out;
            }
            __set_bit(block, touched);
        }
        stage = ktime_get_ns();
        for (i = 0, count = 0; i < n; i += j, count++) {
            struct slot *s = &slots[count % r->depth];
            ret = wait_slot(s); if (ret) goto out;
            if (!r->mode) {
                u64 off = cmds[i].offset, g = gpu_addr(&data, off), h = host_dma + off;
                ret = copy_sync(wq, s, cmds[i].write ? h : g, cmds[i].write ? g : h, r->block_bytes);
                j = 1;
            } else {
                unsigned int actual = min_t(u64, r->batch_size, n - i);
                memset(s->comps, 0, 32 * sizeof(*s->comps));
                for (j = 0; j < actual; j++) {
                    u64 off = cmds[i+j].offset, g = gpu_addr(&data, off), h = host_dma + off;
                    prep(&s->children[j], DSA_OPCODE_MEMMOVE,
                        cmds[i+j].write ? h : g, cmds[i+j].write ? g : h,
                        r->block_bytes, s->comps_dma + j * sizeof(*s->comps));
                }
                s->n = actual;
                /* 硬件 BATCH 至少两项；尾批只有一项时补 NOOP，不计入完成数。 */
                if (actual == 1) {
                    prep(&s->children[1], DSA_OPCODE_NOOP, 0, 0, 0,
                         s->comps_dma + sizeof(*s->comps));
                    s->n = 2;
                }
                prep(s->top->hw, DSA_OPCODE_BATCH, 0, s->children_dma, s->n, s->top->compl_dma);
                ret = submit(wq, s);
                j = actual;
            }
            if (ret) goto out;
        }
        for (k = 0; k < r->depth; k++) { ret = wait_slot(&slots[k]); if (ret) goto out; }
        r->payload_ns += ktime_get_ns() - stage;
        r->completed += n;
        cond_resched();
    }
    r->total_ns = ktime_get_ns() - begin;
out:
    /* 所有退出路径都先处理已发布的 DMA，再撤销 P2P 映射/释放 host 内存。 */
    for (k = 0; k < allocated; k++) {
        err = wait_slot(&slots[k]); if (err && !ret) ret = err;
    }
    if (submitted) drain(wq, &slots[0]);
    if (!ret && host) {
        for (i = 0; i < r->data_bytes / 8; i++) {
            u64 b = i * 8 / r->block_bytes;
            bool changed = test_bit(b, touched) && dg_write(b, r->direction);
            u64 expected = dg_pattern(i) ^ ((dg_write(b, r->direction) && !changed) ? U64_MAX : 0);
            if (host[i] != expected) r->host_errors++;
        }
    }
    unpin(pdev, &sq); unpin(pdev, &data);
    for (k = 0; k < allocated; k++) {
        struct slot *s = &slots[k];
        if (s->children) dma_free_coherent(dev, 32 * sizeof(*s->children), s->children, s->children_dma);
        if (s->comps) dma_free_coherent(dev, 32 * sizeof(*s->comps), s->comps, s->comps_dma);
        idxd_free_desc(wq, s->top);
    }
    bitmap_free(touched);
    if (cmds) dma_free_coherent(dev, DG_WINDOW * sizeof(*cmds), cmds, cmds_dma);
    if (host) dma_free_coherent(dev, r->data_bytes, host, host_dma);
    return ret;
}
static long ioctl_run(struct file *f, unsigned int cmd, unsigned long arg)
{
    struct dg_run r;
    long ret;
    if (!capable(CAP_SYS_RAWIO)) return -EPERM;
    if (cmd != DG_RUN) return -ENOTTY;
    if (copy_from_user(&r, (void __user *)arg, sizeof(r))) return -EFAULT;
    if (!mutex_trylock(&run_lock)) return -EBUSY;
    ret = run(&r);
    mutex_unlock(&run_lock);
    if (!ret && copy_to_user((void __user *)arg, &r, sizeof(r))) return -EFAULT;
    return ret;
}
static const struct file_operations fops = { .owner = THIS_MODULE, .unlocked_ioctl = ioctl_run };
static struct miscdevice misc = { .minor = MISC_DYNAMIC_MINOR, .name = "dsa_gpu_test", .fops = &fops, .mode = 0600 };
static int __init test_init(void)
{
    unsigned int sizes[] = {64}, priorities[] = {1};
    struct dsa_cfg_params p = {1, 1, 1, 1, sizes, priorities};
    int ret;
    int id, i;
    struct device *found = NULL;
    struct idxd_device *idxd;
    char name[16];
    if (!exclusive) return -EINVAL;
    /* backend 会接管扫描到的第一个 DSA。接管前必须检查它的所有 WQ，
     * 避免重配已经启用的用户态队列或内核 dmaengine 队列。 */
    for (id = 0; id < DSA_SCAN_MAX_DEVS; id++) {
        snprintf(name, sizeof(name), "dsa%d", id);
        found = bus_find_device_by_name(&dsa_bus_type, NULL, name);
        if (found) break;
    }
    if (!found) return -ENODEV;
    /* backend 在 attach 前写配置，因此设备本身也必须先 disable/unbind，
     * 仅停用 WQ 不足以保证新的 group/engine 配置写入硬件。 */
    if (found->driver) {
        put_device(found);
        pr_err("dsa_gpu_test: disable the selected DSA device with accel-config first\n");
        return -EBUSY;
    }
    idxd = idxd_dev_to_idxd(confdev_to_idxd_dev(found));
    for (i = 0; i < idxd->max_wqs; i++) {
        if (idxd->wqs[i]->state != IDXD_WQ_DISABLED) {
            put_device(found);
            pr_err("dsa_gpu_test: all WQs on the selected DSA must be disabled\n");
            return -EBUSY;
        }
    }
    put_device(found);
    /* 与 SwarmIO 一样接管 DSA 配置；必须停用 SwarmIO 和其它 DSA 用户。 */
    ret = dsa_set_params(&p); if (ret) return ret;
    ret = dsa_enable_devices(); if (ret) return ret;
    ret = misc_register(&misc);
    if (ret) dsa_disable_devices();
    return ret;
}
static void __exit test_exit(void) { misc_deregister(&misc); dsa_disable_devices(); }
module_init(test_init); module_exit(test_exit);
MODULE_LICENSE("GPL");
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0)
MODULE_IMPORT_NS("IDXD");
#else
MODULE_IMPORT_NS(IDXD);
#endif
MODULE_DESCRIPTION("SwarmIO style GPU SQE fetch and CPU/GPU DSA payload benchmark");
