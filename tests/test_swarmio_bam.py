"""源码与统一入口的回归检查；不在开发机加载内核模块或执行 GPU I/O。"""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("swarmio_bam", ROOT / "scripts/swarmio_bam.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)

class MiscDeviceTests(unittest.TestCase):
    def test_miscdevice_inode_dispatch_and_cleanup_in_c(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        patched = pipeline.validate_sources(ROOT)
        # Compile the actual committed controller implementation against small
        # kernel API doubles, exercising dynamic minor lookup and error cleanup.
        header = re.sub(r"(?m)^#include[^\n]*\n", "", patched["ctrl.h"])
        source = re.sub(r"(?m)^#include[^\n]*\n", "", patched["ctrl.c"])
        prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#define GFP_KERNEL 0
#define kmalloc(n, flags) malloc(n)
#define kfree free
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define KBUILD_MODNAME "libnvm"
#define KERN_WARNING ""
#define KERN_ERR ""
#define KERN_INFO ""
#define KERN_DEBUG ""
#define KERN_CRIT ""
#define printk(...) ((void)0)
#define MAJOR(d) ((unsigned)((d) >> 20))
#define MINOR(d) ((unsigned)((d) & 0xfffff))
#define TEST_MKDEV(a, b) ((dev_t)(((unsigned)(a) << 20) | (unsigned)(b)))
#define MISC_DYNAMIC_MINOR 255
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
struct class { int dummy; };
struct device { dev_t devt; };
struct pci_dev { struct device dev; };
struct inode { dev_t i_rdev; void *i_cdev; };
struct file_operations { int dummy; };
struct list_node { struct list_node *next; };
struct list { struct list_node head; };
static void list_node_init(struct list_node *n) { n->next = NULL; }
static void list_insert(struct list *l, struct list_node *n) { n->next = l->head.next; l->head.next = n; }
static void list_remove(struct list_node *n) { (void)n; }
static struct list_node *list_next(const struct list_node *n) { return n->next; }
struct miscdevice {
    int minor;
    const char *name;
    const struct file_operations *fops;
    struct device *parent, *this_device;
    unsigned mode;
};
static int registrations, deregistrations, failure;
static struct device nodes[8];
static int misc_register(struct miscdevice *m) {
    if (failure) return failure;
    m->minor = 120 + registrations;
    nodes[registrations].devt = TEST_MKDEV(10, m->minor);
    m->this_device = &nodes[registrations++];
    return 0;
}
static void misc_deregister(struct miscdevice *m) { (void)m; ++deregistrations; }
'''
        checks = r'''
int main(void) {
    struct list list = {0};
    struct pci_dev pci0 = {0}, pci1 = {0};
    struct file_operations fops = {0};
    struct ctrl *a = ctrl_get(&list, NULL, &pci0, 0);
    struct ctrl *b = ctrl_get(&list, NULL, &pci1, 1);
    struct inode inode = {0};
    assert(a && b);
    assert(ctrl_find_by_inode(&list, &inode) == NULL);
    failure = -EBUSY;
    assert(ctrl_chrdev_create(a, 0, &fops) == -EBUSY);
    ctrl_chrdev_remove(a);
    assert(!a->chrdev && !a->rdev && deregistrations == 0);
    failure = 0;
    assert(ctrl_chrdev_create(a, 0, &fops) == 0);
    assert(ctrl_chrdev_create(b, 0, &fops) == 0);
    assert(strcmp(a->miscdev.name, "libnvm0") == 0);
    assert(strcmp(b->miscdev.name, "libnvm1") == 0);
    assert(a->miscdev.fops == &fops && a->miscdev.parent == &pci0.dev);
    assert(a->miscdev.mode == 0600 && a->rdev != b->rdev);
    inode.i_rdev = a->rdev;
    assert(ctrl_find_by_inode(&list, &inode) == a);
    inode.i_rdev = b->rdev;
    assert(ctrl_find_by_inode(&list, &inode) == b);
    assert(ctrl_find_by_pci_dev(&list, &pci0) == a);
    assert(ctrl_chrdev_create(a, 0, &fops) == 0 && registrations == 2);
    ctrl_chrdev_remove(a);
    ctrl_chrdev_remove(a);
    assert(!a->chrdev && !a->rdev && deregistrations == 1);
    ctrl_chrdev_remove(b);
    assert(ctrl_find_by_inode(&list, &inode) == NULL);
    assert(deregistrations == 2);
    free(a); free(b);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            unit = base / "controller.c"
            unit.write_text(prelude + header + source + checks)
            compiled = subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", str(unit), "-o", str(base / "probe")],
                                      capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            subprocess.run([str(base / "probe")], check=True)


class CPUConfigurationTests(unittest.TestCase):
    def test_actual_dma_init_debug_calls_accept_opaque_idxd_wq(self):
        if not shutil.which('cc'):
            self.skipTest('C compiler unavailable')
        calls = []
        for name in ('dispatcher', 'worker'):
            text = (ROOT / 'src' / (name + '.c')).read_text()
            init = text[text.index('int swarmio_' + name + '_init('):]
            found = re.findall(r'SWARMIO_DEBUG\("%s assigned .*?\);', init, re.S)
            self.assertEqual(len(found), 1)
            calls.extend(found)
        # 和 CONFIG_DYNAMIC_DEBUG 一样，日志参数实际参与 C 类型检查；不能靠运行时 if(false) 隐藏错误。
        prefix = r'''
#include <stdio.h>
#include <stdbool.h>
struct idxd_wq;
struct dma_context { struct idxd_wq *wq; };
struct service_thread { const char *thread_name; struct dma_context *dma_ctx; };
#define SWARMIO_DEBUG(...) fprintf(stderr, __VA_ARGS__)
void probe(bool using_dma, struct service_thread *dispatcher, struct service_thread *worker) {
    int dev_id = 0, wq_id = 0;
    if (using_dma) {
'''
        suffix = '\n    }\n}\n'
        broken = [r'SWARMIO_DEBUG("%s assigned %s\n", dispatcher->thread_name, dispatcher->dma_ctx->wq->name);',
                  r'SWARMIO_DEBUG("%s assigned %s\n", worker->thread_name, worker->dma_ctx->wq->name);']
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'init-debug.c'
            for optimization in ('-O0', '-O2'):
                command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', optimization,
                           '-fsyntax-only', str(source)]
                source.write_text(prefix + '\n'.join(broken) + suffix)
                before = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(before.returncode, 0)
                self.assertRegex(before.stderr, 'incomplete|undefined')
                source.write_text(prefix + '\n'.join(calls) + suffix)
                after = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(after.returncode, 0, after.stderr)

    def test_kbuild_selects_cpu_or_real_dsa_backend(self):
        if not shutil.which("make"):
            self.skipTest("make unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Makefile"
            path.write_text("include " + str(ROOT / "Kbuild") +
                            "\nprint:\n\t@echo $(swarmio-objs) $(ccflags-y)\n")
            command = ["make", "-s", "-f", str(path), "print"]
            cpu = subprocess.check_output(command + ["CONFIG_SWARMIO_DMA_DISPATCHER=n",
                                                     "CONFIG_SWARMIO_DMA_WORKER=n"], text=True)
            self.assertIn("dsa/stub.o", cpu)
            self.assertNotIn("dsa/dsa.o", cpu)
            self.assertNotIn("drivers/dma/idxd", cpu)
            for dispatcher, worker in [("y", "n"), ("n", "y"), ("y", "y")]:
                dsa = subprocess.check_output(command + ["CONFIG_SWARMIO_DMA_DISPATCHER=" + dispatcher,
                                                         "CONFIG_SWARMIO_DMA_WORKER=" + worker], text=True)
                self.assertIn("dsa/dsa.o", dsa)
                self.assertNotIn("dsa/stub.o", dsa)

    def test_cpu_backend_compiles_without_private_idxd_headers(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "linux").mkdir()
            (base / "logging.h").write_text("")
            (base / "linux/kernel.h").write_text("""
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef int64_t s64;
typedef uint64_t dma_addr_t;
struct list_head { void *next, *prev; };
""")
            for name in ("version.h", "pci.h", "dma-mapping.h"):
                (base / "linux" / name).write_text("")
            (base / "linux/errno.h").write_text("#include <errno.h>\n")
            (base / "linux/bitmap.h").write_text("#define DECLARE_BITMAP(name, n) unsigned long name[1]\n")
            (base / "linux/idxd.h").write_text("""
struct dsa_hw_desc {
    u32 flags; char opcode; u64 src_addr, dst_addr, xfer_size, completion_addr; int priv;
};
""")
            # 不提供私有 idxd.h：若 CPU 配置仍包含它，实际编译会失败。
            shutil.copy2(ROOT / "dsa/dsa.h", base / "dsa.h")
            shutil.copy2(ROOT / "dsa/stub.c", base / "stub.c")
            (base / "main.c").write_text("""
#include <assert.h>
#include <errno.h>
#include "dsa.h"
int main(void) {
    int dev = 0, wq = 0;
    assert(dsa_enable_devices() == -EOPNOTSUPP);
    assert(dsa_dma_ctx_single_init(NULL, 0, 0, 1, 1) == -EOPNOTSUPP);
    assert(dsa_dma_issue_sync_single(NULL, 0, 0, 512, NULL, NULL) == -EOPNOTSUPP);
    assert(dsa_dma_issue_async_batch(NULL, 0, 0, 512, NULL, NULL) == -EOPNOTSUPP);
    dsa_assign_wq_by_group_rr(0, 0, &dev, &wq);
    assert(dev == -1 && wq == -1);
    return 0;
}
""")
            compiled = subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                                       "-I", str(base), str(base / "stub.c"), str(base / "main.c"),
                                       "-o", str(base / "probe")], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            subprocess.run([str(base / "probe")], check=True)


class SQDiagnosticTests(unittest.TestCase):
    def test_source_guard_precedes_namespace_lookup(self):
        modified = (ROOT / "src/dispatcher.c").read_text()
        function = modified[modified.index("int swarmio_dispatch_sq("):]
        self.assertLess(function.index("sq_diag_batch("), function.index("&vdev->ns["))
        entry = modified[modified.index("static bool __dispatch_sq_entry("):]
        self.assertLess(entry.index("sq_diag_valid("), entry.index("nsid = cmd->common.nsid - 1"))
        self.assertNotIn("int num_fetch = new_db - sq->fetched_pos", function)
        self.assertEqual(function.count("sq_diag_copy("), 2)

    def test_c_guards_all_batch_commands_and_page_aware_io_copy(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define __iomem
#define __force
#define CONFIG_SWARMIO_SQ_DIAGNOSTICS 1
#define NR_MAX_IO_QUEUE 64
#define NR_SQE_PER_PAGE 64
#define LBA_BITS 9
#define nvme_cmd_read 2
#define nvme_cmd_write 1
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define le32_to_cpu(x) (x)
#define le16_to_cpu(x) (x)
#define le64_to_cpu(x) (x)
#define min(a, b) ((a) < (b) ? (a) : (b))
#define module_param(a, b, c)
#define MODULE_PARM_DESC(a, b)
#define MODULE_INFO(a, b)
#define KERN_ERR ""
#define DUMP_PREFIX_NONE 0
static int messages;
#define SWARMIO_ERROR(...) (++messages)
#define print_hex_dump(...) ((void)0)
struct nvme_command {
    union {
        struct { uint8_t opcode, flags; uint16_t command_id; uint32_t nsid; } common;
        struct { char pad[24]; struct { u64 prp1, prp2; } dptr; u64 slba; uint16_t length; char tail[14]; } rw;
    };
};
_Static_assert(sizeof(struct nvme_command) == 64, "SQE size");
struct nvme_submission_queue {
    int qid, queue_size;
    u64 dma_addr;
    void *mapped;
    struct nvme_command *pages[2];
};
struct ns { u64 size; };
static struct { unsigned num_ns; struct ns *ns; } state;
#define vdev (&state)
static struct nvme_command * __sq_entry_at(struct nvme_submission_queue *sq, int entry) {
    return &sq->pages[entry / NR_SQE_PER_PAGE][entry % NR_SQE_PER_PAGE];
}
static int io_copies;
static void memcpy_fromio(void *dst, const void *src, size_t n) { ++io_copies; memcpy(dst, src, n); }
'''
        checks = r'''
int main(void) {
    struct ns namespace = { .size = 1ULL << 30 };
    struct nvme_submission_queue sq = { .qid = 1, .queue_size = 128, .dma_addr = 0xf0000000, .mapped = (void *)1 };
    struct nvme_command cmds[2] = {0};
    struct nvme_command page0[64] = {0}, unrelated[64] = {0}, page1[64] = {0}, copied[4] = {0};
    (void)unrelated;
    state.num_ns = 1; state.ns = &namespace;
    cmds[0].common.opcode = nvme_cmd_read;
    cmds[0].common.nsid = 1;
    cmds[0].rw.dptr.prp1 = 0xf0010000;
    cmds[1] = cmds[0];
    assert(sq_diag_batch(&sq, cmds, 0, 2));
    cmds[1].common.nsid = 0;
    assert(!sq_diag_batch(&sq, cmds, 0, 2) && sq_diag_faulted);
    cmds[1] = cmds[0];
    assert(!sq_diag_valid(&sq, cmds, 0)); /* fault remains latched */
    sq_diag_faulted = false;
    cmds[1].common.nsid = UINT32_MAX;
    assert(!sq_diag_valid(&sq, &cmds[1], 1));
    sq_diag_faulted = false;
    cmds[1] = cmds[0]; cmds[1].rw.slba = (1ULL << 30) >> 9;
    assert(!sq_diag_valid(&sq, &cmds[1], 1));
    sq_diag_faulted = false;
    cmds[1] = cmds[0]; cmds[1].rw.slba = ((1ULL << 30) >> 9) - 1; cmds[1].rw.length = 1;
    assert(!sq_diag_valid(&sq, &cmds[1], 1));
    sq_diag_faulted = false;
    cmds[1] = cmds[0]; cmds[1].rw.dptr.prp1 = 0;
    assert(!sq_diag_valid(&sq, &cmds[1], 1));
    sq_diag_faulted = false;
    cmds[1] = cmds[0]; cmds[1].common.opcode = 0xff;
    assert(!sq_diag_valid(&sq, &cmds[1], 1));
    sq_diag_faulted = false;
    sq.pages[0] = page0; sq.pages[1] = page1;
    memset(&page0[62], 0x12, 128); memset(&page1[0], 0x34, 128);
    sq_diag_copy(&sq, copied, 62, 4);
    assert(io_copies == 2);
    assert(memcmp(copied, &page0[62], 128) == 0);
    assert(memcmp(&copied[2], &page1[0], 128) == 0);
    io_copies = 0; sq_cpu_fromio = false;
    memset(copied, 0, sizeof(copied)); sq_diag_copy(&sq, copied, 62, 4);
    assert(io_copies == 0 && memcmp(&copied[2], &page1[0], 128) == 0);
    sq_cpu_fromio = true; sq.mapped = NULL;
    sq_diag_copy(&sq, copied, 62, 4);
    assert(io_copies == 0);
    assert(messages > 0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "linux").mkdir()
            for name in ["io.h", "module.h", "moduleparam.h", "printk.h"]:
                (base / "linux" / name).write_text("/* kernel API provided by test prelude */\n")
            unit = base / "probe.c"
            unit.write_text(prelude + (ROOT / "src/swarmio_sq_diagnostics.h").read_text() + checks)
            result = subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                                     "-I", str(base), str(unit), "-o", str(base / "probe")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(base / "probe")], check=True)


COMMON = (ROOT / "src/swarmio_queue_diagnostics.h").read_text()


def _implementation(name, end):
    text = (ROOT / "src" / (name + ".c")).read_text()
    start = text.index('#include "swarmio_queue_diagnostics.h"')
    return text[start:text.index(end, start)] + "#endif\n"


DISPATCH = _implementation("dispatcher", "#else\nstatic inline void qdiag_reset_sq")
WORKER = _implementation("worker", "#else\nstatic inline void qdiag_sample_cq")



class QueueDiagnosticTests(unittest.TestCase):
    def copied_tree(self, base):
        (base / "src").mkdir()
        for name in ("dispatcher", "worker"):
            shutil.copy2(ROOT / "src" / (name + ".c"), base / "src" / (name + ".c"))
        for name in ("swarmio_sq_diagnostics.h", "swarmio_queue_diagnostics.h"):
            shutil.copy2(ROOT / "src" / name, base / "src" / name)

    def test_committed_owner_sites_and_reset(self):
        dispatch = (ROOT / "src/dispatcher.c").read_text()
        worker = (ROOT / "src/worker.c").read_text()
        self.assertLess(dispatch.index("qdiag_reset_sq();"),
                        dispatch.index("WRITE_ONCE(vdev->stop_flag, 0);"))
        self.assertEqual(dispatch.count("++qdiag_dispatched;"), 1)
        self.assertEqual(worker.count("++qdiag_cqe_writes;"), 1)
        self.assertNotIn("memcpy_fromio", WORKER + COMMON)

    def test_actual_cq_write_wrap_full_and_snapshot_getters(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
typedef uint32_t u32;
typedef unsigned long long u64;
typedef uint16_t u16;
#define CONFIG_SWARMIO_QUEUE_DIAGNOSTICS 1
#define NR_MAX_IO_QUEUE 64
#define PAGE_SIZE 4096
#define CONFIG_SWARMIO_DISP_COALESCE_SQE 1
#define READ_ONCE(x) (x)
#define le16_to_cpu(x) (x)
#define le32_to_cpu(x) (x)
#define cpu_to_le16(x) (x)
#define __iomem
#define WRITE_ONCE(x, v) ((x) = (v))
extern int io_writes, barriers, last_io_barrier;
#define wmb() (++barriers)
static void writel(u32 value, u32 *dst) {
    if (io_writes % 2) assert(barriers > last_io_barrier);
    else last_io_barrier = barriers;
    ++io_writes; *dst = value;
}
#define DEFINE_SPINLOCK(x) int x
#define spin_lock_irqsave(lock, flags) ((void)(lock), (flags) = 0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock), (void)(flags))
#define smp_load_acquire(ptr) (*(ptr))
#define unlikely(x) (x)
#define SWARMIO_DEBUG_RL(...) ((void)0)
#define module_param_named(name, arg, type, mode)
#define module_param_cb(name, ops, arg, mode)
#define MODULE_PARM_DESC(name, desc)
#define MODULE_INFO(name, value)
struct kernel_param { int unused; };
struct kernel_param_ops { int (*get)(char *, const struct kernel_param *); };
static int scnprintf(char *dst, size_t size, const char *format, ...) {
    int result; va_list args; va_start(args, format);
    result = vsnprintf(dst, size, format, args); va_end(args);
    return result < (int)size ? result : (int)size - 1;
}
struct nvme_completion { u32 result, reserved; u16 sq_head, sq_id, command_id, status; };
struct nvme_submission_queue { int queue_size, sq_head, fetched_pos, cqid; };
struct nvme_completion_queue {
    int queue_size, cq_head, cq_tail, phase; bool interrupt_ready;
    void *mapped;
    struct nvme_completion entries[128];
};
struct swarmio_io_work {
    struct nvme_completion cqe; int cqid; bool is_issued, is_copied, is_completed;
    u64 nsecs_target;
};
struct swarmio_worker_local_queue {
    struct swarmio_io_work *works; unsigned short head, tail, size;
};
struct swarmio_dispatcher {
    struct { int start, end, stride; } qids;
    unsigned int id, num_workers;
};
struct swarmio_worker {
    struct swarmio_dispatcher *dispatcher; unsigned int disp_id, id;
    struct swarmio_worker_local_queue local_queue;
};
struct state {
    struct nvme_submission_queue *sqes[65];
    struct nvme_completion_queue *cqes[65]; u32 dbs[130], old_dbs[130];
};
extern struct state *vdev;
extern u64 test_now;
extern int dma_batch_size, num_dma_descs_per_worker;
static u64 ktime_get_ns(void) { return test_now; }
static unsigned int __get_worker(int qid, struct swarmio_dispatcher *disp) {
    return ((qid - disp->qids.start) / disp->qids.stride) % disp->num_workers;
}
static struct nvme_completion *__cq_entry_at(struct nvme_completion_queue *cq, int pos) {
    return &cq->entries[pos];
}
'''
        checks = r'''
extern void sample_sq(struct swarmio_dispatcher *);
extern void sample_cq(struct swarmio_worker *);
extern void get_sq(char *);
extern void get_cq(char *);
extern void reset_all(void);
extern bool fill(struct swarmio_io_work *);
struct state storage, *vdev = &storage;
u64 test_now = 1000000000ULL;
int dma_batch_size = 2, num_dma_descs_per_worker = 2;
int io_writes, barriers, last_io_barrier;
int main(void) {
    struct nvme_submission_queue sq = { .queue_size=128, .sq_head=5, .fetched_pos=5, .cqid=1 };
    struct nvme_completion_queue cq = { .queue_size=128, .phase=1 };
    struct swarmio_dispatcher disp = { .qids={1,64,4}, .num_workers=1 };
    struct swarmio_worker worker = { .dispatcher=&disp, .local_queue={.size=16} };
    struct swarmio_io_work work = { .cqe={.sq_id=1, .command_id=7}, .cqid=1 };
    struct swarmio_io_work works[16] = {0};
    char buffer[4096], saved[4096];
    worker.local_queue.works = works;
    storage.sqes[1]=&sq; storage.cqes[1]=&cq; storage.dbs[2]=5;
    for (int i=0; i<5; ++i) assert(fill(&work));
    sample_sq(&disp); sample_cq(&worker);
    get_sq(buffer); assert(strstr(buffer, "sq_head=5 fetched=5"));
    get_cq(buffer); assert(strstr(buffer, "cq_tail=5 phase=1 cqe_writes=5 full_hits=0"));
    assert(strstr(buffer, "last_slot=4 last_dw2=00010005 last_dw3=00010007"));
    strcpy(saved, buffer);
    test_now += 1000000ULL;
    sample_cq(&worker); get_cq(buffer); assert(strcmp(saved, buffer)==0);
    while (cq.cq_tail < 127) assert(fill(&work));
    assert(!fill(&work));
    test_now += 250000000ULL; sample_cq(&worker); get_cq(buffer);
    assert(strstr(buffer, "cq_tail=127 phase=1 cqe_writes=127 full_hits=1"));
    storage.dbs[3]=64;
    assert(fill(&work)); assert(cq.cq_tail==0 && cq.phase==0);
    assert(cq.entries[127].status==1);
    assert(fill(&work)); assert(cq.entries[0].status==0);
    storage.dbs[3]=127; assert(fill(&work));
    storage.dbs[3]=0; assert(fill(&work));
    test_now += 250000000ULL; sample_cq(&worker); get_cq(buffer);
    assert(strstr(buffer, "cq_head=0 cq_head_abs=128 cq_tail=3 phase=0 cqe_writes=131"));
    assert(strstr(buffer, "last_dw3=00000007"));
    cq.mapped = (void *)1;
    work.cqe.status = 12;
    assert(fill(&work)); assert(io_writes == 2);
    assert(cq.entries[3].status == 12 && cq.entries[3].command_id == 7);
    assert(cq.entries[3].sq_id == 1 && cq.entries[3].sq_head == 5);
    strcpy(saved, buffer);
    /* A wrong owner and a removed SQ must not access this queue or replace text. */
    disp.qids.start=2; test_now += 250000000ULL;
    sample_cq(&worker); get_cq(buffer); assert(!strcmp(saved, buffer));
    disp.qids.start=1; storage.sqes[1]=NULL;
    sample_cq(&worker); get_cq(buffer); assert(!strcmp(saved, buffer));
    /* Getter remains safe with no live device: it only copies saved text. */
    vdev=NULL; get_cq(buffer); assert(!strcmp(saved, buffer));
    vdev=&storage; storage.sqes[1]=&sq;
    reset_all(); get_sq(buffer); assert(strstr(buffer, "epoch=1 reset"));
    get_cq(buffer); assert(strstr(buffer, "epoch=1 reset"));
    cq.cq_tail=cq.cq_head=0; cq.phase=1; storage.dbs[3]=storage.old_dbs[3]=0;
    sample_cq(&worker); get_cq(buffer);
    assert(strstr(buffer, "epoch=1") && strstr(buffer, "cqe_writes=0 full_hits=0 last_slot=-1"));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.copied_tree(base)
            transformed = (base / "src/worker.c").read_text()
            start = transformed.index("static bool __fill_cq_result(")
            end = transformed.index("static inline void __swarmio_complete", start)
            fill = transformed[start:end]
            (base / "linux").mkdir()
            for name in ("module.h", "moduleparam.h", "spinlock.h", "ktime.h"):
                (base / "linux" / name).write_text("/* APIs in prelude */\n")
            shutil.copy2(base / "src/swarmio_queue_diagnostics.h", base / "swarmio_queue_diagnostics.h")
            (base / "dispatch.c").write_text(prelude + DISPATCH + r'''
void sample_sq(struct swarmio_dispatcher *d) { qdiag_sample_sq(d); }
void get_sq(char *buffer) { qdiag_get(buffer, NULL); }
void reset_all(void) { qdiag_reset_sq(); }
''')
            (base / "worker.c").write_text(prelude + WORKER + fill + r'''
void sample_cq(struct swarmio_worker *w) { qdiag_sample_cq(w); }
void get_cq(char *buffer) { qdiag_get(buffer, NULL); }
bool fill(struct swarmio_io_work *w) { return __fill_cq_result(w); }
''')
            (base / "main.c").write_text(prelude + checks)
            result = subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                                     "-Wno-unused-function", "-Wno-unused-variable", "-I", str(base),
                                     str(base / "dispatch.c"), str(base / "worker.c"),
                                     str(base / "main.c"), "-o", str(base / "probe")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(base / "probe")], check=True)



class CpuBarTransferTests(unittest.TestCase):
    def test_actual_prp_copy_offsets_lists_and_mapping_failure(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        source = (ROOT / "src/worker.c").read_text()
        start = source.index("static int __swarmio_xfer_data(")
        end = source.index("static inline int __swarmio_submit_dma", start)
        prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
typedef uint64_t u64;
#define __iomem
#define PAGE_SIZE 4096UL
#define PAGE_SHIFT 12
#define PAGE_MASK (~(PAGE_SIZE - 1))
#define PAGE_OFFSET_MASK (PAGE_SIZE - 1)
#define PTRS_PER_PRP_LIST 512
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define le64_to_cpu(x) (x)
#define BUG_ON(x) assert(!(x))
enum { nvme_cmd_read, nvme_cmd_write, nvme_cmd_zone_append };
struct nvme_rw_command { int opcode, nsid; size_t length; struct { u64 prp1, prp2; } dptr; };
static unsigned char gpu[8 * PAGE_SIZE], ram[8 * PAGE_SIZE], disk[8 * PAGE_SIZE];
static struct { struct { void *mapped; } ns[1]; } device, *vdev = &device;
static int active, io_reads, io_writes, fail_map;
static size_t __cmd_io_offset(struct nvme_rw_command *c) { (void)c; return 0; }
static size_t __cmd_io_size(struct nvme_rw_command *c) { return c->length; }
static bool pfn_valid(u64 pfn) { return pfn < 0xf0000000UL / PAGE_SIZE; }
static void *pfn_to_page(u64 pfn) { return ram + (pfn * PAGE_SIZE - 0x100000); }
static void *kmap_local_page(void *page) { ++active; return page; }
static void kunmap_local(void *p) { assert(((uintptr_t)p - (uintptr_t)ram) % PAGE_SIZE == 0); --active; }
static void *ioremap(u64 addr, size_t n) {
    assert(addr % PAGE_SIZE == 0 && n == PAGE_SIZE);
    if (fail_map) return NULL;
    assert(addr >= 0xf0000000 && addr + n <= 0xf0000000 + sizeof(gpu));
    ++active; return gpu + (addr - 0xf0000000);
}
static void iounmap(void *p) { assert(((uintptr_t)p - (uintptr_t)gpu) % PAGE_SIZE == 0); --active; }
static void memcpy_fromio(void *dst, const void *src, size_t n) { ++io_reads; memcpy(dst, src, n); }
static void memcpy_toio(void *dst, const void *src, size_t n) { ++io_writes; memcpy(dst, src, n); }
'''
        checks = r'''
int main(void) {
    device.ns[0].mapped = disk;
    for (size_t i=0; i<sizeof(disk); ++i) disk[i] = (unsigned char)(i * 7 + i / 512);
    /* 每个 512 B 页内位置都检查前后哨兵，捕获重复加偏移及邻接内存破坏。 */
    for (int io=0; io<2; ++io) {
        unsigned char *target = io ? gpu : ram;
        u64 phys = io ? 0xf0000000 : 0x100000;
        for (size_t off=0; off<PAGE_SIZE; off+=512) {
            memset(target, 0xa5, sizeof(gpu));
            struct nvme_rw_command c = { .opcode=nvme_cmd_read, .nsid=1, .length=512,
                .dptr={phys+off,0} };
            assert(__swarmio_xfer_data(&c)==0 && active==0);
            assert(!memcmp(target+off, disk, 512));
            for (size_t i=0; i<sizeof(gpu); ++i)
                if (i<off || i>=off+512) assert(target[i]==0xa5);
            memset(disk, 0, 512); c.opcode=nvme_cmd_write;
            assert(__swarmio_xfer_data(&c)==0 && active==0);
            assert(!memcmp(target+off, disk, 512));
            memset(disk, 0x6b, 512);
        }
        /* PRP1 跨页 + PRP2 直接页地址。 */
        struct nvme_rw_command c = { .opcode=nvme_cmd_read, .nsid=1, .length=1024,
            .dptr={phys+3584,phys+PAGE_SIZE} };
        assert(__swarmio_xfer_data(&c)==0 && active==0);
        assert(!memcmp(target+3584,disk,1024));
        /* 偏移 8 B 的 PRP 列表必须用原始页基址释放；列表可在 RAM 或 BAR。 */
        u64 list[2] = {phys+PAGE_SIZE,phys+2*PAGE_SIZE};
        memcpy(target+4*PAGE_SIZE+8,list,sizeof(list));
        c.length=3*PAGE_SIZE; c.dptr.prp1=phys; c.dptr.prp2=phys+4*PAGE_SIZE+8;
        assert(__swarmio_xfer_data(&c)==0 && active==0);
        assert(!memcmp(target,disk,3*PAGE_SIZE));
    }
    assert(io_reads>0 && io_writes>0);
    fail_map=1;
    struct nvme_rw_command c = { .opcode=nvme_cmd_read, .nsid=1, .length=512,
        .dptr={0xf0000200,0} };
    assert(__swarmio_xfer_data(&c)==-ENOMEM && active==0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "probe.c").write_text(prelude + source[start:end] + checks)
            result = subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                                     str(path / "probe.c"), "-o", str(path / "probe")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(path / "probe")], check=True)


class PipelineTests(unittest.TestCase):
    def test_cmake_322_is_supported_without_native_architecture(self):
        with patch.object(pipeline, 'output', return_value='cmake version 3.22.1') as query:
            self.assertEqual(pipeline.cmake_info(), 'cmake version 3.22.1')
            query.assert_called_once()
        with patch.object(pipeline, 'output', return_value='cmake version 3.21.4'):
            with self.assertRaises(pipeline.Error):
                pipeline.cmake_info()
        for path in [ROOT / 'bam/CMakeLists.txt', *(ROOT / 'bam/benchmarks').glob('*/CMakeLists.txt')]:
            self.assertNotIn('VERSION 3.24', path.read_text())
        self.assertNotIn('set(CMAKE_CUDA_ARCHITECTURES native)', (ROOT / 'bam/CMakeLists.txt').read_text())

    def test_explicit_cuda_root_does_not_require_nvcc_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            nvcc = root / 'bin/nvcc'
            nvcc.write_text('#!/bin/sh\nexit 0\n')
            nvcc.chmod(0o755)
            with patch.object(pipeline.shutil, 'which', return_value=None):
                self.assertEqual(pipeline.find_nvcc(root), nvcc.resolve())
            with self.assertRaises(pipeline.Error):
                pipeline.find_nvcc(root / 'missing')

    def test_inaccessible_kernel_input_has_permission_diagnostic(self):
        with patch.object(Path, 'open', side_effect=PermissionError(13, 'Permission denied')):
            with self.assertRaises(pipeline.Error) as caught:
                pipeline.require_readable_file(Path('/kernel/Makefile'))
        self.assertIn('/kernel/Makefile', str(caught.exception))
        self.assertIn('namei', str(caught.exception))

    def test_config_and_workload_limits(self):
        config = pipeline.read_config(ROOT / "configs/bam-cpu.json")
        self.assertEqual(config["benchmark"]["reqs"], 1)
        for field, invalid in [("queue_depth", 3), ("page_size", 4096), ("threads", 65),
                               ("num_queues", 65), ("num_blks", 1 << 50)]:
            bench = dict(config["benchmark"], **{field: invalid})
            with self.assertRaises(pipeline.Error):
                pipeline.validate_benchmark(bench, config)

    def test_sizes_and_cpu_ranges(self):
        self.assertEqual(pipeline.parse_size("24G"), 24 << 30)
        self.assertEqual(pipeline.cpu_set("8-10,12"), {8, 9, 10, 12})
        for invalid in ("10-8", "a", "1,,2"):
            with self.assertRaises(pipeline.Error):
                pipeline.cpu_set(invalid)

    def test_module_audit_rejects_bad_crc_and_gpl_imports(self):
        nvidia = {"nvidia_p2p_get_pages": {"crc": 0x1234, "export": "EXPORT_SYMBOL"}}
        kernel = {"device_create": {"crc": 0x9999, "export": "EXPORT_SYMBOL_GPL"}}
        for bad_crc, gpl in [(False, False), (True, False), (False, True)]:
            def output(command, **kwargs):
                if command[0] == "nm":
                    return " U nvidia_p2p_get_pages\n" + (" U device_create\n" if gpl else "")
                return ("0xdead" if bad_crc else "0x1234") + " nvidia_p2p_get_pages"
            with patch.object(pipeline, "output", side_effect=output):
                if bad_crc or gpl:
                    with self.assertRaises(pipeline.Error):
                        pipeline.audit_libnvm(Path("libnvm.ko"), kernel, nvidia)
                else:
                    self.assertEqual(pipeline.audit_libnvm(Path("libnvm.ko"), kernel, nvidia),
                                     {"nvidia_p2p_get_pages": "0x1234"})

    def test_physical_nvme_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, vendor, device in [("0001:10:00.0", "0x0c51", "0x0110"),
                                         ("0000:01:00.0", "0x144d", "0xa808")]:
                p = base / name
                p.mkdir()
                (p / "vendor").write_text(vendor)
                (p / "device").write_text(device)
            with patch.object(pipeline, "PCI", base):
                self.assertEqual(pipeline.virtual_device().name, "0001:10:00.0")
                with self.assertRaises(pipeline.Error):
                    pipeline.virtual_device("0000:01:00.0")

    def test_artifact_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in pipeline.ARTIFACTS:
                p = base / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(name.encode())
            manifest = {"state": "built", "environment": {"kernel": "test-kernel"},
                        "artifacts_sha256": {name: pipeline.digest(base / name) for name in pipeline.ARTIFACTS}}
            pipeline.save_json(base / "manifest.json", manifest)
            args = SimpleNamespace(build_dir=base)
            with patch.object(pipeline.os, "uname", return_value=SimpleNamespace(release="test-kernel")):
                pipeline.manifest_for(args)
                (base / pipeline.ARTIFACTS[0]).write_bytes(b"changed")
                with self.assertRaises(pipeline.Error):
                    pipeline.manifest_for(args)

    def test_fault_detection_and_command_arguments(self):
        with self.assertRaises(pipeline.Error):
            pipeline.check_kernel_health("BUG: kernel NULL pointer\nRIP: __fill_cq_result [swarmio]\nOops: 0000")
        pipeline.check_kernel_health("SwarmIO: controller enabled")
        bench = pipeline.read_config(ROOT / "configs/bam-cpu.json")["benchmark"]
        command = pipeline.benchmark_command(Path("/tmp/nvm-block-bench"), bench)
        self.assertIn("--random=false", command)
        self.assertIn("--gpu=0", command)
        self.assertIn("--access_type=0", command)


class LifecycleTests(unittest.TestCase):
    def test_failed_bam_insertion_restores_virtual_nvme_binding(self):
        config = pipeline.read_config(ROOT / 'configs/bam-cpu.json')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            devices = base / 'devices'
            device = devices / '0001:10:00.0'
            device.mkdir(parents=True)
            (device / 'class').write_text('0x010802')
            driver = base / 'drivers/nvme'
            driver.mkdir(parents=True)
            (driver / 'bind').write_text('')
            (driver / 'unbind').write_text('')
            (device / 'driver').symlink_to(driver)
            (base / 'cmdline').write_text('memmap=24G$4G')
            (base / 'online').write_text('0-31')
            manifest = {'config': config}
            commands = []

            def path(value):
                return {'/proc/cmdline': base / 'cmdline',
                        '/sys/devices/system/cpu/online': base / 'online'}.get(str(value), Path(value))

            def execute(command, **kwargs):
                commands.append(command)
                if command[0] == 'insmod' and command[1].endswith('libnvm/libnvm.ko'):
                    (device / 'driver').unlink()  # 模拟前面的 sysfs unbind 已经完成。
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)

            with patch.object(pipeline, 'Path', side_effect=path), patch.object(pipeline, 'PCI', devices), \
                 patch.object(pipeline, 'linux_only'), patch.object(pipeline, 'manifest_for', return_value=manifest), \
                 patch.object(pipeline, 'check_no_workload'), patch.object(pipeline, 'verify_gpu'), \
                 patch.object(pipeline, 'dmesg_text', return_value=''), \
                 patch.object(pipeline, 'module_state', return_value={}), \
                 patch.object(pipeline, 'virtual_device', return_value=device), \
                 patch.object(pipeline, 'output', return_value=''), \
                 patch.object(pipeline.subprocess, 'run', side_effect=execute):
                with self.assertRaises(subprocess.CalledProcessError):
                    pipeline.load(SimpleNamespace(build_dir=base / 'build', bdf=None))
            self.assertEqual((driver / 'unbind').read_text(), device.name)
            self.assertEqual((driver / 'bind').read_text(), device.name)
            self.assertTrue(all(c[0] != 'rmmod' for c in commands))

    def test_build_copies_sources_and_never_loads_modules(self):
        config = pipeline.read_config(ROOT / 'configs/bam-cpu.json')
        for bad_crc in (False, True):
            with self.subTest(bad_crc=bad_crc), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                kernel = base / 'kernel'
                kernel.mkdir()
                (kernel / 'Module.symvers').write_text('0x9999 device_create vmlinux EXPORT_SYMBOL_GPL\n')
                nvtable = base / 'nvidia.symvers'
                nvtable.write_text('0x1234 nvidia_p2p_get_pages nvidia EXPORT_SYMBOL\n')
                out = base / 'build'
                env = {'kernel': 'fake-kernel', 'kernel_build': str(kernel), 'nvidia_include': '/fake/nvidia',
                       'nvcc_path': '/fake/cuda/bin/nvcc', 'cuda_root': '/fake/cuda',
                       'nvidia_symvers': str(nvtable), 'gpu': {'cuda_arch': '70'}}

                def execute(command, **kwargs):
                    if command[0] == 'make':
                        source = Path(next(p[2:] for p in command if p.startswith('M=')))
                        name = 'swarmio' if source.parent.name == 'swarmio' else 'libnvm'
                        (source / (name + '.ko')).write_bytes(name.encode())
                    elif command[0] == 'objcopy':
                        shutil.copy2(command[-2], command[-1])
                    elif command[0] == 'cmake' and '--build' in command:
                        for name in pipeline.ARTIFACTS[2:]:
                            p = out / name
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_bytes(b'fixture')
                    return SimpleNamespace(stdout=iter(['fixture build\n']), wait=lambda: 0)

                def output(command, **kwargs):
                    if command[0] == 'git':
                        return '' if command[-1] == '--short' else 'fake-commit'
                    if command[0] == 'nm':
                        return ' U nvidia_p2p_get_pages' if 'libnvm.ko' in command[-1] else ' U memcpy'
                    if command[0] == 'modprobe':
                        return ('0xdead' if bad_crc else '0x1234') + ' nvidia_p2p_get_pages'
                    if command[0] == 'ldd':
                        return 'libnvm.so => ' + str(out / 'bam/lib/libnvm.so')
                    field = command[2]
                    if field == 'name':
                        return Path(command[-1]).stem
                    return {'vermagic': 'fake-kernel SMP', 'swarmio_skip_io': '0',
                            'swarmio_sq_diag': '1', 'swarmio_queue_diag': '1'}[field]

                args = SimpleNamespace(build_dir=out, jobs=2)
                with patch.object(pipeline, 'environment', return_value=env), \
                     patch.object(pipeline, 'source_inventory', return_value={}), \
                     patch.object(pipeline, 'output', side_effect=output), \
                     patch.object(pipeline.subprocess, 'Popen', side_effect=execute), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if bad_crc:
                        with self.assertRaises(pipeline.Error):
                            pipeline.build(args, config)
                    else:
                        pipeline.build(args, config)
                manifest = json.loads((out / 'manifest.json').read_text())
                self.assertEqual(manifest['state'], 'failed' if bad_crc else 'built')
                self.assertTrue(all(c[0] in ('make', 'cmake', 'objcopy') for c in manifest['commands']))
                self.assertEqual((out / 'swarmio/source/src/dispatcher.c').read_bytes(),
                                 (ROOT / 'src/dispatcher.c').read_bytes())
                self.assertEqual((out / 'libnvm/source/ctrl.c').read_bytes(),
                                 (ROOT / 'bam/module/ctrl.c').read_bytes())

    def test_run_records_completion_and_selects_gpu_uuid(self):
        config = pipeline.read_config(ROOT / 'configs/bam-cpu.json')
        for finished in (False, True):
            with self.subTest(finished=finished), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                out = base / 'build'
                out.mkdir()
                (out / 'manifest.json').write_text('{}')
                pipeline.save_json(out / 'loaded.json', {'run_state': 'not_started'})
                probe = base / 'probe.py'
                probe.write_text('import os\nprint(os.environ["CUDA_VISIBLE_DEVICES"])\n' +
                                 ('print("Ops/sec: 1")\n' if finished else 'print("Unexpected error")\n'))
                manifest = {'config': config, 'environment': {'gpu': {'uuid': 'GPU-12345678'}}}
                args = SimpleNamespace(build_dir=out, threads=None, reqs=None, queue_depth=None,
                                       num_queues=None, random=False, access=None)
                with patch.object(pipeline, 'ROOT', base), \
                     patch.object(pipeline, 'linux_only'), \
                     patch.object(pipeline, 'verify_session', return_value=manifest), \
                     patch.object(pipeline, 'check_no_workload'), \
                     patch.object(pipeline, 'check_library'), \
                     patch.object(pipeline, 'dmesg_text', return_value=''), \
                     patch.object(pipeline, 'snapshot', return_value='fixture\n'), \
                     patch.object(pipeline, 'benchmark_command', return_value=[sys.executable, str(probe)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if finished:
                        pipeline.run(args)
                    else:
                        with self.assertRaises(pipeline.Error):
                            pipeline.run(args)
                result = next((base / 'results').iterdir())
                self.assertIn('GPU-12345678', (result / 'bench.log').read_text())
                expected = 'completed' if finished else 'incomplete'
                self.assertEqual(json.loads((result / 'run.json').read_text())['state'], expected)
                self.assertEqual(json.loads((out / 'loaded.json').read_text())['run_state'], expected)

    def test_incomplete_workload_prevents_unload(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'swarmio/parameters').mkdir(parents=True)
            (base / 'swarmio/parameters/sq_diag_faulted').write_text('N')
            pipeline.save_json(base / 'loaded.json', {'run_state': 'incomplete'})
            with patch.object(pipeline, 'MODULES', base), patch.object(pipeline, 'linux_only'), \
                 patch.object(pipeline, 'check_no_workload'), patch.object(pipeline, 'dmesg_text', return_value=''), \
                 patch.object(pipeline, 'module_state', return_value={'swarmio': 'Live'}), \
                 patch.object(pipeline.subprocess, 'Popen') as execute:
                with self.assertRaises(pipeline.Error):
                    pipeline.unload(SimpleNamespace(build_dir=base))
                execute.assert_not_called()

    def test_unload_timeout_does_not_kill_kernel_task(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'swarmio/parameters').mkdir(parents=True)
            (base / 'swarmio/parameters/sq_diag_faulted').write_text('N')
            (base / 'swarmio/refcnt').write_text('0')
            (base / 'boot-id').write_text('fixture-boot')
            pipeline.save_json(base / 'loaded.json', {'run_state': 'completed', 'boot_id': 'fixture-boot'})
            process = Mock(pid=123)
            process.wait.side_effect = subprocess.TimeoutExpired(['rmmod', 'swarmio'], 15)

            def path(value):
                return base / 'boot-id' if str(value) == '/proc/sys/kernel/random/boot_id' else Path(value)

            with patch.object(pipeline, 'ROOT', base), patch.object(pipeline, 'MODULES', base), \
                 patch.object(pipeline, 'Path', side_effect=path), patch.object(pipeline, 'linux_only'), \
                 patch.object(pipeline, 'check_no_workload'), patch.object(pipeline, 'dmesg_text', return_value=''), \
                 patch.object(pipeline, 'module_state', return_value={'swarmio': 'Live'}), \
                 patch.object(pipeline, 'snapshot', return_value='fixture'), \
                 patch.object(pipeline.subprocess, 'Popen', return_value=process):
                with self.assertRaises(pipeline.Error):
                    pipeline.unload(SimpleNamespace(build_dir=base))
                process.kill.assert_not_called()
                process.terminate.assert_not_called()
            self.assertEqual(len(list((base / 'results').glob('unload-*.log'))), 1)


if __name__ == "__main__":
    unittest.main()
