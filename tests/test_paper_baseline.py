"""Offline checks: no kernel module loading, CUDA execution or hardware changes."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'script/baseline'))
import baseline_modules as modules
import benchmark
import paper_baseline as b
from benchmark import author_source as compare_source


class PaperBaselineTests(unittest.TestCase):
    def test_pci_addresses_from_cuda_and_nvml_are_canonicalized(self):
        for raw, canonical in (('0000:2A:00.0', '0000:2a:00.0'),
                               ('00000000:AB:00.0\n', '0000:ab:00.0'),
                               ('00000001:10:00.0', '0001:10:00.0')):
            self.assertEqual(b.normalize_pci_bdf(raw), canonical)
        for raw in ('0000:2a:20.0', '0000:2a:00.8', '10000000:2a:00.0', '2a:00.0', '../0000:2a:00.0'):
            with self.assertRaises(RuntimeError):
                b.normalize_pci_bdf(raw)

    def test_gpu_lookup_follows_lowercase_sysfs_link_and_keeps_numa_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pci = root / 'pci'
            pci.mkdir()
            target = root / 'MixedCaseDeviceTree' / '0000:2a:00.0'
            target.mkdir(parents=True)
            (target / 'numa_node').write_text('0\n')
            (pci / '0000:2a:00.0').symlink_to(target, target_is_directory=True)
            with patch.object(b, 'PCI_DEVICES', pci), patch.object(b, 'command', return_value='0000:2A:00.0\n'):
                self.assertEqual(b.gpu_location(0), ('0000:2a:00.0', 0))
            self.assertEqual(b.numa_node(pci / '00000000:2A:00.0'), 0)
            (target / 'numa_node').write_text('-1\n')
            with self.assertRaises(RuntimeError):
                b.numa_node(pci / '0000:2a:00.0')

    def test_missing_device_does_not_inherit_an_unrelated_parent_numa_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'numa_node').write_text('1\n')
            with self.assertRaisesRegex(RuntimeError, '设备路径不存在'):
                b.numa_node(root / '0000:AB:00.0')
            device = root / '0000:ab:00.0' / 'dsa2'
            device.mkdir(parents=True)
            (device.parent / 'numa_node').write_text('1\n')
            self.assertEqual(b.numa_node(device), 1)

    def test_grid_is_cartesian_and_respects_warps_cache_and_bar1(self):
        args = argparse.Namespace(gpus=[0], threads=[8192, 16384, 32768, 65536],
                                  qps=[32, 64, 128, 256], qds=[256, 512, 1024],
                                  weights=[152, 156, 160], cache_mib=128)
        points, skipped = b.plan(args)
        self.assertEqual(len(points), 252)
        self.assertTrue(skipped)
        self.assertEqual(len({p.key for p in points}), len(points))
        for p in points:
            self.assertEqual(p.threads % (32 * p.qps), 0)
            self.assertLessEqual(p.threads * p.block_bytes, 128 * b.MIB)
            self.assertLessEqual(b.mapped_bytes(128*b.MIB, p.qps, p.qd) + b.BAR1_RESERVE, 254*b.MIB)
        self.assertEqual({(p.physical_dsa, p.gpu) for p in points if p.scenario == 'single'},
                         {(0, 0)})
        self.assertEqual({p.qps for p in points if p.scenario == 'dual-weighted'}, {256})
        self.assertEqual({p.threads for p in points if p.block_bytes == 4096}, {8192, 16384, 32768})
        args.cache_mib = 256
        larger, _ = b.plan(args)
        self.assertEqual(len(larger), 288)
        self.assertIn(65536, {p.threads for p in larger if p.block_bytes == 4096})
        for p in points:
            if p.scenario == 'single' and p.threads == 8192:
                self.assertTrue(b.geometry(p.block_bytes, p.threads, 8, p.qd))
                self.assertTrue(b.geometry(p.block_bytes, p.threads, 256, p.qd))

    def test_cache_uses_actual_bar1_and_maintains_guard_margin(self):
        self.assertEqual(b.choose_cache('auto', {0: 254*b.MIB, 1: 254*b.MIB}, 1024), 128)
        self.assertEqual(b.choose_cache('auto', {0: 1022*b.MIB, 1: 510*b.MIB}, 1024), 256)
        self.assertEqual(b.choose_cache('auto', {0: 1022*b.MIB, 1: 254*b.MIB}, 1024), 128)
        with self.assertRaises(RuntimeError):
            b.choose_cache(256, {0: 254*b.MIB}, 1024)
        with self.assertRaises(RuntimeError):
            b.choose_cache('auto', {0: 223*b.MIB}, 1024)

    def test_large_bar_trial_maps_65536_4k_buffers_and_rejects_insufficient_free_space(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'raw_kernel', return_value=[]):
            out = Path(tmp)
            runner = b.Runner(argparse.Namespace(), out)
            runner.cache_mib = 256
            runner.gpus = {0: {'bdf': '0000:2a:00.0', 'numa_node': 0}}
            runner.loaded_module = out / 'swarmio.ko'
            runner.loaded_parameters = {}
            runner.modules = {('original', False): runner.loaded_module}
            params = out / 'params'
            params.mkdir()
            (params / 'prepare_unload').write_text('0')
            p = b.Point('single', 0, 4096, 65536, 256, 1024, 0)
            log = '\n'.join(f'swarmio_dispatcher_{i} assigned sq [{i+1:02d}-256, stride: 04]' for i in range(4))
            seen = []

            def benchmark(cmd, path):
                seen.append(cmd)
                values = dict(v[2:].split('=', 1) for v in cmd if v.startswith('--'))
                qps, depth = int(values['num_queues']), int(values['queue_depth'])
                ops = int(values['threads']) * int(values['reqs'])
                path.write_text(f'Actual QPs: {qps} SQD: {depth} CQD: {depth}\n'
                                f'Actual QID range: 1-{qps}\nReserved SQs: 256 CQs: 256\nCompare kernel: author\n'
                                f'Elapsed Time: 1000000 Number of Ops: {ops}\n'
                                f'Ops/sec: {ops}\nExtra READs: 0\n')
                return 0

            with patch.object(runner, 'load'), patch.object(runner, 'verify_module'), \
                 patch.object(runner, 'telemetry'), patch.object(runner, 'build_id', return_value='test'), \
                 patch.object(runner, 'check_kernel', return_value=log), \
                 patch.object(b, 'PARAMS', params), patch.object(b, 'parameters', return_value={}), \
                 patch.object(b, 'check_controller'), patch.object(b, 'cpu_snapshot', return_value={}), \
                 patch.object(b, 'bar1_bytes', return_value=(512*b.MIB, 510*b.MIB)), \
                 patch.object(b, 'run_benchmark', side_effect=benchmark):
                record = runner.trial(p, 'scan', 1, 2**24)
                self.assertEqual(record['cache_mib'], 256)
                self.assertIn('--pages=65536', seen[0])
                self.assertIn('SWARMIO_COMPARE_KERNEL=author', seen[0])
                # Replay the user's failing case: 64 created QPs, reserved range 1..256.
                small = b.Point('single', 0, 512, 16384, 64, 512, 0)
                record64 = runner.trial(small, 'scan', 1, 2**26)
                self.assertEqual(record64['qps'], 64)
                self.assertEqual(record64['reserved_sqs'], 256)
                routing_file = next((out / 'trials' / small.key).glob('**/qid-assignment.csv'))
                rows = list(csv.DictReader(io.StringIO(routing_file.read_text())))
                self.assertEqual(sum(int(r['num_qps']) for r in rows), 64)
                self.assertEqual(sum(int(r['num_reserved_qps']) for r in rows), 256)
                with patch.object(b, 'bar1_bytes', return_value=(512*b.MIB, 351*b.MIB)):
                    with self.assertRaises(RuntimeError):
                        runner.trial(p, 'scan', 2, 2**24)
                self.assertEqual(len(seen), 2)

    def test_raw_kernel_cursor_handles_clock_changes_and_detects_loss(self):
        before = ['[1] boot', '[2] old Oops:', '[3] ready']
        delta = b.kernel_delta(before, before + ['[4] new normal'])
        self.assertNotRegex(delta, b.KERNEL_ERROR)
        delta = b.kernel_delta(before, before + ['[4] BUG: new failure'])
        self.assertRegex(delta, b.KERNEL_ERROR)
        with self.assertRaises(RuntimeError):
            b.kernel_delta(before, ['[100] ring overwritten'])

    def test_qid_guard_checks_weighted_and_original_actual_assignments(self):
        p = b.Point('dual-weighted', 0, 512, 32768, 256, 1024, -1, 156)
        lines, start = [], 1
        for i in range(8):
            length = 39 if i < 4 else 25
            lines.append(f'SwarmIO: swarmio_dispatcher_{i} assigned sq [{start:02d}-{start+length-1:02d}, stride: 01]')
            start += length
        rows = b.qid_assignment('\n'.join(lines), p)
        self.assertEqual(sum(r['num_qps'] for r in rows if r['physical_dsa'] == 0), 156)
        with self.assertRaises(RuntimeError):
            b.qid_assignment('\n'.join(lines), b.replace(p, dsa0_qps=128))
        original = '\n'.join(f'swarmio_dispatcher_{i} assigned sq [{i+1:02d}-256, stride: 08]' for i in range(8))
        rows = b.qid_assignment(original, b.replace(p, scenario='dual-equal', dsa0_qps=-1))
        self.assertEqual([r['num_qps'] for r in rows], [32]*8)
        with self.assertRaises(RuntimeError):
            b.qid_assignment(original.replace('01-256', '02-256'), b.replace(p, scenario='dual-equal', dsa0_qps=-1))

    def test_reserved_256_does_not_mean_256_created_queues(self):
        reported = '''[91229.172948] SwarmIO: swarmio_dispatcher_0 assigned sq [01-256, stride: 04]
[91229.172955] SwarmIO: swarmio_dispatcher_1 assigned sq [02-256, stride: 04]
[91229.172955] SwarmIO: swarmio_dispatcher_2 assigned sq [03-256, stride: 04]
[91229.172956] SwarmIO: swarmio_dispatcher_3 assigned sq [04-256, stride: 04]'''
        for qps in (32, 64, 128, 256):
            p = b.Point('single', 0, 512, 16384, qps, 512, 0)
            inventory = f'Actual QID range: 1-{qps}\nReserved SQs: 256 CQs: 256\n'
            self.assertEqual(b.queue_inventory(inventory, p), (256, 256))
            rows = b.qid_assignment(reported, p, 256)
            self.assertEqual(sum(r['num_reserved_qps'] for r in rows), 256)
            self.assertEqual([r['num_qps'] for r in rows], [qps//4]*4)
            self.assertEqual(sum(r['expected_main_read_share'] for r in rows), 1)
            self.assertTrue(all(max(map(int, r['qids'].split())) <= qps for r in rows))
        p = b.Point('dual-equal', 0, 512, 16384, 64, 512)
        interleaved = '\n'.join(f'swarmio_dispatcher_{i} assigned sq [{i+1:02d}-256, stride: 08]' for i in range(8))
        rows = b.qid_assignment(interleaved, p, 256)
        self.assertEqual(sum(r['num_qps'] for r in rows if r['physical_dsa'] == 0), 32)
        with self.assertRaises(RuntimeError):
            b.qid_assignment(interleaved.replace('01-256', '02-256'), p, 256)
        with self.assertRaises(RuntimeError):
            b.qid_assignment(interleaved, p, 128)
        contiguous = '\n'.join(f'swarmio_dispatcher_{i} assigned sq [{i*32+1:02d}-{(i+1)*32:02d}, stride: 01]' for i in range(8))
        with self.assertRaisesRegex(RuntimeError, '实际 DSA0 QP 数'):
            b.qid_assignment(contiguous, p, 256)
        for bad in ('', 'Actual QID range: 2-65\nReserved SQs: 256 CQs: 256\n',
                    'Actual QID range: 1-64\nReserved SQs: 32 CQs: 256\n'):
            with self.assertRaises(RuntimeError):
                b.queue_inventory(bad, p)

    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler unavailable')
    def test_host_queue_guard_checks_real_created_ids_and_depth_with_reserved_capacity(self):
        source = (ROOT / 'script/baseline/benchmark_checks.cuh').read_text()
        function = re.search(r'^static void require_queue_geometry\(.*?^\}', source, re.M | re.S)[0]
        program = r'''
#include <cassert>
#include <iostream>
#include <stdexcept>
#include <vector>
using error = std::runtime_error;
struct Queue { unsigned qs, no; };
struct QueuePair { Queue sq, cq; unsigned qp_id; };
struct Controller { unsigned n_qps, n_sqs, n_cqs; std::vector<QueuePair*> h_qps; };
''' + function + r'''
int main() {
    for (unsigned count : {32,64,128,256}) for (unsigned depth : {256,512,1024}) {
        std::vector<QueuePair> pairs(count);
        Controller ctrl {count,256,256,{}};
        for (unsigned q=0;q<count;q++) {
            pairs[q] = {{depth,q+1},{depth,q+1},q+1};
            ctrl.h_qps.push_back(&pairs[q]);
        }
        require_queue_geometry(ctrl,count,depth);
        auto rejected = [&]() {
            bool bad=false;
            try { require_queue_geometry(ctrl,count,depth); }
            catch (const error&) {bad=true;}
            assert(bad);
        };
        pairs[0].sq.no=0; rejected(); pairs[0].sq.no=1;
        pairs[0].cq.no=0; rejected(); pairs[0].cq.no=1;
        pairs[0].qp_id=0; rejected(); pairs[0].qp_id=1;
        pairs[0].sq.qs=depth/2; rejected(); pairs[0].sq.qs=depth;
        ctrl.n_sqs=count-1; rejected(); ctrl.n_sqs=256;
        ctrl.h_qps[0]=nullptr; rejected();
    }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'queue-guard'
            subprocess.run(['c++', '-std=c++17', '-x', 'c++', '-', '-o', str(exe)],
                           input=program, text=True, check=True)
            output = subprocess.check_output([str(exe)], text=True)
        self.assertEqual(output.count('Reserved SQs: 256 CQs: 256'), 12)
        self.assertEqual(output.count('Actual QID range: 1-64'), 3)

    def test_author_kernels_and_launches_are_preserved_in_generated_program(self):
        source = subprocess.check_output(['git', '-C', str(ROOT / 'bam'), 'show',
                                          '2a08f65:benchmarks/block/main.cu'], text=True)
        result = compare_source(source)
        for name in ('sequential_access_kernel', 'random_access_kernel'):
            pattern = rf'^void {name}\(.*?^\}}'
            self.assertEqual(re.findall(pattern, source, re.M | re.S),
                             re.findall(pattern, result, re.M | re.S))
        self.assertEqual([line for line in source.splitlines() if '<<<' in line],
                         [line for line in result.splitlines() if '<<<' in line])
        self.assertNotIn('lab_read_kernel', result)
        self.assertIn('"Compare kernel: author" << \'\\n\';', result)
        script = (ROOT / 'script/baseline/paper_baseline.py').read_text()
        self.assertIn('SWARMIO_COMPARE_KERNEL=author', script)
        self.assertNotIn('SWARMIO_COMPARE_KERNEL=dsa-read-v1', script)

    def test_author_build_uses_isolated_sources_and_inherits_cuda_configuration(self):
        source = subprocess.check_output(['git', '-C', str(ROOT / 'bam'), 'show',
                                          '2a08f65:benchmarks/block/main.cu'], text=True)
        queue = subprocess.check_output(['git', '-C', str(ROOT / 'bam'), 'show',
                                         '2a08f65:include/nvm_parallel_queue.h'], text=True)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bam, build = base / 'bam', base / 'build'
            for folder in ('benchmarks/block', 'include', 'build'):
                (bam / folder).mkdir(parents=True)
            (bam / 'benchmarks/block/main.cu').write_text(source)
            (bam / 'include/nvm_parallel_queue.h').write_text(queue)
            (bam / 'build/CMakeCache.txt').write_text(
                'CMAKE_CUDA_COMPILER:STRING=/usr/local/cuda/bin/nvcc\n'
                'CMAKE_CUDA_ARCHITECTURES:STRING=120\n')
            (build / 'bin').mkdir(parents=True)
            binary = build / 'bin/nvm-block-bench'
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            with patch.object(benchmark, 'BAM', bam), patch.object(benchmark, 'BUILD', build), \
                 patch.dict(os.environ, {'CMAKE_ARGS': ''}), \
                 patch.object(benchmark.subprocess, 'run') as run, \
                 patch.object(benchmark.subprocess, 'check_output', return_value='2a08f65\n'):
                benchmark.build_benchmark()
            configure, compile_ = [call.args[0] for call in run.call_args_list]
            self.assertIn('-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc', configure)
            self.assertIn('-DCMAKE_CUDA_ARCHITECTURES=120', configure)
            self.assertIn('-Dno_module=ON', configure)
            self.assertIn('-Dno_smartio=ON', configure)
            self.assertEqual(compile_[-2:], ['--target', 'block-benchmark-module'])
            staged = (build / 'baseline_main.cu').read_text()
            self.assertLess(staged.index('#include "baseline_queue.cuh"'), staged.index('#include <page_cache.h>'))
            self.assertEqual((build / 'baseline_queue.cuh').read_text(),
                             queue.replace('(++spins & 255) == 0', '(++spins & 4095) == 0', 1))
            metadata = json.loads((build / 'benchmark.json').read_text())
            self.assertEqual((metadata['kernel'], metadata['fault_exit'], metadata['fault_poll_spins']),
                             ('author', 1, 4096))
            self.assertEqual((bam / 'benchmarks/block/main.cu').read_text(), source)
            self.assertEqual((bam / 'include/nvm_parallel_queue.h').read_text(), queue)

    def test_incompatible_bam_sources_fail_before_build(self):
        with self.assertRaisesRegex(RuntimeError, '故障退出路径'):
            compare_source('int main(int argc, char** argv) {}')
        with self.assertRaisesRegex(RuntimeError, '故障轮询源码不匹配'):
            benchmark.fault_queue_source('/* upstream queue without fault exit */')

    def test_relocated_launcher_generates_plan_and_rejects_wrong_directory(self):
        launcher = ROOT / 'script/baseline/run_paper_baseline.sh'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'plan'
            result = subprocess.run(['bash', str(launcher), '--quick', '--out', str(out)],
                                    cwd=ROOT, text=True, capture_output=True, check=True)
            self.assertIn('没有访问 GPU 或加载模块', result.stdout)
            self.assertEqual(len(list(csv.DictReader((out / 'plan.csv').read_text().splitlines()))), 24)
            self.assertFalse((out / 'modules').exists())
            result = subprocess.run(['bash', str(launcher), '--quick'], cwd=tmp,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('SwarmIO 项目目录', result.stderr)

    def test_staged_latency_code_is_isolated_and_covers_both_retirement_paths(self):
        originals = {name: modules.digest(ROOT / name) for name in
                     ('src/proc.c', 'src/dispatcher.c', 'dsa/batch.c', 'dsa/single.c', 'Kbuild')}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            build = modules.stage_original(folder)
            modules.instrument(build)
            self.assertEqual((build / 'dsa/single.c').read_bytes(), (ROOT / 'dsa/single.c').read_bytes())
            body = (build / 'dsa/batch.c').read_text()
            for function, next_function in (('dsa_dma_wait_oldest_batch', 'dsa_dma_wait_all_batch'),
                                             ('dsa_dma_wait_all_batch', 'dsa_dma_batch_idle')):
                start, end = body.index('int ' + function), body.index(next_function, body.index('int ' + function))
                part = body[start:end]
                self.assertLess(part.index('baseline_begin'), part.index('ret = __dsa_sync_wait'))
                self.assertLess(part.index('if (ret)'), part.index('baseline_end'))
                self.assertIn('DSA_OPCODE_NOOP', part)
                self.assertIn('BASELINE_RECLAIM, baseline_units', part)
                self.assertLess(part.index('list_del'), part.index('BASELINE_RECLAIM, baseline_units'))
            proc = (build / 'src/proc.c').read_text()
            self.assertIn('baseline_latency_init()', proc)
            self.assertIn('baseline_latency_exit()', proc)
        for name, digest in originals.items():
            self.assertEqual(modules.digest(ROOT / name), digest)

    def test_all_six_build_variants_execute_the_real_staging_interfaces(self):
        originals = {name: modules.digest(ROOT / name) for name in
                     ('src/main.c', 'nvmev/admin.c', 'src/proc.c', 'src/dispatcher.c', 'dsa/dsa.c', 'dsa/batch.c')}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            headers = base / 'headers'
            headers.mkdir()
            (headers / 'idxd.h').write_text('/* Header discovery fixture; no kernel compilation. */\n')
            with patch.dict(os.environ, IDXD_INCLUDE=str(headers)):
                for kind in ('original', 'dsa2', 'weighted'):
                    for profiling in (False, True):
                        with self.subTest(kind=kind, profiling=profiling):
                            out = base / (kind + ('-latency' if profiling else ''))
                            build = modules.stage_variant(kind, out, weights=(128, 152, 156, 160))
                            manifest = json.loads((out / 'variant.json').read_text())
                            if kind == 'dsa2':
                                self.assertEqual(manifest['selected_physical_dsa'], 2)
                                self.assertIn('__dev_id_to_idxd(2)', (build / 'dsa/dsa.c').read_text())
                            if kind == 'weighted':
                                self.assertEqual(manifest['tested_dsa0_qps'], [128, 152, 156, 160])
                                self.assertIn('qid_dsa0_qps', (build / 'src/main.c').read_text())
                            if kind != 'original':
                                self.assertTrue((out / 'stage.log').is_file())
                            if profiling:
                                modules.instrument(build)
                                self.assertTrue((build / 'dsa/baseline_latency.c').exists())
                                self.assertIn('baseline_begin', (build / 'src/dispatcher.c').read_text())
                            else:
                                self.assertNotIn('baseline_begin', (build / 'src/dispatcher.c').read_text())
        for name, digest in originals.items():
            self.assertEqual(modules.digest(ROOT / name), digest)

    def test_latency_delta_uses_samples_not_calls_and_does_not_invent_exact_percentiles(self):
        row = dict(physical_dsa='2', wq='1', stage='worker_wait', calls='640', samples='10',
                   units='300', sum_ns='3000', max_ns='900')
        row.update({f'h{i}': str(10 if i == 9 else 0) for i in range(32)})
        before = dict(row, calls='64', samples='1', units='30', sum_ns='300', h9='1')
        result = b.latency_difference({(2, 1, 'worker_wait'): before}, {(2, 1, 'worker_wait'): row})[0]
        self.assertEqual(result['mean_ns_per_call'], 300)
        self.assertEqual(result['amortized_ns_per_unit'], 10)
        self.assertEqual(result['p95_upper_ns'], 511)
        self.assertEqual(result['calls'], 576)
        self.assertEqual(result['samples'], 9)
        bad = dict(row, samples='0')
        with self.assertRaises(RuntimeError):
            b.latency_difference({(2, 1, 'worker_wait'): before}, {(2, 1, 'worker_wait'): bad})

    @unittest.skipUnless(shutil.which('cc'), 'C compiler unavailable')
    def test_c_sampler_records_wait_and_reclaim_without_touching_disabled_calls(self):
        source = (ROOT / 'script/baseline/baseline_latency.c').read_text()
        core = source[source.index('#define BASELINE_DEVS'):source.index('DEFINE_PROC_SHOW_ATTRIBUTE')]
        header = (ROOT / 'script/baseline/baseline_latency.h').read_text()
        header = re.sub(r'^#include[^\n]*', '', header, flags=re.M)
        stubs = r'''
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
typedef unsigned long long u64;
struct idxd_device { int id; };
struct idxd_wq { int id; struct idxd_device *idxd; };
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define DEFINE_SPINLOCK(x) int x
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define spin_lock_irqsave(lock,flags) ((flags)=0)
#define spin_unlock_irqrestore(lock,flags) ((void)(flags))
#define min_t(t,a,b) ((a)<(b)?(a):(b))
#define max(a,b) ((a)>(b)?(a):(b))
static unsigned int fls64(u64 n) { return n ? 64 - __builtin_clzll(n) : 0; }
static u64 fake_clock;
static u64 ktime_get_ns(void) { return fake_clock; }
struct seq_file { char data[8192]; int length; };
static void seq_printf(struct seq_file *m, const char *fmt, ...) {
    va_list args; va_start(args,fmt);
    m->length += vsnprintf(m->data+m->length, sizeof(m->data)-m->length,fmt,args);
    va_end(args);
}
static void seq_puts(struct seq_file *m, const char *s) { seq_printf(m,"%s",s); }
static void seq_putc(struct seq_file *m, char c) { seq_printf(m,"%c",c); }
'''
        exercise = r'''
int main(void) {
    struct idxd_device dev = { .id=2 };
    struct idxd_wq wq = { .id=1, .idxd=&dev };
    struct baseline_token t = baseline_begin(&wq, BASELINE_WAIT);
    assert(!t.sampled && baseline_stats[2][1][BASELINE_WAIT].calls == 0);
    baseline_enabled = true; baseline_sample_every = 2;
    t = baseline_begin(&wq, BASELINE_WAIT); assert(!t.sampled);
    fake_clock = 100; t = baseline_begin(&wq, BASELINE_WAIT); assert(t.sampled);
    fake_clock = 400; baseline_end(&t, BASELINE_WAIT, 31);
    t.start = 400; fake_clock = 450; baseline_end(&t, BASELINE_RECLAIM, 31);
    assert(baseline_stats[2][1][BASELINE_WAIT].sum_ns == 300);
    assert(baseline_stats[2][1][BASELINE_WAIT].histogram[9] == 1);
    assert(baseline_stats[2][1][BASELINE_WAIT].calls == 2);
    assert(baseline_stats[2][1][BASELINE_RECLAIM].sum_ns == 50);
    assert(baseline_stats[2][1][BASELINE_RECLAIM].units == 31);
    baseline_enabled = false;
    t = baseline_begin(&wq, BASELINE_WAIT); assert(!t.sampled);
    assert(baseline_stats[2][1][BASELINE_WAIT].calls == 2);
    struct seq_file snapshot = {0};
    baseline_show(&snapshot, NULL);
    assert(strstr(snapshot.data, "2,1,worker_wait,2,1,31,300,300,") != NULL);
    assert(strstr(snapshot.data, "2,1,worker_reclaim,2,1,31,50,50,") != NULL);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'check'
            subprocess.run(['cc', '-std=gnu11', '-x', 'c', '-', '-o', str(exe)],
                           input=stubs + header + core + exercise, text=True, check=True)
            subprocess.run([str(exe)], check=True)

    def test_summary_excludes_warmups_and_profiles_from_best_and_checks_conditional_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            p = b.Point('single', 0, 512, 8192, 256, 1024, 0)
            p2 = b.replace(p, threads=16384)
            for point, stage, rates in ((p, 'warmup', [9999]), (p, 'scan', [100, 120, 110]),
                                        (p2, 'scan', [90, 100, 110]), (p, 'confirm', [9997, 9999]),
                                        (p2, 'confirm', [98, 102]), (p, 'latency', [9998])):
                for i, rate in enumerate(rates, 1):
                    b.write_json(out / 'trials' / point.key / f'{stage}-{i:02d}' / 'record.json',
                                 b.asdict(point) | dict(point_id=point.key, stage=stage, kernel='author',
                                 repeat=i, status='ok', logical_iops=rate, elapsed_us=1000000, extra_reads=1))
            rows = b.summarize(out)
            self.assertNotIn('warmup', {r['stage'] for r in rows})
            best = list(csv.DictReader(io.StringIO((out / 'best.csv').read_text())))
            self.assertEqual(float(next(r for r in best if r['axis'] == 'overall')['median_iops']), 110)
            self.assertEqual(len([r for r in best if r['axis'] == 'threads']), 2)
            b.write_json(out / 'baseline.json', {'schema': 1})
            b.summarize(out)
            legacy = list(csv.DictReader(io.StringIO((out / 'best.csv').read_text())))
            self.assertEqual(float(next(r for r in legacy if r['axis'] == 'overall')['median_iops']), 9998)

    def test_safe_unload_does_not_continue_when_prepare_fails(self):
        args = argparse.Namespace()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(b, 'raw_kernel', return_value=[]):
                runner = b.Runner(args, Path(tmp))
            with patch.object(b, 'PARAMS', Path(tmp)), patch.object(b, 'mutate', side_effect=RuntimeError('drain failed')) as mutation:
                with self.assertRaises(RuntimeError):
                    runner.stop_stack(Path(tmp) / 'stop.log')
                self.assertEqual(mutation.call_count, 1)
                self.assertEqual(mutation.call_args.args[0], ['bash', 'scripts/load.sh', '--prepare-unload'])

    def test_restore_stops_at_first_failed_control_operation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'raw_kernel', return_value=[]):
            runner = b.Runner(argparse.Namespace(), Path(tmp))
            runner.changed = True
            runner.original_parameters = {'dsa_num_devs': '2', 'cpus': '8-15,28-35'}
            for failed_step in range(5):
                with self.subTest(failed_step=failed_step), \
                     patch.object(Path, 'exists', return_value=True), \
                     patch.object(runner, 'verify_module'), \
                     patch.object(b, 'mutate', side_effect=[None]*failed_step + [RuntimeError('control failed')]) as mutation:
                    with self.assertRaisesRegex(RuntimeError, 'control failed'):
                        runner.restore()
                    self.assertEqual(mutation.call_count, failed_step + 1)
                    commands = [call.args[0] for call in mutation.call_args_list]
                    self.assertEqual(commands[0], ['bash', 'scripts/load.sh', '--prepare-unload'])
                    self.assertFalse(any('--force' in cmd or '-f' in cmd for cmd in commands))

    def test_restore_verifies_original_module_and_parameters(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'raw_kernel', return_value=[]):
            runner = b.Runner(argparse.Namespace(), Path(tmp))
            runner.changed = True
            runner.original_parameters = {'dsa_num_devs': '2', 'cpus': '8-15,28-35'}
            with patch.object(runner, 'stop_stack') as drain, \
                 patch.object(b, 'mutate') as mutation, \
                 patch.object(runner, 'verify_module') as verify, \
                 patch.object(b, 'check_controller'), \
                 patch.object(b, 'parameters', return_value=runner.original_parameters), \
                 patch.object(runner, 'check_kernel'), \
                 patch.object(runner, 'build_id', return_value='original-id'):
                runner.restore()
                drain.assert_called_once()
                verify.assert_called_once_with(runner.original)
                self.assertEqual(mutation.call_args_list[0].args[0],
                                 ['insmod', runner.original, 'dsa_num_devs=2', 'cpus=8-15,28-35'])
            restored = json.loads((Path(tmp) / 'restored.json').read_text())
            self.assertEqual(restored['parameters'], runner.original_parameters)
            self.assertEqual(restored['build_id'], 'original-id')

    def test_resume_skips_completed_trial_without_touching_hardware(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'raw_kernel', return_value=[]):
            runner = b.Runner(argparse.Namespace(), Path(tmp))
            p = b.Point('single', 0, 512, 8192, 256, 1024, 0)
            record = Path(tmp) / 'trials' / p.key / 'scan-01' / 'record.json'
            saved = dict(logical_ops=2**26, kernel='author', point_id=p.key)
            b.write_json(record, saved)
            with patch.object(runner, 'load', side_effect=AssertionError('must not load hardware')):
                self.assertEqual(runner.trial(p, 'scan', 1, 2**26), saved)
                with self.assertRaises(RuntimeError):
                    runner.trial(p, 'scan', 1, 2**25)

    def test_full_workflow_and_resume_with_simulated_measurements(self):
        class FakeRunner(b.Runner):
            def preflight(self):
                self.gpus = {0: {'numa_node': 0}, 1: {'numa_node': 1}}
                self.args.resolved_cache_mib = 128

            def prepare_modules(self):
                pass

            def restore(self):
                self.restored = True

            def trial(self, point, stage, repeat, ops, profiling=False):
                folder = self.out / 'trials' / point.key / f'{stage}-{repeat:02d}'
                if (folder / 'record.json').exists():
                    previous = json.loads((folder / 'record.json').read_text())
                    self_assert = previous['logical_ops'] == ops
                    if not self_assert:
                        raise RuntimeError('resume changed operation count')
                    return previous
                rate = 1000000 * (1 + point.devices / 10) * (0.95 if profiling else 1)
                row = b.asdict(point) | dict(point_id=point.key, stage=stage, kernel='author',
                          repeat=repeat, logical_ops=ops, logical_iops=rate, status='ok',
                          elapsed_us=ops / rate * 1e6, extra_reads=2,
                          gpu_numa=point.gpu)
                if profiling:
                    metrics = []
                    for device in ((point.physical_dsa,) if point.devices == 1 else (0, 2)):
                        for metric in ('sq_sync', 'worker_wait', 'worker_reclaim'):
                            metrics.append(dict(physical_dsa=device, wq=1, metric=metric,
                                                samples=10, units=100, sum_ns=3000,
                                                **{f'h{i}': 10 if i == 9 else 0 for i in range(32)}))
                    b.csv_file(folder / 'latency.csv', metrics)
                b.write_json(folder / 'record.json', row)
                return row

        args = argparse.Namespace(gpus=[0], topology_gpus=[0, 1], cache_mib='auto', threads=[8192], qps=[256], qds=[1024],
                                  weights=[152, 156, 160], seed=1, repeats=3,
                                  ops_512=2**20, ops_4096=2**20)
        points, _ = b.plan(args)
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'Runner', FakeRunner), patch.object(b, 'raw_kernel', return_value=[]):
            out = Path(tmp)
            b.execute(args, out, points)
            self.assertEqual(json.loads((out / 'status.json').read_text())['status'], 'complete')
            first = {p.relative_to(out): p.read_bytes() for p in (out / 'trials').glob('**/record.json')}
            best = list(csv.DictReader(io.StringIO((out / 'best.csv').read_text())))
            self.assertTrue(best)
            self.assertEqual({r['kernel'] for r in best}, {'author'})
            summary = list(csv.DictReader(io.StringIO((out / 'summary.csv').read_text())))
            self.assertEqual({r['stage'] for r in summary}, {'scan', 'topology-throughput', 'latency'})
            self.assertTrue(all(int(r['runs']) == 3 for r in summary))
            self.assertEqual(len(first), len(points)*3 + 6*3 + 14*3)
            self.assertFalse(any(r['stage'].startswith('warmup') for r in summary))
            overhead = list(csv.DictReader(io.StringIO((out / 'profile-overhead.csv').read_text())))
            self.assertTrue(all(abs(float(r['relative_iops_change_pct']) + 5) < 1e-6 for r in overhead))
            latency = list(csv.DictReader(io.StringIO((out / 'latency-summary.csv').read_text())))
            self.assertTrue(any(r['path'] == 'remote' for r in latency))
            self.assertTrue(any(r['path'] == 'local' for r in latency))
            self.assertTrue(all(float(r['mean_ns_per_call']) == 300 for r in latency))
            comparisons = list(csv.DictReader(io.StringIO((out / 'weight-comparison.csv').read_text())))
            self.assertTrue(comparisons)
            for row in comparisons:
                self.assertEqual(float(row['gain_vs_contiguous_equal_pct']), 0)
            b.execute(args, out, points)
            second = {p.relative_to(out): p.read_bytes() for p in (out / 'trials').glob('**/record.json')}
            self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
