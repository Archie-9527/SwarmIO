// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief SwarmIO 模块配置、逻辑块单位和 namespace 时序接口。
 */
#ifndef _SWARMIO_COMMON_H
#define _SWARMIO_COMMON_H

#include <linux/pci.h>
#include <linux/msi.h>
#include <linux/wait.h>
#include <linux/atomic.h>
#include <linux/cpumask.h>
#include <linux/kthread.h>

#include <linux/nvme.h>

#include "logging.h"

/*
 * If CONFIG_SWARMIO_IDLE_TIMEOUT is set, sleep for a jiffie after
 * CONFIG_SWARMIO_IDLE_TIMEOUT seconds have passed to lower CPU power
 * consumption on idle.
 *
 * This may introduce a (1000/CONFIG_HZ) ms processing latency penalty
 * when exiting an I/O idle state.
 *
 * The default is set to 60 seconds, which is extremely conservative and
 * should not have an impact on I/O testing.
 */
#define CONFIG_SWARMIO_IDLE_TIMEOUT 600

#define VDEV_PCI_VERSION 0x0110
#define VDEV_PCI_DEV_ID VDEV_PCI_VERSION
#define VDEV_PCI_VENDOR_ID 0x0c51
#define VDEV_PCI_SUBSYSTEM_ID 0x370d
#define VDEV_PCI_SUBSYSTEM_VENDOR_ID VDEV_PCI_VENDOR_ID

#include "dsa/dsa.h"

#ifndef NR_MAX_IO_QUEUE
#define NR_MAX_IO_QUEUE 64
#endif
#ifndef NR_MAX_QUEUE_SIZE
#define NR_MAX_QUEUE_SIZE 1024
#endif
#ifndef NR_WORKER_QUEUE_SIZE
#define NR_WORKER_QUEUE_SIZE 4096
#endif

#define NR_MAX_DBS_SIZE (sizeof(u32) * 2 * (NR_MAX_IO_QUEUE + 1))

#define PAGE_OFFSET_MASK (PAGE_SIZE - 1)
#define PRP_PFN(x) ((unsigned long)((x) >> PAGE_SHIFT))

#define KB(k) ((k) << 10)
#define MB(m) ((m) << 20)
#define GB(g) ((g) << 30)

#define BYTE_TO_KB(b) ((b) >> 10)
#define BYTE_TO_MB(b) ((b) >> 20)
#define BYTE_TO_GB(b) ((b) >> 30)

/*
 * Active device configuration.
 * Legacy per-SSD configuration is no longer used in the active tree.
 */
#define MDTS (5)
/// @brief 协议逻辑块大小的以 2 为底的指数。当前为 9，对应 512 B。
#define LBA_BITS (9)
#define LBA_SIZE (1 << LBA_BITS)

#if defined(CONFIG_SWARMIO_DISP_PREFETCH_SQE) &&       \
	(!defined(CONFIG_SWARMIO_DISP_COALESCE_SQE) || \
	 !defined(CONFIG_SWARMIO_DMA_DISPATCHER))
#undef CONFIG_SWARMIO_DISP_PREFETCH_SQE
#endif

#if defined(CONFIG_SWARMIO_AGGREGATED_TIMING_UPDATE) && \
	!defined(CONFIG_SWARMIO_DISP_COALESCE_SQE)
#undef CONFIG_SWARMIO_AGGREGATED_TIMING_UPDATE
#endif

/**
 * @brief 将起始 LBA 换算为相对后备区起点的字节偏移。
 * @param cmd NVMe 读写命令。
 * @return 相对偏移，单位为字节。
 */
static inline size_t __cmd_io_offset(struct nvme_rw_command *cmd)
{
	return cmd->slba << LBA_BITS;
}

/**
 * @brief 将 NVMe 的零基块数换算为实际传输字节数。
 * @param cmd NVMe 读写命令，其中 length 为逻辑块数减一。
 * @return 传输总字节数。
 */
static inline size_t __cmd_io_size(struct nvme_rw_command *cmd)
{
	return (cmd->length + 1) << LBA_BITS;
}

/// @brief 模块加载时解析出的资源布局、线程配置与时序参数。
struct swarmio_cfg {
	/// 预留物理内存的起始地址。
	unsigned long memmap_start; // byte
	/// 预留物理内存的总字节数。
	unsigned long memmap_size; // byte

	unsigned long cmd_buffer_start; /**< 主机 SQE 暂存区的物理基地址。 */
	unsigned long cmd_buffer_size; /**< 所有 SQE 暂存区的总字节数。 */

	/// 虚拟盘后备 DRAM 的物理基地址。
	unsigned long storage_start; //byte
	/// 暴露给 namespace 的后备区字节数。
	unsigned long storage_size; // byte

