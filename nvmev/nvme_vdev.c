// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief SwarmIO 虚拟控制器的设备、后备区和 namespace 生命周期。
 */
#include <linux/kernel.h>

#include "admin.h"
#include "nvme_vdev.h"
#include "proc.h"
#include "dispatcher.h"
#include "pci.h"
#include "worker.h"
#include "simple_timing_model.h"

struct nvme_vdev *nvme_vdev = NULL; /**< 当前模块唯一的虚拟控制器实例。 */
extern struct nvme_vdev *vdev __attribute__((alias("nvme_vdev")));

/// @brief 映射预留 DRAM 作为虚拟盘后备区，使 CPU 拷贝路径可直接访问。
static int vdev_storage_init(struct nvme_vdev *vdev)
{
	SWARMIO_INFO("Storage: %#010lx-%#010lx (%lu MiB)\n",
		     vdev->config.storage_start,
		     vdev->config.storage_start + vdev->config.storage_size,
		     BYTE_TO_MB(vdev->config.storage_size));

	vdev->sched_inst_stat = kzalloc(sizeof(*vdev->sched_inst_stat) *
						vdev->config.num_sched_insts,
					GFP_KERNEL);
	if (!vdev->sched_inst_stat)
		return -ENOMEM;

#ifndef CONFIG_SWARMIO_ATOMIC_CAS_TIMING_UPDATE
	spin_lock_init(&vdev->sched_inst_stat_lock);
#endif

	vdev->storage_mapped = memremap(vdev->config.storage_start,
					vdev->config.storage_size, MEMREMAP_WB);
	if (!vdev->storage_mapped) {
		SWARMIO_ERROR("Failed to map storage memory.\n");
		kfree(vdev->sched_inst_stat);
		vdev->sched_inst_stat = NULL;
		return -ENOMEM;
	}

	if (swarmio_proc_init(vdev))
		goto err_proc;

	return 0;

err_proc:
	SWARMIO_ERROR("Failed to create proc entries.\n");
	swarmio_proc_exit(vdev);
	memunmap(vdev->storage_mapped);
	vdev->storage_mapped = NULL;
	kfree(vdev->sched_inst_stat);
	vdev->sched_inst_stat = NULL;
	return -ENOMEM;
}

static void vdev_storage_exit(struct nvme_vdev *vdev)
{
	swarmio_proc_exit(vdev);

	if (vdev->storage_mapped)
		memunmap(vdev->storage_mapped);
	vdev->storage_mapped = NULL;

	kfree(vdev->sched_inst_stat);
	vdev->sched_inst_stat = NULL;
}

/// @brief 在当前后备区上建立 namespace，并绑定简化时序模型。
static int vdev_namespace_init(struct nvme_vdev *vdev)
{
	const int num_ns = 1;
	struct swarmio_nvme_ns *ns;

	ns = kmalloc(sizeof(*ns) * num_ns, GFP_KERNEL);
	if (!ns)
		return -ENOMEM;

	simple_init_namespace(&ns[0], 0, vdev->config.storage_size,
			      vdev->storage_mapped);

	vdev->ns = ns;
	vdev->num_ns = num_ns;
	vdev->mdts = MDTS;
	return 0;
}

static void vdev_namespace_exit(struct nvme_vdev *vdev)
{
	struct swarmio_nvme_ns *ns = vdev->ns;

	if (!ns)
		return;

	simple_remove_namespace(&ns[0]);

	kfree(ns);
	vdev->ns = NULL;
}

/**
 * @brief 分配虚拟 NVMe 设备并建立存储、namespace 和 PCI 资源。
 * @param config 已解析的运行配置。
 * @return 成功返回 0，失败返回负错误码。
 */
int vdev_init(struct swarmio_cfg *config)
{
	struct nvme_vdev *vdev;
	int ret;

	if (!config)
		return -EINVAL;

	vdev = kzalloc(sizeof(*vdev), GFP_KERNEL);
	if (!vdev)
		return -ENOMEM;

	vdev->virtDev = kzalloc(PAGE_SIZE, GFP_KERNEL);
	if (!vdev->virtDev) {
		kfree(vdev);
		return -ENOMEM;
	}

	vdev->pcihdr = vdev->virtDev + OFFS_PCI_HDR;
	vdev->pmcap = vdev->virtDev + OFFS_PCI_PM_CAP;
	vdev->msixcap = vdev->virtDev + OFFS_PCI_MSIX_CAP;
	vdev->pciecap = vdev->virtDev + OFFS_PCIE_CAP;
	vdev->extcap = vdev->virtDev + OFFS_PCI_EXT_CAP;
	vdev->admin_q = NULL;
	vdev->dsa_enabled = false;
	vdev->config = *config;
	nvme_vdev = vdev;

	ret = vdev_storage_init(vdev);
	if (ret)
		goto err;

	ret = vdev_namespace_init(vdev);
	if (ret)
		goto err;

	ret = vdev_pci_init(vdev);
	if (ret)
		goto err;

	return 0;

err:
	vdev_exit(vdev);
	nvme_vdev = NULL;
	return ret;
}

/// @brief 停止并移除虚拟 PCI 总线。
void vdev_stop_pci(struct nvme_vdev *vdev)
{
	if (!vdev)
		return;

	if (vdev->virt_bus) {
		pci_stop_root_bus(vdev->virt_bus);
		pci_remove_root_bus(vdev->virt_bus);
		vdev->virt_bus = NULL;
	}
}

/// @brief 释放虚拟控制器的 namespace、存储映射、队列和其他设备资源。
void vdev_exit(struct nvme_vdev *vdev)
{
	int i;

	if (!vdev)
		return;

	vdev_stop_pci(vdev);

	vdev_namespace_exit(vdev);
	vdev_storage_exit(vdev);

	for (i = 1; i <= vdev->num_sq; i++) {
		vdev_delete_sq(vdev->sqes[i]);
		vdev->sqes[i] = NULL;
	}

	for (i = 1; i <= vdev->num_cq; i++) {
		vdev_delete_cq(vdev->cqes[i]);
		vdev->cqes[i] = NULL;
	}

	if (vdev->msix_table)
		memunmap(vdev->msix_table);
	vdev->msix_table = NULL;

	if (vdev->bar)
		memunmap(vdev->bar);
	vdev->bar = NULL;

	kfree(vdev->old_bar);
	vdev->old_bar = NULL;

	kfree(vdev->old_dbs_orig);
	vdev->old_dbs_orig = NULL;
	vdev->old_dbs = NULL;
	vdev->dbs = NULL;

	if (vdev->admin_q) {
		kfree(vdev->admin_q->nvme_cq);
		kfree(vdev->admin_q->nvme_sq);
		kfree(vdev->admin_q);
	}
	vdev->admin_q = NULL;

	kfree(vdev->virtDev);
	vdev->virtDev = NULL;

	if (vdev->config.cpu_mask)
		free_cpumask_var(vdev->config.cpu_mask);
	vdev->config.cpu_mask = NULL;

	kfree(vdev);
}
