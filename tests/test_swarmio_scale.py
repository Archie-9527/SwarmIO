"""Large-QP budgets and shared-WQ ownership on a single DSA."""
import json
import contextlib
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_swarmio_dsa import pipeline
from test_swarmio_dsa_c import compile_run

ROOT = Path(__file__).resolve().parents[1]


class ScalingTests(unittest.TestCase):
    def read_changed(self, change, **kwargs):
        c = json.loads((ROOT / 'configs/bam-dsa-scale-lab230.json').read_text())
        change(c)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.json'
            p.write_text(json.dumps(c))
            return pipeline.read_config(p, **kwargs)

    def test_large_queue_and_payload_budgets(self):
        c = pipeline.read_config(ROOT / 'configs/bam-dsa-scale-lab230.json')
        for queues, threads, reqs, mib in ((128,65536,1024,48), (256,131072,512,96), (512,262144,256,192)):
            b = dict(c['benchmark'], num_queues=queues, threads=threads, pages=threads,
                     reqs=reqs, num_blks=262144)
            pipeline.validate_benchmark(b, c)
            budget = pipeline.buffer_budget(b)
            self.assertEqual(budget['gpu_pinned_bytes'], mib * 2**20)
            self.assertEqual(b['threads'] * b['reqs'], 67108864)
            small = dict(c, libnvm={'max_gpu_pinned_mb':mib-1})
            with self.assertRaisesRegex(pipeline.Error, 'GPU pin'):
                pipeline.validate_benchmark(b, small)
        self.assertEqual(pipeline.buffer_budget(dict(c['benchmark'],queue_depth=512))['queue_bytes'],16*2**20)
        with self.assertRaises(pipeline.Error):
            pipeline.validate_benchmark(dict(c['benchmark'],num_queues=513),c)

    def test_pin_limit_is_configurable_and_old_default_is_preserved(self):
        old = pipeline.read_config(ROOT / 'configs/bam-dsa-sq-lab230.json')
        self.assertEqual(pipeline.gpu_pin_limit_mb(old),32)
        new = pipeline.read_config(ROOT / 'configs/bam-dsa-sq-lab230.json', gpu_pinned_mb=256)
        self.assertEqual(new['libnvm']['max_gpu_pinned_mb'],256)
        for value in (0,-1,True,1.5,65537):
            with self.subTest(value=value), self.assertRaises(pipeline.Error):
                pipeline.read_config(ROOT / 'configs/bam-dsa-scale-lab230.json',gpu_pinned_mb=value)

    def test_service_units_share_wq_capacity_not_extra_devices(self):
        for units, depth, fetch in ((4,16,2),(8,8,4),(16,4,8)):
            path = ROOT / ('configs/bam-dsa-scale-lab230.json' if units == 4 else f'configs/bam-dsa-scale-su{units}-lab230.json')
            c = pipeline.read_config(path)
            plan = pipeline.dsa_wq_requirements(c)
            self.assertEqual(len(plan),8)
            self.assertEqual([x[3] for x in plan],[fetch,16]*4)
            self.assertEqual(c['module']['num_dma_descs_per_worker'],depth)
            self.assertEqual(len(pipeline.cpu_set(c['module']['cpus'])),2*units)
        def insufficient(c):
            c['module'].update(num_service_units=8,cpus='4-19')
        with self.assertRaisesRegex(pipeline.Error,'共享容量不足'):
            self.read_changed(insufficient)
        def uneven(c):
            c['module'].update(num_service_units=5,num_dma_descs_per_worker=8,cpus='4-13')
        c = self.read_changed(uneven)
        self.assertEqual([x[3] for x in pipeline.dsa_wq_requirements(c)],[4,16,2,8,2,8,2,8])
        c = pipeline.read_config(ROOT / 'configs/bam-dsa-scale-d30-lab230.json')
        self.assertEqual([x[3] for x in pipeline.dsa_wq_requirements(c)],[2,30]*4)

    def test_runtime_pin_limit_mismatch_is_rejected(self):
        c = pipeline.read_config(ROOT / 'configs/bam-dsa-scale-su8-lab230.json')
        m = {'config':c,'environment':{'dsa':{'pci':'0000:75:01.0'}}}
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp); p = b / 'swarmio/parameters'; p.mkdir(parents=True)
            lib = b / 'libnvm/parameters'; lib.mkdir(parents=True)
            (lib/'dma_peer_bdf').write_text('0000:75:01.0')
            (p/'dsa_state').write_text('version=3 device=0 borrowed=8 fault=0 submitted=0 completed=0 bytes=0 errors=0 timeouts=0 mode=1 batches_submitted=0 batches_retired=0 active_batches=0 pending_batches=0 peak_batches=0 max_batch=0 sq_fetch=1 sq_submitted=0 sq_completed=0 sq_bytes=0 sq_entries=0 sq_max_entries=0 sq_pending=0')
            for k in ('dma_batch_size','num_dma_descs_per_worker','dma_batch_flush_us','num_dma_descs_per_disp',
                      'num_service_units','dsa_num_grps_per_dev'):
                (p/k).write_text(str(c['module'][k]))
            for k in ('worker_using_dma','disp_using_dma'): (p/k).write_text('Y')
            with patch.object(pipeline,'MODULES',b):
                (lib/'max_gpu_pinned_mb').write_text('32')
                with self.assertRaisesRegex(pipeline.Error,'pin 上限'):
                    pipeline.read_dsa_state(m)
                (lib/'max_gpu_pinned_mb').write_text('256')
                pipeline.read_dsa_state(m)
                (p/'num_service_units').write_text('4')
                with self.assertRaisesRegex(pipeline.Error,'服务单元'):
                    pipeline.read_dsa_state(m)

    def test_kernel_validation_checks_shared_descriptor_totals(self):
        source = (ROOT/'src/main.c').read_text()
        validation = source[source.index('static int __init __validate_dsa_params(void)'):source.index('static int __load_configs(')]
        pre = r'''
#include <assert.h>
#include <errno.h>
#define __init
#define CONFIG_SWARMIO_WORKER_BATCH_IO 1
#define DSA_MAX_BATCH_SIZE 1024
#define DSA_MAX_NUM_DESCS_PER_THREAD 64
#define SWARMIO_ERROR(...) ((void)0)
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
static unsigned int dma_batch_size=128,num_dma_descs_per_worker=16,num_dma_descs_per_disp=2,dma_timeout=5000;
static unsigned int dsa_num_devs=1,dsa_num_grps_per_dev=4,dsa_num_wqs_per_grp=2;
static unsigned int num_service_units=4,num_workers_per_service_unit=1;
static unsigned int dsa_wq_sizes[]={16,16};
static struct { struct { unsigned int size,priority; } wq; } num_arr_params={{2,2}};
static int disp_using_dma=1,worker_using_dma=1;
'''
        checks = r'''
int main(void) {
    assert(!__validate_dsa_params());
    num_service_units=8; num_dma_descs_per_worker=8; assert(!__validate_dsa_params());
    num_dma_descs_per_worker=9; assert(__validate_dsa_params()==-EINVAL);
    num_service_units=16; num_dma_descs_per_worker=4; assert(!__validate_dsa_params());
    num_service_units=17; assert(__validate_dsa_params()==-EINVAL);
    num_service_units=4; dsa_wq_sizes[0]=2; dsa_wq_sizes[1]=30;
    num_dma_descs_per_worker=30; assert(!__validate_dsa_params());
    num_dma_descs_per_worker=32; assert(__validate_dsa_params()==-EINVAL);
    dsa_num_grps_per_dev=0; assert(__validate_dsa_params()==-EINVAL);
    return 0;
}
'''
        compile_run(pre+validation+checks)

    def test_load_passes_configured_pin_limit_to_libnvm_only(self):
        c = pipeline.read_config(ROOT/'configs/bam-dsa-scale-lab230.json')
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp); devices=b/'pci'; device=devices/'0001:10:00.0'; device.mkdir(parents=True)
            (device/'class').write_text('0x010802')
            drv=b/'drivers/nvme'; drv.mkdir(parents=True)
            for key in ('bind','unbind'): (drv/key).write_text('')
            (device/'driver').symlink_to(drv)
            (b/'cmdline').write_text('memmap=24G$4G'); (b/'online').write_text('0-79')
            paths={'/proc/cmdline':b/'cmdline','/sys/devices/system/cpu/online':b/'online'}
            dsa={'pci':'0000:75:01.0'}
            manifest={'config':c,'environment':{'gpu':{},'dsa':dsa}}
            commands=[]
            def execute(command,**kwargs):
                commands.append(command)
                if command[0]=='insmod' and command[1].endswith('libnvm/libnvm.ko'):
                    (device/'driver').unlink()
                    raise subprocess.CalledProcessError(1,command)
                return subprocess.CompletedProcess(command,0)
            with contextlib.ExitStack() as stack:
                for name,value in (('PCI',devices),): stack.enter_context(patch.object(pipeline,name,value))
                stack.enter_context(patch.object(pipeline,'Path',side_effect=lambda p: paths.get(str(p),Path(p))))
                for name in ('linux_only','check_no_workload','verify_gpu'): stack.enter_context(patch.object(pipeline,name))
                for name,value in (('manifest_for',manifest),('dsa_environment',dsa),('dmesg_text',''),
                                   ('module_state',{}),('virtual_device',device),('output','')):
                    stack.enter_context(patch.object(pipeline,name,return_value=value))
                stack.enter_context(patch.object(pipeline.subprocess,'run',side_effect=execute))
                with self.assertRaises(subprocess.CalledProcessError):
                    pipeline.load(SimpleNamespace(build_dir=b/'build',bdf=None))
            inserts=[cmd for cmd in commands if cmd[0]=='insmod']
            self.assertEqual(len(inserts),2)
            self.assertIn('max_gpu_pinned_mb=256',inserts[1])
            self.assertFalse(any(x.startswith('max_gpu_pinned_mb=') for x in inserts[0]))


if __name__ == '__main__':
    unittest.main()
