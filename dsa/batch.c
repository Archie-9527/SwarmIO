// SPDX-License-Identifier: GPL-2.0-only
/* Single-owner asynchronous payload batches; no blocking wait on the hot path. */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/dma-mapping.h>
#include <linux/delay.h>
#include <linux/ktime.h>
#include "dsa.h"

extern struct idxd_device *dsa_idxd_list[DSA_MAX_DEVS];
static unsigned int flush_us = 5;
module_param_named(dma_batch_flush_us, flush_us, uint, 0444);
MODULE_PARM_DESC(dma_batch_flush_us, "Maximum partial batch fill time in microseconds");

enum { B_FREE, B_FILLING, B_ACTIVE, B_FAILED, B_DRAINING, B_QUARANTINED };

static void flush(struct dsa_dma_ctx_batch *ctx)
{
    struct dsa_batch_desc *s;
    int i = ctx->filling, n, ret;
    if (i < 0) return;
    s = &ctx->batch_descs[i]; n = s->num_filled;
    if (!n) return;
    ctx->filling = -1;
    if (!swarmio_dsa_batch_begin()) {
        s->state = B_FAILED; s->result = -EIO; return;
    }
    /* Hardware BATCH requires at least two children. Padding is not an I/O. */
    if (n == 1) {
        memset(&s->lv2_hw_descs[1], 0, sizeof(*s->lv2_hw_descs));
        __dsa_prep_hw_desc(&s->lv2_hw_descs[1], DSA_OPCODE_NOOP, 0, 0, 0,
            ctx->lv2_compl_list_dma + (i * ctx->batch_size + 1) * sizeof(*s->lv2_compls),
            IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR);
    }
    memset(s->lv2_compls, 0, (n == 1 ? 2 : n) * sizeof(*s->lv2_compls));
    __dsa_prep_hw_desc(s->idxd_desc->hw, DSA_OPCODE_BATCH,
        ctx->lv2_hw_desc_list_dma + i * ctx->batch_size * sizeof(*s->lv2_hw_descs),
        0, n == 1 ? 2 : n, s->idxd_desc->compl_dma,
        IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR);
    WRITE_ONCE(s->idxd_desc->completion->status, DSA_COMP_NONE);
    dma_wmb();
    ret = idxd_submit_desc(ctx->wq, s->idxd_desc);
    if (ret) {
        swarmio_dsa_batch_cancel();
        swarmio_dsa_error(false);
        pr_err("SwarmIO: async BATCH submission rejected: slot=%d count=%d error=%d\n", i, n, ret);
        s->state = B_FAILED; s->result = -EIO;
        return;
    }
    s->state = B_ACTIVE; s->start_ns = ktime_get_ns(); ctx->active++;
    swarmio_dsa_batch_submit(n);
}

int dsa_dma_ctx_batch_init(struct dsa_dma_ctx_batch *ctx, int dev_id, int wq_id,
                          int batch_size, int count, int timeout)
{
    struct idxd_device *idxd;
    size_t n;
    int i, ret = -ENOMEM;
    if (!ctx || dev_id != 0 || !(idxd = dsa_idxd_list[0]) || wq_id < 0 ||
        wq_id >= idxd->max_wqs || !idxd->wqs[wq_id] || batch_size < 2 ||
        batch_size > DSA_MAX_BATCH_SIZE || batch_size > idxd->wqs[wq_id]->max_batch_size ||
        count < 1 || count > DSA_MAX_NUM_DESCS_PER_THREAD || count > idxd->wqs[wq_id]->size ||
        timeout < 1 || !flush_us || flush_us > 100000) return -EINVAL;
    ctx->wq = idxd->wqs[wq_id]; ctx->dev = &idxd->pdev->dev;
    ctx->batch_size = batch_size; ctx->num_descs = count;
    ctx->timeout = timeout; ctx->filling = -1;
    n = (size_t)batch_size * count;
    ctx->lv2_hw_desc_list = dma_alloc_coherent(ctx->dev,
        n * sizeof(*ctx->lv2_hw_desc_list), &ctx->lv2_hw_desc_list_dma, GFP_KERNEL);
    ctx->lv2_compl_list = dma_alloc_coherent(ctx->dev,
        n * sizeof(*ctx->lv2_compl_list), &ctx->lv2_compl_list_dma, GFP_KERNEL);
    ctx->data_list = kcalloc(n, sizeof(*ctx->data_list), GFP_KERNEL);
    ctx->batch_descs = kcalloc(count, sizeof(*ctx->batch_descs), GFP_KERNEL);
    if (!ctx->lv2_hw_desc_list || !ctx->lv2_compl_list || !ctx->data_list || !ctx->batch_descs)
        goto fail;
    for (i = 0; i < count; ++i) {
        struct dsa_batch_desc *s = &ctx->batch_descs[i];
        struct idxd_desc *d = idxd_alloc_desc(ctx->wq, IDXD_OP_NONBLOCK);
        if (IS_ERR(d)) { ret = PTR_ERR(d); goto fail; }
        s->idxd_desc = d;
        s->lv2_hw_descs = ctx->lv2_hw_desc_list + i * batch_size;
        s->lv2_compls = ctx->lv2_compl_list + i * batch_size;
        s->data = ctx->data_list + i * batch_size;
    }
    return 0;
fail:
    dsa_dma_ctx_batch_remove(ctx);
    return ret;
}

