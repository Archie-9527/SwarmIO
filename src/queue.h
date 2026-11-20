// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief 虚拟 NVMe 的 SQ、CQ、管理队列及按页定位队列项的辅助函数。
 */
#ifndef _SWARMIO_QUEUE_H
#define _SWARMIO_QUEUE_H

#include "common.h"

/// @brief SwarmIO 持有的 I/O SQ 状态和地址视图。
struct nvme_submission_queue {
	int qid;
	int cqid; /**< 该 SQ 的完成项写入的 CQ 编号。 */

	int queue_size;
	int sq_head; /**< 下一条待派发的 SQE 位置，也通过 CQE 回报。 */

	struct nvme_command **sq; /**< 系统内存中 SQ 的 CPU 逐页访问视图。 */
	struct nvme_command __iomem *mapped; /**< 设备内存中 SQ 的非缓存映射。 */
	dma_addr_t dma_addr; /**< 原始 SQ 的 DMA 基地址，DSA 取命令时作为源。 */

#ifdef CONFIG_SWARMIO_DISP_PREFETCH_SQE
	int pref_size; /**< 已预取或正在预取的连续 SQE 数。 */
	int dma_desc_id;
	bool is_pref_issued;
#endif

#if defined(CONFIG_SWARMIO_DISP_COALESCE_SQE)
	int fetched_pos; /**< 下一个待取的 SQ 槽，可以领先于实际派发的 sq_head。 */
#if defined(CONFIG_SWARMIO_AGGREGATED_TIMING_UPDATE) && \
	defined(CONFIG_SWARMIO_PROFILE_REQ)
	unsigned long long *nsecs_starts;
#endif
	unsigned long long *nsecs_targets; /**< 按 SQ 槽保存的最早完成时间，单位为纳秒。 */
#endif

	struct nvme_command *cmd_buffer; /**< 在主机预留 DRAM 中暂存 SQE 的 CPU 地址。 */
	dma_addr_t cmd_buffer_dma; /**< 同一暂存区的 DMA 地址，取 SQE 时作为目的。 */
};

/// @brief SwarmIO 持有的 I/O CQ 环状态和 CPU 写入视图。
struct nvme_completion_queue {
	int qid;
	int irq_vector;
	bool irq_enabled;
	bool interrupt_ready;
	bool phys_contig;

	spinlock_t entry_lock;
	struct mutex irq_lock;

	int queue_size;

	int phase; /**< 写入 CQE 的阶段位，每轮回绕时翻转。 */
	int cq_head; /**< 从 CQ doorbell 得到的客户端消费位置。 */
#if !defined(CONFIG_SWARMIO_DISP_ALLOC_WORKER_RR) || \
	defined(CONFIG_SWARMIO_COMMIT)
	int cq_tail; /**< 控制器下一次发布 CQE 的位置。 */
#else
	atomic_t cq_tail; /**< 多个 worker 共享 CQ 时用于原子预约槽位的尾指针。 */
#endif

	struct nvme_completion __iomem **cq; /**< CQ 的 CPU 逐页写入视图。 */
	void *mapped;
};

/// @brief 管理命令专用队列，由 dispatcher 0 处理。
struct nvme_admin_queue {
	int phase;

	int sq_depth;
	int cq_depth;

	int cq_head;

	struct nvme_command __iomem **nvme_sq;
	struct nvme_completion __iomem **nvme_cq;
};

#define NR_SQE_PER_PAGE (PAGE_SIZE / sizeof(struct nvme_command))
#define NR_CQE_PER_PAGE (PAGE_SIZE / sizeof(struct nvme_completion))

#define SQ_ENTRY_TO_PAGE_NUM(entry_id) (entry_id / NR_SQE_PER_PAGE)
#define CQ_ENTRY_TO_PAGE_NUM(entry_id) (entry_id / NR_CQE_PER_PAGE)

#define SQ_ENTRY_TO_PAGE_OFFSET(entry_id) (entry_id % NR_SQE_PER_PAGE)
#define CQ_ENTRY_TO_PAGE_OFFSET(entry_id) (entry_id % NR_CQE_PER_PAGE)

/// @brief 按环内槽号定位系统内存中的 I/O SQE。
static inline struct nvme_command *
__sq_entry_at(struct nvme_submission_queue *sq, int entry_id)
{
	return &sq->sq[SQ_ENTRY_TO_PAGE_NUM(entry_id)]
		      [SQ_ENTRY_TO_PAGE_OFFSET(entry_id)];
}

/// @brief 按环内槽号定位 I/O CQE。
static inline struct nvme_completion *
__cq_entry_at(struct nvme_completion_queue *cq, int entry_id)
{
	return &cq->cq[CQ_ENTRY_TO_PAGE_NUM(entry_id)]
		      [CQ_ENTRY_TO_PAGE_OFFSET(entry_id)];
}

static inline struct nvme_command *
__admin_sq_entry_at(struct nvme_admin_queue *queue, int entry_id)
{
	return &queue->nvme_sq[SQ_ENTRY_TO_PAGE_NUM(entry_id)]
			      [SQ_ENTRY_TO_PAGE_OFFSET(entry_id)];
}

static inline struct nvme_completion *
__admin_cq_entry_at(struct nvme_admin_queue *queue, int entry_id)
{
	return &queue->nvme_cq[CQ_ENTRY_TO_PAGE_NUM(entry_id)]
			      [CQ_ENTRY_TO_PAGE_OFFSET(entry_id)];
}

#endif /* _SWARMIO_QUEUE_H */
