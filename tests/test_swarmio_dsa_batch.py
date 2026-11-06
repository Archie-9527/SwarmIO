"""Compile the real asynchronous batch engine and worker loop with fault injection."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from test_swarmio_dsa_c import compile_run
from test_swarmio_dsa import pipeline

ROOT = Path(__file__).resolve().parents[1]

class BatchTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'),'no C compiler')
    def test_real_transfer_wrapper_preserves_backpressure_through_worker_retry(self):
        source=(ROOT/'src/worker.c').read_text()
        issue=source[source.index('static inline int __dsa_dma_issue('):source.index('#include "swarmio_queue_diagnostics.h"')]
        transfer=source[source.index('static inline int __swarmio_submit_dma('):source.index('void swarmio_proc_io(struct swarmio_worker *worker, unsigned long *last_io_time)\n{')]
        start=source.index('void swarmio_proc_io(struct swarmio_worker *worker, unsigned long *last_io_time)\n{')
        worker=source[start:source.index('\n#ifndef CONFIG_SWARMIO_COMMIT\nvoid swarmio_proc_irqs',start)]
        pre=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
typedef unsigned long long u64,dma_addr_t;
typedef unsigned char u8;
#define CONFIG_SWARMIO_WORKER_BATCH_IO 1
#define PAGE_SIZE 4096UL
#define PAGE_SHIFT 12
#define PAGE_MASK (~(PAGE_SIZE-1))
#define PAGE_OFFSET_MASK (PAGE_SIZE-1)
#define PRP_PFN(p) ((p)>>12)
#define PTRS_PER_PRP_LIST 512
#define MEMREMAP_WT 0
#define BUG_ON(x) assert(!(x))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define unlikely(x) (x)
#define smp_load_acquire(p) (*(p))
#define smp_store_release(p,v) (*(p)=(v))
#define cpu_to_le16(x) (x)
#define NVME_SC_INTERNAL 6
enum { nvme_cmd_read=2,nvme_cmd_write=1,nvme_cmd_zone_append=0x7d };
struct nvme_rw_command { u8 opcode; unsigned short length; u64 slba; struct { u64 prp1,prp2; } dptr; };
struct swarmio_io_work {
    struct { struct nvme_rw_command rw; } cmd;
    struct { unsigned status; } cqe;
    struct { bool enabled; } bar_timing;
    bool is_internal,is_issued,is_copied,is_completed;
};
typedef struct { struct swarmio_io_work *pending[2]; unsigned count,attempts; int forced_error; } dma_ctx_t;
struct swarmio_worker_local_queue { struct swarmio_io_work *works; unsigned short head,tail,size; };
struct swarmio_worker { dma_ctx_t *dma_ctx; struct swarmio_worker_local_queue local_queue; u64 *paddr_list; };
static struct { struct { u64 storage_start; } config; } storage={.config={0x200000000ULL}},*vdev=&storage;
static bool worker_using_dma=true,complete;
static int dma_batch_size=2,num_dma_descs_per_worker=1;
static unsigned cqe_writes,errors;
static unsigned long jiffies;
static size_t __cmd_io_offset(struct nvme_rw_command *c) { return c->slba<<9; }
static size_t __cmd_io_size(struct nvme_rw_command *c) { return (c->length+1)*512; }
static bool pfn_valid(u64 p) { return false; }
static void *kmap_atomic_pfn(u64 p) { assert(0); return NULL; }
static void *memremap(u64 p,size_t n,int flags) { assert(0); return NULL; }
static void memunmap(void *p) { assert(0); }
static void kunmap_atomic(void *p) { assert(0); }
static void __swarmio_dma_cb(void *p) { assert(0 && "batch callback is deferred to poll"); }
static void __swarmio_mark_copied(struct swarmio_io_work *w) { assert(!w->is_copied); w->is_copied=true; }
static void __swarmio_batch_cb(void *p,int status) { assert(!status); __swarmio_mark_copied(p); }
static int dsa_dma_batch_enqueue(dma_ctx_t *c,u64 dst,u64 src,size_t n,void *p) {
    struct swarmio_io_work *w=p; c->attempts++;
    assert(src==storage.config.storage_start+(w->cmd.rw.slba<<9) && dst==w->cmd.rw.dptr.prp1 && n==512);
    if(c->forced_error) return c->forced_error;
    if(c->count==2) return -EAGAIN;
    c->pending[c->count++]=w; return 0;
}
static void dsa_dma_batch_poll(dma_ctx_t *c,void (*cb)(void *,int)) {
    if(!complete) return;
    for(unsigned i=0;i<c->count;i++) cb(c->pending[i],0);
    c->count=0;
}
static void dsa_dma_batch_flush_due(dma_ctx_t *c) {}
static bool swarmio_dsa_faulted(void) { return false; }
static bool swarmio_dsa_batch_idle(void) { return true; }
static void __swarmio_complete(struct swarmio_io_work *w) { assert(w->is_copied && !w->is_completed); w->is_completed=true; cqe_writes++; errors+=!!w->cqe.status; }
static int __swarmio_xfer_data(struct nvme_rw_command *cmd,void *timing) { assert(0 && "no CPU fallback"); return -EIO; }
'''
        checks=r'''
int main(void) {
    dma_ctx_t ctx={0}; struct swarmio_io_work works[4]={0}; u64 prps[512]; unsigned long stamp=0;
    struct swarmio_worker w={.dma_ctx=&ctx,.local_queue={works,0,3,4},.paddr_list=prps};
    for(unsigned i=0;i<3;i++) { works[i].cmd.rw.opcode=nvme_cmd_read; works[i].cmd.rw.slba=i; works[i].cmd.rw.dptr.prp1=0x10000+i*512; }
    swarmio_proc_io(&w,&stamp); assert(ctx.count==2 && !cqe_writes);
    swarmio_proc_io(&w,&stamp);
    assert(ctx.count==2 && ctx.attempts==3 && !works[2].is_issued && !works[2].is_copied && !works[2].cqe.status);
    assert(w.local_queue.head==0 && !cqe_writes && !errors);
    complete=true; swarmio_proc_io(&w,&stamp);
    assert(ctx.count==1 && works[2].is_issued && !works[2].is_copied && cqe_writes==2 && w.local_queue.head==2);
    swarmio_proc_io(&w,&stamp);
    assert(!ctx.count && cqe_writes==3 && !errors && w.local_queue.head==3);
    /* Actual I/O failures still produce error completions instead of retrying. */
    memset(works,0,sizeof(works)); w.local_queue.head=0; w.local_queue.tail=1; ctx.forced_error=-EIO;
    works[0].cmd.rw.opcode=nvme_cmd_read; works[0].cmd.rw.dptr.prp1=0x10000;
    swarmio_proc_io(&w,&stamp); assert(cqe_writes==4 && errors==1 && works[0].is_completed && !ctx.count);
    /* Unsupported geometry is rejected before accepting a partial request. */
    unsigned attempts=ctx.attempts; works[0].cmd.rw.length=15;
    assert(__swarmio_xfer_data_using_dma(&works[0].cmd.rw,&ctx,&works[0],prps)==-EINVAL && ctx.attempts==attempts);
    works[0].cmd.rw.length=0; works[0].cmd.rw.dptr.prp1=0x10f80;
    assert(__swarmio_xfer_data_using_dma(&works[0].cmd.rw,&ctx,&works[0],prps)==-EINVAL && ctx.attempts==attempts);
    return 0;
}
'''
        compile_run(pre+issue+transfer+worker+checks)

    @unittest.skipUnless(shutil.which('make'),'no make')
    def test_kbuild_selects_batch_object_only_for_async_profile(self):
        source='include '+str(ROOT/'Kbuild')+'\nreport:\n\t@echo $(swarmio-objs)\n'
        for worker,dispatcher,batch,ok in [('n','n','n',True),('y','n','n',True),
                                         ('y','n','y',True),('y','y','y',False)]:
            result=subprocess.run(['make','-s','-f','-','report','CONFIG_SWARMIO_DMA_WORKER='+worker,
                                   'CONFIG_SWARMIO_DMA_DISPATCHER='+dispatcher,'CONFIG_SWARMIO_WORKER_BATCH_IO='+batch],
                                  input=source,text=True,capture_output=True)
            self.assertEqual(result.returncode==0,ok,result.stderr)
            if ok:
                self.assertEqual('dsa/batch.o' in result.stdout,batch=='y')
                self.assertEqual('dsa/stub.o' in result.stdout,worker=='n')

    def test_run_requires_real_batch_retirement_and_records_mode(self):
        config=pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json')
        for retired in (2,1,0):
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp); build=base/'build'; build.mkdir()
                (build/'manifest.json').write_text('{}'); (build/'loaded.json').write_text('{"run_state":"not_started"}')
                child=base/'bench.py'; child.write_text('print("Verification: PASS\\nOps/sec: 1")\n')
                args=SimpleNamespace(build_dir=build,threads=64,reqs=1,queue_depth=None,num_queues=None,
                                     page_size=512,num_blks=131072,pages=4096,random=True,access=None,verify=True)
                manifest={'config':config,'environment':{'gpu':{'uuid':'GPU-fixture'}}}
                before=dict(submitted=0,completed=0,bytes=0,errors=0,timeouts=0,
                            batches_submitted=0,batches_retired=0,peak_batches=0,max_batch=0)
                after=dict(before,submitted=64,completed=64,bytes=32768,
                           batches_submitted=2,batches_retired=retired,peak_batches=2,max_batch=32)
                with patch.object(pipeline,'ROOT',base),patch.object(pipeline,'linux_only'), \
                     patch.object(pipeline,'verify_session',return_value=manifest), \
                     patch.object(pipeline,'check_no_workload'),patch.object(pipeline,'check_library'), \
                     patch.object(pipeline,'dmesg_text',return_value=''),patch.object(pipeline,'snapshot',return_value='fixture'), \
                     patch.object(pipeline,'read_dsa_state',side_effect=[before,after]), \
                     patch.object(pipeline,'benchmark_command',return_value=[sys.executable,str(child)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if retired==2: pipeline.run(args)
                    else:
                        with self.assertRaises(pipeline.Error): pipeline.run(args)
                record=json.loads(next((base/'results').glob('*/run.json')).read_text())
                self.assertEqual(record['payload_backend'],'dsa-borrowed-batch')
                self.assertEqual(record['dsa_submission']['dma_batch_size'],32)
                self.assertEqual(record['dsa_batches']['average_batch_size'],32)
                self.assertEqual(record['state'],'completed' if retired==2 else 'dsa_mismatch')

    def test_batch_configuration_and_build_overrides(self):
        c = pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json')
        self.assertTrue(pipeline.is_dsa_batch(c))
        self.assertEqual(c['module']['num_dma_descs_per_worker'],16)
        c = pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json',
                                 {'dma_batch_size':1024, 'num_dma_descs_per_worker':8, 'dma_batch_flush_us':10})
        self.assertEqual(c['module']['dma_batch_size'],1024)
        for change in ({'dma_batch_size':1},{'dma_batch_size':1025},
                       {'num_dma_descs_per_worker':0},{'num_dma_descs_per_worker':65},
                       {'dma_batch_flush_us':0}):
            with self.assertRaises(pipeline.Error):
                pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json',change)
        with self.assertRaises(pipeline.Error):
            pipeline.read_config(ROOT/'configs/bam-dsa-lab230.json',{'dma_batch_size':32})

    def test_batch_mode_and_runtime_dimensions_are_verified(self):
        import tempfile
        c = pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json')
        m = {'config':c,'environment':{'dsa':{'pci':'0000:75:01.0'}}}
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            for mod in ('swarmio','libnvm'): (base/mod/'parameters').mkdir(parents=True)
            p=base/'swarmio/parameters'
            state='version=2 device=0 borrowed=4 fault=0 submitted=64 completed=64 bytes=32768 errors=0 timeouts=0 mode=1 batches_submitted=2 batches_retired=2 active_batches=0 pending_batches=0 peak_batches=2 max_batch=32'
            (p/'dsa_state').write_text(state); (p/'worker_using_dma').write_text('Y')
            for k in ('dma_batch_size','num_dma_descs_per_worker','dma_batch_flush_us'): (p/k).write_text(str(c['module'][k]))
            (base/'libnvm/parameters/dma_peer_bdf').write_text('0000:75:01.0')
            with patch.object(pipeline,'MODULES',base):
                self.assertEqual(pipeline.read_dsa_state(m)['max_batch'],32)
                for old,new in [('mode=1','mode=0'),('active_batches=0','active_batches=1'),('pending_batches=0','pending_batches=1'),('version=2','version=1')]:
                    (p/'dsa_state').write_text(state.replace(old,new))
                    with self.assertRaises(pipeline.Error): pipeline.read_dsa_state(m)
                (p/'dsa_state').write_text(state); (p/'dma_batch_size').write_text('1024')
                with self.assertRaises(pipeline.Error): pipeline.read_dsa_state(m)

    @unittest.skipUnless(shutil.which('cc'),'no C compiler')
    def test_real_batch_and_worker_backpressure_failure_tail_and_teardown(self):
        header=(ROOT/'dsa/dsa.h').read_text()
        structs=header[header.index('struct dsa_batch_desc {'):header.index('static inline void __dsa_prep_hw_desc')]
        prep=header[header.index('static inline void __dsa_prep_hw_desc'):header.index('#ifdef CONFIG_SWARMIO_HAS_DSA',header.index('static inline void __dsa_prep_hw_desc'))]
        implementation=(ROOT/'dsa/batch.c').read_text()
        worker=(ROOT/'src/worker.c').read_text()
        worker=worker[worker.index('void swarmio_proc_io(struct swarmio_worker *worker, unsigned long *last_io_time)\n{'):worker.index('\n#ifndef CONFIG_SWARMIO_COMMIT\nvoid swarmio_proc_irqs',worker.index('void swarmio_proc_io(struct swarmio_worker *worker, unsigned long *last_io_time)\n{'))]
        pre=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef unsigned long long u64,dma_addr_t;
typedef unsigned int u32; typedef unsigned char u8;
#define DSA_MAX_DEVS 4
#define DSA_MAX_BATCH_SIZE 1024
#define DSA_MAX_NUM_DESCS_PER_THREAD 64
#define DSA_OPCODE_MEMMOVE 3
#define DSA_OPCODE_BATCH 1
#define DSA_OPCODE_DRAIN 2
#define DSA_OPCODE_NOOP 0
#define IDXD_OP_NONBLOCK 0
#define IDXD_OP_FLAG_CRAV 4
#define IDXD_OP_FLAG_RCR 8
#define DSA_COMP_NONE 0
#define DSA_COMP_SUCCESS 1
#define DSA_COMP_STATUS(x) ((x)&127)
#define GFP_KERNEL 0
#define IS_ERR(x) ((uintptr_t)(x)>(uintptr_t)-4096)
#define PTR_ERR(x) ((long)(x))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define module_param_named(...)
#define MODULE_PARM_DESC(...)
static void log_error(const char *format, ...) { (void)format; }
#define pr_err log_error
#define dma_wmb() ((void)0)
#define dma_rmb() ((void)0)
struct device { int unused; };
struct pci_dev { struct device dev; };
struct dsa_hw_desc { u32 flags,opcode,priv,pasid,xfer_size; u64 src_addr,dst_addr,completion_addr; } __attribute__((aligned(64)));
struct dsa_completion_record { u8 status; unsigned char rest[31]; };
struct idxd_desc { struct dsa_hw_desc *hw; struct dsa_completion_record *completion; dma_addr_t compl_dma; bool armed; };
struct idxd_wq { int size; u64 max_xfer_bytes; int max_batch_size; };
struct idxd_device { int max_wqs; struct idxd_wq **wqs; struct pci_dev *pdev; };
static struct idxd_device *dsa_idxd_list[4];
static u64 now;
static unsigned mem_calls,mem_fail,live,desc_calls,desc_fail,descs,submits,submit_fail;
static unsigned submitted,completed,bytes,batches,retired,active,peak,max_batch;
static unsigned errors,timeouts,callbacks,cqe_writes;
static bool fault;
static u64 ktime_get_ns(void) { return now; }
static void *allocate(size_t bytes) {
    if (++mem_calls==mem_fail) return NULL;
    void *p=aligned_alloc(64,(bytes+63)&~(size_t)63); assert(p); memset(p,0,(bytes+63)&~(size_t)63); live++; return p;
}
static void *fake_calloc(size_t n,size_t s) { return allocate(n*s); }
#define kcalloc(n,s,f) fake_calloc(n,s)
static void release(void *p) { if(p) { assert(live); live--; free(p); } }
#define kfree release
static void *dma_alloc_coherent(struct device *d,size_t n,dma_addr_t *a,int flags) {
    void *p=allocate(n); *a=(uintptr_t)p; return p;
}
static void dma_free_coherent(struct device *d,size_t n,void *p,dma_addr_t a) { assert((uintptr_t)p==a); release(p); }
static struct idxd_desc *idxd_alloc_desc(struct idxd_wq *q,int flags) {
    assert(flags==IDXD_OP_NONBLOCK);
    if(++desc_calls==desc_fail) return (void *)(intptr_t)-ENOMEM;
    struct idxd_desc *d=calloc(1,sizeof(*d));
    d->hw=aligned_alloc(64,sizeof(*d->hw)); memset(d->hw,0,sizeof(*d->hw));
    d->completion=calloc(1,sizeof(*d->completion)); d->compl_dma=(uintptr_t)d->completion;
    d->hw->pasid=99; descs++; return d;
}
static void idxd_free_desc(struct idxd_wq *q,struct idxd_desc *d) {
    assert(!d->armed || d->completion->status); descs--; free(d->hw); free(d->completion); free(d);
}
static int idxd_submit_desc(struct idxd_wq *q,struct idxd_desc *d) {
    assert(d->hw->pasid==99); assert(d->hw->completion_addr==d->compl_dma);
    if(++submits==submit_fail) return -EIO;
    d->armed=true; return 0;
}
static bool swarmio_dsa_faulted(void) { return fault; }
static void swarmio_dsa_error(bool t) { fault=true; errors++; timeouts+=t; }
static bool swarmio_dsa_batch_begin(void) { active++; if(!fault) return true; active--; return false; }
static void swarmio_dsa_batch_cancel(void) { assert(active); active--; }
static bool swarmio_dsa_batch_idle(void) { return !active; }
static void swarmio_dsa_batch_submit(unsigned n) {
    submitted+=n; batches++; if(active>peak) peak=active; if(n>max_batch) max_batch=n;
}
static void swarmio_dsa_batch_retire(unsigned n,u64 b) { completed+=n; bytes+=b; retired++; assert(active); active--; }
static void (*sleep_hook)(void);
static void usleep_range(unsigned a,unsigned b) { assert(sleep_hook && "normal hot path must never sleep"); sleep_hook(); }
struct dsa_dma_ctx_batch;
void dsa_dma_ctx_batch_remove(struct dsa_dma_ctx_batch *);
'''
        harness=r'''
struct request { unsigned done; int status; unsigned char src[512],dst[512]; };
static void callback(void *p,int status) {
    struct request *r=p; assert(!r->done); r->done++; r->status=status; callbacks++;
    if(!status) assert(!memcmp(r->src,r->dst,512));
}
static void finish(struct dsa_dma_ctx_batch *ctx,int slot,int child_error,int parent_error) {
    struct dsa_batch_desc *s=&ctx->batch_descs[slot];
    assert(s->state==B_ACTIVE);
    unsigned n=s->idxd_desc->hw->xfer_size;
    assert(n==(s->num_filled==1 ? 2U : (unsigned)s->num_filled));
    assert(s->idxd_desc->hw->src_addr==ctx->lv2_hw_desc_list_dma+slot*ctx->batch_size*sizeof(struct dsa_hw_desc));
    for(unsigned j=0;j<n;j++) {
        struct dsa_hw_desc *d=&s->lv2_hw_descs[j];
        assert(d->completion_addr==(uintptr_t)&s->lv2_compls[j]);
        if(d->opcode==DSA_OPCODE_MEMMOVE && (int)j!=child_error) memcpy((void *)(uintptr_t)d->dst_addr,(void *)(uintptr_t)d->src_addr,d->xfer_size);
        else if(d->opcode!=DSA_OPCODE_MEMMOVE) assert(d->opcode==DSA_OPCODE_NOOP);
        s->lv2_compls[j].status=(int)j==child_error ? 2 : 0x81;
    }
    s->idxd_desc->completion->status=parent_error ? 2 : 0x81;
}
static void reset(void) {
    assert(!live && !descs); now=0; mem_calls=mem_fail=desc_calls=desc_fail=submits=submit_fail=0;
    submitted=completed=bytes=batches=retired=active=peak=max_batch=errors=timeouts=callbacks=cqe_writes=0; fault=false; sleep_hook=NULL;
}
static struct dsa_dma_ctx_batch *drain_target;
static unsigned drain_sleeps;
static void finish_on_sleep(void) { assert(live && descs && drain_target->active); drain_sleeps++; finish(drain_target,0,-1,0); }
static int enqueue(struct dsa_dma_ctx_batch *ctx,struct request *r) {
    memset(r->src,0x5a,512); memset(r->dst,0xa5,512);
    return dsa_dma_batch_enqueue(ctx,(uintptr_t)r->dst,(uintptr_t)r->src,512,r);
}
'''
        worker_pre=r'''
#define CONFIG_SWARMIO_WORKER_BATCH_IO 1
#define smp_load_acquire(p) (*(p))
#define smp_store_release(p,v) (*(p)=(v))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define unlikely(x) (x)
#define cpu_to_le16(x) (x)
#define NVME_SC_INTERNAL 6
static unsigned long jiffies;
static bool worker_using_dma=true;
static int dma_batch_size=2,num_dma_descs_per_worker=2;
typedef struct dsa_dma_ctx_batch dma_ctx_t;
struct rw { struct request *request; };
struct swarmio_io_work {
    struct { struct rw rw; } cmd;
    struct { unsigned status; } cqe;
    struct { bool enabled; } bar_timing;
    bool is_internal,is_issued,is_copied,is_completed;
};
struct swarmio_worker_local_queue { struct swarmio_io_work *works; unsigned short head,tail,size; };
struct swarmio_worker { dma_ctx_t *dma_ctx; struct swarmio_worker_local_queue local_queue; u64 *paddr_list; };
static bool publish=true;
static void __swarmio_mark_copied(struct swarmio_io_work *w) { assert(!w->is_copied); w->is_copied=true; }
static void __swarmio_complete(struct swarmio_io_work *w) { assert(w->is_copied && !w->is_completed); if(publish) { w->is_completed=true; cqe_writes++; } }
static void __swarmio_batch_cb(void *p,int status) { struct swarmio_io_work *w=p; if(status) w->cqe.status=NVME_SC_INTERNAL<<1; __swarmio_mark_copied(w); }
static int __swarmio_xfer_data_using_dma(struct rw *c,dma_ctx_t *ctx,struct swarmio_io_work *w,u64 *list) {
    return dsa_dma_batch_enqueue(ctx,(uintptr_t)c->request->dst,(uintptr_t)c->request->src,512,w);
}
static int __swarmio_xfer_data(struct rw *cmd,void *timing) { assert(0 && "no CPU fallback"); return -EIO; }
'''
        checks=r'''
int main(void) {
    struct idxd_wq q={16,2147483648ULL,1024},*qs[]={&q}; struct pci_dev pci;
    struct idxd_device d={1,qs,&pci}; dsa_idxd_list[0]=&d;
    /* Every partial initialization failure releases exactly what was owned. */
    for(unsigned f=1;f<=4;f++) {
        reset(); mem_fail=f; struct dsa_dma_ctx_batch ctx={0};
        assert(dsa_dma_ctx_batch_init(&ctx,0,0,2,3,1)==-ENOMEM);
        dsa_dma_ctx_batch_remove(&ctx); assert(!live && !descs);
    }
    for(unsigned f=1;f<=3;f++) {
        reset(); desc_fail=f; struct dsa_dma_ctx_batch ctx={0};
        assert(dsa_dma_ctx_batch_init(&ctx,0,0,2,3,1)==-ENOMEM);
        dsa_dma_ctx_batch_remove(&ctx); assert(!live && !descs);
    }
    reset(); struct dsa_dma_ctx_batch c={0}; struct request r[8]={0};
    assert(dsa_dma_ctx_batch_init(&c,0,0,1,2,1)==-EINVAL);
    assert(dsa_dma_ctx_batch_init(&c,0,0,2,17,1)==-EINVAL);
    assert(!dsa_dma_ctx_batch_init(&c,0,0,2,2,1));
    for(unsigned i=0;i<4;i++) assert(!enqueue(&c,&r[i]));
    assert(submits==2 && c.active==2 && !callbacks);
    assert(enqueue(&c,&r[4])==-EAGAIN);
    dsa_dma_batch_poll(&c,callback); assert(!callbacks);
    finish(&c,1,-1,0); dsa_dma_batch_poll(&c,callback);
    assert(callbacks==2 && r[2].done && r[3].done && !r[0].done);
    assert(!enqueue(&c,&r[4])); now=4999; dsa_dma_batch_flush_due(&c); assert(submits==2);
    now=5000; dsa_dma_batch_flush_due(&c); assert(submits==3 && c.batch_descs[1].num_filled==1);
    finish(&c,0,-1,0); finish(&c,1,-1,0); dsa_dma_batch_poll(&c,callback);
    dsa_dma_batch_poll(&c,callback); assert(callbacks==5 && submitted==5 && completed==5 && bytes==2560 && peak==2);
    dsa_dma_ctx_batch_remove(&c); dsa_dma_ctx_batch_remove(&c);
    /* 1024 children are a single actual hardware submission. */
    reset(); memset(&c,0,sizeof(c)); assert(!dsa_dma_ctx_batch_init(&c,0,0,1024,2,1));
    struct request *many=calloc(1024,sizeof(*many));
    for(unsigned i=0;i<1024;i++) assert(!enqueue(&c,&many[i]));
    assert(submits==1 && submitted==1024 && max_batch==1024);
    finish(&c,0,-1,0); dsa_dma_batch_poll(&c,callback);
    assert(callbacks==1024 && completed==1024 && bytes==524288); free(many); dsa_dma_ctx_batch_remove(&c);
    /* Two service-unit contexts share one WQ, with disjoint child lists and
     * callbacks; saturating either owner must not steal the other's slots. */
    for(unsigned bs=64;bs<=128;bs*=2) {
        reset(); memset(&c,0,sizeof(c)); struct dsa_dma_ctx_batch other={0};
        assert(!dsa_dma_ctx_batch_init(&c,0,0,bs,8,1));
        assert(!dsa_dma_ctx_batch_init(&other,0,0,bs,8,1));
        unsigned cap=bs*8;
        struct request *load=calloc(2*cap+1,sizeof(*load)); assert(load);
        for(unsigned round=0;round<3;round++) {
            memset(load,0,(2*cap+1)*sizeof(*load));
            for(unsigned k=0;k<cap;k++) {
                assert(!enqueue(&c,&load[k])); assert(!enqueue(&other,&load[cap+k]));
            }
            assert(c.active==8 && other.active==8 && active==16);
            assert(enqueue(&c,&load[2*cap])==-EAGAIN && enqueue(&other,&load[2*cap])==-EAGAIN);
            for(int slot=7;slot>=0;slot--) finish(&other,slot,-1,0);
            dsa_dma_batch_poll(&other,callback);
            assert(c.active==8 && !other.active && !load[0].done && load[cap].done);
            for(int slot=7;slot>=0;slot--) finish(&c,slot,-1,0);
            dsa_dma_batch_poll(&c,callback);
            assert(!active && callbacks==(round+1)*2*cap && completed==submitted);
        }
        free(load); dsa_dma_ctx_batch_remove(&other); dsa_dma_ctx_batch_remove(&c);
        assert(!live && !descs);
    }
    /* Larger legal depth allocates every slot, including the final child. */
    reset(); memset(&c,0,sizeof(c)); q.size=64;
    assert(!dsa_dma_ctx_batch_init(&c,0,0,128,64,1));
    many=calloc(8193,sizeof(*many)); assert(many);
    for(unsigned k=0;k<8192;k++) assert(!enqueue(&c,&many[k]));
    assert(c.active==64 && enqueue(&c,&many[8192])==-EAGAIN);
    for(int slot=63;slot>=0;slot--) finish(&c,slot,-1,0);
    dsa_dma_batch_poll(&c,callback); assert(callbacks==8192 && completed==8192);
    memset(many,0,sizeof(*many)); assert(!enqueue(&c,many));
    now=5000; dsa_dma_batch_flush_due(&c); finish(&c,0,-1,0); dsa_dma_batch_poll(&c,callback);
    assert(callbacks==8193 && completed==submitted && !active);
    free(many); dsa_dma_ctx_batch_remove(&c); q.size=16;
    /* Submission rejection reports accepted requests once, without fallback. */
    reset(); memset(&c,0,sizeof(c)); memset(r,0,sizeof(r)); assert(!dsa_dma_ctx_batch_init(&c,0,0,2,2,1));
    submit_fail=2; for(unsigned i=0;i<4;i++) assert(!enqueue(&c,&r[i]));
    assert(fault && submitted==2 && enqueue(&c,&r[4])==-EIO);
    dsa_dma_batch_poll(&c,callback); assert(!callbacks && c.active==1);
    finish(&c,0,-1,0); dsa_dma_batch_poll(&c,callback); assert(callbacks==4 && completed==2 && r[2].status==-EIO);
    dsa_dma_ctx_batch_remove(&c);
    /* Parent/child errors require successful DRAIN before callbacks/reuse. */
    for(unsigned parent=0;parent<2;parent++) {
        reset(); memset(&c,0,sizeof(c)); memset(r,0,sizeof(r)); assert(!dsa_dma_ctx_batch_init(&c,0,0,2,2,1));
        assert(!enqueue(&c,&r[0]) && !enqueue(&c,&r[1])); finish(&c,0,parent ? -1 : 1,parent);
        dsa_dma_batch_poll(&c,callback); assert(fault && !callbacks && c.batch_descs[0].state==B_DRAINING && c.active==1);
        assert(c.batch_descs[0].idxd_desc->hw->opcode==DSA_OPCODE_DRAIN);
        c.batch_descs[0].idxd_desc->completion->status=1; dsa_dma_batch_poll(&c,callback);
        assert(callbacks==2 && r[0].status==-EIO && r[1].status==-EIO && !c.active); dsa_dma_ctx_batch_remove(&c);
    }
    /* Timeout is sticky, nonblocking, and never releases an in-flight slot. */
    reset(); memset(&c,0,sizeof(c)); memset(r,0,sizeof(r)); assert(!dsa_dma_ctx_batch_init(&c,0,0,2,1,1));
    assert(!enqueue(&c,&r[0])); now=5000; dsa_dma_batch_flush_due(&c);
    now+=1000001; dsa_dma_batch_poll(&c,callback); dsa_dma_batch_poll(&c,callback);
    assert(timeouts==1 && !callbacks && c.active==1 && enqueue(&c,&r[1])==-EIO);
    finish(&c,0,-1,0); dsa_dma_batch_poll(&c,callback); assert(callbacks==1 && r[0].status==-ETIMEDOUT);
    dsa_dma_ctx_batch_remove(&c);
    /* Real worker loop: backpressure does not consume or duplicate the fifth I/O. */
    reset(); memset(&c,0,sizeof(c)); memset(r,0,sizeof(r)); assert(!dsa_dma_ctx_batch_init(&c,0,0,2,2,1));
    struct swarmio_io_work works[8]={0}; struct swarmio_worker w={.dma_ctx=&c,.local_queue={works,0,5,8}};
    for(unsigned i=0;i<5;i++) { works[i].cmd.rw.request=&r[i]; memset(r[i].src,0x5a,512); }
    unsigned long stamp=0; swarmio_proc_io(&w,&stamp);
    assert(submitted==4 && !cqe_writes && !works[4].is_issued && w.local_queue.head==0);
    swarmio_proc_io(&w,&stamp); assert(submitted==4 && !cqe_writes);
    publish=false; finish(&c,0,-1,0); swarmio_proc_io(&w,&stamp);
    assert(works[4].is_issued && !works[4].is_copied && !cqe_writes && w.local_queue.head==0);
    now+=5000; publish=true; swarmio_proc_io(&w,&stamp); assert(submitted==5 && cqe_writes==2);
    finish(&c,0,-1,0); finish(&c,1,-1,0); swarmio_proc_io(&w,&stamp);
    assert(cqe_writes==5 && w.local_queue.head==5 && submitted==5 && completed==5);
    dsa_dma_ctx_batch_remove(&c); assert(!live && !descs);
    /* Teardown must retain active descriptors until real hardware completion. */
    reset(); memset(&c,0,sizeof(c)); memset(r,0,sizeof(r)); assert(!dsa_dma_ctx_batch_init(&c,0,0,2,1,1));
    assert(!enqueue(&c,&r[0]) && !enqueue(&c,&r[1]));
    drain_target=&c; sleep_hook=finish_on_sleep; dsa_dma_ctx_batch_remove(&c);
    assert(drain_sleeps==1 && !live && !descs && !active && completed==2 && !callbacks);
    /* DRAIN rejection quarantines active DMA resources: no premature callback/free. */
    reset(); memset(&c,0,sizeof(c)); memset(r,0,sizeof(r)); assert(!dsa_dma_ctx_batch_init(&c,0,0,2,1,1));
    assert(!enqueue(&c,&r[0]) && !enqueue(&c,&r[1])); finish(&c,0,1,1); submit_fail=submits+1;
    dsa_dma_batch_poll(&c,callback); assert(c.batch_descs[0].state==B_QUARANTINED && c.active==1 && !callbacks && live && descs);
    return 0;
}
'''
        compile_run(pre+structs+prep+implementation+harness+worker_pre+worker+checks,
                    ('module.h','kernel.h','dma-mapping.h','delay.h','ktime.h'))

if __name__=='__main__': unittest.main()