int dsa_dma_batch_enqueue(struct dsa_dma_ctx_batch *ctx, dma_addr_t dst,
                         dma_addr_t src, size_t size, void *data)
{
    struct dsa_batch_desc *s;
    int i, j;
    if (swarmio_dsa_faulted()) return -EIO;
    if (!size || size > ctx->wq->max_xfer_bytes) return -EINVAL;
    if (ctx->filling < 0) {
        for (i = 0; i < ctx->num_descs; ++i)
            if (ctx->batch_descs[i].state == B_FREE) break;
        if (i == ctx->num_descs) return -EAGAIN;
        ctx->filling = i;
        ctx->batch_descs[i].state = B_FILLING;
        ctx->batch_descs[i].start_ns = ktime_get_ns();
    }
    i = ctx->filling; s = &ctx->batch_descs[i]; j = s->num_filled;
    memset(&s->lv2_hw_descs[j], 0, sizeof(*s->lv2_hw_descs));
    __dsa_prep_hw_desc(&s->lv2_hw_descs[j], DSA_OPCODE_MEMMOVE, src, dst, size,
        ctx->lv2_compl_list_dma + (i * ctx->batch_size + j) * sizeof(*s->lv2_compls),
        IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR);
    s->data[j] = data; s->num_filled++;
    if (s->num_filled == ctx->batch_size) flush(ctx);
    /* A rejected hardware submission still owns accepted entries until poll
     * reports failure. Never return an ambiguously partially accepted I/O. */
    return 0;
}

void dsa_dma_batch_flush_due(struct dsa_dma_ctx_batch *ctx)
{
    if (ctx->filling >= 0 && (swarmio_dsa_faulted() ||
        ktime_get_ns() - ctx->batch_descs[ctx->filling].start_ns >= (u64)flush_us * 1000))
        flush(ctx);
}

static void begin_drain(struct dsa_dma_ctx_batch *ctx, struct dsa_batch_desc *s)
{
    /* A failed BATCH can leave children pending. DRAIN must finish before
     * reporting errors, reusing the slot, or freeing any child/buffer. */
    swarmio_dsa_error(false); s->result = -EIO;
    __dsa_prep_hw_desc(s->idxd_desc->hw, DSA_OPCODE_DRAIN, 0, 0, 0,
        s->idxd_desc->compl_dma, IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR);
    WRITE_ONCE(s->idxd_desc->completion->status, DSA_COMP_NONE);
    dma_wmb();
    if (idxd_submit_desc(ctx->wq, s->idxd_desc)) {
        s->state = B_QUARANTINED;
        pr_err("SwarmIO: DSA DRAIN rejected; retaining batch resources for maintenance recovery\n");
    } else {
        s->state = B_DRAINING; s->start_ns = ktime_get_ns();
    }
}

