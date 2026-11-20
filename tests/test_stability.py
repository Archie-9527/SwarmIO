"""Local regression checks. They do not load modules or submit hardware I/O."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def source_function(path, name):
    source = (ROOT / path).read_text()
    pattern = rf'^(?:static (?:inline )?)?(?:int|bool|void) {name}\([^;]*?\n\{{.*?^\}}'
    return re.search(pattern, source, re.M | re.S).group()


class StabilityTests(unittest.TestCase):
    def run_c_check(self, program):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp, 'check')
            subprocess.run(['cc', '-std=c11', '-x', 'c', '-', '-o', str(exe)],
                           input=program, text=True, check=True)
            subprocess.run([str(exe)], check=True, timeout=5)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_batch_checks_each_nsid_before_scheduling_or_dispatch(self):
        stubs = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint32_t u32;
#define CONFIG_SWARMIO_DISP_COALESCE_SQE 1
#define CONFIG_SWARMIO_AGGREGATED_TIMING_UPDATE 1
#define NR_MAX_FETCH_SQE 1024
#define SWARMIO_ERROR(...) ((void)0)
#define le32_to_cpu(x) (x)
#define likely(x) (x)
#define unlikely(x) (x)
#define min(a,b) ((a) < (b) ? (a) : (b))
#define ktime_get_ns() (1ULL)
#define swarmio_trace_sqe(...) ((void)0)
struct nvme_command { struct { u32 nsid; } common; };
struct swarmio_dispatcher;
struct swarmio_nvme_ns {
    void (*sched_nvme_cmd_aggregated)(struct swarmio_nvme_ns *,
        struct nvme_command *, unsigned long long, unsigned long long *,
        int, struct swarmio_dispatcher *);
};
struct nvme_submission_queue {
    int fetched_pos, queue_size;
    struct nvme_command *cmd_buffer, *source;
    unsigned long long *nsecs_targets;
};
struct nvme_vdev {
    unsigned int num_ns;
    struct swarmio_nvme_ns *ns;
    struct nvme_submission_queue *sqes[2];
};
static struct nvme_vdev *vdev;
static int io_error, scheduled, dispatched;
static void swarmio_io_fault(struct nvme_vdev *dev, int error) { io_error = error; }
static struct nvme_command *__sq_entry_at(struct nvme_submission_queue *sq, int i)
{
    return &sq->source[i];
}
static bool __dispatch_sq_entry(int qid, int slot, size_t *size,
                               struct swarmio_dispatcher *disp)
{
    ++dispatched;
    return true;
}
static void schedule_batch(struct swarmio_nvme_ns *ns, struct nvme_command *cmds,
                           unsigned long long now, unsigned long long *targets,
                           int count, struct swarmio_dispatcher *disp)
{
    assert(ns == vdev->ns);
    scheduled += count;
}
"""
        exercise = r"""
int main(void)
{
    struct swarmio_nvme_ns ns = { .sched_nvme_cmd_aggregated = schedule_batch };
    struct nvme_command source[4] = {0}, buffer[4] = {0};
    unsigned long long targets[4] = {0};
    struct nvme_submission_queue sq = {
        .queue_size = 4, .cmd_buffer = buffer, .source = source, .nsecs_targets = targets,
    };
    struct nvme_vdev dev = { .num_ns = 1, .ns = &ns, .sqes = {NULL, &sq} };
    vdev = &dev;

    assert(swarmio_dispatch_sq(1, 2, 0, NULL) == 0);
    assert(io_error == -EIO && sq.fetched_pos == 0 && scheduled == 0 && dispatched == 0);

    io_error = 0;
    source[0].common.nsid = 1;
    source[1].common.nsid = 2;
    assert(swarmio_dispatch_sq(1, 2, 0, NULL) == 0);
    assert(io_error == -EIO && sq.fetched_pos == 0 && scheduled == 0 && dispatched == 0);

    io_error = 0;
    source[1].common.nsid = 1;
    assert(swarmio_dispatch_sq(1, 2, 0, NULL) == 2);
    assert(io_error == 0 && sq.fetched_pos == 2 && scheduled == 2 && dispatched == 2);
}
"""
        functions = ('swarmio_validate_sqe_nsid', 'swarmio_dispatch_sq')
        program = '\n'.join((stubs, *(source_function('src/dispatcher.c', fn) for fn in functions),
                             exercise))
        self.run_c_check(program)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_dma_timeout_keeps_inflight_records(self):
        functions = (
            ('dsa/dsa.h', '__dsa_sync_wait'),
            ('dsa/single.c', 'dsa_dma_wait_one_single'),
            ('dsa/single.c', 'dsa_dma_single_idle'),
            ('dsa/batch.c', 'dsa_dma_wait_oldest_batch'),
            ('dsa/batch.c', 'dsa_dma_batch_idle'),
        )
        stubs = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef uint64_t u64;