	unsigned int num_service_units;
	unsigned int num_workers_per_service_unit;
	unsigned int num_workers;
	cpumask_var_t cpu_mask; /**< 用于绑定服务线程的 CPU 集合。 */

	/* TODO Refactoring storage configurations */
	unsigned int num_sched_insts; /**< 相互独立的模型调度实例数。 */
	/// 模型 I/O 单元大小的以 2 为底的指数。
	unsigned int io_unit_shift; // 2^

	unsigned int read_min_delay; // ns
	unsigned int read_sched_delay; // ns
	unsigned int write_min_delay; // ns
	unsigned int write_sched_delay; // ns

	unsigned int dma_batch_size;
};

#ifdef CONFIG_SWARMIO_PROFILE_REQ
struct swarmio_stats {
	unsigned long long sum;
	size_t count;
};

static inline void __set_stats_merge(struct swarmio_stats *stats1,
				     struct swarmio_stats *stats2)
{
	stats1->sum += stats2->sum;
	stats1->count += stats2->count;
}

static inline unsigned long long __get_stats_avg(struct swarmio_stats *stats)
{
	if (stats->count > 0)
		return stats->sum / stats->count;
	else
		return 0;
}

static inline void __set_stats_add(struct swarmio_stats *stats,
				   unsigned long long after,
				   unsigned long long before)
{
	if (after > before) {
		stats->sum += after - before;
		stats->count++;
	}
}

static inline void __set_stats_add_n(struct swarmio_stats *stats,
				     unsigned long long after,
				     unsigned long long before, size_t count)
{
	if (after > before) {
		stats->sum += after - before;
		stats->count += count;
	}
}

static inline void __reset_stats(struct swarmio_stats *stats)
{
	stats->sum = 0;
	stats->count = 0;
}
#endif

/**
 * @brief 在控制面置暂停标志时让服务线程进入屏障。
 * @param flag 控制面设置和清除的暂停标志。
 * @param[in,out] num_waiting 当前进入屏障的线程数。
 * @param wait_q 等待标志清除的等待队列。
 */
static inline void __thread_wait_on_flag(int *flag, atomic_t *num_waiting,
					 wait_queue_head_t *wait_q)
{
	if (unlikely(READ_ONCE(*flag))) {
		atomic_inc_return(num_waiting);
		wake_up_all(wait_q);
		wait_event(*wait_q, smp_load_acquire(flag) == 0 ||
			   kthread_should_stop());
		atomic_dec(num_waiting);
		wake_up_all(wait_q);
	}
}

struct swarmio_dispatcher;
struct swarmio_worker;
struct nvme_vdev;

/// @brief 单个模型调度实例的共享时序状态。
struct sched_inst_stat_t {
#ifdef CONFIG_SWARMIO_ATOMIC_CAS_TIMING_UPDATE
	atomic64_t t_next_available; /**< 最早可预约的服务时刻，单位为纳秒，经 CAS 更新。 */
#else
	u64 t_next_available; /**< 最早可预约的服务时刻，单位为纳秒，由外层锁保护。 */
#endif
};

/// @brief 时序模型的单命令输入和输出。
struct nvme_sched_info {
	struct nvme_command *cmd;
	uint64_t nsecs_start; /**< 时序计算采用的请求起点，单位为纳秒。 */
	uint32_t status;
	uint64_t nsecs_target; /**< 模型给出的最早完成时刻，单位为纳秒。 */
};

/// @brief 虚拟 namespace 的后备地址范围与时序计算入口。
struct swarmio_nvme_ns {
	uint32_t id;
	uint32_t csi;
	uint64_t size;
	void *mapped; /**< 后备 DRAM 的 CPU 虚拟映射。 */

	/// @brief 逐条计算命令的目标完成时间。
	bool (*sched_nvme_cmd)(struct swarmio_nvme_ns *ns,
			       struct nvme_sched_info *sched_info);

#ifdef CONFIG_SWARMIO_AGGREGATED_TIMING_UPDATE
#ifdef CONFIG_SWARMIO_PROFILE_REQ
	void (*sched_nvme_cmd_aggregated)(
		struct swarmio_nvme_ns *ns, struct nvme_command *cmds,
		unsigned long long nsecs_start,
		unsigned long long *nsecs_starts,
		unsigned long long *nsecs_targets, int num_reqs,
		struct swarmio_dispatcher *dispatcher);
#else
	void (*sched_nvme_cmd_aggregated)(
		struct swarmio_nvme_ns *ns, struct nvme_command *cmds,
		unsigned long long nsecs_start,
		unsigned long long *nsecs_targets, int num_reqs,
		struct swarmio_dispatcher *dispatcher);
#endif
#endif
};

#endif /* _SWARMIO_COMMON_H */
