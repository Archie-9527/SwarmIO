// SPDX-License-Identifier: GPL-2.0-only
/*
 * 无 DSA 构建的明确失败入口：CPU 模式不链接 idxd，也不把 DMA 请求伪装成成功。
 * 正常 CPU 路径不会调用这些函数；误调用时返回 -EOPNOTSUPP，初始化阶段即失败。
 */
#include <linux/errno.h>
#include "dsa.h"

int dsa_set_params(const struct dsa_cfg_params *p) { return -EOPNOTSUPP; }
int dsa_enable_devices(void) { return -EOPNOTSUPP; }
void dsa_disable_devices(void) {}

void dsa_assign_wq_by_group_rr(int team_id, int local_id, int *dev_id, int *wq_id)
{
	*dev_id = *wq_id = -1;
}

void dsa_assign_wq_by_group_disagg(int team_id, int local_id, int *dev_id, int *wq_id)
{
	*dev_id = *wq_id = -1;
}

int dsa_dma_ctx_single_init(struct dsa_dma_ctx_single *ctx, int dev_id,
			   int wq_id, int num_descs, int timeout)
{ return -EOPNOTSUPP; }
void dsa_dma_ctx_single_remove(struct dsa_dma_ctx_single *ctx) {}

int dsa_dma_issue_async_single(struct dsa_dma_ctx_single *ctx,
			      dma_addr_t dma_dst, dma_addr_t dma_src, size_t size,
			      int *desc_id, void *data, void (*callback)(void *))
{ return -EOPNOTSUPP; }
int dsa_dma_issue_sync_single(struct dsa_dma_ctx_single *ctx,
			     dma_addr_t dma_dst, dma_addr_t dma_src, size_t size,
			     void *data, void (*callback)(void *))
{ return -EOPNOTSUPP; }
int dsa_dma_wait_one_single(struct dsa_dma_ctx_single *ctx, int desc_id,
			    void (*callback)(void *))
{ return -EOPNOTSUPP; }
int dsa_dma_wait_all_single(struct dsa_dma_ctx_single *ctx, void (*callback)(void *))
{ return -EOPNOTSUPP; }
bool __dsa_dma_should_sync_single(struct dsa_dma_ctx_single *ctx) { return false; }

int dsa_dma_ctx_batch_init(struct dsa_dma_ctx_batch *ctx, int dev_id, int wq_id,
			  int batch_size, int num_descs, int timeout)
{ return -EOPNOTSUPP; }
void dsa_dma_ctx_batch_remove(struct dsa_dma_ctx_batch *ctx) {}
int dsa_dma_issue_async_batch(struct dsa_dma_ctx_batch *ctx, dma_addr_t dma_dst,
			     dma_addr_t dma_src, size_t size, void *data,
			     void (*callback)(void *))
{ return -EOPNOTSUPP; }
int dsa_dma_issue_async_remaining_batch(struct dsa_dma_ctx_batch *ctx)
{ return -EOPNOTSUPP; }
int dsa_dma_wait_oldest_batch(struct dsa_dma_ctx_batch *ctx, void (*callback)(void *))
{ return -EOPNOTSUPP; }
int dsa_dma_wait_all_batch(struct dsa_dma_ctx_batch *ctx, void (*callback)(void *))
{ return -EOPNOTSUPP; }
bool __dsa_dma_is_issue_timeout_batch(struct dsa_dma_ctx_batch *ctx) { return false; }
bool __dsa_dma_is_wait_timeout_batch(struct dsa_dma_ctx_batch *ctx) { return false; }
bool __dsa_dma_all_busy_batch(struct dsa_dma_ctx_batch *ctx) { return false; }
