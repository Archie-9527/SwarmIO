// SPDX-License-Identifier: GPL-2.0-only
/* v3.1：单个DSA，最多4个group/4个engine/8个WQ，SQE与payload并行流水。
 * 一个group有独立fetch线程/WQ和worker线程/WQ；环形slot替代128项窗口。
 * 地址来自针对DSA pdev建立的NVIDIA P2P DMA映射，不直接使用CUDA虚拟地址。
 */
#include <linux/module.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <linux/delay.h>
#include <linux/ktime.h>
#include <linux/capability.h>
#include <linux/log2.h>
#include <linux/kthread.h>
#include <linux/completion.h>
#include <linux/cpu.h>
#include <linux/panic.h>
#include <linux/sched.h>
#include <linux/sched/task.h>
#include <nv-p2p.h>
#include "dsa.h"
#include "uapi.h"
#include "host_pages.h"
#include "backend.h"
#include "gpu_region.h"

#ifndef NVIDIA_P2P_CAP_GET_PAGES_PERSISTENT_API
#error "需要支持persistent P2P API的匹配NVIDIA驱动与头文件"
#endif
extern struct idxd_device *dsa_idxd_list[];
static DEFINE_MUTEX(run_lock);
static bool exclusive;
static unsigned int groups = 4;
static unsigned int max_pinned_mb = 32;
unsigned int dg_device_id;
static int cpus[DG_GROUPS * 2];
static unsigned int nr_cpus;
static struct dg_info info;
#define DG_BUILD "v3.1-pin-ownership"
#define DG_TIMEOUT_NS (5ULL * 1000000000)
module_param(exclusive, bool, 0444);
module_param(groups, uint, 0444);
module_param(max_pinned_mb, uint, 0444);
module_param_named(device_id, dg_device_id, uint, 0444);
module_param_array(cpus, int, &nr_cpus, 0444);
MODULE_PARM_DESC(exclusive, "确认已获准使用指定DSA；仅独占借用通道，不接管设备，必须为1");
MODULE_PARM_DESC(device_id, "明确选择预配置DSA设备编号，默认0；不自动选择其它设备");
MODULE_PARM_DESC(groups, "配置1/2/4个group，每group一个engine和两个WQ，默认4");
MODULE_PARM_DESC(max_pinned_mb, "GPU data+SQE映射的总软件预算MiB，默认32；管理员核对BAR1余量后才增大");
MODULE_PARM_DESC(cpus, "可选CPU列表：fetch0,worker0,fetch1,worker1,...，需互不重复且在线");

/* 清理API失败时保留句柄，并钉住模块/其设备引用，禁止不明状态下卸载。
 * 默认每次测试只有data/SQ两块，第一次故障后所有新ioctl均被拒绝。
 */
static struct dg_region quarantined_regions[2];
static atomic_t gpu_cleanup_fault = ATOMIC_INIT(0);
enum dg_state { DG_FREE, DG_FETCHING, DG_READY, DG_COPYING };
struct dg_slot {
    int state;
    u32 count, children_count;
    u64 first, fetch_start, copy_start;
    struct idxd_desc *fetch, *copy;
    bool fetch_active, copy_active;
    struct dg_cmd *cmds;
    struct dsa_hw_desc *children;
    struct dsa_completion_record *comps;
    dma_addr_t cmds_dma, children_dma, comps_dma;
} ____cacheline_aligned_in_smp;
struct dg_test;
struct dg_group;
struct dg_thread {
    struct task_struct *task;
    struct completion ready, done;
    struct dg_group *g;
    unsigned int cpu;
    bool worker;
};
struct dg_group {
    struct dg_test *test;
    unsigned int id;
    struct idxd_wq *fetch_wq, *copy_wq;
    struct dg_slot *slots;
    struct dg_thread fetch_thread, worker_thread;
    bool fetch_done, started;
    u64 cursor, limit;
};
struct dg_test {
    struct dg_run *r;
    struct device *dev;
    struct pci_dev *pdev;
    struct dg_region data, sq;
    struct dg_host_page *host;
    struct dg_group *g;
    unsigned long *touched;
    struct completion start;
    atomic_t error;
    u64 begin, measure_begin, deadline, blocks_per_group, sq_stride;
    u32 batch;
};

