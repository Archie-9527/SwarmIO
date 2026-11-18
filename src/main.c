// SPDX-License-Identifier: GPL-2.0-only
/*
 * Based on code from https://github.com/snu-csl/nvmevirt
 * Copyright (C) 2026 VIA-Research, modified on 2026-04-07
 */

/**
 * @file
 * @brief SwarmIO 模块入口、加载参数、预留内存划分和服务线程生命周期。
 */
#include <linux/kernel.h>
#include <linux/kthread.h>
#include <linux/types.h>
#include <linux/init.h>
#include <linux/module.h>
#include <linux/delay.h>
#include <linux/sched/clock.h>

#ifdef CONFIG_X86
#include <asm/e820/types.h>
#include <asm/e820/api.h>
#endif

#include "committer.h"
#include "nvmev/nvme_vdev.h"
#include "dispatcher.h"
#include "worker.h"
#include "dsa/dsa.h"

/****************************************************************
 * Memory Layout
 ****************************************************************
 *
 * +--- memmap_start
 * |
 * v
 * +--------------+------------------------------------------+
 * | <---4GiB---> | <---------- Storage area --------------> |
 * +--------------+------------------------------------------+
 *
 *  memmap_start + 0x000000000: BAR, 4 KiB
 *	memmap_start + 0x000001000: Doorbells, 2 * (NR_MAX_IO_QUEUE + 1) * 4 bytes
 *	memmap_start + 0x000081000: MSI-X table, (VDEV_PCI_MSIX_TS * 16) bytes
 *  memmap_start + 0x000100000: SQ fetch buffer
 *  memmap_start + 0x100000000: Storage area
 *
 ****************************************************************/

static unsigned long memmap_start = 0; /**< 虚拟设备使用的预留物理内存起点。 */
static unsigned long memmap_size = 0; /**< 预留物理内存范围的字节数。 */

static unsigned int read_sched_delay = 1; /**< READ 占用模型调度单元的时间，单位为纳秒。 */
static unsigned int read_min_delay = 1; /**< READ 服务结束到允许发布完成的附加时延，单位为纳秒。 */

static unsigned int write_sched_delay = 1; /**< WRITE 占用模型调度单元的时间，单位为纳秒。 */
static unsigned int write_min_delay = 1; /**< WRITE 服务结束到允许发布完成的附加时延，单位为纳秒。 */

static unsigned int num_sched_insts = 8; /**< 时序模型中的调度实例数量。 */
static unsigned int io_unit_shift = 12; /**< 模型 I/O 单元大小的以 2 为底的指数。 */

static unsigned int num_service_units = 1; /**< 服务单元数，每个单元有一个 dispatcher。 */
static unsigned int num_workers_per_service_unit = 1; /**< 每个服务单元的 worker 数。 */

static char *cpus; /**< 线程绑定使用的 CPU 列表，支持逗号和范围表示法。 */

static bool unload_ready;

int dma_batch_size = 2; /**< 每个 worker DSA BATCH 的子描述符容量。 */
int num_dma_descs_per_disp = 2; /**< 每个 dispatcher 的单操作描述符数。 */
int num_dma_descs_per_worker = 2; /**< 每个 worker 的描述符槽数，批处理模式下按外层 BATCH 计。 */
bool disp_using_dma = true; /**< 运行时取 SQE 是否使用 DSA。 */
bool worker_using_dma = true; /**< 运行时搬运 payload 是否使用 DSA。 */
int dma_timeout = 5000; /**< 单次等待 DSA 完成记录的轮询时限，单位为毫秒。 */

/* DSA params */
static unsigned int dsa_num_devs = 1;
static unsigned int dsa_num_grps_per_dev = 4;
static unsigned int dsa_num_wqs_per_grp = 2;
static unsigned int dsa_num_engs_per_grp = 1;

static struct {
	struct {
		int size;
		int priority;
	} wq;
} num_arr_params = { .wq = {
			     .size = 2,
			     .priority = 2,
		     } };
static unsigned int dsa_wq_sizes[DSA_MAX_WQS] = { [0 ...(DSA_MAX_WQS - 1)] =
							  DSA_MAX_WQ_SIZE };
