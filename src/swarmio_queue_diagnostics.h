/* SWARMIO_QUEUE_DIAGNOSTICS v1: diagnostic snapshots, not a fix. */
#ifndef SWARMIO_QUEUE_DIAGNOSTICS_H
#define SWARMIO_QUEUE_DIAGNOSTICS_H
#ifdef CONFIG_SWARMIO_QUEUE_DIAGNOSTICS
/* 只读取 owner 线程保存的文本，不在 sysfs 回调中访问活动队列或 GPU 显存。 */
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/spinlock.h>
#include <linux/ktime.h>

#if defined(CONFIG_SWARMIO_COMMIT) || defined(CONFIG_SWARMIO_DISP_ALLOC_WORKER_RR)
#error "Queue snapshots require the CPU full-I/O builder's single-owner profile"
#endif

extern int swarmio_qdiag_qid;
extern u64 swarmio_qdiag_epoch;
void swarmio_qdiag_reset_cq(void);

/* Each translation unit owns its saved text and lock. No queue pointer escapes. */
static DEFINE_SPINLOCK(qdiag_text_lock);
static char qdiag_text[768] = "not_sampled\n";
static u64 qdiag_next_ns;

static int qdiag_get(char *buffer, const struct kernel_param *kp)
{
    unsigned long flags;
    int length;
    (void)kp;
    spin_lock_irqsave(&qdiag_text_lock, flags);
    length = scnprintf(buffer, PAGE_SIZE, "%s", qdiag_text);
    spin_unlock_irqrestore(&qdiag_text_lock, flags);
    return length;
}

static const struct kernel_param_ops qdiag_ops = { .get = qdiag_get };

static bool qdiag_owns(struct swarmio_dispatcher *dispatcher, int qid)
{
    return dispatcher && qid > 0 && qid <= NR_MAX_IO_QUEUE &&
        dispatcher->qids.start > 0 && dispatcher->qids.stride > 0 &&
        qid >= dispatcher->qids.start && qid <= dispatcher->qids.end &&
        (qid - dispatcher->qids.start) % dispatcher->qids.stride == 0;
}

static bool qdiag_due(u64 now)
{
    if (now < qdiag_next_ns)
        return false;
    qdiag_next_ns = now + 250000000ULL;
    return true;
}
#endif /* CONFIG_SWARMIO_QUEUE_DIAGNOSTICS */
#endif