typedef int64_t s64;
static unsigned long jiffies;
#define READ_ONCE(x) (x)
#define dma_rmb() ((void)0)
#define msecs_to_jiffies(x) (x)
#define time_after_eq(a,b) ((a) >= (b))
#define cpu_relax() (++jiffies)
#define cond_resched() ((void)0)
#define local_clock() (0)
#define clamp(v,lo,hi) (v)
#define unlikely(x) (x)
#define SWARMIO_WARN_RL(...) ((void)0)
#define SWARMIO_ERROR_RL(...) ((void)0)
#define SWARMIO_ERROR(...) ((void)0)
#define SWARMIO_DEBUG_RL(...) ((void)0)
#define DSA_COMP_NONE 0
#define DSA_COMP_SUCCESS 1
#define test_bit(i,a) ((a)[0] & (1UL << (i)))
#define clear_bit(i,a) ((a)[0] &= ~(1UL << (i)))
#define for_each_set_bit(i,a,n) for (i = 0; i < (n); ++i) if (test_bit(i,a))
struct completion_record { unsigned char status; };
struct idxd_wq { int id; };
struct idxd_desc { struct completion_record *completion; struct idxd_wq *wq; };
struct dsa_dma_ctx_single {
    struct idxd_desc **single_descs;
    unsigned long issued_mask[1], last_wait_time;
    int num_descs, curr_desc, timeout;
    void **data;
};
struct list_head { struct list_head *prev, *next; };
#define list_empty(h) ((h)->next == (h))
#define list_first_entry(h,t,m) ((t*)((char*)((h)->next) - offsetof(t,m)))
static void list_del(struct list_head *node)
{
    node->prev->next = node->next;
    node->next->prev = node->prev;
}
struct dsa_batch_desc {
    struct idxd_desc *idxd_desc;
    struct completion_record *lv2_compls;
    struct list_head node;
    int id, num_filled;
    void **data;
};
struct dsa_dma_ctx_batch {
    struct list_head issued_descs;
    struct dsa_batch_desc *batch_descs;
    unsigned long issued_mask[1];
    int num_descs, timeout;
    u64 last_wait_time;
    s64 batch_sync_timeout_ns;
};
"""
        exercise = r"""
static void complete(void *arg) { ++*(int *)arg; }

int main(void)
{
    int completed = 0;
    void *args[] = { &completed };
    struct completion_record record = { DSA_COMP_NONE };
    struct idxd_desc desc = { .completion = &record };
    struct idxd_desc *descs[] = { &desc };
    struct dsa_dma_ctx_single single = {
        .single_descs = descs, .issued_mask = {1},
        .num_descs = 1, .data = args, .timeout = 2
    };
    assert(dsa_dma_wait_one_single(&single, 0, complete) == -ETIMEDOUT);
    assert(single.issued_mask[0] == 1 && completed == 0);
    assert(!dsa_dma_single_idle(&single));
    record.status = DSA_COMP_SUCCESS;
    assert(dsa_dma_single_idle(&single));
    assert(dsa_dma_wait_one_single(&single, 0, complete) == 0);
    assert(single.issued_mask[0] == 0 && completed == 1);

    struct dsa_batch_desc work = {
        .idxd_desc = &desc, .lv2_compls = &record, .num_filled = 1, .data = args
    };
    struct dsa_dma_ctx_batch batch = {
        .batch_descs = &work, .issued_mask = {1}, .num_descs = 1, .timeout = 2
    };
    batch.issued_descs.next = batch.issued_descs.prev = &work.node;
    work.node.next = work.node.prev = &batch.issued_descs;
    record.status = DSA_COMP_NONE;
    assert(dsa_dma_wait_oldest_batch(&batch, complete) == -ETIMEDOUT);
    assert(batch.issued_mask[0] == 1 && work.num_filled == 1 && completed == 1);
    assert(!list_empty(&batch.issued_descs) && !dsa_dma_batch_idle(&batch));
    record.status = DSA_COMP_SUCCESS;
    assert(dsa_dma_batch_idle(&batch));
    assert(dsa_dma_wait_oldest_batch(&batch, complete) == 0);
    assert(batch.issued_mask[0] == 0 && work.num_filled == 0 && completed == 2);
    assert(list_empty(&batch.issued_descs));

    work.data[0] = &completed;
    work.num_filled = 1;
    batch.issued_mask[0] = 1;
    batch.issued_descs.next = batch.issued_descs.prev = &work.node;
    work.node.next = work.node.prev = &batch.issued_descs;
    record.status = 5;
    assert(dsa_dma_batch_idle(&batch));
    assert(dsa_dma_wait_oldest_batch(&batch, complete) == -EIO);
    assert(batch.issued_mask[0] == 0 && work.num_filled == 0 && completed == 2);
    assert(list_empty(&batch.issued_descs));
}
"""
        program = '\n'.join((stubs, *(source_function(*fn) for fn in functions), exercise))
        self.run_c_check(program)

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_quiesce_clears_work_only_after_hardware_stops(self):
        stubs = r"""
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#define CONFIG_SWARMIO_DSA 1
#define CONFIG_SWARMIO_WORKER_BATCH_IO 1
#define SWARMIO_STOP_TIMEOUT_MS 3
#define SWARMIO_ERROR(...) ((void)0)
#define msecs_to_jiffies(x) (x)
#define time_after_eq(a,b) ((a) >= (b))
#define dma_rmb() ((void)0)
static unsigned long jiffies;
static int complete_at, pause_error;
static void usleep_range(int min, int max) { ++jiffies; }
struct dsa_dma_ctx_single { bool reset; };
struct dsa_dma_ctx_batch { bool reset; };
struct swarmio_dispatcher { struct dsa_dma_ctx_single *dma_ctx; };
struct swarmio_worker {
    struct dsa_dma_ctx_batch *dma_ctx;
    struct { unsigned short head, tail; } local_queue;
};
struct nvme_vdev {
    unsigned int num_service_units_initialized, num_workers_initialized;
    bool paused;
    struct swarmio_dispatcher *dispatchers;
    struct swarmio_worker *workers;
};
static int swarmio_pause_io(struct nvme_vdev *dev)
{
    if (pause_error)
        return pause_error;
    dev->paused = true;
    return 0;
}
static void swarmio_resume_io(struct nvme_vdev *dev) { dev->paused = false; }
static bool swarmio_dma_idle(struct nvme_vdev *dev)
{
    assert(dev->paused);
    return complete_at >= 0 && jiffies >= (unsigned long)complete_at;
}
static void dsa_dma_single_reset(struct dsa_dma_ctx_single *ctx) { ctx->reset = true; }
static void dsa_dma_batch_reset(struct dsa_dma_ctx_batch *ctx) { ctx->reset = true; }
"""
        exercise = r"""
