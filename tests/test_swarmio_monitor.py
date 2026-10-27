"""监控回归：公式、缺失值、真实采样线程与 GPU 子进程替身、实际内核计数实现。"""
import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('swarmio_monitor', ROOT / 'scripts/swarmio_monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def test_miops_and_all_axis_labels_are_plain_decimals(self):
        for value in (0, 200000, 0.00000012, 1.2e12):
            self.assertNotRegex(monitor.plain_number(value), r'\d[eE][+-]?\d')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with (base / 'resources.csv').open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=monitor.FIELDS)
                writer.writeheader()
                writer.writerow({'elapsed_s': 0.000001, 'nvme_iops': 200000, 'gpu_memory_used_mib': 8192})
                writer.writerow({'elapsed_s': 0.000002, 'nvme_iops': 400000, 'gpu_memory_used_mib': 16384})
            monitor.render(base)
            self.assertIn('MIOPS', (base / 'nvme-iops.svg').read_text())
            for path in base.glob('*.svg'):
                root = ET.fromstring(path.read_text())
                labels = ' '.join(node.text or '' for node in root.iter() if node.tag.endswith('text'))
                self.assertNotRegex(labels, r'\d[eE][+-]?\d')
            self.assertIn('0.432', (base / 'nvme-iops.svg').read_text())

    def test_bar_averages_weighted_totals_and_parallel_wall_ratio(self):
        old = {'version': 2, 'sample_ns': 1000000000, 'ops': 0, 'bytes': 0, 'errors': 0,
               **{key: 0 for key in monitor.BAR_COUNTERS.values()}}
        new = dict(old, sample_ns=1100000000, ops=100, bytes=409600,
                   latency_ios=100, segments=200, map_ns=2000000, copy_ns=1000000,
                   unmap_ns=1000000, bar_ns=5000000, xfer_ns=8000000, request_ns=20000000)
        interval = monitor.bar_interval(old, new)
        self.assertEqual(interval['bar_map_us'], 20)
        self.assertEqual(interval['bar_calls_us'], 40)
        self.assertEqual(interval['bar_calls_share_xfer_pct'], 50)
        self.assertEqual(interval['bar_calls_share_request_pct'], 20)
        self.assertEqual(monitor.bar_interval(new, old), {})
        metadata = {'bar_latency_boundary': {'baseline': old, 'final': new},
                    'bam_kernel_average': {'elapsed_s': .002}}
        summary = monitor.bar_summary([], metadata)
        self.assertEqual(summary['stages']['three_calls_sum']['total_ms'], 4)
        self.assertEqual(summary['stages']['ioremap']['avg_us_per_io'], 20)
        self.assertEqual(summary['cumulative_calls_over_bam_wall_pct'], 200)
        self.assertIn('100%', monitor.bar_table(summary))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'bench.log').write_text('Elapsed Time: 2000 Number of Ops: 100 Data Size (bytes): 409600')
            monitor.render(base, metadata)
            loaded = json.loads((base / 'monitor-summary.json').read_text())
            self.assertEqual(loaded['gpu_bar_latency']['successful_bar_ios'], 100)
            self.assertIn('GPU BAR 延迟汇总', (base / 'monitor.html').read_text())

    def test_latency_switch_restores_after_failure_and_rejects_old_module(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            metrics, switch = base / 'metrics', base / 'switch'
            keys = ' '.join(f'{key}=0' for key in monitor.BAR_COUNTERS.values())
            metrics.write_text('version=2 sample_ns=1 ops=0 bytes=0 errors=0 ' + keys)
            switch.write_text('N\n')
            with patch.object(monitor, 'METRICS', metrics), patch.object(monitor, 'LATENCY_SWITCH', switch):
                saved = None
                with self.assertRaises(RuntimeError):
                    with monitor.bar_latency_mode() as state:
                        saved = state
                        self.assertEqual(switch.read_text().strip(), 'Y')
                        raise RuntimeError('fake benchmark failure')
                self.assertTrue(saved['restored'])
                self.assertIn('baseline', saved)
                self.assertIn('final', saved)
                self.assertEqual(switch.read_text().strip(), 'N')
                metrics.write_text('version=1 sample_ns=1 ops=0 bytes=0 errors=0')
                with self.assertRaises(ValueError):
                    with monitor.bar_latency_mode():
                        self.fail('old module must be rejected before benchmark')
                self.assertEqual(switch.read_text().strip(), 'N')

    def test_counter_rates_and_cpu_guest_accounting(self):
        previous = monitor.io_stats('version=1 sample_ns=1000000000 ops=100 bytes=51200 errors=0')
        current = monitor.io_stats('version=1 sample_ns=1100000000 ops=120 bytes=133120 errors=1')
        iops, bandwidth = monitor.io_delta(previous, current)
        self.assertAlmostEqual(iops, 200)
        self.assertAlmostEqual(bandwidth, 81920 / .1 / 2**30)
        self.assertEqual(monitor.io_delta(current, previous), (None, None))
        counters = monitor.cpu_stats('cpu 100 0 20 100 10 0 0 0 30 0\n')
        self.assertEqual(counters['cpu'], (230, 100, 10))
        self.assertEqual(monitor.cpu_delta((100, 50, 0), (200, 80, 10)), (60, 10))
        for value in ('0', '-1', 'nan', 'inf', '61'):
            with self.assertRaises(argparse.ArgumentTypeError):
                monitor.interval_value(value)
        self.assertEqual(monitor.interval_value('0.05'), .05)

    def test_task_name_spaces_and_parentheses(self):
        fields = ['S'] + ['0'] * 22
        fields[11], fields[12], fields[19], fields[21] = '14', '6', '123', '25'
        self.assertEqual(monitor.task_stat('42 (a name) with ) brackets) ' + ' '.join(fields)), (123, 20, 25))

    def test_actual_sampler_with_fake_proc_and_streaming_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proc, out = base / 'proc', base / 'results'
            proc.mkdir(); out.mkdir()
            (proc / 'stat').write_text('cpu 10 0 10 100 0 0 0 0\ncpu0 10 0 10 100 0 0 0 0\n')
            (proc / 'meminfo').write_text('MemTotal: 102400 kB\nMemAvailable: 51200 kB\nSwapTotal: 2048 kB\nSwapFree: 1024 kB\n')
            def task(pid, comm, ticks):
                path = proc / str(pid)
                path.mkdir(exist_ok=True)
                (path / 'comm').write_text(comm)
                fields = ['S'] + ['0'] * 22
                fields[11], fields[19], fields[21] = str(ticks), '123', '20'
                (path / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields))
            task(12345, 'nvm-block-bench', 0)
            task(12346, 'swarmio_worker_0', 0)
            task(os.getpid(), 'python3', 0)
            metrics = base / 'io_metrics'
            zero = ' '.join(f'{key}=0' for key in monitor.BAR_COUNTERS.values())
            metrics.write_text('version=2 sample_ns=1000000000 ops=0 bytes=0 errors=0 ' + zero)
            (out / 'bench.log').write_text('Elapsed Time: 100000\tNumber of Ops: 10\tData Size (bytes): 40960\n')
            popen = subprocess.Popen
            children = []
            def fake_gpu(command, **kwargs):
                self.assertIn('--loop-ms=20', command)
                self.assertIn('--id=GPU-test', command)
                child = popen([sys.executable, '-u', '-c',
                    'import time\nwhile True:\n print("75, 12, 1024, 8192, 85, 45", flush=True)\n time.sleep(.01)'], **kwargs)
                children.append(child)
                return child
            with patch.object(monitor, 'PROC', proc), patch.object(monitor, 'METRICS', metrics), \
                 patch.object(monitor.subprocess, 'Popen', side_effect=fake_gpu):
                sampler = monitor.ResourceMonitor(out, 'GPU-test', 12345, .02)
                sampler.start()
                time.sleep(.07)
                task(12345, 'nvm-block-bench', 3)
                task(12346, 'swarmio_worker_0', 6)
                (proc / 'stat').write_text('cpu 30 0 20 120 0 0 0 0\ncpu0 30 0 20 120 0 0 0 0\n')
                metrics.write_text('version=2 sample_ns=1100000000 ops=20 bytes=81920 errors=0 '
                                   'latency_ios=20 segments=20 map_ns=20000 copy_ns=40000 '
                                   'unmap_ns=10000 bar_ns=80000 xfer_ns=100000 request_ns=200000')
                time.sleep(.06)
                sampler.stop()
                self.assertFalse(sampler.thread.is_alive())
                self.assertTrue(all(p.poll() is not None for p in children))
                report = sampler.report({'state': 'completed'})
            with (out / 'resources.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertGreaterEqual(len(rows), 3)
            self.assertTrue(any(r['gpu_busy_pct'] == '75.0' for r in rows))
            self.assertTrue(any(float(r['nvme_iops'] or 0) > 0 for r in rows))
            self.assertTrue(any(float(r['swarmio_cpu_pct'] or 0) > 0 for r in rows))
            self.assertEqual(float(rows[-1]['host_memory_used_pct']), 50)
            summary = json.loads((out / 'monitor-summary.json').read_text())
            self.assertEqual(summary['bam_kernel_average']['logical_iops'], 100)
            self.assertEqual(summary['nvme_monitor_window']['ops'], 20)
            self.assertEqual(summary['gpu_bar_latency']['stages']['ioremap']['avg_us_per_io'], 1)
            self.assertTrue(any(r['bar_map_us'] == '1.0' for r in rows))
            self.assertIn('0.02', report.read_text())
            for svg in out.glob('*.svg'):
                ET.fromstring(svg.read_text())
            self.assertTrue((out / 'cpu-cores-0.svg').exists())

    def test_missing_metrics_remain_gaps_and_can_render_again(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with (base / 'resources.csv').open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=monitor.FIELDS)
                writer.writeheader()
                writer.writerow({'elapsed_s': 0, 'host_memory_used_pct': 25})
            monitor.render(base, {'run_state': 'incomplete', 'warnings': ['missing GPU']})
            self.assertIn('No available samples', (base / 'nvme-iops.svg').read_text())
            monitor.render(base)
            self.assertEqual(json.loads((base / 'monitor-summary.json').read_text())['run_state'], 'incomplete')

    def test_kernel_metrics_header_counts_all_queues_and_payload_sizes(self):
        if not shutil.which('cc'):
            self.skipTest('C compiler unavailable')
        prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
typedef unsigned seqcount_t;
#define CONFIG_SWARMIO_QUEUE_DIAGNOSTICS 1
#define NR_MAX_IO_QUEUE 64
#define PAGE_SIZE 4096
#define ____cacheline_aligned_in_smp __attribute__((aligned(64)))
#define seqcount_init(p) (*(p)=0)
#define preempt_disable() (++preempt)
#define preempt_enable() (--preempt)
#define write_seqcount_begin(p) (++*(p))
#define write_seqcount_end(p) (++*(p))
#define read_seqcount_begin(p) (*(p))
#define read_seqcount_retry(p, s) (*(p)!=(s))
#define module_param_cb(...)
#define MODULE_PARM_DESC(...)
#define scnprintf snprintf
struct kernel_param { int unused; };
struct kernel_param_ops { int (*get)(char *, const struct kernel_param *); };
static int preempt;
static u64 ktime_get_ns(void) { return 123456; }
'''
        checks = r'''
int main(void) {
    char buffer[4096];
    struct swarmio_bar_timing timing = { .enabled=true, .start_ns=122456,
        .segments=1, .map_ns=100, .copy_ns=200, .unmap_ns=50, .bar_ns=400, .xfer_ns=500 };
    swarmio_metrics_init();
    assert(sizeof(struct swarmio_io_counter) % 64 == 0);
    swarmio_metrics_record(1, 512, true, &timing);
    swarmio_metrics_record(1, 512, true, &timing);
    swarmio_metrics_record(16, 4096, true, &timing);
    swarmio_metrics_record(16, 4096, false, &timing);
    swarmio_metrics_record(0, 512, true, &timing);
    swarmio_metrics_record(65, 512, true, &timing);
    swarmio_metrics_ops.get(buffer, NULL);
    assert(strstr(buffer, "ops=3 bytes=5120 errors=1"));
    assert(strstr(buffer, "version=2"));
    assert(strstr(buffer, "latency_ios=3 segments=3 map_ns=300 copy_ns=600 unmap_ns=150 bar_ns=1200 xfer_ns=1500 request_ns=3000"));
    timing.enabled = false;
    swarmio_metrics_record(1, 512, true, &timing);
    swarmio_metrics_ops.get(buffer, NULL);
    assert(strstr(buffer, "ops=4 bytes=5632 errors=1"));
    assert(strstr(buffer, "latency_ios=3"));
    assert(preempt==0);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'linux').mkdir()
            for name in ('seqlock.h', 'preempt.h', 'types.h'):
                (base / 'linux' / name).write_text('/* API doubles in prelude */')
            (base / 'probe.c').write_text(prelude + (ROOT / 'src/swarmio_io_metrics.h').read_text() + checks)
            compiled = subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-I', str(base), '-I', str(ROOT / 'src'),
                str(base / 'probe.c'), '-o', str(base / 'probe')], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            subprocess.run([str(base / 'probe')], check=True)


if __name__ == '__main__':
    unittest.main()