static unsigned int dsa_wq_priorities[DSA_MAX_WQS] = {
	[0 ...(DSA_MAX_WQS - 1)] = DSA_MIN_PRIORITY
};

static int set_parse_mem_param(const char *val, const struct kernel_param *kp)
{
	unsigned long *arg = (unsigned long *)kp->arg;
	*arg = memparse(val, NULL);
	return 0;
}

static struct kernel_param_ops ops_parse_mem_param = {
	.set = set_parse_mem_param,
	.get = param_get_ulong,
};

module_param_cb(memmap_start, &ops_parse_mem_param, &memmap_start, 0444);
MODULE_PARM_DESC(memmap_start, "Reserved memory address");
module_param_cb(memmap_size, &ops_parse_mem_param, &memmap_size, 0444);
MODULE_PARM_DESC(memmap_size, "Reserved memory size");
module_param(read_sched_delay, uint, 0644);
MODULE_PARM_DESC(read_sched_delay, "Read scheduling delay in nanoseconds");
module_param(read_min_delay, uint, 0644);
MODULE_PARM_DESC(read_min_delay, "Read minimum delay in nanoseconds");
module_param(write_sched_delay, uint, 0644);
MODULE_PARM_DESC(write_sched_delay, "Write scheduling delay in nanoseconds");
module_param(write_min_delay, uint, 0644);
MODULE_PARM_DESC(write_min_delay, "Write minimum delay in nanoseconds");
module_param(num_sched_insts, uint, 0444);
MODULE_PARM_DESC(num_sched_insts,
		 "Number of scheduling instances that operates in parallel");
module_param(io_unit_shift, uint, 0444);
MODULE_PARM_DESC(io_unit_shift, "Size of each I/O unit (2^)");
module_param(cpus, charp, 0444);
MODULE_PARM_DESC(
	cpus,
	"Comma-separated list of CPU IDs and CPU ID ranges (e.g., 1,2,3-4)");
module_param(num_service_units, uint, 0644);
MODULE_PARM_DESC(
	num_service_units,
	"Number of service units (i.e., groups of a dispatcher and workers)");
module_param(num_workers_per_service_unit, uint, 0644);
MODULE_PARM_DESC(num_workers_per_service_unit,
		 "Number of workers in each service unit");

module_param(dma_batch_size, int, 0644);
MODULE_PARM_DESC(dma_batch_size, "I/O worker's DMA batch size");
module_param(num_dma_descs_per_disp, int, 0644);
MODULE_PARM_DESC(num_dma_descs_per_disp,
		 "Number of DMA descriptors per dispatcher");
module_param(num_dma_descs_per_worker, int, 0644);
MODULE_PARM_DESC(num_dma_descs_per_worker,
		 "Number of DMA descriptors per I/O worker");
module_param(disp_using_dma, bool, 0444);
MODULE_PARM_DESC(disp_using_dma, "Enable/Disable dispatcher DMA");
module_param(worker_using_dma, bool, 0444);
MODULE_PARM_DESC(worker_using_dma, "Enable/Disable worker DMA");
module_param(dma_timeout, int, 0644);
MODULE_PARM_DESC(dma_timeout, "DMA timeout in milliseconds");

static int prepare_unload_set(const char *value, const struct kernel_param *kp)
{
	struct nvme_vdev *device;
	bool prepare;
	int ret = kstrtobool(value, &prepare);

	if (ret || !prepare)
		return -EINVAL;
	if (unload_ready)
		return 0;
	device = READ_ONCE(vdev);
	if (!device || !READ_ONCE(device->is_threads_initialized))
		return -ENODEV;

	if (!READ_ONCE(device->unload_prepared)) {
		smp_store_release(&device->unload_requested, true);
		wake_up_process(device->dispatchers[0].task_struct);
		if (!wait_event_timeout(device->wait_queue,
					!smp_load_acquire(&device->unload_requested),
					msecs_to_jiffies(2 * SWARMIO_STOP_TIMEOUT_MS + 1000)))
			return -ETIMEDOUT;
		ret = device->unload_result;
		if (ret)
			return ret;
	}

	unload_ready = true;
	module_put(THIS_MODULE);
	return 0;
}

static int prepare_unload_get(char *buffer, const struct kernel_param *kp)
{
	return sprintf(buffer, "%d\n", unload_ready);
}