static void fail(struct dg_test *t, int error)
{
    atomic_cmpxchg(&t->error, 0, error);
}
static bool measured(struct dg_test *t, u64 now)
{
    return !t->r->seconds || (now >= t->measure_begin && now < t->deadline);
}
static void unpin(struct dg_test *t, struct dg_region *r)
{
    int ret = dg_region_unpin(t->pdev, r);
    if (ret) {
        pr_err("dsa_gpu_test: GPU cleanup failed: %d; keeping owned handles and module reference, refusing new runs\n", ret);
        quarantined_regions[r == &t->data ? 0 : 1] = *r;
        memset(r, 0, sizeof(*r));
        if (!atomic_cmpxchg(&gpu_cleanup_fault, 0, 1)) __module_get(THIS_MODULE);
        fail(t, ret < 0 ? ret : -EIO);
    }
}
static int pin(struct dg_test *t, struct dg_region *r, u64 va, u64 bytes)
{
    int ret = dg_region_pin(t->pdev, r, va, bytes);
    if (ret)
        pr_err("dsa_gpu_test: GPU pin/map va=%#llx bytes=%llu failed: %d; pages_owned=%u map_owned=%u\n",
               (unsigned long long)va, (unsigned long long)bytes, ret,
               r->pages_owned, r->map_owned);
    return ret;
}
static dma_addr_t gpu_addr(struct dg_region *r, u64 off)
{
    return r->map->dma_addresses[off / DG_ALIGN] + off % DG_ALIGN;
}
static void prep(struct dsa_hw_desc *d, u32 opcode, u64 dst, u64 src,
                 u32 size, u64 completion)
{
    memset(d, 0, sizeof(*d));
    d->opcode = opcode;
    d->flags = IDXD_OP_FLAG_CRAV | IDXD_OP_FLAG_RCR;
    d->src_addr = src; d->dst_addr = dst; d->xfer_size = size;
    d->completion_addr = completion;
}
static void prep_top(struct idxd_desc *d, u32 opcode, u64 dst, u64 src, u32 size)
{
    /* idxd_alloc_desc可能已经设置内核PASID；重用描述符时必须保留它。 */
    u32 pasid = d->hw->pasid;
    prep(d->hw, opcode, dst, src, size, d->compl_dma);
    d->hw->pasid = pasid;
}
static int submit(struct idxd_wq *wq, struct idxd_desc *d, bool *active)
{
    int ret;
    WRITE_ONCE(d->completion->status, DSA_COMP_NONE);
    dma_wmb();
    ret = idxd_submit_desc(wq, d);
    if (!ret) *active = true;
    return ret;
}
/* 返回0=仍在途、1=成功、负数=终止错误。状态在dma_rmb后才能读取结果。 */
static int poll_desc(struct idxd_desc *d, bool *active)
{
    u8 status = READ_ONCE(d->completion->status);
    if (!status) return 0;
    dma_rmb();
    *active = false;
    if (DSA_COMP_STATUS(status) == DSA_COMP_SUCCESS) return 1;
    pr_err("dsa_gpu_test: wq%d opcode=%u completion=%#x\n",
           d->wq->id, d->hw->opcode, status);
    return -EIO;
}
static int wait_desc(struct idxd_desc *d, bool *active)
{
    unsigned long next = jiffies + 5 * HZ;
    int ret;
    if (!*active) return 0;
    while (!(ret = poll_desc(d, active))) {
        /* 这里是错误清理/排空路径，不是吞吐热路径；睡眠轮询避免占满CPU。 */
        usleep_range(50, 100);
        if (time_after(jiffies, next)) {
            pr_err("dsa_gpu_test: DMA still pending on wq%d; keeping buffers pinned\n", d->wq->id);
            next = jiffies + 5 * HZ;
        }
    }
    return ret < 0 ? ret : 0;
}
static void drain(struct dg_test *t, struct idxd_wq *wq, struct idxd_desc *d)
{
    bool active = false;
    int ret;
    prep_top(d, DSA_OPCODE_DRAIN, 0, 0, 0);
    ret = submit(wq, d, &active);
    if (!ret) ret = wait_desc(d, &active);
    if (!ret) return;
    fail(t, ret);
    pr_emerg("dsa_gpu_test: DRAIN failed; buffers quarantined, reboot required\n");
    for (;;) schedule_timeout_uninterruptible(HZ);
}

