// SPDX-License-Identifier: GPL-2.0-only
/* Bounded descriptors, synchronous baseline, no reuse while DMA is pending. */
#include <linux/kernel.h>
#include <linux/pci.h>
#include <linux/dma-mapping.h>
#include <linux/delay.h>
#include "dsa.h"
extern struct idxd_device *dsa_idxd_list[DSA_MAX_DEVS];

int dsa_dma_ctx_single_init(struct dsa_dma_ctx_single *ctx, int dev_id,
                           int wq_id, int count, int timeout)
{
    struct idxd_device *idxd;
    int i, ret;
    if (!ctx || dev_id != 0 || !(idxd = dsa_idxd_list[0]) || wq_id < 0 ||
        wq_id >= idxd->max_wqs || count < 1 || count > DSA_MAX_NUM_DESCS_PER_THREAD ||
        count > idxd->wqs[wq_id]->size || timeout < 1) return -EINVAL;
    ctx->wq = idxd->wqs[wq_id]; ctx->num_descs = count; ctx->timeout = timeout;
    ctx->dev_id = dev_id; ctx->wq_id = wq_id;
    bitmap_zero(ctx->issued_mask, DSA_MAX_NUM_DESCS_PER_THREAD);
    ctx->data = kcalloc(count, sizeof(*ctx->data), GFP_KERNEL);
    ctx->single_descs = kcalloc(count, sizeof(*ctx->single_descs), GFP_KERNEL);
    if (!ctx->data || !ctx->single_descs) { ret = -ENOMEM; goto fail; }
    for (i = 0; i < count; ++i) {
        struct idxd_desc *d = idxd_alloc_desc(ctx->wq, IDXD_OP_NONBLOCK);
        if (IS_ERR(d)) { ret = PTR_ERR(d); goto fail; }
        ctx->single_descs[i] = d;
        /* Do not clear the kernel PASID configured by idxd_alloc_desc. */
        __dsa_prep_hw_desc(d->hw, DSA_OPCODE_MEMMOVE, 0, 0, 0, d->compl_dma,
                          IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR);
    }
    return 0;
fail:
    dsa_dma_ctx_single_remove(ctx);
    return ret;
}
int dsa_dma_wait_one_single(struct dsa_dma_ctx_single *ctx, int id,
                           void (*callback)(void *))
{
    struct idxd_desc *d;
    unsigned long deadline = jiffies + msecs_to_jiffies(ctx->timeout);
    bool timed_out = false;
    u8 status;
    if (id < 0 || id >= ctx->num_descs || !test_bit(id, ctx->issued_mask)) return -EINVAL;
    d = ctx->single_descs[id];
    while (!(status = READ_ONCE(d->completion->status))) {
        if (!timed_out && time_after(jiffies, deadline)) {
            timed_out = true; swarmio_dsa_error(true);
            pr_err("SwarmIO: DSA DMA timeout; retaining descriptor and buffers until completion, refusing new submissions\n");
        }
        if (timed_out) usleep_range(1000, 2000);
        else cpu_relax();
    }
    dma_rmb();
    clear_bit(id, ctx->issued_mask);
    ctx->curr_desc = id; ctx->last_wait_time = jiffies;
    if (DSA_COMP_STATUS(status) != DSA_COMP_SUCCESS) {
        swarmio_dsa_error(false);
        if (ctx->sq_fetch) swarmio_dsa_sq_end();
        pr_err("SwarmIO: DSA completion failed: wq=%d status=%#x\n", ctx->wq_id, status);
        return -EIO;
    }
    if (ctx->sq_fetch) {
        swarmio_dsa_sq_complete(d->hw->xfer_size);
        swarmio_dsa_sq_end();
    } else {
        swarmio_dsa_complete(d->hw->xfer_size);
    }
    if (timed_out) return -ETIMEDOUT;
    if (callback) callback(ctx->data[id]);
    return 0;
}
int dsa_dma_wait_all_single(struct dsa_dma_ctx_single *ctx, void (*callback)(void *))
{
    int i, ret = 0, rc;
    for_each_set_bit(i, ctx->issued_mask, ctx->num_descs) {
        rc = dsa_dma_wait_one_single(ctx, i, callback);
        if (rc) ret = rc;
    }
    return ret;
}
void dsa_dma_ctx_single_remove(struct dsa_dma_ctx_single *ctx)
{
    int i;
    if (!ctx) return;
    if (ctx->single_descs) {
        dsa_dma_wait_all_single(ctx, NULL);
        for (i = 0; i < ctx->num_descs; ++i)
            if (ctx->single_descs[i]) idxd_free_desc(ctx->wq, ctx->single_descs[i]);
    }
    kfree(ctx->single_descs); kfree(ctx->data);
    ctx->single_descs = NULL; ctx->data = NULL;
}
int dsa_dma_issue_async_single(struct dsa_dma_ctx_single *ctx, dma_addr_t dst,
        dma_addr_t src, size_t size, int *id_out, void *data, void (*callback)(void *))
{
    struct idxd_desc *d;
    int id;
    if (swarmio_dsa_faulted()) return -EIO;
    if (!size || size > ctx->wq->max_xfer_bytes) return -EINVAL;
    if (ctx->sq_fetch && size % 64) return -EINVAL;
    id = find_first_zero_bit(ctx->issued_mask, ctx->num_descs);
    if (id >= ctx->num_descs) return -EBUSY;
    d = ctx->single_descs[id];
    d->hw->src_addr = src; d->hw->dst_addr = dst; d->hw->xfer_size = size;
    WRITE_ONCE(d->completion->status, DSA_COMP_NONE);
    ctx->data[id] = data;
    if (ctx->sq_fetch && !swarmio_dsa_sq_begin()) return -EIO;
    dma_wmb();
    if (idxd_submit_desc(ctx->wq, d)) {
        swarmio_dsa_error(false);
        if (ctx->sq_fetch) swarmio_dsa_sq_end();
        return -EIO;
    }
    set_bit(id, ctx->issued_mask);
    if (ctx->sq_fetch) swarmio_dsa_sq_submit(size);
    else swarmio_dsa_submit();
    if (id_out) *id_out = id;
    return 0;
}
int dsa_dma_issue_sync_single(struct dsa_dma_ctx_single *ctx, dma_addr_t dst,
        dma_addr_t src, size_t size, void *data, void (*callback)(void *))
{
    int id, ret = dsa_dma_issue_async_single(ctx, dst, src, size, &id, data, callback);
    return ret ? ret : dsa_dma_wait_one_single(ctx, id, callback);
}
bool __dsa_dma_should_sync_single(struct dsa_dma_ctx_single *ctx)
{
    return ctx->curr_desc >= ctx->num_descs;
}
