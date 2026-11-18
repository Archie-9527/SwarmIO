// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief 服务单元的 dispatcher 状态、队列分工和派发入口。
 */
#ifndef _SWARMIO_DISPATCHER_H
#define _SWARMIO_DISPATCHER_H

#include "common.h"

struct swarmio_worker;
struct nvme_vdev;

/// @brief 一个服务单元中负责取 SQE 和派发 work 的内核线程状态。
struct swarmio_dispatcher {
	struct swarmio_worker **workers; /**< 属于本服务单元的 worker 指针数组。 */
	struct dsa_dma_ctx_single *dma_ctx; /**< 读取 SQE 使用的单描述符 DSA 上下文。 */

	struct {
		int start; /**< 负责的第一个 SQ ID，-1 表示尚未分配。 */
		int end; /**< 负责范围的 SQ ID 上界。 */
		int stride; /**< 扫描时相邻 SQ ID 之间的步长。 */
	} qids;

#ifdef CONFIG_SWARMIO_DISP_ALLOC_WORKER_RR
	int worker_turn;
#endif

	unsigned int id;
	unsigned int num_workers;

	struct task_struct *task_struct;
	char thread_name[32];

	unsigned int cpu_id;

#ifdef CONFIG_SWARMIO_PROFILE_REQ
	struct swarmio_stats stats_target;
	struct swarmio_stats stats_dispatch;
#endif

	unsigned long long *timing_workspace; /**< 聚合调度使用的线程私有临时数组。 */
};

#ifndef CONFIG_SWARMIO_DISP_ALLOC_WORKER_RR
/// @brief 按本 SU 中的 SQ 序号选择固定的 worker。
static inline unsigned int __get_worker(int sqid,
					struct swarmio_dispatcher *dispatcher)
{
	return (((sqid - dispatcher->qids.start) / dispatcher->qids.stride) %
		dispatcher->num_workers);
}
#else
/// @brief 按请求轮转选择 worker。
static inline unsigned int __get_worker(int sqid,
					struct swarmio_dispatcher *dispatcher)
{
	return dispatcher->worker_turn++ % dispatcher->num_workers;
}
#endif

int swarmio_dispatcher_init(struct nvme_vdev *vdev);
void swarmio_dispatcher_exit(struct nvme_vdev *vdev);
bool swarmio_proc_bars(struct swarmio_dispatcher *dispatcher);
#ifdef CONFIG_SWARMIO_DISP_PREFETCH_SQE
void swarmio_prefetch_sq(int qid, struct swarmio_dispatcher *dispatcher);
#endif
int swarmio_dispatch_sq(int qid, int new_db, int old_db,
			struct swarmio_dispatcher *dispatcher);

#endif /* _SWARMIO_DISPATCHER_H */