int main(void)
{
    struct dsa_dma_ctx_single single = {0};
    struct dsa_dma_ctx_batch batch = {0};
    struct swarmio_dispatcher disp = { .dma_ctx = &single };
    struct swarmio_worker worker = { .dma_ctx = &batch, .local_queue = {1, 7} };
    struct nvme_vdev dev = {
        .num_service_units_initialized = 1, .num_workers_initialized = 1,
        .dispatchers = &disp, .workers = &worker
    };
    pause_error = -ETIMEDOUT;
    assert(swarmio_quiesce_io(&dev) == -ETIMEDOUT);
    assert(!dev.paused && !single.reset && !batch.reset && worker.local_queue.head == 1);
    pause_error = 0;
    complete_at = -1;
    assert(swarmio_quiesce_io(&dev) == -ETIMEDOUT);
    assert(!dev.paused && !single.reset && !batch.reset && worker.local_queue.head == 1);
    complete_at = jiffies + 1;
    assert(swarmio_quiesce_io(&dev) == 0);
    assert(dev.paused && single.reset && batch.reset && worker.local_queue.head == 7);
}
"""
        self.run_c_check('\n'.join((
            stubs, source_function('src/dispatcher.c', 'swarmio_quiesce_io'), exercise,
        )))

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_cpu_quiesce_works_without_dma_declarations(self):
        stubs = r"""
#include <assert.h>
#define dma_rmb() ((void)0)
struct swarmio_worker { struct { unsigned short head, tail; } local_queue; };
struct nvme_vdev {
    unsigned int num_workers_initialized;
    struct swarmio_worker *workers;
};
static int swarmio_pause_io(struct nvme_vdev *dev) { return 0; }
"""
        exercise = r"""
int main(void)
{
    struct swarmio_worker worker = { .local_queue = {1, 7} };
    struct nvme_vdev dev = { .num_workers_initialized = 1, .workers = &worker };
    assert(swarmio_quiesce_io(&dev) == 0);
    assert(worker.local_queue.head == 7);
}
"""
        self.run_c_check('\n'.join((
            stubs, source_function('src/dispatcher.c', 'swarmio_quiesce_io'), exercise,
        )))

    def test_unload_requires_successful_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            module = tmp / 'swarmio'
            param = module / 'parameters/prepare_unload'
            param.parent.mkdir(parents=True)
            param.touch()
            script_dir = tmp / 'scripts'
            script_dir.mkdir()
            script = script_dir / 'load.sh'
            script.write_text((ROOT / 'scripts/load.sh').read_text()
                              .replace('/sys/module/swarmio', str(module)))
            for name, contents in {
                'getopt': "printf '%s\\n' '-u --'\n",
                'sudo': 'printf "%s\\n" "$*" >> "$TEST_LOG"\n'
                        'if [ "$1" = tee ]; then cat > /dev/null; exit "$DRAIN_STATUS"; fi\n',
            }.items():
                mock = tmp / name
                mock.write_text('#!/bin/sh\n' + contents)
                mock.chmod(0o755)
            log = tmp / 'calls'
            for drain_status in (1, 0):
                log.write_text('')
                env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}",
                           TEST_LOG=str(log), DRAIN_STATUS=str(drain_status))
                run = subprocess.run(['bash', str(script), '-u'], env=env,
                                     capture_output=True, text=True, timeout=5)
                self.assertEqual(run.returncode, drain_status, run.stderr)
                expected = [f'tee {param}']
                if drain_status == 0:
                    expected.append('modprobe -r swarmio')
                self.assertEqual(log.read_text().splitlines(), expected)


if __name__ == '__main__':
    unittest.main()
