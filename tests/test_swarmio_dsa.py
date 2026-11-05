"""DSA protocol/ownership regression; real Linux/CUDA execution is still required."""
import ast
import contextlib
import importlib.util
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
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('dsa_pipeline',ROOT/'scripts/swarmio_bam.py')
pipeline=importlib.util.module_from_spec(spec); spec.loader.exec_module(pipeline)
MOCK=next(ast.literal_eval(n.value) for n in ast.parse((ROOT/'tests/dsa_gpu/test_kernel_syntax.py').read_text()).body
          if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='MOCK' for t in n.targets))

class DSATests(unittest.TestCase):
    def test_config_rejects_fallback_and_unsafe_verification(self):
        c=pipeline.read_config(ROOT/'configs/bam-dsa-lab230.json')
        self.assertTrue(pipeline.is_dsa(c))
        for scope,key,value in [('module','worker_using_dma',0),('module','disp_using_dma',1),
                                ('build','CONFIG_SWARMIO_WORKER_SKIP_IO','y'),
                                ('build','CONFIG_SWARMIO_WORKER_BATCH_IO','y'),('module','dsa_num_devs',2)]:
            bad=json.loads(json.dumps(c)); bad[scope][key]=value
            with tempfile.TemporaryDirectory() as d:
                p=Path(d)/'config.json'; p.write_text(json.dumps(bad))
                with self.assertRaises(pipeline.Error): pipeline.read_config(p)
        b=dict(c['benchmark'],verify=True)
        pipeline.validate_benchmark(b,c)
        for change in ({'reqs':2},{'num_blks':131073},{'pages':1},{'access_type':1}):
            with self.assertRaises(pipeline.Error): pipeline.validate_benchmark(dict(b,**change),c)

    def test_runtime_peer_and_completion_state(self):
        c=pipeline.read_config(ROOT/'configs/bam-dsa-lab230.json')
        m={'config':c,'environment':{'dsa':{'pci':'0000:75:01.0'}}}
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            for mod in ('swarmio','libnvm'): (base/mod/'parameters').mkdir(parents=True)
            state=base/'swarmio/parameters/dsa_state'
            state.write_text('version=1 device=0 borrowed=4 fault=0 submitted=64 completed=64 bytes=32768 errors=0 timeouts=0')
            (base/'swarmio/parameters/worker_using_dma').write_text('Y')
            peer=base/'libnvm/parameters/dma_peer_bdf'; peer.write_text('0000:75:01.0')
            with patch.object(pipeline,'MODULES',base):
                self.assertEqual(pipeline.read_dsa_state(m)['bytes'],32768)
                peer.write_text('0000:f2:01.0')
                with self.assertRaises(pipeline.Error): pipeline.read_dsa_state(m)
                peer.write_text('0000:75:01.0'); state.write_text('device=0 borrowed=4 fault=1')
                with self.assertRaises(pipeline.Error): pipeline.read_dsa_state(m)

    def test_run_rejects_successful_log_with_missing_actual_dma(self):
        config=pipeline.read_config(ROOT/'configs/bam-dsa-lab230.json')
        for actual in (64,63):
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp); build=base/'build'; build.mkdir()
                (build/'manifest.json').write_text('{}')
                (build/'loaded.json').write_text('{"run_state":"not_started"}')
                child=base/'bench.py'; child.write_text('print("Verification: PASS\\nOps/sec: 1")\n')
                args=SimpleNamespace(build_dir=build,threads=64,reqs=1,queue_depth=None,num_queues=None,
                                     page_size=512,num_blks=131072,pages=4096,random=True,access=None,verify=True)
                manifest={'config':config,'environment':{'gpu':{'uuid':'GPU-fixture'}}}
                before={'submitted':0,'completed':0,'bytes':0,'errors':0,'timeouts':0}
                after=dict(before,submitted=actual,completed=actual,bytes=actual*512)
                with patch.object(pipeline,'ROOT',base), patch.object(pipeline,'linux_only'), \
                     patch.object(pipeline,'verify_session',return_value=manifest), \
                     patch.object(pipeline,'check_no_workload'),patch.object(pipeline,'check_library'), \
                     patch.object(pipeline,'dmesg_text',return_value=''),patch.object(pipeline,'snapshot',return_value='fixture'), \
                     patch.object(pipeline,'read_dsa_state',side_effect=[before,after]), \
                     patch.object(pipeline,'benchmark_command',return_value=[sys.executable,str(child)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if actual==64: pipeline.run(args)
                    else:
                        with self.assertRaises(pipeline.Error): pipeline.run(args)
                record=json.loads(next((base/'results').glob('*/run.json')).read_text())
                self.assertEqual(record['payload_backend'],'dsa-borrowed-single')
                self.assertEqual(record['state'],'completed' if actual==64 else 'dsa_mismatch')
                self.assertEqual(record['dsa_delta']['completed'],actual)

    def test_backend_borrows_payload_only_and_precise_rollback(self):
        backend=(ROOT/'dsa/dsa.c').read_text()
        for fn in ('device_release_driver','device_attach','idxd_drv_enable_wq','idxd_drv_disable_wq','__idxd_wq_quiesce'):
            self.assertNotRegex(backend,r'\b'+fn+r'\s*\(')
        extra=r'''
#include <assert.h>
#include <stdio.h>
#define PAGE_SIZE 4096
#define module_param_cb(...)
typedef struct { long long counter; } atomic64_t;
#define ATOMIC64_INIT(x) {x}
static void atomic64_inc(atomic64_t *p) { p->counter++; }
static void atomic64_add(u64 x,atomic64_t *p) { p->counter+=x; }
static long long atomic64_read(atomic64_t *p) { return p->counter; }
int atomic_read(const atomic_t *p) { return p->counter; }
void atomic_set(atomic_t *p,int x) { p->counter=x; }
struct kernel_param {};
struct kernel_param_ops { int (*get)(char *, const struct kernel_param *); };
#define scnprintf snprintf
static bool pasid=false;
static void *domain;
static void *iommu_get_domain_for_dev(struct device *d) { (void)d; return domain; }
static bool device_pasid_enabled(struct idxd_device *d) { (void)d; return pasid; }
int test_taint(unsigned int x) { (void)x; return 0; }
'''
        unit=r'''
#include "mock.h"
unsigned int dsa_num_devs,dsa_num_groups_per_dev,dsa_num_wqs_per_group,dsa_num_engines_per_group;
bool disp_using_dma=false,worker_using_dma=true;
int dsa_bus_type;
static struct idxd_device d;
static struct pci_dev pci;
static struct idxd_wq queues[8], *qp[8];
static struct idxd_group gs[4], *gp[4];
static struct idxd_dma_chan dc[8];
static struct device_driver parent_driver={"idxd"},wq_driver={"dmaengine"};
static unsigned refs,acquired,released,attempt,fail_at;
struct device *bus_find_device_by_name(const int *bus,struct device *start,const char *name) {
    (void)bus;(void)start; assert(!strcmp(name,"dsa0")); refs++; return &d.idxd_dev.conf_dev;
}
void put_device(struct device *dev) { assert(dev==&d.idxd_dev.conf_dev && refs); refs--; }
struct dma_chan *dma_request_channel(dma_cap_mask_t m,bool (*filter)(struct dma_chan *,void *),void *arg) {
    struct idxd_wq *q=arg; (void)m; assert(q->id%2==1); attempt++;
    if(attempt==fail_at) return NULL;
    assert(!q->client_count && filter(&q->idxd_chan->chan,arg)); q->client_count++; acquired++;
    return &q->idxd_chan->chan;
}
void dma_release_channel(struct dma_chan *chan) {
    for(unsigned i=0;i<8;i++) if(chan==&dc[i].chan) {
        assert(queues[i].client_count==1); queues[i].client_count--; released++; return;
    }
    assert(0);
}
#include "backend.c"
static void setup(void) {
    memset(&d,0,sizeof(d)); memset(queues,0,sizeof(queues));
    refs=acquired=released=attempt=fail_at=0; domain=NULL; pasid=false;
    d.pdev=&pci; d.idxd_dev.conf_dev.driver=&parent_driver;
    d.state=IDXD_DEV_ENABLED; d.max_wqs=8; d.max_groups=4; d.wqs=qp; d.groups=gp;
    for(unsigned i=0;i<4;i++) gp[i]=&gs[i];
    for(unsigned i=0;i<8;i++) {
        struct idxd_wq *q=&queues[i]; qp[i]=q; q->id=i;
        q->state=IDXD_WQ_ENABLED; q->type=IDXD_WQT_KERNEL;
        q->idxd_dev.conf_dev.driver=&wq_driver; q->group=gp[i/2];
        q->size=16; q->max_xfer_bytes=65536; q->idxd_chan=&dc[i]; q->portal=q; q->descs=q;
    }
    unsigned sizes[]={16,16}; struct dsa_cfg_params p={1,4,2,1,sizes,NULL};
    assert(!dsa_set_params(&p));
}
static void unchanged(void) {
    assert(d.state==IDXD_DEV_ENABLED && d.idxd_dev.conf_dev.driver==&parent_driver);
    for(unsigned i=0;i<8;i++) assert(queues[i].state==IDXD_WQ_ENABLED && queues[i].size==16 &&
        queues[i].group==gp[i/2] && queues[i].idxd_dev.conf_dev.driver==&wq_driver);
}
int main(void) {
    for(unsigned run=0;run<3;run++) {
        setup(); queues[0].client_count=1;
        assert(!dsa_enable_devices() && acquired==4 && refs==1);
        dsa_disable_devices(); dsa_disable_devices();
        assert(released==4 && !refs && queues[0].client_count==1); unchanged();
    }
    for(unsigned n=1;n<=4;n++) {
        setup(); fail_at=n; assert(dsa_enable_devices()==-EBUSY);
        assert(acquired==n-1 && released==n-1 && !refs); unchanged();
    }
    setup(); queues[3].client_count=1;
    assert(dsa_enable_devices()==-EBUSY && acquired==1 && released==1 && !refs && queues[3].client_count==1);
    setup(); domain=&d; assert(dsa_enable_devices()==-EOPNOTSUPP && !attempt && !refs);
    setup(); pasid=true; assert(dsa_enable_devices()==-EOPNOTSUPP && !attempt && !refs);
    swarmio_dsa_submit(); swarmio_dsa_complete(512); swarmio_dsa_error(false);
    assert(swarmio_dsa_faulted());
    char out[4096]; state_get(out,NULL);
    assert(strstr(out,"submitted=1 completed=1 bytes=512 errors=1"));
}
'''
        if not shutil.which('cc'): self.skipTest('no C compiler')
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'linux').mkdir()
            # Include the extension once, outside the common mock's guard.
            (p/'mock.h').write_text(MOCK.replace('#endif',extra+'\n#endif'))
            for n in ('module.h','dmaengine.h','iommu.h','panic.h','mm.h'): (p/'linux'/n).write_text('#include "mock.h"\n')
            (p/'dsa.h').write_text('#include "mock.h"\n')
            (p/'backend.c').write_text(backend); (p/'test.c').write_text(unit)
            subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-unused-const-variable',
                            '-I',str(p),str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True,timeout=10)

if __name__=='__main__': unittest.main()