static int validate_sq(struct dg_group *g, struct dg_slot *s)
{
    struct dg_test *t = g->test;
    struct dg_run *r = t->r;
    u64 index = s->first % t->blocks_per_group;
    u32 j;
    for (j = 0; j < s->count; j++) {
        struct dg_cmd expected = dg_command(g->id, index + j, r->data_bytes,
                         r->block_bytes, groups, r->direction, r->seed);
        if (memcmp(&s->cmds[j], &expected, sizeof(expected))) {
            pr_err("dsa_gpu_test: invalid/stale SQE group=%u index=%llu\n",
                   g->id, (unsigned long long)(index + j));
            return -EBADMSG;
        }
    }
    return 0;
}
static int issue_copy(struct dg_group *g, struct dg_slot *s)
{
    struct dg_test *t = g->test;
    struct dg_run *r = t->r;
    u32 j, n = s->count;
    if (!r->mode) {
        struct dg_cmd *c = &s->cmds[0];
        u64 h = dg_host_dma(t->host, c->offset), v = gpu_addr(&t->data, c->offset);
        prep_top(s->copy, DSA_OPCODE_MEMMOVE, c->write ? h : v, c->write ? v : h, c->bytes);
        s->children_count = 0;
    } else {
        memset(s->comps, 0, max(n, 2U) * sizeof(*s->comps));
        for (j = 0; j < n; j++) {
            struct dg_cmd *c = &s->cmds[j];
            u64 h = dg_host_dma(t->host, c->offset), v = gpu_addr(&t->data, c->offset);
            prep(&s->children[j], DSA_OPCODE_MEMMOVE, c->write ? h : v,
                 c->write ? v : h, c->bytes, s->comps_dma + j * sizeof(*s->comps));
        }
        s->children_count = max(n, 2U);
        if (n == 1)
            prep(&s->children[1], DSA_OPCODE_NOOP, 0, 0, 0,
                 s->comps_dma + sizeof(*s->comps));
        prep_top(s->copy, DSA_OPCODE_BATCH, 0, s->children_dma, s->children_count);
    }
    s->copy_start = ktime_get_ns();
    return submit(g->copy_wq, s->copy, &s->copy_active);
}
static void busy_pause(unsigned int *spins)
{
    cpu_relax();
    if (!(++*spins & 1023)) cond_resched();
}
static void fetch_loop(struct dg_group *g)
{
    struct dg_test *t = g->test;
    struct dg_run *r = t->r;
    unsigned int tail = 0, spins = 0, k;
    bool stopping = false;
    while (!atomic_read(&t->error)) {
        unsigned int fetching = 0;
        int ret;
        u64 now = ktime_get_ns();
        if ((r->seconds && now >= t->deadline) ||
            (!r->seconds && g->cursor >= g->limit)) stopping = true;
        for (k = 0; k < r->depth; k++) {
            struct dg_slot *s = &g->slots[k];
            if (smp_load_acquire(&s->state) != DG_FETCHING) continue;
            ret = poll_desc(s->fetch, &s->fetch_active);
            if (!ret) {
                if (now - s->fetch_start > DG_TIMEOUT_NS) {
                    pr_err("dsa_gpu_test: fetch timeout; stopping submissions and retaining in-flight buffers\n");
                    fail(t, -ETIMEDOUT); goto done;
                }
                fetching++; continue;
            }
            if (ret < 0 || (ret = validate_sq(g, s))) { fail(t, ret); goto done; }
            if (measured(t, ktime_get_ns())) r->stats[g->id].sq_measured += s->count;
            /* 发布READY之前，CPU命令缓冲区已完整DMA写入并通过边界/内容检查。 */
            smp_store_release(&s->state, DG_READY);
        }
        if (stopping && !fetching) break;
        if (!stopping) {
            struct dg_slot *s = &g->slots[tail];
            if (smp_load_acquire(&s->state) == DG_FREE) {
                u64 index = g->cursor % t->blocks_per_group;
                u64 n = min_t(u64, t->batch, t->blocks_per_group - index);
                if (!r->seconds) n = min(n, g->limit - g->cursor);
                s->first = g->cursor; s->count = n;
                prep_top(s->fetch, DSA_OPCODE_MEMMOVE, s->cmds_dma,
                    gpu_addr(&t->sq, g->id * t->sq_stride + index * sizeof(struct dg_cmd)),
                    n * sizeof(struct dg_cmd));
                s->fetch_start = ktime_get_ns();
                ret = submit(g->fetch_wq, s->fetch, &s->fetch_active);
                if (ret) { fail(t, ret); break; }
                WRITE_ONCE(s->state, DG_FETCHING);
                g->cursor += n;
                tail = (tail + 1) % r->depth;
            }
        }
        busy_pause(&spins);
    }
done:
    smp_store_release(&g->fetch_done, true);
}
static void worker_loop(struct dg_group *g)
{
    struct dg_test *t = g->test;
    struct dg_run *r = t->r;
    unsigned int head = 0, spins = 0, k;
    while (!atomic_read(&t->error)) {
        bool pending = false;
        int ret;
        /* 先提交所有READY slot；不在每次发布后同步等待。 */
        for (k = 0; k < r->depth; k++) {
            struct dg_slot *s = &g->slots[k];
            if (smp_load_acquire(&s->state) == DG_READY) {
                ret = issue_copy(g, s);
                if (ret) { fail(t, ret); return; }
                WRITE_ONCE(s->state, DG_COPYING);
            }
        }
        /* 按环顺序回收，防止一个慢slot未完成时其它slot绕环覆盖同一数据区。 */
        if (smp_load_acquire(&g->slots[head].state) == DG_COPYING) {
            struct dg_slot *s = &g->slots[head];
            ret = poll_desc(s->copy, &s->copy_active);
            if (!ret && ktime_get_ns() - s->copy_start > DG_TIMEOUT_NS) {
                pr_err("dsa_gpu_test: payload timeout; stopping submissions and retaining in-flight buffers\n");
                fail(t, -ETIMEDOUT); return;
            }
            if (ret < 0) { fail(t, ret); return; }
            if (ret == 1) {
                for (k = 0; k < s->children_count; k++)
                    if (DSA_COMP_STATUS(READ_ONCE(s->comps[k].status)) != DSA_COMP_SUCCESS) {
                        pr_err("dsa_gpu_test: group=%u child=%u status=%#x\n", g->id, k, s->comps[k].status);
                        fail(t, -EIO); return;
                    }
                r->stats[g->id].executed += s->count;
                if (measured(t, ktime_get_ns())) r->stats[g->id].measured += s->count;
                smp_store_release(&s->state, DG_FREE);
                head = (head + 1) % r->depth;
            }
        }
        /* 先acquire fetch_done，再检查slot，不能用过期slot快照提前退出。 */
        if (smp_load_acquire(&g->fetch_done)) {
            for (k = 0; k < r->depth; k++)
                if (smp_load_acquire(&g->slots[k].state) != DG_FREE) pending = true;
            if (!pending) break;
        }
        busy_pause(&spins);
    }
}
static int thread_main(void *arg)
{
    struct dg_thread *th = arg;
    complete(&th->ready);
    wait_for_completion(&th->g->test->start);
    if (!atomic_read(&th->g->test->error)) {
        if (th->worker) worker_loop(th->g); else fetch_loop(th->g);
    }
    complete(&th->done);
    return 0;
}
static int create_thread(struct dg_thread *th, struct dg_group *g, bool worker)
{
    int node = dev_to_node(g->test->dev);
    unsigned int index = g->id * 2 + worker;
    th->g = g; th->worker = worker;
    th->cpu = nr_cpus ? cpus[index] : cpumask_local_spread(index, node);
    init_completion(&th->ready); init_completion(&th->done);
    th->task = kthread_create_on_node(thread_main, th, node, "dg-%s%u", worker ? "copy" : "sq", g->id);
    if (IS_ERR(th->task)) { int ret = PTR_ERR(th->task); th->task = NULL; return ret; }
    /* 持有task引用直到done，避免线程快速退出后task指针失效。 */
    get_task_struct(th->task);
    g->started = true;
    kthread_bind(th->task, th->cpu);
    wake_up_process(th->task);
    return 0;
}

