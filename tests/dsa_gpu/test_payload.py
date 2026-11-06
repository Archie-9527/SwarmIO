"""Run actual payload/replay C loops and descriptor banks with delayed fake DMA."""
from pathlib import Path
import subprocess
import tempfile
import shutil

HERE=Path(__file__).resolve().parent
source=(HERE/'kernel.c').read_text()
structs=source[source.index('enum dg_state'):source.index('static void fail(')]
state=source[source.index('static void fail('):source.index('static void unpin(')]
helpers=source[source.index('static dma_addr_t gpu_addr('):source.index('static void fetch_loop(')]
loop=source[source.index('static void payload_loop('):source.index('static int thread_main(')]
allocation=source[source.index('static int alloc_group('):source.index('static long run_test(')]
pre=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
typedef unsigned long long u64,dma_addr_t;
typedef unsigned int u32; typedef unsigned char u8;
#include "uapi.h"
#define DG_TIMEOUT_NS 5000000000ULL
#define ____cacheline_aligned_in_smp
#define GFP_KERNEL 0
#define IDXD_OP_NONBLOCK 0
#define IDXD_OP_FLAG_CRAV 4
#define IDXD_OP_FLAG_RCR 8
#define DSA_OPCODE_NOOP 0
#define DSA_OPCODE_BATCH 1
#define DSA_OPCODE_DRAIN 2
#define DSA_OPCODE_MEMMOVE 3
#define DSA_COMP_NONE 0
#define DSA_COMP_SUCCESS 1
#define DSA_COMP_STATUS(x) ((x)&127)
#define IS_ERR(x) ((uintptr_t)(x)>(uintptr_t)-4096)
#define PTR_ERR(x) ((long)(x))
#define U64_MAX (~0ULL)
#define HZ 1000
#define time_after(a,b) ((long)((b)-(a))<0)
#define max(a,b) ((a)>(b)?(a):(b))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define is_power_of_2(x) ((x)&&!((x)&((x)-1)))
#define WRITE_ONCE(x,v) ((x)=(v))
#define READ_ONCE(x) fake_read(&(x))
#define dma_wmb() ((void)0)
#define dma_rmb() ((void)0)
#define pr_err(...) ((void)0)
#define pr_emerg(...) assert(0 && "unexpected permanent DMA failure")
struct device { int unused; };
struct pci_dev { struct device dev; };
struct completion { int unused; };
struct task_struct { int unused; };
typedef struct { int value; } atomic_t;
static int atomic_read(atomic_t *a) { return a->value; }
static int atomic_cmpxchg(atomic_t *a,int from,int to) { int old=a->value; if(old==from) a->value=to; return old; }
struct dsa_hw_desc { u32 opcode,flags,pasid; u64 src_addr,dst_addr,completion_addr; u32 xfer_size; } __attribute__((aligned(64)));
struct dsa_completion_record { u8 status; unsigned char pad[31]; };
struct idxd_wq { unsigned id; };
struct idxd_desc { struct dsa_hw_desc *hw; struct dsa_completion_record *completion; dma_addr_t compl_dma; struct idxd_wq *wq; bool pending; u64 due; };
struct idxd_device { struct idxd_wq **wqs; };
static struct idxd_device *dsa_idxd_list[1];
struct dma_map { u64 *dma_addresses; };
struct dg_region { struct dma_map *map; };
static unsigned groups=1;
static struct dg_info info={.max_batch=1024,.max_depth=16,.max_transfer=65536,.max_pin_bytes=32ULL<<20};
static u64 now;
static unsigned long jiffies;
static unsigned live,allocations,fail_at,descs,issues,sq_issues;
static void *allocate(size_t n) {
    if(++allocations==fail_at) return NULL;
    void *p=aligned_alloc(64,(n+63)&~(size_t)63); assert(p); memset(p,0,(n+63)&~(size_t)63); live++; return p;
}
static void release(void *p) { if(p) { assert(live); live--; free(p); } }
#define kcalloc(n,s,f) allocate((n)*(s))
#define kvcalloc(n,s,f) allocate((n)*(s))
#define kfree release
#define kvfree release
static void *dma_alloc_coherent(struct device *d,size_t n,dma_addr_t *a,int f) { void *p=allocate(n); *a=(uintptr_t)p; return p; }
static void dma_free_coherent(struct device *d,size_t n,void *p,dma_addr_t a) { assert(a==(uintptr_t)p); release(p); }
#include "host_pages.h"
static struct idxd_desc *all[32];
static struct idxd_desc *idxd_alloc_desc(struct idxd_wq *w,int flags) {
    struct idxd_desc *d=calloc(1,sizeof(*d)); assert(d);
    d->hw=aligned_alloc(64,sizeof(*d->hw)); memset(d->hw,0,sizeof(*d->hw)); d->hw->pasid=99;
    d->completion=calloc(1,sizeof(*d->completion)); d->compl_dma=(uintptr_t)d->completion; d->wq=w;
    for(unsigned i=0;i<32;i++) if(!all[i]) { all[i]=d; descs++; return d; }
    assert(0); return NULL;
}
static void idxd_free_desc(struct idxd_wq *w,struct idxd_desc *d) {
    assert(!d->pending);
    for(unsigned i=0;i<32;i++) if(all[i]==d) { all[i]=NULL; descs--; free(d->hw); free(d->completion); free(d); return; }
    assert(0);
}
static void execute(struct dsa_hw_desc *h) {
    if(h->opcode==DSA_OPCODE_MEMMOVE) memcpy((void *)(uintptr_t)h->dst_addr,(void *)(uintptr_t)h->src_addr,h->xfer_size);
    else assert(h->opcode==DSA_OPCODE_NOOP);
    ((struct dsa_completion_record *)(uintptr_t)h->completion_addr)->status=1;
}
static u8 fake_read(const u8 *p) {
    now+=100;
    for(unsigned i=0;i<32;i++) if(all[i] && all[i]->pending && now>=all[i]->due) {
        struct idxd_desc *d=all[i];
        if(d->hw->opcode==DSA_OPCODE_BATCH) {
            struct dsa_hw_desc *children=(void *)(uintptr_t)d->hw->src_addr;
            for(unsigned j=0;j<d->hw->xfer_size;j++) execute(children+j);
        } else if(d->hw->opcode!=DSA_OPCODE_DRAIN) execute(d->hw);
        d->completion->status=1; d->pending=false;
    }
    return *p;
}
static int idxd_submit_desc(struct idxd_wq *w,struct idxd_desc *d) {
    assert(!d->pending && d->hw->pasid==99); d->pending=true; d->due=now+1000;
    issues++; if(!(w->id%2)) sq_issues++; return 0;
}
static u64 ktime_get_ns(void) { now+=100; return now; }
static void cpu_relax(void) {}
static int cond_resched(void) { return 0; }
static void usleep_range(unsigned a,unsigned b) { now+=10000; }
static void schedule_timeout_uninterruptible(unsigned n) { assert(0); }
'''
checks=r'''
int main(void) {
    struct idxd_wq qs[2]={{0},{1}},*qp[]={&qs[0],&qs[1]};
    struct idxd_device idxd={qp}; dsa_idxd_list[0]=&idxd;
    for(unsigned path=DG_PAYLOAD;path<=DG_REPLAY;path++)
    for(unsigned size=512;size<=65536;size*=2)
    for(unsigned dir=0;dir<=2;dir++)
    for(unsigned mode=0;mode<=1;mode++) {
        if(path==DG_REPLAY && !mode) continue;
        struct dg_run r={.abi=DG_ABI,.path=path,.mode=1,.block_bytes=size,.data_bytes=1<<20,
            .batch_size=size<4096 ? 32 : 2,.depth=4,.direction=dir,.seed=12345,.gpu_data=65536};
        r.mode=mode; if(!mode) r.depth=1;
        u64 blocks=r.data_bytes/size;
        r.operations=3*blocks+r.batch_size;
        if(path==DG_PAYLOAD) r.operations++; /* exercise dynamic partial tail */
        assert(!validate_run(&r));
        struct device dev={0}; struct dg_test t={.r=&r,.dev=&dev,.batch=mode ? r.batch_size : 1,
            .host_chunk=max(size,4096),.blocks_per_group=blocks};
        struct dg_group g={.test=&t,.limit=r.operations};
        struct dma_map mapping; u64 addresses[16]; mapping.dma_addresses=addresses; t.data.map=&mapping;
        for(unsigned i=0;i<16;i++) addresses[i]=(uintptr_t)allocate(65536);
        t.host=dg_host_alloc_chunks(&dev,r.data_bytes,t.host_chunk); assert(t.host);
        for(u64 i=0;i<r.data_bytes/8;i++) {
            u64 b=i*8/size; bool write=dg_write(b,dir);
            *dg_host_word_chunk(t.host,i,t.host_chunk)=dg_pattern(i)^(write ? U64_MAX : 0);
            *(u64 *)(uintptr_t)(addresses[i*8/65536]+i*8%65536)=dg_pattern(i)^(write ? 0 : U64_MAX);
        }
        assert(!alloc_group(&g));
        struct dsa_hw_desc first;
        if(path==DG_REPLAY) first=g.banks[0].children[0];
        unsigned sq_before=sq_issues; payload_loop(&g);
        assert(!t.error.value && g.cursor==r.operations && r.stats[0].executed==r.operations);
        assert(r.stats[0].measured==r.operations && sq_issues==sq_before);
        if(path==DG_REPLAY) assert(!memcmp(&first,&g.banks[0].children[0],sizeof(first)));
        g.started=true; free_group(&g);
        assert(!descs);
        for(u64 i=0;i<r.data_bytes/8;i++) {
            assert(*dg_host_word_chunk(t.host,i,t.host_chunk)==dg_pattern(i));
            assert(*(u64 *)(uintptr_t)(addresses[i*8/65536]+i*8%65536)==dg_pattern(i));
        }
        dg_host_free_chunks(&dev,t.host,r.data_bytes,t.host_chunk);
        for(unsigned i=0;i<16;i++) release((void *)(uintptr_t)addresses[i]);
        assert(!live);
    }
    /* Every partial bank allocation frees exactly its owned allocations. */
    {
        struct dg_run r={.abi=DG_ABI,.path=DG_REPLAY,.mode=1,.block_bytes=65536,.data_bytes=1<<20,
            .batch_size=2,.depth=4,.operations=16,.gpu_data=65536};
        struct device dev={0}; struct dg_test t={.r=&r,.dev=&dev,.batch=2,.host_chunk=65536,.blocks_per_group=16};
        struct dma_map mapping; u64 addresses[16]; mapping.dma_addresses=addresses; t.data.map=&mapping;
        for(unsigned i=0;i<16;i++) addresses[i]=(uintptr_t)allocate(65536);
        t.host=dg_host_alloc_chunks(&dev,r.data_bytes,t.host_chunk); assert(t.host);
        unsigned baseline=live;
        for(unsigned f=1;f<=18;f++) {
            struct dg_group g={.test=&t}; allocations=0; fail_at=f;
            assert(alloc_group(&g)==-ENOMEM); free_group(&g);
            assert(live==baseline && !descs);
        }
        fail_at=0; dg_host_free_chunks(&dev,t.host,r.data_bytes,t.host_chunk);
        for(unsigned i=0;i<16;i++) release((void *)(uintptr_t)addresses[i]);
        assert(!live);
    }
    /* Reject incompatible ABI, big pipeline copies and replay partial batches. */
    struct dg_run r={.abi=DG_ABI,.path=DG_REPLAY,.mode=1,.block_bytes=65536,.data_bytes=1<<20,
        .batch_size=2,.depth=4,.operations=17,.gpu_data=65536};
    assert(validate_run(&r)==-EINVAL); r.operations=16; assert(!validate_run(&r));
    r.path=DG_PIPELINE; assert(validate_run(&r)==-EINVAL);
    r.path=DG_REPLAY; r.abi=3; assert(validate_run(&r)==-EINVAL);
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='dg-payload-') as tmp:
    p=Path(tmp); (p/'linux').mkdir()
    for h in ('dma-mapping.h','slab.h','vmalloc.h','ioctl.h'): (p/'linux'/h).write_text('')
    (p/'linux/types.h').write_text('typedef unsigned long long __u64; typedef unsigned int __u32;\n')
    (p/'check.c').write_text(pre+structs+state+helpers+loop+allocation+checks)
    subprocess.run([shutil.which('cc'),'-std=gnu11','-O1','-Wall','-Wextra','-Werror',
                    '-Wno-unused-parameter','-Wno-unused-function','-Wno-sign-compare',
                    '-fsanitize=undefined','-fno-sanitize-recover=all','-I',str(p),'-I',str(HERE),
                    str(p/'check.c'),'-o',str(p/'check')],check=True)
    subprocess.run([str(p/'check')],check=True,timeout=30)
print('PASS: actual payload/replay C loops, full working-set coverage, 512B..64KiB, read/write/mixed, tails, no SQ DMA, descriptor reuse, cleanup and ABI checks')
