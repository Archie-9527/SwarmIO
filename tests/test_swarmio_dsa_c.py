"""Execute real DSA descriptor and NVIDIA mapping ownership code with API doubles."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

def compile_run(source, headers=()):
    with tempfile.TemporaryDirectory() as d:
        p=Path(d); (p/'linux').mkdir()
        (p/'unit.c').write_text(source)
        for h in headers: (p/'linux'/h).write_text('')
        (p/'dsa.h').write_text('')
        subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                        '-Wno-sign-compare','-Wno-unused-variable','-fsanitize=undefined',
                        '-I',str(p),str(p/'unit.c'),'-o',str(p/'unit')],check=True)
        subprocess.run([str(p/'unit')],check=True,timeout=10)

class DSACTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'),'no C++ compiler')
    def test_benchmark_translation_and_actual_argument_parser(self):
        source=(ROOT/'benchmarks/bam_dsa_read.cu').read_text()
        runtime=r'''
#pragma once
#include <cstdint>
#include <cstddef>
#include <stdexcept>
#include <vector>
using error=std::runtime_error;
#define __global__
struct Dim { unsigned x; }; extern Dim blockIdx,blockDim,threadIdx;
using cudaError_t=int; using cudaEvent_t=void*;
constexpr int cudaSuccess=0,cudaMemcpyDeviceToHost=2;
int cudaSetDevice(int); int cudaMalloc(void**,size_t); int cudaFree(void*);
template<class T> int cudaMalloc(T** p,size_t n) { return cudaMalloc((void**)p,n); }
int cudaEventCreate(cudaEvent_t*); int cudaEventRecord(cudaEvent_t);
int cudaEventSynchronize(cudaEvent_t); int cudaEventDestroy(cudaEvent_t);
int cudaEventElapsedTime(float*,cudaEvent_t,cudaEvent_t); int cudaGetLastError();
int cudaMemcpy(void*,const void*,size_t,int); const char* cudaGetErrorString(int);
struct nvm_cmd_t {};
struct nvm_cpl_t { uint32_t dword[4]; };
struct nvm_queue_t { void *vaddr; uint64_t qs; };
struct QueuePair { nvm_queue_t sq,cq; unsigned nvmNamespace; };
struct Controller {
    uint64_t n_qps,blk_size; struct { uint64_t size; } ns; QueuePair *d_qps,**h_qps;
    Controller(const char*,int,int,uint64_t,uint64_t);
};
struct page_cache_d_t { uint64_t *prp1,*prp2; bool prps; Controller **d_ctrls; uint8_t *base_addr; };
struct page_cache_t { page_cache_d_t pdt,*d_pc_ptr; page_cache_t(uint64_t,uint64_t,int,Controller&,uint64_t,std::vector<Controller*>&); };
constexpr int NVM_IO_READ=2;
uint16_t get_cid(nvm_queue_t*); void put_cid(nvm_queue_t*,uint16_t);
void nvm_cmd_header(nvm_cmd_t*,uint16_t,int,unsigned);
void nvm_cmd_data_ptr(nvm_cmd_t*,uint64_t,uint64_t); void nvm_cmd_rw_blks(nvm_cmd_t*,uint64_t,uint64_t);
void sq_enqueue(nvm_queue_t*,nvm_cmd_t*); uint32_t cq_poll(nvm_queue_t*,uint16_t,uint32_t*,uint32_t*);
void cq_dequeue(nvm_queue_t*,uint32_t,nvm_queue_t*,uint32_t,uint32_t);
'''
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for name in ('cuda_runtime.h','ctrl.h','page_cache.h'): (p/name).write_text(runtime if name=='cuda_runtime.h' else '#include "cuda_runtime.h"\n')
            transformed=source.replace('read_kernel<<<c.threads/64,64>>>(', 'read_kernel(')
            transformed=transformed.replace('"../src/swarmio_pattern.h"','"'+str(ROOT/'src/swarmio_pattern.h')+'"')
            unit=p/'bench.cpp'; unit.write_text(transformed)
            subprocess.run(['c++','-std=c++17','-fsyntax-only','-I',str(p),str(unit)],check=True)
            parser=source[source.index('struct Config {'):source.index('__global__ void read_kernel')]
            unit.write_text('#include <cstdint>\n#include <string>\n#include <stdexcept>\nusing error=std::runtime_error;\n'+parser+
                            '\nint main(int n,char** a) { try {parse(n,a);return 0;} catch(const std::exception&) {return 1;} }')
            binary=p/'parse'
            subprocess.run(['c++','-std=c++17',str(unit),'-o',str(binary)],check=True)
            for args,ok in [([],True),(['--verify=true'],True),(['--verify=true','--reqs=2'],False),
                            (['--pages=1'],False),(['--access_type=1'],False),(['--threads=-64'],False),
                            (['--reqs=18446744073709551615'],False)]:
                with self.subTest(args=args):
                    self.assertEqual(subprocess.run([str(binary),*args]).returncode,0 if ok else 1)

    @unittest.skipUnless(shutil.which('cc'),'no C compiler')
    def test_single_partial_alloc_errors_timeout_and_no_reuse_before_completion(self):
        implementation=(ROOT/'dsa/single.c').read_text()
        header=(ROOT/'dsa/dsa.h').read_text()
        prep=header[header.index('static inline void __dsa_prep_hw_desc'):header.index('#ifdef CONFIG_SWARMIO_HAS_DSA',header.index('static inline void __dsa_prep_hw_desc'))]
        pre=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef unsigned long long u64,dma_addr_t;
typedef unsigned int u32;
typedef unsigned char u8;
#define DSA_MAX_DEVS 4
#define DSA_MAX_NUM_DESCS_PER_THREAD 64
#define DSA_OPCODE_MEMMOVE 3
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
#define time_after(a,b) ((long)((b)-(a))<0)
#define msecs_to_jiffies(x) (x)
#define unlikely(x) (x)
#define pr_err(...) ((void)0)
#define dma_wmb() ((void)0)
#define dma_rmb() ((void)0)
#define kcalloc(n,s,f) calloc(n,s)
#define kfree(p) free(p)
#define bitmap_zero(p,n) (*(p)=0)
#define test_bit(i,p) ((*(p)>>(i))&1)
#define set_bit(i,p) (*(p)|=1UL<<(i))
#define clear_bit(i,p) (*(p)&=~(1UL<<(i)))
#define for_each_set_bit(i,p,n) for((i)=0;(i)<(n);++(i)) if(test_bit(i,p))
static int find_first_zero_bit(unsigned long *p,int n) { for(int i=0;i<n;i++) if(!test_bit(i,p)) return i; return n; }
struct dsa_hw_desc { u32 flags,opcode,priv,pasid,xfer_size; u64 src_addr,dst_addr,completion_addr; };
struct completion { u8 status; };
struct idxd_desc { struct dsa_hw_desc *hw; struct completion *completion; u64 compl_dma; bool armed; };
struct idxd_wq { int size; u64 max_xfer_bytes; };
struct idxd_device { int max_wqs; struct idxd_wq **wqs; };
struct dsa_dma_ctx_single {
    struct idxd_desc **single_descs; struct idxd_wq *wq; void **data; bool sq_fetch;
    unsigned long issued_mask[1],last_wait_time;
    int num_descs,curr_desc,dev_id,wq_id,timeout;
};
static unsigned long jiffies;
static unsigned calls,allocs,frees,fail_at,mode,errors,timeouts,submitted,completed,bytes,callbacks;
static unsigned sq_pending,sq_submitted,sq_completed,sq_bytes;
static bool fault;
static struct idxd_desc *pending;
static void swarmio_dsa_error(bool t) { fault=true; errors++; timeouts+=t; }
static bool swarmio_dsa_faulted(void) { return fault; }
static void swarmio_dsa_submit(void) { submitted++; }
static void swarmio_dsa_complete(u64 n) { completed++; bytes+=n; }
static bool swarmio_dsa_sq_begin(void) { sq_pending++; if(!fault) return true; sq_pending--; return false; }
static void swarmio_dsa_sq_end(void) { assert(sq_pending); sq_pending--; }
static void swarmio_dsa_sq_submit(u64 n) { sq_submitted++; }
static void swarmio_dsa_sq_complete(u64 n) { sq_completed++; sq_bytes+=n; }
static struct idxd_desc *idxd_alloc_desc(struct idxd_wq *q,int type) {
    if(++calls==fail_at) return (void *)(intptr_t)-ENOMEM;
    struct idxd_desc *d=calloc(1,sizeof(*d)); d->hw=calloc(1,sizeof(*d->hw));
    d->completion=calloc(1,sizeof(*d->completion)); d->hw->pasid=99; allocs++; return d;
}
static void idxd_free_desc(struct idxd_wq *q,struct idxd_desc *d) {
    assert(!d->armed || d->completion->status); frees++;
    free(d->hw); free(d->completion); free(d);
}
static int idxd_submit_desc(struct idxd_wq *q,struct idxd_desc *d) {
    assert(d->hw->pasid==99);
    if(mode==1) return -EIO;
    d->armed=true; pending=d;
    d->completion->status=mode==2 ? 2 : mode==3 ? 0 : 0x81;
    return 0;
}
static void cpu_relax(void) { jiffies++; }
static void usleep_range(unsigned a,unsigned b) { if(++jiffies>=4) pending->completion->status=1; }
static void callback(void *p) { assert(p==(void *)42); callbacks++; }
void dsa_dma_ctx_single_remove(struct dsa_dma_ctx_single *);
int dsa_dma_wait_all_single(struct dsa_dma_ctx_single *,void (*)(void *));
'''
        checks=r'''
struct idxd_device *dsa_idxd_list[4];
int main(void) {
    struct idxd_wq q={16,65536},*qs[]={&q}; struct idxd_device d={1,qs}; dsa_idxd_list[0]=&d;
    for(unsigned f=1;f<=2;f++) {
        struct dsa_dma_ctx_single c={0}; fail_at=f; calls=allocs=frees=0;
        assert(dsa_dma_ctx_single_init(&c,0,0,2,1)==-ENOMEM);
        assert(allocs==f-1 && frees==allocs && !c.single_descs && !c.data);
        dsa_dma_ctx_single_remove(&c);
    }
    for(unsigned role=0;role<2;role++) for(unsigned m=0;m<4;m++) {
        struct dsa_dma_ctx_single c={0}; fail_at=0; mode=m;
        allocs=frees=calls=errors=timeouts=submitted=completed=bytes=callbacks=jiffies=0; fault=false;
        sq_pending=sq_submitted=sq_completed=sq_bytes=0;
        assert(!dsa_dma_ctx_single_init(&c,0,0,2,1));
        c.sq_fetch=role;
        int r=dsa_dma_issue_sync_single(&c,44,55,512,(void *)42,callback);
        assert(!c.issued_mask[0]);
        unsigned nsubmit=role ? sq_submitted : submitted,ncomplete=role ? sq_completed : completed;
        unsigned nbytes=role ? sq_bytes : bytes;
        assert(!sq_pending);
        if(role) assert(!submitted && !completed && !bytes);
        else assert(!sq_submitted && !sq_completed && !sq_bytes);
        if(m==0) assert(!r && callbacks==1 && ncomplete==1 && nbytes==512 && !fault);
        if(m==1) assert(r==-EIO && errors==1 && !nsubmit && !callbacks);
        if(m==2) assert(r==-EIO && errors==1 && nsubmit==1 && !ncomplete && !callbacks);
        if(m==3) assert(r==-ETIMEDOUT && timeouts==1 && ncomplete==1 && !callbacks && jiffies>=4);
        if(m) assert(dsa_dma_issue_sync_single(&c,44,55,512,NULL,NULL)==-EIO);
        dsa_dma_ctx_single_remove(&c); assert(allocs==frees);
    }
}
'''
        compile_run(pre+prep+implementation+checks,('kernel.h','pci.h','dma-mapping.h','delay.h'))

    @unittest.skipUnless(shutil.which('cc'),'no C compiler')
    def test_persistent_gpu_mapping_uses_dsa_peer_and_exact_handle_ownership(self):
        source=(ROOT/'bam/module/map.c').read_text()
        region=source[source.index('struct gpu_region {'):source.index('#endif',source.index('struct gpu_region {'))]
        funcs=source[source.index('static void release_gpu_memory'):source.index('#endif',source.index('static void release_gpu_memory'))]
        pre=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
typedef unsigned long long u64;
#define GPU_PAGE_SIZE 65536UL
#define GFP_KERNEL 0
#define THIS_MODULE ((void *)0)
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define pr_err(...) ((void)0)
#define kzalloc(n,f) calloc(1,n)
#define kfree(p) free(p)
#define PCI_DEVFN(s,f) (((s)<<3)|(f))
#define NVIDIA_P2P_PAGE_SIZE_64KB 1
#define NVIDIA_P2P_PAGE_TABLE_VERSION_COMPATIBLE(p) ((p)->version==1)
#define NVIDIA_P2P_DMA_MAPPING_VERSION_COMPATIBLE(p) ((p)->version==1)
typedef struct { long long n; } atomic64_t;
typedef struct { int n; } atomic_t;
#define ATOMIC64_INIT(x) {x}
#define ATOMIC_INIT(x) {x}
static long long atomic64_add_return(u64 n,atomic64_t *p) { return p->n+=n; }
static void atomic64_sub(u64 n,atomic64_t *p) { p->n-=n; }
static int atomic_read(atomic_t *p) { return p->n; }
static void atomic_set(atomic_t *p,int n) { p->n=n; }
struct pci_dev { int id; };
struct list {};
struct map { u64 vaddr; unsigned long n_addrs; void *data; struct pci_dev *pdev; void (*release)(struct map *); u64 addrs[4]; };
typedef struct { int version,page_size; unsigned entries; } nvidia_p2p_page_table_t;
typedef struct { int version,page_size_type; unsigned entries; u64 *dma_addresses; } nvidia_p2p_dma_mapping_t;
static struct pci_dev dsa={1},virt={2};
static unsigned refs,module_refs,put_calls,unmaps;
static bool fail_get,fail_map,fail_unmap,fail_put;
static nvidia_p2p_page_table_t pages={1,1,2};
static u64 addresses[]={0x100000,0x200000};
static nvidia_p2p_dma_mapping_t mapping={1,1,2,addresses};
static struct pci_dev *pci_get_domain_bus_and_slot(unsigned d,unsigned b,unsigned f) {
    assert(d==0 && b==0x75 && f==8); refs++; return &dsa;
}
static struct pci_dev *pci_dev_get(struct pci_dev *p) { refs++; return p; }
static void pci_dev_put(struct pci_dev *p) { assert(refs); refs--; }
static bool try_module_get(void *p) { module_refs++; return true; }
static void module_put(void *p) { assert(module_refs); module_refs--; }
static int nvidia_p2p_get_pages_persistent(u64 va,u64 bytes,nvidia_p2p_page_table_t **p,unsigned flags) {
    *p=fail_get ? (void *)1 : &pages; return fail_get ? -EINVAL : 0;
}
static int nvidia_p2p_put_pages_persistent(u64 va,nvidia_p2p_page_table_t *p,unsigned flags) {
    assert(p==&pages); put_calls++; return fail_put ? -EIO : 0;
}
static int nvidia_p2p_dma_map_pages(struct pci_dev *p,nvidia_p2p_page_table_t *pg,nvidia_p2p_dma_mapping_t **m) {
    assert(p==&dsa && pg==&pages); *m=fail_map ? (void *)1 : &mapping; return fail_map ? -EINVAL : 0;
}
static int nvidia_p2p_dma_unmap_pages(struct pci_dev *p,nvidia_p2p_page_table_t *pg,nvidia_p2p_dma_mapping_t *m) {
    assert(p==&dsa && pg==&pages && m==&mapping); unmaps++; return fail_unmap ? -EIO : 0;
}
'''
        checks=r'''
int main(void) {
    dma_peer_bdf="0000:75:01.0";
    struct map a={.vaddr=0x10000,.n_addrs=2,.pdev=&virt};
    assert(!map_gpu_memory(&a,NULL)); assert(a.addrs[1]==addresses[1] && refs==1 && module_refs==1);
    release_gpu_memory(&a); assert(!a.data && !refs && !module_refs && put_calls==1 && unmaps==1 && !gpu_pinned.n);
    fail_get=true;
    assert(map_gpu_memory(&a,NULL)==-EINVAL); release_gpu_memory(&a);
    assert(!refs && !module_refs && put_calls==1 && unmaps==1); fail_get=false;
    fail_map=true;
    assert(map_gpu_memory(&a,NULL)==-EINVAL); release_gpu_memory(&a);
    assert(!refs && !module_refs && put_calls==2 && unmaps==1 && !gpu_pinned.n); fail_map=false;
    assert(!map_gpu_memory(&a,NULL)); fail_unmap=true; release_gpu_memory(&a);
    assert(a.data && refs==1 && module_refs==1 && gpu_cleanup_fault.n);
    struct map b={.vaddr=0x20000,.n_addrs=2,.pdev=&virt}; assert(map_gpu_memory(&b,NULL)==-EIO);
    fail_unmap=false; release_gpu_memory(&a); assert(!a.data && !refs && !module_refs);
}
'''
        compile_run(pre+region+funcs+checks)

if __name__=='__main__': unittest.main()
