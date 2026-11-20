// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (C) 2026 VIA-Research
 */

/**
 * @file
 * @brief Intel DSA 设备参数、WQ 分配和描述符执行接口。
 */
#ifndef __SWARMIO_DSA_H
#define __SWARMIO_DSA_H

/* 保留模块参数的数组尺寸和默认值，便于两种构建共用加载脚本。 */
#define DSA_MAX_WQS 16
#define DSA_MAX_WQ_SIZE 256
#define DSA_MIN_PRIORITY 1

#ifdef CONFIG_SWARMIO_DSA
#include <linux/kernel.h>
#include <linux/version.h>
#include <linux/pci.h>
#include <linux/dma-mapping.h>
#include <linux/idxd.h>
#include <linux/bitmap.h>
#include <linux/sched.h>

#include <idxd.h>

#include "logging.h"

#define DSA_MAX_DEVS 4
#define DSA_SCAN_MAX_DEVS 16
#define DSA_MAX_ENGINES 4
#define DSA_MAX_GROUPS 4
#define DSA_MAX_BATCH_SIZE 32
#define DSA_MIN_WQ_SIZE 1
#define DSA_MAX_PRIORITY 15
#define DSA_MAX_NUM_DESCS_PER_THREAD 64

/// @brief 部分填充批次的自适应提交等待上限，单位为微秒。
#define DSA_BATCH_MAX_SUBMIT_TIMEOUT_US 5LL
/// @brief 两轮批次回收之间的自适应等待上限，单位为微秒。
#define DSA_BATCH_MAX_SYNC_TIMEOUT_US 5LL

enum dsa_wq_alloc_mode {
	DSA_WQ_ALLOC_DEV_ORDERED = 0,
	DSA_WQ_ALLOC_DEV_INTERLEAVE,
};

/// @brief 模块加载参数传入的 DSA 设备、组、队列及引擎配置。
struct dsa_cfg_params {
	unsigned int num_devs;
	unsigned int num_groups_per_dev;
	unsigned int num_wqs_per_group;
	unsigned int num_engines_per_group;
	const unsigned int *wq_sizes; /**< 按组内 WQ 编号给出的队列容量。 */
	const unsigned int *wq_priorities; /**< 按组内 WQ 编号给出的硬件优先级。 */
};

struct dsa_engine_cfg {
	int group_id;
};

struct dsa_wq_cfg {
	u32 size;
	u32 priority;
	int group_id;
};

/// @brief 一个 DSA group 使用的 WQ 和 engine 编号集合。
struct dsa_group_cfg {
	int wq_ids[DSA_MAX_WQS];
	int engine_ids[DSA_MAX_ENGINES];
	int num_wqs;
	int num_engines;
};

struct dsa_dev_cfg {
	const struct dsa_group_cfg *groups;
	int num_groups;
};

/// @brief 一个线程持有的单描述符 DSA 上下文。
struct dsa_dma_ctx_single {
	struct idxd_desc **single_descs; /**< 预先从 IDXD WQ 分配的 MEMMOVE 描述符。 */

	struct idxd_wq *wq;
	void **data; /**< 完成时传回调用方的上下文，按描述符槽编号索引。 */

	DECLARE_BITMAP(issued_mask, DSA_MAX_NUM_DESCS_PER_THREAD); /**< 软件记录的在途描述符集合。 */

	unsigned long last_wait_time;
	int num_descs;
	int curr_desc;

	int dev_id;
	int wq_id;
	int timeout;
};

/// @brief 一个外层 BATCH 及其子描述符、完成记录和软件上下文。
struct dsa_batch_desc {
	struct idxd_desc *idxd_desc; /**< 向 IDXD WQ 投递的外层 BATCH 描述符。 */

	struct dsa_hw_desc *lv2_hw_descs; /**< 外层描述符引用的子操作数组，通常为 MEMMOVE。 */
	struct dsa_completion_record *lv2_compls; /**< 各子操作的设备完成记录。 */

	void **data; /**< 每个子描述符对应的回调参数。 */
	struct list_head node;
	int id;

	int num_filled; /**< 已填充的子描述符数，包含尾批补入的 NOOP。 */
};

/// @brief 每个 worker 的批次资源池和提交、回收状态。
struct dsa_dma_ctx_batch {
	struct dsa_batch_desc *batch_descs;
	struct dsa_hw_desc *lv2_hw_desc_list;
	struct dsa_completion_record *lv2_compl_list;
	dma_addr_t lv2_hw_desc_list_dma; /**< 设备读取子描述符数组所用的 DMA 基地址。 */
	dma_addr_t lv2_compl_list_dma; /**< 设备写入子操作完成记录的 DMA 基地址。 */

	struct idxd_wq *wq;
	void **data_list;

	struct list_head issued_descs; /**< 按提交顺序保存的在途批次，回收时取队首。 */
	DECLARE_BITMAP(issued_mask, DSA_MAX_NUM_DESCS_PER_THREAD); /**< 软件跟踪的在途批次集合。 */

	u64 start_issue_time;
	u64 last_wait_time;
	s64 batch_issue_timeout_ns; /**< 等待填满尾批的时间阈值，单位为纳秒。 */
	s64 batch_sync_timeout_ns; /**< 触发回收最老在途批次的时间阈值，单位为纳秒。 */
	int batch_size; /**< 每个外层 BATCH 的子描述符容量。 */
	int num_descs;
	int curr_desc; /**< 当前可填充的描述符槽，达到 num_descs 表示没有空槽。 */
	int curr_num_filled;

