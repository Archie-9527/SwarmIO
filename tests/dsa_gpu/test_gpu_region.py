"""真实gpu_region.h失败注入：失败API返回非NULL毒指针也绝不误释放。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
from test_kernel_syntax import MOCK

HERE=Path(__file__).resolve().parent
C=r'''
#include <assert.h>
#include "mock.h"
static nvidia_p2p_page_table_t good_pages;
static nvidia_p2p_dma_mapping_t good_map;
static u64 addresses[4];
static int get_rc,map_rc,unmap_rc,put_rc,empty_pages,empty_map;
static unsigned gets,maps,unmaps,puts;
static char order[8];
static unsigned events;
int nvidia_p2p_get_pages_persistent(u64 va,u64 bytes,nvidia_p2p_page_table_t **p,u32 flags)
{
    (void)va;(void)bytes;(void)flags; gets++;
    *p=get_rc?(nvidia_p2p_page_table_t *)(uintptr_t)1:(empty_pages?NULL:&good_pages);
    return get_rc;
}
int nvidia_p2p_dma_map_pages(struct pci_dev *pdev,nvidia_p2p_page_table_t *p,nvidia_p2p_dma_mapping_t **m)
{
    (void)pdev; assert(p==&good_pages); maps++;
    *m=map_rc?(nvidia_p2p_dma_mapping_t *)(uintptr_t)1:(empty_map?NULL:&good_map);
    return map_rc;
}
int nvidia_p2p_dma_unmap_pages(struct pci_dev *pdev,nvidia_p2p_page_table_t *p,nvidia_p2p_dma_mapping_t *m)
{
    (void)pdev; assert(p==&good_pages && m==&good_map);
    unmaps++; order[events++]='U'; return unmap_rc;
}
int nvidia_p2p_put_pages_persistent(u64 va,nvidia_p2p_page_table_t *p,u32 flags)
{
    (void)va;(void)flags; assert(p==&good_pages);
    puts++; order[events++]='P'; return put_rc;
}
#include "gpu_region.h"
static void reset(void)
{
    get_rc=map_rc=unmap_rc=put_rc=empty_pages=empty_map=0;
    gets=maps=unmaps=puts=events=0; memset(order,0,sizeof(order));
    good_pages=(nvidia_p2p_page_table_t){1,NVIDIA_P2P_PAGE_SIZE_64KB,4};
    good_map=(nvidia_p2p_dma_mapping_t){1,NVIDIA_P2P_PAGE_SIZE_64KB,4,addresses};
}
int main(void)
{
    struct pci_dev dev={0};
    struct dg_region r={0};
    reset(); get_rc=-ENOMEM;
    assert(dg_region_pin(&dev,&r,65536,65536)==-ENOMEM);
    assert(!r.pages_owned && !r.map_owned && !r.pages && !r.map);
    assert(!dg_region_unpin(&dev,&r) && !puts && !unmaps && !maps);
    reset(); map_rc=-EIO;
    assert(dg_region_pin(&dev,&r,65536,65536)==-EIO);
    assert(r.pages_owned && !r.map_owned && !r.map);
    assert(!dg_region_unpin(&dev,&r) && puts==1 && !unmaps);
    reset(); good_pages.version=0;
    assert(dg_region_pin(&dev,&r,65536,65536)==-EOPNOTSUPP);
    assert(!dg_region_unpin(&dev,&r) && puts==1 && !maps);
    reset(); good_pages.page_size=0;
    assert(dg_region_pin(&dev,&r,65536,65536)==-EOPNOTSUPP);
    assert(!dg_region_unpin(&dev,&r) && puts==1 && !maps);
    for(unsigned which=0;which<3;which++) {
        reset();
        if(which==0)good_map.version=0;
        if(which==1)good_map.entries=0;
        if(which==2)good_map.dma_addresses=NULL;
        assert(dg_region_pin(&dev,&r,65536,65536)==-EOPNOTSUPP);
        assert(!dg_region_unpin(&dev,&r));
        assert(puts==1 && unmaps==1 && order[0]=='U' && order[1]=='P');
    }
    reset(); empty_pages=1;
    assert(dg_region_pin(&dev,&r,65536,65536)==-EIO);
    assert(!dg_region_unpin(&dev,&r) && !puts && !unmaps);
    reset(); empty_map=1;
    assert(dg_region_pin(&dev,&r,65536,65536)==-EIO);
    assert(!dg_region_unpin(&dev,&r) && puts==1 && !unmaps);
    reset(); assert(!dg_region_pin(&dev,&r,65536,65536));
    assert(dg_region_pin(&dev,&r,65536,65536)==-EBUSY && gets==1);
    assert(!dg_region_unpin(&dev,&r) && unmaps==1 && puts==1);
    assert(!dg_region_unpin(&dev,&r) && unmaps==1 && puts==1);
    reset(); assert(!dg_region_pin(&dev,&r,65536,65536)); unmap_rc=-EIO;
    assert(dg_region_unpin(&dev,&r)==-EIO);
    assert(r.pages_owned && r.map_owned && !puts);
    /* 模拟上层隔离该对象，绝不盲目继续put或重试失败API。 */
    memset(&r,0,sizeof(r));
    reset(); assert(!dg_region_pin(&dev,&r,65536,65536)); put_rc=-EIO;
    assert(dg_region_unpin(&dev,&r)==-EIO);
    assert(r.pages_owned && !r.map_owned && puts==1 && unmaps==1);
    memset(&r,0,sizeof(r));
    reset();
    struct dg_region data={0},sq={0};
    assert(!dg_region_pin(&dev,&data,65536,65536));
    get_rc=-ENOMEM;
    assert(dg_region_pin(&dev,&sq,131072,65536)==-ENOMEM);
    assert(!dg_region_unpin(&dev,&sq) && !puts && !unmaps);
    assert(!dg_region_unpin(&dev,&data) && puts==1 && unmaps==1);
}
'''
with tempfile.TemporaryDirectory(prefix='dg-gpu-region-') as tmp:
    p=Path(tmp)
    (p/'linux').mkdir()
    (p/'mock.h').write_text(MOCK)
    for name in ('errno.h','string.h'):
        (p/'linux'/name).write_text('#include "mock.h"\n')
    (p/'nv-p2p.h').write_text('#include "mock.h"\n')
    (p/'test.c').write_text(C)
    subprocess.run([shutil.which('cc'),'-std=gnu11','-Wall','-Wextra','-Werror',
                    '-I',str(p),'-I',str(HERE),str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
print('PASS: real GPU region helper failure/poison-pointer injection, ownership, cleanup order, double cleanup, data/SQ partial failure')