static const struct kernel_param_ops prepare_unload_ops = {
	.set = prepare_unload_set,
	.get = prepare_unload_get,
};
module_param_cb(prepare_unload, &prepare_unload_ops, NULL, 0600);
MODULE_PARM_DESC(prepare_unload, "Drain active I/O before removing the module");

module_param(dsa_num_devs, uint, 0444);
MODULE_PARM_DESC(dsa_num_devs, "Number of DSA devices to enable (Default: 1)");
module_param(dsa_num_grps_per_dev, uint, 0444);
MODULE_PARM_DESC(dsa_num_grps_per_dev,
		 "Number of groups to configure per DSA device (Default: 4)");
module_param(dsa_num_wqs_per_grp, uint, 0444);
MODULE_PARM_DESC(dsa_num_wqs_per_grp, "Number of WQs per group (Default: 2)");
module_param(dsa_num_engs_per_grp, uint, 0444);
MODULE_PARM_DESC(dsa_num_engs_per_grp,
		 "Number of engines per group (Default: 1)");
module_param_array(dsa_wq_sizes, uint, &num_arr_params.wq.size, 0444);
MODULE_PARM_DESC(dsa_wq_sizes, "WQ sizes within a group (Array, Default: 16)");
module_param_array(dsa_wq_priorities, uint, &num_arr_params.wq.priority, 0444);
MODULE_PARM_DESC(dsa_wq_priorities,
		 "WQ priorities within a group (Array, Default: 1)");

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 14, 0)
MODULE_IMPORT_NS("IDXD");
#else
MODULE_IMPORT_NS(IDXD);
#endif

#ifdef CONFIG_X86
static int __validate_configs_arch(void)
{
	unsigned long resv_start_bytes;
	unsigned long resv_end_bytes;

	resv_start_bytes = memmap_start;
	resv_end_bytes = resv_start_bytes + memmap_size - 1;

	if (e820__mapped_any(resv_start_bytes, resv_end_bytes, E820_TYPE_RAM)) {
		SWARMIO_ERROR("[mem %#010lx-%#010lx] is system RAM region\n",
			      (unsigned long)resv_start_bytes,
			      (unsigned long)resv_end_bytes);
		return -EPERM;
	}

	if (!e820__mapped_any(resv_start_bytes, resv_end_bytes,
			      E820_TYPE_RESERVED)) {
		SWARMIO_ERROR("[mem %#010lx-%#010lx] is not reseved region\n",
			      (unsigned long)resv_start_bytes,
			      (unsigned long)resv_end_bytes);
		return -EPERM;
	}
	return 0;
}
#endif

static int __validate_configs(void)
{
	if (!memmap_start) {
		SWARMIO_ERROR("[memmap_start] should be specified\n");
		return -EINVAL;
	}
	if (!memmap_size) {
		SWARMIO_ERROR("[memmap_size] should be specified\n");
		return -EINVAL;
	} else if (memmap_size <= GB(4ULL)) {
		SWARMIO_ERROR("[memmap_size] should be greater than 4 GiB\n");
		return -EINVAL;
	}

#ifdef CONFIG_X86
	if (__validate_configs_arch()) {
		return -EPERM;
	}
#endif

	if (!cpus || !*cpus) {
		SWARMIO_ERROR("[cpus] should be specified\n");
		return -EINVAL;
	}

	if (num_sched_insts == 0) {
		num_sched_insts = 1;
	}
	if (io_unit_shift == 0) {
		io_unit_shift = 12;
	}
	if (read_sched_delay == 0) {
		read_sched_delay = 1;
	}
	if (write_sched_delay == 0) {
		write_sched_delay = 1;
	}
	if (num_service_units == 0) {
		num_service_units = 1;
	}
	if (num_workers_per_service_unit == 0) {
		num_workers_per_service_unit = 1;
	}

	return 0;
}

