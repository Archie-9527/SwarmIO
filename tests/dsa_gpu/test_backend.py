"""编译真实backend.c并注入DMA通道失败，验证不解绑设备与精确归还。
接口使用替身，不等价于真实Linux ABI/驱动测试。
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from test_kernel_syntax import MOCK

HERE=Path(__file__).resolve().parent
backend=(HERE/'backend.c').read_text()
for forbidden in ('device_release_driver','device_attach','idxd_drv_enable_wq',
                  'idxd_drv_disable_wq','idxd_device_reset','idxd_device_disable',
                  '__idxd_wq_quiesce'):
    assert not re.search(r'\b'+forbidden+r'\s*\(',backend), forbidden
kernel=(HERE/'kernel.c').read_text()
assert not re.search(r'->(?:group|size|rdbufs_allowed|rdbufs_reserved)\s*=(?!=)',kernel)

TEST=r'''
#include <assert.h>
#include "mock.h"
unsigned int dg_device_id;
int dsa_bus_type;
static struct idxd_device d;
static struct idxd_wq queues[8], *qp[8];
static struct idxd_group gs[4], *gp[4];
static struct idxd_engine es[4], *ep[4];
static struct idxd_dma_chan dc[8];
static struct device_driver parent_driver={"idxd"}, wq_driver={"dmaengine"};
static unsigned refs,acquired,released,attempt,fail_at;
struct device *bus_find_device_by_name(const int *bus,struct device *start,const char *name)
{
    (void)bus; (void)start;
    if (strcmp(name,"dsa0")) return NULL;
    refs++; return &d.idxd_dev.conf_dev;
}
void put_device(struct device *dev) { assert(dev==&d.idxd_dev.conf_dev && refs); refs--; }
struct dma_chan *dma_request_channel(dma_cap_mask_t mask,bool (*filter)(struct dma_chan *,void *),void *arg)
{
    struct idxd_wq *wq=arg; (void)mask;
    attempt++;
    if(attempt==fail_at) return NULL;
    assert(wq->client_count==0 && filter(&wq->idxd_chan->chan,arg));
    wq->client_count++; acquired++;
    return &wq->idxd_chan->chan;
}
void dma_release_channel(struct dma_chan *chan)
{
    for(unsigned i=0;i<8;i++) if(chan==&dc[i].chan) {
        assert(queues[i].client_count==1); queues[i].client_count--; released++; return;
    }
    assert(0);
}
#include "backend.c"
static void setup(void)
{
    memset(&d,0,sizeof(d)); memset(queues,0,sizeof(queues));
    refs=acquired=released=attempt=fail_at=0;
    d.idxd_dev.conf_dev.driver=&parent_driver;
    d.state=IDXD_DEV_ENABLED; d.max_wqs=8; d.max_engines=4;
    d.wqs=qp; d.groups=gp; d.engines=ep;
    for(unsigned i=0;i<4;i++) { gp[i]=&gs[i]; ep[i]=&es[i]; es[i].group=gp[i]; }
    for(unsigned i=0;i<8;i++) {
        struct idxd_wq *wq=&queues[i]; qp[i]=wq;
        wq->state=IDXD_WQ_ENABLED; wq->type=IDXD_WQT_KERNEL;
        wq->idxd_dev.conf_dev.driver=&wq_driver; strcpy(wq->name,"dmaengine");
        wq->group=gp[i/2]; wq->size=16; wq->max_batch_size=1024;
        wq->max_xfer_bytes=2147483648ULL; wq->idxd_chan=&dc[i];
        wq->portal=wq; wq->descs=wq;
    }
    unsigned int sizes[2]={16,16};
    struct dsa_cfg_params p={1,4,2,1,sizes,NULL};
    assert(!dsa_set_params(&p));
}
static void unchanged(void)
{
    assert(d.state==IDXD_DEV_ENABLED && d.idxd_dev.conf_dev.driver==&parent_driver);
    for(unsigned i=0;i<8;i++) {
        assert(queues[i].state==IDXD_WQ_ENABLED && queues[i].size==16);
        assert(queues[i].group==gp[i/2] && queues[i].idxd_dev.conf_dev.driver==&wq_driver);
    }
}
int main(void)
{
    for(unsigned run=0;run<3;run++) {
        setup(); assert(!dsa_enable_devices());
        assert(dsa_test_ready() && acquired==8 && refs==1);
        queues[0].state=IDXD_WQ_DISABLED; assert(!dsa_test_ready()); queues[0].state=IDXD_WQ_ENABLED;
        dsa_disable_devices(); assert(!dsa_test_ready() && released==8 && !refs);
        dsa_disable_devices(); assert(released==8); unchanged();
    }
    for(unsigned n=1;n<=8;n++) {
        setup(); fail_at=n;
        assert(dsa_enable_devices()==-EBUSY);
        assert(acquired==n-1 && released==n-1 && !refs && !dsa_test_ready());
        for(unsigned i=0;i<8;i++) assert(!queues[i].client_count);
        unchanged();
    }
    setup(); queues[3].client_count=1;
    assert(dsa_enable_devices()==-EBUSY && !attempt && !refs && queues[3].client_count==1);
    unchanged();
    setup(); queues[2].size=8;
    assert(dsa_enable_devices()==-EINVAL && !attempt && !refs);
}
'''
with tempfile.TemporaryDirectory(prefix='dg-borrow-backend-') as tmp:
    p=Path(tmp)
    (p/'linux').mkdir()
    (p/'mock.h').write_text(MOCK)
    for name in ('device.h','dmaengine.h'):
        (p/'linux'/name).write_text('#include "mock.h"\n')
    (p/'dsa.h').write_text('#include "mock.h"\n#define DSA_MAX_DEVS 4\n#define DSA_MAX_WQS 16\n')
    (p/'test.c').write_text(TEST)
    subprocess.run([shutil.which('cc'),'-std=gnu11','-Wall','-Wextra','-Werror',
                    '-I',str(p),'-I',str(HERE),str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
print('PASS: real backend exclusive borrowing, all partial-failure points, double cleanup, no parent/WQ lifecycle mutation')
