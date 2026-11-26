// SPDX-License-Identifier: GPL-2.0-only
/* Separate diagnostic module: CPU-observed duration, not device timestamps. */
#include <linux/kernel.h>
#include <linux/moduleparam.h>
#include <linux/ktime.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/spinlock.h>
#include <linux/pci.h>
#include <linux/idxd.h>
#include <linux/dma-mapping.h>
#include <idxd.h>
#include "baseline_latency.h"

#define BASELINE_DEVS 16
#define BASELINE_WQS 16
#define BASELINE_BUCKETS 32
static bool baseline_enabled;
static unsigned int baseline_sample_every = 64;
module_param(baseline_enabled, bool, 0644);
module_param(baseline_sample_every, uint, 0444);
MODULE_PARM_DESC(baseline_enabled, "Enable sampled CPU timing in the diagnostic module");
MODULE_PARM_DESC(baseline_sample_every, "Sample one in this many calls, per WQ and stage");

struct baseline_stat {
	u64 calls, samples, units, sum_ns, max_ns;
	u64 histogram[BASELINE_BUCKETS];
};
/* One submitter owns each WQ. Only sampled updates take the snapshot lock. */
static struct baseline_stat baseline_stats[BASELINE_DEVS][BASELINE_WQS][3];
static DEFINE_SPINLOCK(baseline_snapshot_lock);
static struct proc_dir_entry *baseline_proc_entry;
static const char *const stage_names[] = { "sq_sync", "worker_wait", "worker_reclaim" };

struct baseline_token baseline_begin(struct idxd_wq *wq, unsigned int stage)
{
	struct baseline_token t = { .wq = wq };
	struct baseline_stat *s;
	unsigned int every = baseline_sample_every;
	if (!READ_ONCE(baseline_enabled) || !every || stage > BASELINE_RECLAIM ||
	    wq->idxd->id >= BASELINE_DEVS || wq->id >= BASELINE_WQS)
		return t;
	s = &baseline_stats[wq->idxd->id][wq->id][stage];
	/* This counter has a single writer. The proc reader uses READ_ONCE. */
	WRITE_ONCE(s->calls, s->calls + 1);
	if (s->calls % every)
		return t;
	t.sampled = true;
	t.start = ktime_get_ns();
	return t;
}

void baseline_end(struct baseline_token *t, unsigned int stage, u64 units)
{
	struct baseline_stat *s;
	u64 elapsed;
	unsigned long flags;
	unsigned int bucket;
	if (!t->sampled)
		return;
	elapsed = ktime_get_ns() - t->start;
	bucket = min_t(unsigned int, fls64(elapsed), BASELINE_BUCKETS - 1);
	s = &baseline_stats[t->wq->idxd->id][t->wq->id][stage];
	spin_lock_irqsave(&baseline_snapshot_lock, flags);
	s->samples++;
	s->units += units;
	s->sum_ns += elapsed;
	s->max_ns = max(s->max_ns, elapsed);
	s->histogram[bucket]++;
	spin_unlock_irqrestore(&baseline_snapshot_lock, flags);
}

static int baseline_show(struct seq_file *m, void *unused)
{
	unsigned int dev, wq, stage, bucket;
	unsigned long flags;
	struct baseline_stat copy;
	seq_puts(m, "physical_dsa,wq,stage,calls,samples,units,sum_ns,max_ns");
	for (bucket = 0; bucket < BASELINE_BUCKETS; bucket++)
		seq_printf(m, ",h%u", bucket);
	seq_putc(m, '\n');
	for (dev = 0; dev < BASELINE_DEVS; dev++)
		for (wq = 0; wq < BASELINE_WQS; wq++)
			for (stage = 0; stage < 3; stage++) {
				spin_lock_irqsave(&baseline_snapshot_lock, flags);
				copy = baseline_stats[dev][wq][stage];
				copy.calls = READ_ONCE(baseline_stats[dev][wq]
					[stage == BASELINE_RECLAIM ? BASELINE_WAIT : stage].calls);
				spin_unlock_irqrestore(&baseline_snapshot_lock, flags);
				if (!copy.calls)
					continue;
				seq_printf(m, "%u,%u,%s,%llu,%llu,%llu,%llu,%llu", dev, wq,
					stage_names[stage], copy.calls, copy.samples,
					copy.units, copy.sum_ns, copy.max_ns);
				for (bucket = 0; bucket < BASELINE_BUCKETS; bucket++)
					seq_printf(m, ",%llu", copy.histogram[bucket]);
				seq_putc(m, '\n');
			}
	return 0;
}
DEFINE_PROC_SHOW_ATTRIBUTE(baseline);

int baseline_latency_init(void)
{
	if (!baseline_sample_every)
		return -EINVAL;
	baseline_proc_entry = proc_create("swarmio_baseline_latency", 0444, NULL, &baseline_proc_ops);
	return baseline_proc_entry ? 0 : -ENOMEM;
}

void baseline_latency_exit(void)
{
	if (baseline_proc_entry) {
		proc_remove(baseline_proc_entry);
		baseline_proc_entry = NULL;
	}
}
