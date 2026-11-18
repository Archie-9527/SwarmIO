// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief 虚拟 NVMe 控制器的设备状态与生命周期接口。
 */
#ifndef _SWARMIO_NVME_VDEV_H
#define _SWARMIO_NVME_VDEV_H

#include "common.h"
#include "queue.h"

/// @brief 虚拟 PCI/NVMe 设备共享状态，供服务线程访问队列和后备区。
struct nvme_vdev {
	struct pci_bus *virt_bus;
	void *virtDev;
	struct pci_header *pcihdr;
	struct pci_pm_cap *pmcap;
	struct pci_msix_cap *msixcap;
	struct pcie_cap *pciecap;
	struct pci_ext_cap *extcap;

	struct pci_dev *pdev;

	struct swarmio_cfg config;

	void *storage_mapped; /**< 后备存储的 CPU 虚拟地址，供 CPU 拷贝路径使用。 */

	struct swarmio_dispatcher *dispatchers;
	struct swarmio_worker *workers;
	unsigned int num_service_units_initialized;
	unsigned int num_workers_initialized;
#ifdef CONFIG_SWARMIO_COMMIT
	struct swarmio_committer *committers;
	unsigned int num_committers_initialized;
#endif

	void __iomem *msix_table;

	bool intx_disabled;

	struct __nvme_bar *old_bar;
	struct nvme_ctrl_regs __iomem *bar; /**< 虚拟 BAR 中控制器寄存器的 CPU 访问视图。 */

	u32 *old_dbs_orig;
	u32 *old_dbs; /**< 控制器侧已处理的 doorbell 位置。 */
	u32 __iomem *dbs; /**< 客户端可更新的 doorbell 区域，SQ 和 CQ 交错存放。 */

	struct swarmio_nvme_ns *ns;
	unsigned int num_ns;
	unsigned int num_sq;
	unsigned int num_cq;

	struct nvme_admin_queue *admin_q; /**< 控制器启用后由 dispatcher 0 建立的管理队列。 */
	struct nvme_submission_queue *sqes[NR_MAX_IO_QUEUE + 1]; /**< 以 SQ ID 索引的 I/O 提交队列。 */
	struct nvme_completion_queue *cqes[NR_MAX_IO_QUEUE + 1]; /**< 以 CQ ID 索引的 I/O 完成队列。 */

	unsigned int mdts;

	struct proc_dir_entry *proc_root;
	struct proc_dir_entry *proc_read_times;
	struct proc_dir_entry *proc_write_times;
	struct proc_dir_entry *proc_num_sched_insts;
	struct proc_dir_entry *proc_io_unit_shift;
	struct proc_dir_entry *proc_stat;

	struct sched_inst_stat_t *sched_inst_stat; /**< 各调度实例的共享可用时间。 */
#ifndef CONFIG_SWARMIO_ATOMIC_CAS_TIMING_UPDATE
	spinlock_t sched_inst_stat_lock;
#endif

	wait_queue_head_t wait_queue;
	int stop_flag; /**< 重置控制器或分配队列时使用的暂停标志。 */
	atomic_t num_waiting; /**< 已经进入暂停屏障的服务线程数。 */
	atomic_t io_error; /**< 本轮首个 I/O 故障，通过 CSTS.CFS 通知客户端。 */
	bool fault_drain_attempted;
	bool unload_requested;
	bool unload_prepared;
	int unload_result;

	bool dsa_enabled; /**< 记录是否已启用 DSA 设备，供错误和卸载清理使用。 */
	bool is_threads_initialized;
};

extern struct nvme_vdev *nvme_vdev;
extern struct nvme_vdev *vdev;

int vdev_init(struct swarmio_cfg *config);
void vdev_stop_pci(struct nvme_vdev *vdev);
void vdev_exit(struct nvme_vdev *vdev);

#endif /* _SWARMIO_NVME_VDEV_H */
