// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief Worker 的本地环、请求生命周期及 DSA 上下文类型。
 */
#ifndef _SWARMIO_WORKER_H
#define _SWARMIO_WORKER_H

#include "common.h"

struct swarmio_dispatcher;
struct swarmio_commit_queue;
struct nvme_vdev;

#ifdef CONFIG_SWARMIO_WORKER_BATCH_IO
typedef struct dsa_dma_ctx_batch dma_ctx_t;
#else
typedef struct dsa_dma_ctx_single dma_ctx_t;
#endif

/**
 * @brief Dispatcher 传给 worker 的一条 I/O 工作项。
 * @details 默认路径依次记录开始处理、数据就绪和 CQE 发布。开启 committer 时，
 * worker 成功交接完成项后即可回收本地槽，CQE 由 committer 发布。
 */
struct swarmio_io_work {
	struct nvme_command cmd; /**< 从 SQE 复制的命令，包含 SLBA 和 PRP。 */

	union {
		struct nvme_completion cqe; /**< 用于生成 CQE 的 SQ ID 和 CID 等信息。 */
		struct {
			void *write_buffer;
			size_t buffs_to_release;
		} internal;
	};

#ifdef CONFIG_SWARMIO_PROFILE_REQ
	unsigned long long nsecs_start;
#endif
	unsigned long long nsecs_target; /**< 模型规定的最早完成时刻，单位为纳秒。 */
	int cqid; /**< 发布该命令完成项的 CQ ID。 */

	bool is_internal;
	bool is_issued; /**< Worker 已开始处理，所属 DSA batch 可能仍在组装。 */
	bool is_copied; /**< 数据就绪，此工作项可进入完成处理。 */
	bool is_completed; /**< 完成处理已成功，该槽可随连续队首回收。 */

#ifdef CONFIG_SWARMIO_PROFILE_REQ
	unsigned long long nsecs_enqueued;
	unsigned long long nsecs_issued;
	unsigned long long nsecs_copied;
#ifndef CONFIG_SWARMIO_COMMIT
	unsigned long long nsecs_start_cpl;
	unsigned long long nsecs_end_cpl;
#endif
#endif
};

/**
 * @brief 由 dispatcher 生产、worker 消费的本地环。
 * @details 两端以 acquire/release 读写和发布游标。
 */
struct swarmio_worker_local_queue {
	struct swarmio_io_work *works; /**< 环形工作项数组。 */

	unsigned short head; /**< 由 worker 推进，指向最老的待回收 work。 */
	unsigned short tail; /**< 由 dispatcher 推进，指向下一个可发布的槽。 */
	unsigned short size; /**< 环中槽位数，保留一个空槽用于区分空与满。 */
};

/// @brief 负责搬运数据并推进完成处理的内核线程状态。
struct swarmio_worker {
	struct swarmio_worker_local_queue local_queue;
#ifdef CONFIG_SWARMIO_COMMIT
	struct swarmio_commit_queue *commit_queue;
#endif
	dma_ctx_t *dma_ctx; /**< 该 worker 使用的 DSA 描述符和 WQ 上下文。 */
	u64 *paddr_list; /**< 解析当前命令 PRP 时复用的地址数组。 */

	struct swarmio_dispatcher *dispatcher;

	unsigned int id; /**< 该 worker 在所属服务单元内的编号。 */
	unsigned int disp_id; /**< 所属 dispatcher 的编号，即服务单元编号。 */

	struct task_struct *task_struct;
	char thread_name[32];

	unsigned int cpu_id;

#ifdef CONFIG_SWARMIO_PROFILE_REQ
	struct swarmio_stats stats_wait_issue;
	struct swarmio_stats stats_copy;
	struct swarmio_stats stats_wait_cpl;
	struct swarmio_stats stats_fill_cpl;
	struct swarmio_stats stats_error;
#endif
};

int swarmio_worker_init(struct nvme_vdev *vdev);
void swarmio_worker_exit(struct nvme_vdev *vdev);

#endif /* _SWARMIO_WORKER_H */