static int alloc_group(struct dg_group *g)
{
    struct dg_test *t = g->test;
    unsigned int k, n = max(t->batch, 2U);
    g->fetch_wq = dsa_idxd_list[0]->wqs[g->id * 2];
    g->copy_wq = dsa_idxd_list[0]->wqs[g->id * 2 + 1];
    g->slots = kcalloc(t->r->depth, sizeof(*g->slots), GFP_KERNEL);
    if (!g->slots) return -ENOMEM;
    for (k = 0; k < t->r->depth; k++) {
        struct dg_slot *s = &g->slots[k];
        /* NONBLOCK防止depth超过驱动描述符池时在初始化中永久阻塞。 */
        s->fetch = idxd_alloc_desc(g->fetch_wq, IDXD_OP_NONBLOCK);
        if (IS_ERR(s->fetch)) { int ret = PTR_ERR(s->fetch); s->fetch = NULL; return ret; }
        s->copy = idxd_alloc_desc(g->copy_wq, IDXD_OP_NONBLOCK);
        if (IS_ERR(s->copy)) { int ret = PTR_ERR(s->copy); s->copy = NULL; return ret; }
        s->cmds = dma_alloc_coherent(t->dev, t->batch * sizeof(*s->cmds), &s->cmds_dma, GFP_KERNEL);
        s->children = dma_alloc_coherent(t->dev, n * sizeof(*s->children), &s->children_dma, GFP_KERNEL);
        s->comps = dma_alloc_coherent(t->dev, n * sizeof(*s->comps), &s->comps_dma, GFP_KERNEL);
        if (!s->cmds || !s->children || !s->comps) return -ENOMEM;
    }
    return 0;
}
static void free_group(struct dg_group *g)
{
    struct dg_test *t = g->test;
    unsigned int k, n = max(t->batch, 2U);
    bool used_fetch = false, used_copy = false;
    if (!g->slots) return;
    for (k = 0; k < t->r->depth; k++) {
        struct dg_slot *s = &g->slots[k];
        if (s->fetch_active) { used_fetch = true; if (wait_desc(s->fetch, &s->fetch_active)) fail(t, -EIO); }
        if (s->copy_active) { used_copy = true; if (wait_desc(s->copy, &s->copy_active)) fail(t, -EIO); }
    }
    /* 有线程运行过就drain两个WQ，包括已终止但子操作可能仍在途的错误BATCH。 */
    if (g->started || used_fetch || used_copy) {
        if (g->slots[0].fetch) drain(t, g->fetch_wq, g->slots[0].fetch);
        if (g->slots[0].copy) drain(t, g->copy_wq, g->slots[0].copy);
    }
    for (k = 0; k < t->r->depth; k++) {
        struct dg_slot *s = &g->slots[k];
        if (s->cmds) dma_free_coherent(t->dev, t->batch * sizeof(*s->cmds), s->cmds, s->cmds_dma);
        if (s->children) dma_free_coherent(t->dev, n * sizeof(*s->children), s->children, s->children_dma);
        if (s->comps) dma_free_coherent(t->dev, n * sizeof(*s->comps), s->comps, s->comps_dma);
        if (s->fetch) idxd_free_desc(g->fetch_wq, s->fetch);
        if (s->copy) idxd_free_desc(g->copy_wq, s->copy);
    }
    kfree(g->slots);
}
static int validate_run(struct dg_run *r)
{
    u32 batch = r->mode ? r->batch_size : 1;
    if (r->abi != DG_ABI || r->reserved || r->mode > 1 || r->direction > 2 ||
        (r->block_bytes != 512 && r->block_bytes != 4096) ||
        !is_power_of_2(r->data_bytes) || r->data_bytes < (1ULL << 20) || r->data_bytes > (256ULL << 20) ||
        !is_power_of_2(r->batch_size) || r->batch_size < 2 || r->batch_size > info.max_batch ||
        !r->depth || r->depth > info.max_depth || (!r->mode && r->depth != 1) ||
        (u64)r->depth * batch > r->data_bytes / groups / r->block_bytes ||
        r->sq_bytes != groups * dg_sq_stride(r->data_bytes, r->block_bytes, groups) ||
        !r->gpu_data || !r->gpu_sq || ((r->gpu_data | r->gpu_sq) & (DG_ALIGN - 1)) ||
        r->gpu_data > U64_MAX - r->data_bytes || r->gpu_sq > U64_MAX - r->sq_bytes ||
        (r->gpu_data < r->gpu_sq + r->sq_bytes && r->gpu_sq < r->gpu_data + r->data_bytes) ||
        r->seconds > 3600 || r->warmup_ms > 60000 ||
        (r->seconds && r->operations) ||
        (!r->seconds && (r->operations < groups || r->operations > (1ULL << 32) || r->warmup_ms)))
        return -EINVAL;
    if (r->data_bytes + r->sq_bytes > info.max_pin_bytes) {
        pr_err("dsa_gpu_test: mapping request=%llu exceeds software pin budget=%llu; lower mb/batch/depth or have administrator check BAR1 capacity\n",
               (unsigned long long)(r->data_bytes + r->sq_bytes),
               (unsigned long long)info.max_pin_bytes);
        return -E2BIG;
    }
    return 0;
}
static long run_test(struct dg_run *r)
{
    struct dg_test t = { .r = r };
    unsigned int g, k;
    u64 i;
    int ret = validate_run(r);
    if (ret) return ret;
    memset(r->stats, 0, sizeof(r->stats));
    r->elapsed_ns = r->completed = r->host_errors = 0;
    t.pdev = dsa_idxd_list[0]->pdev; t.dev = &t.pdev->dev;
    t.batch = r->mode ? r->batch_size : 1;
    t.blocks_per_group = r->data_bytes / groups / r->block_bytes;
    t.sq_stride = dg_sq_stride(r->data_bytes, r->block_bytes, groups);
    atomic_set(&t.error, 0); init_completion(&t.start);
    /* 先验证GPU映射，BAR1不足时不再分配数万块主机内存。 */
    pr_info("dsa_gpu_test: stage=pin_data bytes=%llu\n", (unsigned long long)r->data_bytes);
    ret = pin(&t, &t.data, r->gpu_data, r->data_bytes); if (ret) goto out;
    pr_info("dsa_gpu_test: stage=pin_sq bytes=%llu\n", (unsigned long long)r->sq_bytes);
    ret = pin(&t, &t.sq, r->gpu_sq, r->sq_bytes); if (ret) goto out;
    pr_info("dsa_gpu_test: stage=host_alloc bytes=%llu\n", (unsigned long long)r->data_bytes);
    t.host = dg_host_alloc(t.dev, r->data_bytes);
    t.g = kcalloc(groups, sizeof(*t.g), GFP_KERNEL);
    t.touched = bitmap_zalloc(r->data_bytes / r->block_bytes, GFP_KERNEL);
    if (!t.host || !t.g || !t.touched) { ret = -ENOMEM; goto out; }
    for (i = 0; i < r->data_bytes / 8; i++) {
        *dg_host_word(t.host, i) = dg_pattern(i) ^ (dg_write(i * 8 / r->block_bytes, r->direction) ? U64_MAX : 0);
        if (!(i & ((1U << 18) - 1))) cond_resched();
    }
    pr_info("dsa_gpu_test: stage=allocate_descriptors\n");
    /* 先完成全部内存/描述符分配，再创建线程，失败时没有半启动的数据搬运。 */
    for (g = 0; g < groups; g++) {
        t.g[g].test = &t; t.g[g].id = g;
        t.g[g].limit = r->operations / groups + (g < r->operations % groups);
        ret = alloc_group(&t.g[g]); if (ret) goto out;
    }
    for (g = 0; g < groups; g++) {
        ret = create_thread(&t.g[g].fetch_thread, &t.g[g], false); if (ret) goto join;
        ret = create_thread(&t.g[g].worker_thread, &t.g[g], true); if (ret) goto join;
    }
join:
    if (ret) fail(&t, ret);
    for (g = 0; g < groups; g++) {
        struct dg_thread *th[2] = { &t.g[g].fetch_thread, &t.g[g].worker_thread };
        for (k = 0; k < 2; k++)
            if (th[k]->task) wait_for_completion(&th[k]->ready);
    }
    pr_info("dsa_gpu_test: stage=start_workers warmup_ms=%u measure_seconds=%u\n", r->warmup_ms, r->seconds);
    t.begin = ktime_get_ns();
    t.measure_begin = t.begin + (u64)r->warmup_ms * 1000000;
    t.deadline = t.measure_begin + (u64)r->seconds * 1000000000;
    complete_all(&t.start);
    for (g = 0; g < groups; g++) {
        struct dg_thread *th[2] = { &t.g[g].fetch_thread, &t.g[g].worker_thread };
        for (k = 0; k < 2; k++) if (th[k]->task) {
            if (wait_for_completion_interruptible(&th[k]->done)) {
                fail(&t, -EINTR);
                wait_for_completion(&th[k]->done);
            }
            put_task_struct(th[k]->task);
            th[k]->task = NULL;
        }
        r->stats[g].fetch_cpu = t.g[g].fetch_thread.cpu;
        r->stats[g].worker_cpu = t.g[g].worker_thread.cpu;
        r->completed += r->stats[g].measured;
    }
    r->elapsed_ns = r->seconds ? (u64)r->seconds * 1000000000 : ktime_get_ns() - t.begin;
out:
    pr_info("dsa_gpu_test: stage=drain_and_cleanup error=%d\n", ret ? ret : atomic_read(&t.error));
    /* free_group先等待/DRAIN全部WQ，再释放DMA描述符；GPU映射和host数据最后释放。 */
    if (t.g) for (g = 0; g < groups; g++) if (t.g[g].slots) free_group(&t.g[g]);
    if (!ret && !atomic_read(&t.error) && t.host) {
        /* 按完成前缀重建访问集合，移出热路径，避免逐请求修改共享bitmap。 */
        for (g = 0; g < groups; g++) {
            u64 n = min_t(u64, r->stats[g].executed, t.blocks_per_group);
            for (i = 0; i < n; i++)
                __set_bit(g * t.blocks_per_group + dg_block(i, t.blocks_per_group, r->seed + g), t.touched);
        }
        for (i = 0; i < r->data_bytes / 8; i++) {
            u64 b = i * 8 / r->block_bytes;
            bool unchanged = dg_write(b, r->direction) && !test_bit(b, t.touched);
            if (*dg_host_word(t.host, i) != (dg_pattern(i) ^ (unchanged ? U64_MAX : 0))) r->host_errors++;
        }
    }
    unpin(&t, &t.sq); unpin(&t, &t.data);
    dg_host_free(t.dev, t.host, r->data_bytes);
    kfree(t.g); bitmap_free(t.touched);
    pr_info("dsa_gpu_test: stage=return error=%d\n", ret ? ret : atomic_read(&t.error));
    return ret ? ret : atomic_read(&t.error);
}
static long dg_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
    struct dg_run r;
    long ret;
    if (!capable(CAP_SYS_RAWIO)) return -EPERM;
    if (atomic_read(&gpu_cleanup_fault)) return -EIO;
    if (test_taint(TAINT_DIE)) return -EIO;
    if (cmd == DG_INFO) {
        if (!dsa_test_ready()) return -ENODEV;
        return copy_to_user((void __user *)arg, &info, sizeof(info)) ? -EFAULT : 0;
    }
    if (cmd != DG_RUN) return -ENOTTY;
    if (copy_from_user(&r, (void __user *)arg, sizeof(r))) return -EFAULT;
    if (!mutex_trylock(&run_lock)) return -EBUSY;
    /* 取得锁后再检查，覆盖前一次测试刚进入清理隔离的交接时刻。 */
    if (atomic_read(&gpu_cleanup_fault) || test_taint(TAINT_DIE)) ret = -EIO;
    else ret = dsa_test_ready() ? run_test(&r) : -ENODEV;
    mutex_unlock(&run_lock);
    if (!ret && copy_to_user((void __user *)arg, &r, sizeof(r))) return -EFAULT;
    return ret;
}
static const struct file_operations dg_fops = { .owner = THIS_MODULE, .unlocked_ioctl = dg_ioctl };
static struct miscdevice dg_misc = { .minor = MISC_DYNAMIC_MINOR, .name = "dsa_gpu_test", .fops = &dg_fops, .mode = 0600 };
static int __init dg_init(void)
{
    unsigned int sizes[2], priorities[] = {1, 1};
    struct dsa_cfg_params p = {1, groups, 2, 1, sizes, priorities};
    struct idxd_device *idxd = NULL;
    struct device *found = NULL;
    int i, j, ret;
    char name[16];
    if (test_taint(TAINT_DIE)) {
        pr_err("dsa_gpu_test: kernel has recorded an Oops; refusing new DMA tests without changing any device\n");
        return -EIO;
    }
    if (!exclusive || !is_power_of_2(groups) || groups > DG_GROUPS ||
        !max_pinned_mb || max_pinned_mb > 1024) return -EINVAL;
    if (num_online_cpus() < groups * 2 || (nr_cpus && nr_cpus != groups * 2)) return -EINVAL;
    for (i = 0; i < (int)nr_cpus; i++) {
        if (cpus[i] < 0 || (unsigned int)cpus[i] >= nr_cpu_ids || !cpu_online(cpus[i])) return -EINVAL;
        for (j = 0; j < i; j++) if (cpus[j] == cpus[i]) return -EINVAL;
    }
    snprintf(name, sizeof(name), "dsa%u", dg_device_id);
    found = bus_find_device_by_name(&dsa_bus_type, NULL, name);
    if (!found) return -ENODEV;
    /* 直接container_of避免旧版idxd_dev_to_idxd宏的形参与成员名冲突。 */
    idxd = container_of(confdev_to_idxd_dev(found), struct idxd_device, idxd_dev);
    ret = -EINVAL;
    if (!found->driver || idxd->state != IDXD_DEV_ENABLED) {
        pr_err("dsa_gpu_test: " DG_BUILD " requires a preconfigured enabled DSA; run prepare_dsa.sh on a healthy idle device\n");
        goto release;
    }
    ret = -EOPNOTSUPP;
    if (idxd->max_groups < (int)groups || idxd->max_engines < (int)groups || idxd->max_wqs < (int)groups * 2 ||
        idxd->max_wq_size < (int)groups * 2 || idxd->max_batch_size < DG_MAX_BATCH ||
        idxd->max_xfer_bytes < DG_ALIGN) goto release;
    sizes[0] = idxd->wqs[0]->size;
    sizes[1] = idxd->wqs[1]->size;
    if (!sizes[0] || sizes[0] != sizes[1]) { ret = -EINVAL; goto release; }
    /* 读取预配置WQ容量；本模块不再写group/engine/WQ硬件配置。 */
    info = (struct dg_info){DG_ABI, idxd->id, groups, groups, groups * 2,
                          sizes[0], DG_MAX_BATCH, min(sizes[0], (unsigned int)DG_MAX_DEPTH),
                          idxd->max_xfer_bytes, (u64)max_pinned_mb << 20};
    ret = 0;
release:
    put_device(found);
    if (ret) {
        pr_err("dsa_gpu_test: requested preconfigured device/topology is unavailable; error=%d\n", ret);
        return ret;
    }
    ret = dsa_set_params(&p); if (ret) return ret;
    ret = dsa_enable_devices(); if (ret) return ret;
    /* 全部通道必须成功借到，任一失败时backend只归还已获取的通道。 */
    for (i = 0; i < (int)groups * 2; i++) {
        struct idxd_wq *wq = dsa_idxd_list[0]->wqs[i];
        if (wq->state != IDXD_WQ_ENABLED || wq->max_batch_size != DG_MAX_BATCH ||
            wq->size != info.wq_size || wq->max_xfer_bytes != info.max_transfer) {
            dsa_disable_devices(); return -EIO;
        }
    }
    ret = misc_register(&dg_misc);
    if (ret) { dsa_disable_devices(); return ret; }
    pr_info("dsa_gpu_test: build=" DG_BUILD " ABI=%u dsa%u groups=%u engines=%u wqs=%u size_per_wq=%u max_batch=%u\n",
            info.abi, info.device_id, groups, groups, info.wqs, info.wq_size, info.max_batch);
    return 0;
}
static void __exit dg_exit(void)
{
    pr_info("dsa_gpu_test: " DG_BUILD " unload: remove misc device, then return borrowed channels\n");
    misc_deregister(&dg_misc);
    dsa_disable_devices();
    pr_info("dsa_gpu_test: unload completed\n");
}
module_init(dg_init); module_exit(dg_exit);
MODULE_LICENSE("GPL");
MODULE_VERSION(DG_BUILD);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0)
MODULE_IMPORT_NS("IDXD");
#else
MODULE_IMPORT_NS(IDXD);
#endif
MODULE_DESCRIPTION("Single DSA multi-group GPU SQE/payload pipeline benchmark, ABI3");