void dsa_dma_batch_poll(struct dsa_dma_ctx_batch *ctx, void (*callback)(void *, int))
{
    int i, j;
    u64 now = ktime_get_ns();
    for (i = 0; i < ctx->num_descs; ++i) {
        struct dsa_batch_desc *s = &ctx->batch_descs[i];
        bool active = s->state == B_ACTIVE || s->state == B_DRAINING;
        unsigned int completed = 0;
        u64 bytes = 0;
        u8 status;
        if (active) {
            status = READ_ONCE(s->idxd_desc->completion->status);
            if (!status) {
                if (!s->timed_out && now - s->start_ns > (u64)ctx->timeout * 1000000) {
                    s->timed_out = true; s->result = -ETIMEDOUT;
                    swarmio_dsa_error(true);
                    pr_err("SwarmIO: async DSA timeout; retaining active batch until DMA ends\n");
                }
                continue;
            }
            dma_rmb();
            if (s->state == B_DRAINING) {
                if (DSA_COMP_STATUS(status) != DSA_COMP_SUCCESS) {
                    s->state = B_QUARANTINED;
                    pr_err("SwarmIO: DSA DRAIN failed; retaining batch resources\n");
                    continue;
                }
            } else {
                bool good = DSA_COMP_STATUS(status) == DSA_COMP_SUCCESS;
                int bad_child = -1;
                for (j = 0; j < (s->num_filled == 1 ? 2 : s->num_filled); ++j)
                    if (DSA_COMP_STATUS(READ_ONCE(s->lv2_compls[j].status)) != DSA_COMP_SUCCESS) {
                        good = false; bad_child = j; break;
                    }
                if (!good) {
                    pr_err("SwarmIO: async BATCH failed: slot=%d status=%#x child=%d child_status=%#x; draining\n",
                        i, status, bad_child, bad_child < 0 ? 0 : READ_ONCE(s->lv2_compls[bad_child].status));
                    begin_drain(ctx, s); continue;
                }
            }
            for (j = 0; j < s->num_filled; ++j) {
                if (DSA_COMP_STATUS(READ_ONCE(s->lv2_compls[j].status)) == DSA_COMP_SUCCESS) {
                    completed++; bytes += s->lv2_hw_descs[j].xfer_size;
                }
            }
            ctx->active--; swarmio_dsa_batch_retire(completed, bytes);
            if (s->result) s->state = B_FAILED;
        } else if (s->state != B_FAILED) {
            continue;
        }
        /* Don't expose an error CQE while another worker can still DMA into
         * this process: a GPU trap may immediately tear down all its buffers. */
        if (s->result && !swarmio_dsa_batch_idle()) continue;
        for (j = 0; j < s->num_filled; ++j) {
            if (callback) callback(s->data[j], s->result);
            s->data[j] = NULL;
        }
        s->num_filled = 0; s->result = 0; s->timed_out = false; s->state = B_FREE;
    }
}

void dsa_dma_ctx_batch_remove(struct dsa_dma_ctx_batch *ctx)
{
    int i;
    size_t n;
    if (!ctx) return;
    /* Never submit pending software entries during teardown. Failed
     * initialization has no active DMA; normal unload requires a drained run. */
    ctx->filling = -1;
    while (ctx->active) {
        dsa_dma_batch_poll(ctx, NULL);
        if (ctx->active) usleep_range(1000, 2000);
    }
    if (ctx->batch_descs)
        for (i = 0; i < ctx->num_descs; ++i)
            if (ctx->batch_descs[i].idxd_desc)
                idxd_free_desc(ctx->wq, ctx->batch_descs[i].idxd_desc);
    kfree(ctx->batch_descs); kfree(ctx->data_list);
    n = (size_t)ctx->batch_size * ctx->num_descs;
    if (ctx->lv2_compl_list)
        dma_free_coherent(ctx->dev, n * sizeof(*ctx->lv2_compl_list),
                          ctx->lv2_compl_list, ctx->lv2_compl_list_dma);
    if (ctx->lv2_hw_desc_list)
        dma_free_coherent(ctx->dev, n * sizeof(*ctx->lv2_hw_desc_list),
                          ctx->lv2_hw_desc_list, ctx->lv2_hw_desc_list_dma);
    ctx->batch_descs = NULL; ctx->data_list = NULL;
    ctx->lv2_compl_list = NULL; ctx->lv2_hw_desc_list = NULL;
}