	int dev_id;
	int wq_id;
	int timeout;
};

/**
 * @brief 初始化 DSA 硬件描述符的通用操作、地址与完成记录字段。
 * @param[out] hw 待写入的硬件描述符。
 * @param opcode DSA 操作码，例如 MEMMOVE 或 BATCH。
 * @param src_addr 源地址，BATCH 时为子描述符数组的 DMA 地址。
 * @param dst_addr 目的地址，未使用的操作可传 0。
 * @param xfer_size MEMMOVE 的字节数，BATCH 时用于填写子描述符个数。
 * @param compl_dma 设备写入完成记录的 DMA 地址。
 * @param flags 描述符标志，例如请求写入设备完成记录。
 */
static inline void __dsa_prep_hw_desc(struct dsa_hw_desc *hw, char opcode,
				      u64 src_addr, u64 dst_addr, u64 xfer_size,
				      u64 compl_dma, u32 flags)
{
	hw->flags = flags;
	hw->opcode = opcode;
	hw->src_addr = src_addr;
	hw->dst_addr = dst_addr;
	hw->xfer_size = xfer_size;
	hw->priv = 0;
	hw->completion_addr = compl_dma;
}

/**
 * @brief 轮询 IDXD 描述符的设备完成记录。
 * @param desc 已提交的 IDXD 描述符。
 * @param timeout 软件轮询时限，单位为毫秒。
 * @return 观察到设备完成记录时返回 0，轮询超时返回 -ETIMEDOUT。
 */
static inline int __dsa_sync_wait(struct idxd_desc *desc, int timeout)
{
	unsigned long last_tick = jiffies;
	unsigned long timeout_jiffies = last_tick + msecs_to_jiffies(timeout);

	while (READ_ONCE(desc->completion->status) == DSA_COMP_NONE) {
		unsigned long now = jiffies;

		if (unlikely(time_after_eq(now, timeout_jiffies))) {
			SWARMIO_WARN_RL("wq %d polling timed out!\n",
					desc->wq->id);
			return -ETIMEDOUT;
		}
		if (unlikely(now != last_tick)) {
			last_tick = now;
			cond_resched();
		}
		cpu_relax();
	}
	dma_rmb();
	return 0;
}

int dsa_set_params(const struct dsa_cfg_params *p);
int dsa_enable_devices(void);
void dsa_disable_devices(void);
/* assign a dsa group to a service unit; 
 * allocate wqs in rr order within the dsa group */
void dsa_assign_wq_by_group_rr(int team_id, int local_id, int *dev_id,
			       int *wq_id);
/* assign dsa group to service unit; 
 * alloc wq0 dedicated to the leader (i.e., local_id = 0),
 * while remaining wqs are shared among other members */
void dsa_assign_wq_by_group_disagg(int team_id, int local_id, int *dev_id,
				   int *wq_id);

/* batch */
int dsa_dma_ctx_batch_init(struct dsa_dma_ctx_batch *ctx, int dev_id, int wq_id,
			   int batch_size, int num_descs, int timeout);
void dsa_dma_ctx_batch_remove(struct dsa_dma_ctx_batch *ctx);
bool dsa_dma_batch_idle(const struct dsa_dma_ctx_batch *ctx);
void dsa_dma_batch_reset(struct dsa_dma_ctx_batch *ctx);

int dsa_dma_issue_async_batch(struct dsa_dma_ctx_batch *ctx, dma_addr_t dma_dst,
			      dma_addr_t dma_src, size_t size, void *data,
			      void (*callback)(void *));
int dsa_dma_issue_async_remaining_batch(struct dsa_dma_ctx_batch *ctx);
int dsa_dma_wait_oldest_batch(struct dsa_dma_ctx_batch *ctx,
			      void (*callback)(void *));
int dsa_dma_wait_all_batch(struct dsa_dma_ctx_batch *ctx,
			   void (*callback)(void *));
bool __dsa_dma_is_issue_timeout_batch(struct dsa_dma_ctx_batch *ctx);
bool __dsa_dma_is_wait_timeout_batch(struct dsa_dma_ctx_batch *ctx);
bool __dsa_dma_all_busy_batch(struct dsa_dma_ctx_batch *ctx);

/* single */
int dsa_dma_ctx_single_init(struct dsa_dma_ctx_single *ctx, int dev_id,
			    int wq_id, int num_descs, int timeout);
void dsa_dma_ctx_single_remove(struct dsa_dma_ctx_single *ctx);
bool dsa_dma_single_idle(const struct dsa_dma_ctx_single *ctx);
void dsa_dma_single_reset(struct dsa_dma_ctx_single *ctx);

int dsa_dma_issue_async_single(struct dsa_dma_ctx_single *ctx,
			       dma_addr_t dma_dst, dma_addr_t dma_src,
			       size_t size, int *desc_id, void *data,
			       void (*callback)(void *));
int dsa_dma_issue_sync_single(struct dsa_dma_ctx_single *ctx,
			      dma_addr_t dma_dst, dma_addr_t dma_src,
			      size_t size, void *data,
			      void (*callback)(void *));
int dsa_dma_wait_one_single(struct dsa_dma_ctx_single *ctx, int desc_id,
			    void (*callback)(void *));
int dsa_dma_wait_all_single(struct dsa_dma_ctx_single *ctx,
			    void (*callback)(void *));
bool __dsa_dma_should_sync_single(struct dsa_dma_ctx_single *ctx);

#endif /* CONFIG_SWARMIO_DSA */
#endif
