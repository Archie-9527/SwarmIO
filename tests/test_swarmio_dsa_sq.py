"""DSA coalesced SQ fetch: existing dispatcher path, role ownership and counters."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_swarmio_dsa import pipeline
from test_swarmio_dsa_c import compile_run

ROOT=Path(__file__).resolve().parents[1]

class SQFetchTests(unittest.TestCase):
    def test_sq_profile_and_kbuild_require_coalescing_without_prefetch(self):
        c=pipeline.read_config(ROOT/'configs/bam-dsa-sq-lab230.json')
        self.assertTrue(pipeline.is_dsa_sq(c))
        self.assertFalse(pipeline.is_dsa_sq(pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json')))
        for section,key,value in [('build','CONFIG_SWARMIO_DISP_COALESCE_SQE','n'),
                                  ('build','CONFIG_SWARMIO_DISP_PREFETCH_SQE','y'),
                                  ('module','num_dma_descs_per_disp',0),('module','disp_using_dma',0)]:
            bad=json.loads(json.dumps(c)); bad[section][key]=value
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'c.json'; p.write_text(json.dumps(bad))
                with self.assertRaises(pipeline.Error): pipeline.read_config(p)
        if shutil.which('make'):
            source='include '+str(ROOT/'Kbuild')+'\nreport:\n\t@echo $(swarmio-objs)\n'
            args=['make','-s','-f','-','report']+[k+'='+v for k,v in c['build'].items()]
            result=subprocess.run(args,input=source,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('dsa/batch.o',result.stdout)
            self.assertIn('dsa/single.o',result.stdout)

    def test_even_wq_environment_validation_and_cpu_sq_compatibility(self):
        c=pipeline.read_config(ROOT/'configs/bam-dsa-sq-lab230.json')
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp); modules=b/'modules'; modules.mkdir()
            pci=b/'pci'; gpu=pci/'0000:2a:00.0'; gpu.mkdir(parents=True); (gpu/'numa_node').write_text('0')
            dev=b/'0000:75:01.0/dsa0'; dev.mkdir(parents=True)
            (dev/'state').write_text('enabled'); (dev/'numa_node').write_text('0')
            bus=b/'bus'; bus.mkdir(); (bus/'dsa0').symlink_to(dev,target_is_directory=True)
            driver=b/'drivers/dmaengine'; driver.mkdir(parents=True)
            for i in range(8):
                q=dev/f'wq0.{i}'; q.mkdir()
                for k,v in dict(state='enabled',type='kernel',mode='dedicated',group_id=str(i//2),
                                size='16',max_batch_size='1024',max_transfer_size='2147483648').items():
                    (q/k).write_text(v)
                (q/'driver').symlink_to(driver,target_is_directory=True)
            cmd=b/'cmdline'; cmd.write_text('intel_iommu=off memmap=24G$4G')
            taint=b/'taint'; taint.write_text('0'); cpus=b/'nodes/node0/cpulist'
            cpus.parent.mkdir(parents=True); cpus.write_text('8-15')
            paths={'/sys/bus/dsa/devices':bus,'/proc/cmdline':cmd,'/proc/sys/kernel/tainted':taint,
                   '/sys/devices/system/node':cpus.parent.parent}
            with patch.object(pipeline,'MODULES',modules),patch.object(pipeline,'PCI',pci), \
                 patch.object(pipeline,'Path',side_effect=lambda p: paths.get(str(p),Path(p))):
                report=pipeline.dsa_environment(c,{'pci':'00000000:2a:00.0'})
                self.assertEqual(report['fetch_wqs'],['wq0.0','wq0.2','wq0.4','wq0.6'])
                self.assertEqual(report['payload_wqs'],['wq0.1','wq0.3','wq0.5','wq0.7'])
                q=dev/'wq0.0'; (q/'size').write_text('1')
                with self.assertRaises(pipeline.Error): pipeline.dsa_environment(c,{'pci':'00000000:2a:00.0'})
                (q/'size').write_text('16'); (q/'max_transfer_size').write_text('4096')
                with self.assertRaises(pipeline.Error): pipeline.dsa_environment(c,{'pci':'00000000:2a:00.0'})
                old=pipeline.read_config(ROOT/'configs/bam-dsa-async-lab230.json')
                self.assertNotIn('fetch_wqs',pipeline.dsa_environment(old,{'pci':'00000000:2a:00.0'}))

    def test_runtime_fetch_mode_and_pending_dma_are_checked(self):
        c=pipeline.read_config(ROOT/'configs/bam-dsa-sq-lab230.json')
        manifest={'config':c,'environment':{'dsa':{'pci':'0000:75:01.0'}}}
        good=dict(version=3,device=0,borrowed=8,fault=0,submitted=0,completed=0,bytes=0,errors=0,timeouts=0,
                  mode=1,batches_submitted=0,batches_retired=0,active_batches=0,pending_batches=0,peak_batches=0,max_batch=0,
                  sq_fetch=1,sq_submitted=0,sq_completed=0,sq_bytes=0,sq_entries=0,sq_max_entries=0,sq_pending=0)
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp); p=b/'swarmio/parameters'; p.mkdir(parents=True)
            peer=b/'libnvm/parameters'; peer.mkdir(parents=True); (peer/'dma_peer_bdf').write_text('0000:75:01.0')
            (p/'worker_using_dma').write_text('Y'); (p/'disp_using_dma').write_text('Y')
            for k in ('dma_batch_size','num_dma_descs_per_worker','dma_batch_flush_us','num_dma_descs_per_disp'):
                (p/k).write_text(str(c['module'][k]))
            with patch.object(pipeline,'MODULES',b):
                for change,valid in [({},True),({'version':2},False),({'borrowed':4},False),
                                     ({'sq_fetch':0},False),({'sq_pending':1},False)]:
                    (p/'dsa_state').write_text(' '.join(f'{k}={v}' for k,v in dict(good,**change).items()))
                    if valid: pipeline.read_dsa_state(manifest)
                    else:
                        with self.assertRaises(pipeline.Error): pipeline.read_dsa_state(manifest)
                (p/'dsa_state').write_text(' '.join(f'{k}={v}' for k,v in good.items()))
                (p/'disp_using_dma').write_text('N')
                with self.assertRaises(pipeline.Error): pipeline.read_dsa_state(manifest)

    def test_run_separates_sq_fetches_from_payload_and_rejects_missing_sqes(self):
        c=pipeline.read_config(ROOT/'configs/bam-dsa-sq-lab230.json')
        for entries in (64,63):
            with tempfile.TemporaryDirectory() as tmp:
                b=Path(tmp); out=b/'build'; out.mkdir()
                (out/'manifest.json').write_text('{}'); (out/'loaded.json').write_text('{"run_state":"not_started"}')
                child=b/'bench.py'; child.write_text('print("Verification: PASS\\nOps/sec: 1")\n')
                args=SimpleNamespace(build_dir=out,threads=64,reqs=1,pages=4096,page_size=512,num_blks=131072,
                                     num_queues=1,queue_depth=512,random=True,access=None,verify=True)
                before=dict(submitted=0,completed=0,bytes=0,errors=0,timeouts=0,batches_submitted=0,batches_retired=0,
                            peak_batches=0,max_batch=0,sq_submitted=0,sq_completed=0,sq_bytes=0,sq_entries=0,sq_max_entries=0)
                after=dict(before,submitted=64,completed=64,bytes=32768,batches_submitted=2,batches_retired=2,
                           peak_batches=2,max_batch=32,sq_submitted=4,sq_completed=4,sq_bytes=entries*64,sq_entries=entries,sq_max_entries=16)
                m={'config':c,'environment':{'gpu':{'uuid':'GPU-fixture'}}}
                with patch.object(pipeline,'ROOT',b),patch.object(pipeline,'linux_only'), \
                     patch.object(pipeline,'verify_session',return_value=m),patch.object(pipeline,'check_no_workload'), \
                     patch.object(pipeline,'check_library'),patch.object(pipeline,'dmesg_text',return_value=''), \
                     patch.object(pipeline,'snapshot',return_value='fixture'), \
                     patch.object(pipeline,'read_dsa_state',side_effect=[before,after]), \
                     patch.object(pipeline,'benchmark_command',return_value=[sys.executable,str(child)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if entries==64: pipeline.run(args)
                    else:
                        with self.assertRaises(pipeline.Error): pipeline.run(args)
                record=json.loads(next((b/'results').glob('*/run.json')).read_text())
                self.assertEqual(record['sq_backend'],'dsa-coalesced')
                self.assertEqual(record['dsa_delta']['completed'],64)
                self.assertEqual(record['dsa_sq_delta']['sq_completed'],4)
                self.assertEqual(record['state'],'completed' if entries==64 else 'dsa_mismatch')

    @unittest.skipUnless(shutil.which('cc'),'no C compiler')
    def test_real_dispatcher_coalesces_wraps_retains_fetched_entries_and_never_falls_back(self):
        source=(ROOT/'src/dispatcher.c').read_text()
        fault=source[source.index('static inline void __swarmio_fault_disp_dma'):source.index('#include "swarmio_queue_diagnostics.h"')]
        dispatch=source[source.index('int swarmio_dispatch_sq(int sqid,'):]
        pre=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
typedef unsigned long long u64,dma_addr_t;
#define CONFIG_SWARMIO_DISP_COALESCE_SQE 1
#define CONFIG_SWARMIO_AGGREGATED_TIMING_UPDATE 1
#define NR_MAX_IO_QUEUE 64
#define NR_MAX_QUEUE_SIZE 1024
#define NR_MAX_FETCH_SQE 1024
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define unlikely(x) (x)
#define min(a,b) ((a)<(b)?(a):(b))
#define SWARMIO_ERROR(...) ((void)0)
#define sq_dma(i) (sq->dma_addr+(i)*sizeof(struct nvme_command))
struct nvme_command { struct { unsigned opcode,nsid; } common; unsigned char rest[56]; };
struct nvme_submission_queue { int queue_size,fetched_pos; dma_addr_t cmd_buffer_dma,dma_addr; struct nvme_command *cmd_buffer; u64 *nsecs_targets; };
struct swarmio_dispatcher { void *dma_ctx; };
struct swarmio_nvme_ns;
struct swarmio_nvme_ns { void (*sched_nvme_cmd_aggregated)(struct swarmio_nvme_ns*,struct nvme_command*,u64,u64*,int,struct swarmio_dispatcher*); };
static struct { struct nvme_submission_queue *sqes[65]; struct swarmio_nvme_ns ns[1]; } device,*vdev=&device;
static bool sq_diag_faulted,disp_using_dma=true;
static unsigned dmas,cpu_copies,dispatched,schedules,fetch_entries,limit=100,fail_dma;
static u64 ktime_get_ns(void) { return 1; }
static int dsa_dma_issue_sync_single(void *ctx,dma_addr_t dst,dma_addr_t src,size_t bytes,void *data,void *cb) {
    dmas++; if(fail_dma) return -EIO;
    assert(bytes%64==0); fetch_entries+=bytes/64; memcpy((void *)(uintptr_t)dst,(void *)(uintptr_t)src,bytes); return 0;
}
static bool sq_diag_batch(struct nvme_submission_queue *q,const struct nvme_command *c,int first,int n) {
    for(int i=0;i<n;i++) assert(c[i].common.nsid==1); return true;
}
static void sq_diag_copy(struct nvme_submission_queue *q,void *dst,int first,int n) {
    assert(!disp_using_dma); cpu_copies++; memcpy(dst,(void *)(uintptr_t)(q->dma_addr+first*64),n*64);
}
static void schedule(struct swarmio_nvme_ns *ns,struct nvme_command *cmd,u64 now,u64 *targets,int n,struct swarmio_dispatcher *d) { schedules++; }
static bool __dispatch_sq_entry(int qid,int i,size_t *bytes,struct swarmio_dispatcher *d) {
    if(dispatched==limit) return false; assert(vdev->sqes[qid]->cmd_buffer[i].common.nsid==1); dispatched++; return true;
}
'''
        checks=r'''
int main(void) {
    struct nvme_command gpu[8],host[8]; u64 targets[8];
    _Static_assert(sizeof(struct nvme_command)==64,"NVMe SQE size");
    for(int i=0;i<8;i++) gpu[i].common.nsid=1;
    struct nvme_submission_queue q={.queue_size=8,.dma_addr=(uintptr_t)gpu,.cmd_buffer_dma=(uintptr_t)host,.cmd_buffer=host,.nsecs_targets=targets};
    struct swarmio_dispatcher d={0}; vdev->sqes[1]=&q; vdev->ns[0].sched_nvme_cmd_aggregated=schedule;
    assert(swarmio_dispatch_sq(1,5,0,&d)==5 && dmas==1 && fetch_entries==5 && dispatched==5 && !cpu_copies);
    q.fetched_pos=6; dmas=fetch_entries=dispatched=schedules=0;
    assert(swarmio_dispatch_sq(1,3,6,&d)==0 && q.fetched_pos==0 && dmas==1 && fetch_entries==2);
    assert(swarmio_dispatch_sq(1,3,0,&d)==3 && dmas==2 && fetch_entries==5 && dispatched==5);
    q.fetched_pos=0; dmas=fetch_entries=dispatched=0; limit=2;
    assert(swarmio_dispatch_sq(1,5,0,&d)==2 && q.fetched_pos==5 && dmas==1);
    limit=100; assert(swarmio_dispatch_sq(1,5,2,&d)==5 && dmas==1 && fetch_entries==5 && dispatched==5);
    assert(swarmio_dispatch_sq(1,5,5,&d)==5 && dmas==1);
    q.fetched_pos=0; dmas=fetch_entries=dispatched=schedules=0; fail_dma=1;
    assert(swarmio_dispatch_sq(1,5,0,&d)==0 && sq_diag_faulted && disp_using_dma);
    assert(!cpu_copies && !dispatched && !schedules && q.fetched_pos==0 && fetch_entries==0);
    assert(swarmio_dispatch_sq(1,5,0,&d)==0 && dmas==1);
    return 0;
}
'''
        compile_run(pre+fault+dispatch+checks)

if __name__=='__main__': unittest.main()
