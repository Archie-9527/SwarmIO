/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef SWARMIO_IO_METRICS_H
#define SWARMIO_IO_METRICS_H
#include "swarmio_io_timing.h"

#ifdef CONFIG_SWARMIO_QUEUE_DIAGNOSTICS
#include <linux/seqlock.h>
#include <linux/preempt.h>

struct swarmio_latency_totals {
	u64 latency_ios, segments, map_ns, copy_ns, unmap_ns, bar_ns, xfer_ns, request_ns;
};

/* 当前 single-owner 配置中每个 SQ 只有一个完成写入者，避免全局原子热点。
 * 对象在模块生命周期内常驻；sysfs 回调不解引用活动队列或 GPU 映射。
 */
struct swarmio_io_counter {
	seqcount_t seq;
	u64 ops, bytes, errors;
	struct swarmio_latency_totals latency;
} ____cacheline_aligned_in_smp;
static struct swarmio_io_counter swarmio_io_counters[NR_MAX_IO_QUEUE + 1];

static void swarmio_metrics_init(void)
{
	int qid;
	for (qid = 0; qid <= NR_MAX_IO_QUEUE; qid++)
		seqcount_init(&swarmio_io_counters[qid].seq);
}

static void swarmio_metrics_record(int qid, u64 bytes, bool success,
				  const struct swarmio_bar_timing *timing)
{
	struct swarmio_io_counter *counter;
	u64 request_ns = 0;
	if (qid < 1 || qid > NR_MAX_IO_QUEUE)
		return;
	counter = &swarmio_io_counters[qid];
	if (timing->enabled && timing->segments)
		request_ns = ktime_get_ns() - timing->start_ns;
	/* seqcount 写入期间禁止抢占，避免同 CPU 的读者等待被抢占的写者。 */
	preempt_disable();
	write_seqcount_begin(&counter->seq);
	if (success) {
		counter->ops++;
		counter->bytes += bytes;
		if (timing->enabled && timing->segments) {
			counter->latency.latency_ios++;
			counter->latency.segments += timing->segments;
			counter->latency.map_ns += timing->map_ns;
			counter->latency.copy_ns += timing->copy_ns;
			counter->latency.unmap_ns += timing->unmap_ns;
			counter->latency.bar_ns += timing->bar_ns;
			counter->latency.xfer_ns += timing->xfer_ns;
			counter->latency.request_ns += request_ns;
		}
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
	struct swarmio_latency_totals total = {0}, sample;
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
			sample = counter->latency;
		} while (read_seqcount_retry(&counter->seq, seq));
		ops += qops;
		bytes += qbytes;
		errors += qerrors;
		total.latency_ios += sample.latency_ios;
		total.segments += sample.segments;
		total.map_ns += sample.map_ns;
		total.copy_ns += sample.copy_ns;
		total.unmap_ns += sample.unmap_ns;
		total.bar_ns += sample.bar_ns;
		total.xfer_ns += sample.xfer_ns;
		total.request_ns += sample.request_ns;
	}
	return scnprintf(buffer, PAGE_SIZE,
		"version=2 sample_ns=%llu ops=%llu bytes=%llu errors=%llu "
		"latency_ios=%llu segments=%llu map_ns=%llu copy_ns=%llu "
		"unmap_ns=%llu bar_ns=%llu xfer_ns=%llu request_ns=%llu\n",
		(unsigned long long)ktime_get_ns(), (unsigned long long)ops,
		(unsigned long long)bytes, (unsigned long long)errors,
		(unsigned long long)total.latency_ios, (unsigned long long)total.segments,
		(unsigned long long)total.map_ns, (unsigned long long)total.copy_ns,
		(unsigned long long)total.unmap_ns, (unsigned long long)total.bar_ns,
		(unsigned long long)total.xfer_ns, (unsigned long long)total.request_ns);
}

static const struct kernel_param_ops swarmio_metrics_ops = { .get = swarmio_metrics_get };
module_param_cb(io_metrics, &swarmio_metrics_ops, NULL, 0444);
MODULE_PARM_DESC(io_metrics, "Successful read/write CQEs and payload bytes across all queues, including BaM extra reads");
#else
static inline void swarmio_metrics_init(void) {}
static inline void swarmio_metrics_record(int qid, u64 bytes, bool success,
					  const struct swarmio_bar_timing *timing) {}
#endif
#endif