/// @brief 检查服务单元、WQ 数量及描述符容量是否满足 DMA 配置。
static int __init __validate_dsa_params(void)
{
	unsigned int total_dsa_groups;
	unsigned int num_worker_wqs;
	unsigned int max_workers_per_wq;
	unsigned int required_worker_wq_size;
	unsigned int i;

	if (num_arr_params.wq.size != dsa_num_wqs_per_grp ||
	    num_arr_params.wq.priority != dsa_num_wqs_per_grp) {
		SWARMIO_ERROR(
			"wq config array mismatch! need %u elems, but got (size: %d, priority: %d)\n",
			dsa_num_wqs_per_grp, num_arr_params.wq.size,
			num_arr_params.wq.priority);
		return -EINVAL;
	}

	total_dsa_groups = dsa_num_devs * dsa_num_grps_per_dev;
	if (num_service_units > total_dsa_groups) {
		SWARMIO_ERROR(
			"num_service_units %u exceeds total DSA groups %u\n",
			num_service_units, total_dsa_groups);
		return -EINVAL;
	}

	if (disp_using_dma) {
		if (dsa_wq_sizes[0] < num_dma_descs_per_disp) {
			SWARMIO_ERROR(
				"WQ0 size %u is smaller than required: %d\n",
				dsa_wq_sizes[0], num_dma_descs_per_disp);
			return -EINVAL;
		}
	}

	if (worker_using_dma) {
		if (dsa_num_wqs_per_grp <= 1) {
			SWARMIO_ERROR(
				"worker DMA requires at least 2 WQs per group\n");
			return -EINVAL;
		}

		num_worker_wqs = dsa_num_wqs_per_grp - 1;
		max_workers_per_wq = DIV_ROUND_UP(num_workers_per_service_unit,
						  num_worker_wqs);
		required_worker_wq_size =
			max_workers_per_wq * num_dma_descs_per_worker;

		for (i = 1; i < dsa_num_wqs_per_grp; i++) {
			if (dsa_wq_sizes[i] < required_worker_wq_size) {
				SWARMIO_ERROR(
					"WQ%u size %u is smaller than required: %u\n",
					i, dsa_wq_sizes[i],
					required_worker_wq_size);
				return -EINVAL;
			}
		}
	}

	return 0;
}

/**
 * @brief 校验模块参数并建立后备 DRAM、SQE 暂存区和 CPU 绑定配置。
 * @param[out] config 待写入的模块配置。
 * @return 成功返回 0，失败返回负错误码。
 */
static int __load_configs(struct swarmio_cfg *config)
{
	bool first = true;
	unsigned int cpu_id;
	char *cpu;
	unsigned int i = 0;
	int ret, num_cpus;

	ret = __validate_configs();
	if (ret < 0)
		return ret;

	config->memmap_start = memmap_start;
	config->memmap_size = memmap_size;
	// storage space starts from 1M offset
	config->cmd_buffer_start = memmap_start + MB(1);
	config->cmd_buffer_size = sizeof(struct nvme_command) *
				  NR_MAX_IO_QUEUE * NR_MAX_QUEUE_SIZE;
	config->storage_start = memmap_start + GB(4ULL);
	config->storage_size = memmap_size - GB(4ULL);

	config->read_sched_delay = read_sched_delay;
	config->read_min_delay = read_min_delay;
	config->write_sched_delay = write_sched_delay;
	config->write_min_delay = write_min_delay;
	config->num_sched_insts = num_sched_insts;
	config->io_unit_shift = io_unit_shift;

	config->num_service_units = num_service_units;
	config->num_workers_per_service_unit = num_workers_per_service_unit;
	config->num_workers = num_service_units * num_workers_per_service_unit;
	if (config->cmd_buffer_size > GB(4ULL)) {
		SWARMIO_ERROR(
			"[memmap_size] cmd buffer size must not exceed 4 GiB\n");
		return -EINVAL;
	}

	if (!alloc_cpumask_var(&config->cpu_mask, GFP_KERNEL)) {
		SWARMIO_ERROR("failed to allocate cpu mask\n");
		return -ENOMEM;
	}

	cpumask_clear(config->cpu_mask);
	ret = cpulist_parse(cpus, config->cpu_mask);
	num_cpus = cpumask_weight(config->cpu_mask);
	if (ret < 0 || num_cpus <= 0) {
		SWARMIO_ERROR("invalid cpu list: %s\n", cpus);
		free_cpumask_var(config->cpu_mask);
		config->cpu_mask = NULL;
		return -EINVAL;
	}

	if (num_cpus < (config->num_service_units + config->num_workers)) {
		SWARMIO_ERROR(
			"CPUs provided (%d) are fewer than required (%d)\n",
			num_cpus,
			config->num_service_units + config->num_workers);
		free_cpumask_var(config->cpu_mask);
		config->cpu_mask = NULL;
		return -EINVAL;
	}

	return 0;
}

