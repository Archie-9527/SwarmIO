"""Exercise actual libnvm host DMA allocation/rollback code with UBSAN."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class DMAMappingTests(unittest.TestCase):
    def test_dynamic_array_partial_pin_and_dma_failure(self):
        if not shutil.which('cc'):
            self.skipTest('no C compiler')
        for engine in ('bam', 'copilot-io'):
            module = ROOT / 'copilotio' / engine / 'module'
            header = re.sub(r'(?m)^#include[^\n]*\n', '', (module/'map.h').read_text())
            source = (module/'map.c').read_text()
            create = source[source.index('static struct map* create_descriptor('):source.index('void unmap_and_release(')]
            release = source[source.index('static void release_user_pages('):source.index('struct map* map_userspace(')]
            prefix = r'''
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <assert.h>
#include <errno.h>
#include <limits.h>
typedef uint64_t u64;
struct list_node { int unused; };
struct task_struct { int unused; };
struct device { int unused; };
struct pci_dev { struct device dev; };
struct ctrl { struct pci_dev* pdev; };
struct page { unsigned id; };
static struct task_struct* current;
static void list_node_init(struct list_node* p) { p->unused=0; }
#define GFP_KERNEL 0
#define KERN_CRIT ""
#define KERN_ERR ""
#define KERN_WARNING ""
#define printk(...) ((void)0)
#define ERR_PTR(v) ((void*)(intptr_t)(v))
#define PAGE_SIZE 4096
#define FOLL_WRITE 1
#define DMA_BIDIRECTIONAL 0
#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define LINUX_VERSION_CODE KERNEL_VERSION(6,18,5)
static size_t total_size(size_t base,size_t element,size_t n) {
    return n>(SIZE_MAX-base)/element ? SIZE_MAX : base+n*element;
}
#define struct_size(p,m,n) total_size(sizeof(*(p)),sizeof((p)->m[0]),(n))
static void* alloc(size_t n) { return n==SIZE_MAX ? NULL : malloc(n); }
#define kvmalloc(n,f) alloc(n)
#define kvcalloc(n,s,f) calloc(n,s)
#define kvfree(p) free(p)
static int pins=3,put_count=0,dma_count=0,unmap_count=0,fail_dma_at=0;
static struct page pages[3]={{0},{1},{2}};
static long get_user_pages(u64 address,unsigned long n,int flags,struct page** out) {
    (void)address;(void)n;(void)flags;
    for(int i=0;i<pins;i++) out[i]=&pages[i];
    return pins;
}
static void put_page(struct page* p) { (void)p; ++put_count; }
static u64 dma_map_page(struct device* d,struct page* p,int off,int size,int dir) {
    (void)d;(void)off;(void)size;(void)dir; ++dma_count;
    return (fail_dma_at==dma_count) ? 0 : (0x10000+p->id*4096);
}
static int dma_mapping_error(struct device* d,u64 address) { (void)d; return address==0; }
static void dma_unmap_page(struct device* d,u64 address,int size,int dir) {
    (void)d;(void)size;(void)dir; assert(address); ++unmap_count;
}
'''
            checks = r'''
int main(void) {
    struct pci_dev pci={0}; struct ctrl ctrl={&pci};
    struct map* m=create_descriptor(&ctrl,0x1000,3);
    assert(m && m->n_addrs==3 && m->n_dma_addrs==0);
    m->addrs[2]=123; assert(m->addrs[2]==123); /* UBSAN must accept the flexible member. */
    assert(map_user_pages(m)==0 && m->n_dma_addrs==3);
    release_user_pages(m); free(m);
    assert(put_count==3 && unmap_count==3);
    assert(create_descriptor(&ctrl,0x1000,ULONG_MAX)==ERR_PTR(-ENOMEM));
    pins=1; put_count=0; dma_count=0; unmap_count=0;
    m=create_descriptor(&ctrl,0x1000,3);
    assert(map_user_pages(m)==-ENOMEM);
    assert(m->data==NULL && put_count==1 && dma_count==0); free(m);
    pins=3; put_count=0; fail_dma_at=2;
    m=create_descriptor(&ctrl,0x1000,3);
    assert(map_user_pages(m)==-EIO && m->n_dma_addrs==1);
    release_user_pages(m); free(m);
    assert(put_count==3 && unmap_count==1);
}
'''
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as d:
                unit, binary = Path(d)/'mapping.c', Path(d)/'mapping'
                for version in ('6,8,0', '6,18,5'):
                    with self.subTest(kernel=version):
                        version_prefix=prefix.replace('KERNEL_VERSION(6,18,5)', 'KERNEL_VERSION('+version+')')
                        unit.write_text(version_prefix+header+create+release+checks)
                        subprocess.run(['cc', '-std=c11', '-fsanitize=undefined', '-fno-sanitize-recover=all',
                                        str(unit), '-o', str(binary)], check=True)
                        subprocess.run([str(binary)], check=True, timeout=10)
