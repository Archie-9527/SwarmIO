/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef SWARMIO_IO_METRICS_H
#define SWARMIO_IO_METRICS_H

#ifdef CONFIG_SWARMIO_QUEUE_DIAGNOSTICS
#include <linux/seqlock.h>
#include <linux/preempt.h>

/* 当前 single-owner 配置中每个 SQ 只有一个完成写入者，避免全局原子热点。
 * 对象在模块生命周期内常驻；sysfs 回调不解引用活动队列或 GPU 映射。
 */
struct swarmio_io_counter {
	seqcount_t seq;
	u64 ops, bytes, errors;
} ____cacheline_aligned_in_smp;
static struct swarmio_io_counter swarmio_io_counters[NR_MAX_IO_QUEUE + 1];

static void swarmio_metrics_init(void)
{
	int qid;
	for (qid = 0; qid <= NR_MAX_IO_QUEUE; qid++)
		seqcount_init(&swarmio_io_counters[qid].seq);
}

static void swarmio_metrics_record(int qid, u64 bytes, bool success)
{
	struct swarmio_io_counter *counter;
	if (qid < 1 || qid > NR_MAX_IO_QUEUE)
		return;
	counter = &swarmio_io_counters[qid];
	/* seqcount 写入期间禁止抢占，避免同 CPU 的读者等待被抢占的写者。 */
	preempt_disable();
	write_seqcount_begin(&counter->seq);
	if (success) {
		counter->ops++;
		counter->bytes += bytes;
	} else {
		counter->errors++;
	}
	write_seqcount_end(&counter->seq);
	preempt_enable();
}

static int swarmio_metrics_get(char *buffer, const struct kernel_param *kp)
{
	u64 ops = 0, bytes = 0, errors = 0, qops, qbytes, qerrors;
	unsigned int seq;
	int qid;
	(void)kp;
	/* 各队列各自一致；聚合为顺序读取，不是所有队列的同步硬件快照。 */
	for (qid = 1; qid <= NR_MAX_IO_QUEUE; qid++) {
		struct swarmio_io_counter *counter = &swarmio_io_counters[qid];
		do {
			seq = read_seqcount_begin(&counter->seq);
			qops = counter->ops;
			qbytes = counter->bytes;
			qerrors = counter->errors;
		} while (read_seqcount_retry(&counter->seq, seq));
		ops += qops;
		bytes += qbytes;
		errors += qerrors;
	}
	return scnprintf(buffer, PAGE_SIZE,
		"version=1 sample_ns=%llu ops=%llu bytes=%llu errors=%llu\n",
		(unsigned long long)ktime_get_ns(), (unsigned long long)ops,
		(unsigned long long)bytes, (unsigned long long)errors);
}

static const struct kernel_param_ops swarmio_metrics_ops = { .get = swarmio_metrics_get };
module_param_cb(io_metrics, &swarmio_metrics_ops, NULL, 0444);
MODULE_PARM_DESC(io_metrics, "Successful read/write CQEs and payload bytes across all queues, including BaM extra reads");
#else
static inline void swarmio_metrics_init(void) {}
static inline void swarmio_metrics_record(int qid, u64 bytes, bool success) {}
#endif
#endif