/**
 * @brief 初始化虚拟设备和 DSA，启动服务线程后发布虚拟 PCI 设备。
 * @return 成功返回 0，初始化失败返回负错误码。
 * @note DSA 启用失败时，两个运行时 DMA 开关都会清零。
 */
static int swarmio_init(void)
{
	int ret = 0;
	struct swarmio_cfg config = { 0 };
	struct dsa_cfg_params dsa_params = {
		.num_devs = dsa_num_devs,
		.num_groups_per_dev = dsa_num_grps_per_dev,
		.num_wqs_per_group = dsa_num_wqs_per_grp,
		.num_engines_per_group = dsa_num_engs_per_grp,
		.wq_sizes = dsa_wq_sizes,
		.wq_priorities = dsa_wq_priorities,
	};

	ret = __load_configs(&config);
	if (ret)
		return ret;

	ret = vdev_init(&config);
	if (ret)
		return ret;
	vdev->is_threads_initialized = false;

#ifndef CONFIG_SWARMIO_DMA_DISPATCHER
	disp_using_dma = false;
#endif
#ifndef CONFIG_SWARMIO_DMA_WORKER
	worker_using_dma = false;
#endif

	if (disp_using_dma || worker_using_dma) {
		ret = __validate_dsa_params();
		if (ret)
			goto err_vdev_init;

		ret = dsa_set_params(&dsa_params);
		if (ret)
			goto err_vdev_init;

		if (dsa_enable_devices() != 0) {
			disp_using_dma = false;
			worker_using_dma = false;
			SWARMIO_ERROR("Cannot use DSA devices\n");
		} else {
			vdev->dsa_enabled = true;
		}
	}

	init_waitqueue_head(&vdev->wait_queue);
	WRITE_ONCE(vdev->stop_flag, 0);
	atomic_set(&vdev->num_waiting, 0);
	atomic_set(&vdev->io_error, 0);
#ifdef CONFIG_SWARMIO_COMMIT
	ret = swarmio_committer_init(vdev);
	if (ret)
		goto err_committers;
#endif
	ret = swarmio_worker_init(vdev);
	if (ret)
		goto err_committers;
	ret = swarmio_dispatcher_init(vdev);
	if (ret)
		goto err_workers;
	/* module_exit cannot report an unsuccessful drain. */
	__module_get(THIS_MODULE);
	WRITE_ONCE(vdev->is_threads_initialized, true);

	pci_bus_add_devices(vdev->virt_bus);

	SWARMIO_INFO("Virtual NVMe device created\n");

	return 0;

err_workers:
	swarmio_worker_exit(vdev);
err_committers:
#ifdef CONFIG_SWARMIO_COMMIT
	swarmio_committer_exit(vdev);
#endif
err_vdev_init:
	if (vdev->dsa_enabled) {
		dsa_disable_devices();
		vdev->dsa_enabled = false;
	}
	vdev_exit(vdev);
	vdev = NULL;
	return ret ? ret : -EIO;
}

/// @brief 移除虚拟 PCI 设备，停止服务线程并释放 DSA 和设备资源。
static void swarmio_exit(void)
{
	WRITE_ONCE(vdev->is_threads_initialized, false);
	vdev_stop_pci(vdev);

	swarmio_dispatcher_exit(vdev);
	swarmio_worker_exit(vdev);
#ifdef CONFIG_SWARMIO_COMMIT
	swarmio_committer_exit(vdev);
#endif

	if (vdev->dsa_enabled) {
		dsa_disable_devices();
		vdev->dsa_enabled = false;
	}

	vdev_exit(vdev);
	vdev = NULL;

	SWARMIO_INFO("Virtual NVMe device closed\n");
}

MODULE_LICENSE("GPL v2");
module_init(swarmio_init);
module_exit(swarmio_exit);
