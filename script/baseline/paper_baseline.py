#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compact author-only Baseline: three repeats, local scans and matched topology tests."""
import argparse
from collections import defaultdict
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import time

import baseline_modules as modules
from benchmark import BUILD, CACHE_KEYS, MODULE_PARAMETERS, benchmark_result, build_benchmark, run_benchmark
from common import (BAR1_RESERVE, CUDA_BDF_QUERY, DATA_SIZE, KERNEL_ERROR,
                    MIB, PARAMS, ROOT, bar1_bytes, check_controller, mapped_bytes)

SCHEMA = 2
VIRTUAL_BDF = '0001:10:00.0'
PCI_DEVICES = Path('/sys/bus/pci/devices')
CPU_LISTS = {0: '8-15', 2: '28-35'}
STOP = False


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def csv_file(path, rows):
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('')
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    temporary = path.with_suffix('.csv.tmp')
    with temporary.open('w', newline='') as file:
        writer = csv.DictWriter(file, fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def command(args, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, cwd=ROOT,
                                   start_new_session=True, **kwargs)


def mutate(args, log):
    with log.open('a') as file:
        file.write('\n$ ' + ' '.join(map(str, args)) + '\n')
        file.flush()
        subprocess.run(list(map(str, args)), cwd=ROOT, check=True, stdout=file,
                       stderr=subprocess.STDOUT, start_new_session=True)


def raw_kernel():
    return command(['dmesg', '--color=never']).splitlines()


def kernel_delta(before, after):
    if before:
        # Match a suffix, rather than only one potentially repeated printk line.
        suffix = before[-min(8, len(before)):]
        matches = [i for i in range(len(after) - len(suffix) + 1)
                   if after[i:i + len(suffix)] == suffix]
        if not matches:
            raise RuntimeError('内核日志游标丢失，停止；保留本次结果，不能假定无故障')
        after = after[matches[-1] + len(suffix):]
    return '\n'.join(after) + ('\n' if after else '')


def abort_requested():
    if STOP:
        raise KeyboardInterrupt


def parameters():
    names = (*MODULE_PARAMETERS, 'write_sched_delay', 'write_min_delay', 'dma_timeout',
             'qid_dsa0_qps', 'baseline_enabled', 'baseline_sample_every', 'trace_first_io')
    return {n: (PARAMS / n).read_text().strip() for n in names if (PARAMS / n).exists()}


def normalize_pci_bdf(value):
    """CUDA/NVML may print uppercase hex or an eight-digit zero PCI domain."""
    match = re.fullmatch(r'([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])', value.strip())
    if not match:
        raise RuntimeError(f'无效的 PCI BDF：{value!r}')
    domain, bus, slot, function = (int(v, 16) for v in match.groups())
    if domain > 0xffff or slot > 0x1f:
        raise RuntimeError(f'PCI BDF 超出范围：{value!r}')
    return f'{domain:04x}:{bus:02x}:{slot:02x}.{function:x}'


def numa_node(path):
    path = Path(path)
    if re.fullmatch(r'[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', path.name):
        path = path.with_name(normalize_pci_bdf(path.name))
    if not path.exists():
        raise RuntimeError(f'设备路径不存在，无法读取 NUMA 节点：{path}')
    for candidate in (path, path / 'device', *path.resolve().parents):
        node = candidate / 'numa_node'
        if node.is_file():
            value = int(node.read_text())
            if value >= 0:
                return value
    raise RuntimeError(f'无法确定设备的 NUMA 节点：{path}')


def gpu_location(gpu):
    bdf = normalize_pci_bdf(command([sys.executable, '-c', CUDA_BDF_QUERY, gpu]))
    return bdf, numa_node(PCI_DEVICES / bdf)


def cpu_snapshot():
    return {line.split()[0]: list(map(int, line.split()[1:9]))
            for line in Path('/proc/stat').read_text().splitlines()
            if re.match(r'^cpu\d+\s', line)}


def cpu_difference(before, after):
    rows = []
    for cpu, new in after.items():
        delta = [n - o for n, o in zip(new, before[cpu])]
        total = sum(delta)
        if total > 0:
            rows.append({'cpu': int(cpu[3:]), 'busy_pct': 100 * (total - delta[3] - delta[4]) / total,
                         'iowait_pct': 100 * delta[4] / total,
                         'scope': 'whole_benchmark_process_including_setup_teardown'})
    return rows


def queue_inventory(log, point):
    reserved = re.search(r'^Reserved SQs: (\d+) CQs: (\d+)$', log, re.M)
    created = re.search(r'^Actual QID range: (\d+)-(\d+)$', log, re.M)
    if (not reserved or not created or tuple(map(int, created.groups())) != (1, point.qps)
            or not all(point.qps <= int(n) <= 256 for n in reserved.groups())):
        raise RuntimeError('BaM 实际 QID 或预留队列信息不符，请查看 benchmark.log 并确认已重建新版程序')
    return int(reserved[1]), int(reserved[2])


def qid_assignment(log, point, reserved_qps=None):
    assignments = {}
    for index, start, end, stride in re.findall(
            r'swarmio_dispatcher_(\d+) assigned sq \[\s*(\d+)-\s*(\d+), stride:\s*(\d+)\]', log):
        start, end, stride = map(int, (start, end, stride))
        if not (1 <= start <= end <= 256 and stride > 0):
            raise RuntimeError('内核日志中的 QID 范围或 stride 无效')
        assignments[int(index)] = list(range(start, end + 1, stride))
    all_qids = [qid for values in assignments.values() for qid in values]
    reserved_qps = reserved_qps if reserved_qps is not None else max(all_qids, default=0)
    if (not point.qps <= reserved_qps <= 256
            or sorted(all_qids) != list(range(1, reserved_qps + 1))
            or set(assignments) != set(range(4 if point.devices == 1 else 8))):
        raise RuntimeError(f'无法核对预留 QID 范围：预留 {reserved_qps}、实际创建 {point.qps}；'
                           '检查本轮 kernel.log 的 assigned sq 记录')
    counts = {0: 0, 2: 0}
    rows = []
    for index, reserved_qids in sorted(assignments.items()):
        # The author reserves MAX_QUEUES, then creates only IDs 1..numQueues.
        # Dispatchers retain the reserved ranges and skip uncreated SQs.
        qids = [qid for qid in reserved_qids if qid <= point.qps]
        physical = point.physical_dsa if point.devices == 1 else (0 if index < 4 else 2)
        counts[physical] += len(qids)
        rows.append({'dispatcher': index, 'physical_dsa': physical, 'qids': ' '.join(map(str, qids)),
                     'reserved_qids': ' '.join(map(str, reserved_qids)),
                     'num_reserved_qps': len(reserved_qids),
                     'num_qps': len(qids), 'expected_main_read_share': len(qids) / point.qps})
    expected0 = point.qps if point.physical_dsa == 0 else 0 if point.physical_dsa == 2 else point.dsa0_qps if point.dsa0_qps >= 0 else point.qps // 2
    if counts[0] != expected0:
        raise RuntimeError(f'实际 DSA0 QP 数 {counts[0]}，预期 {expected0}')
    return rows


@dataclass(frozen=True)
class Point:
    scenario: str
    gpu: int
    block_bytes: int
    threads: int
    qps: int
    qd: int
    physical_dsa: int = -1
    dsa0_qps: int = -1

    @property
    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]

    @property
    def devices(self):
        return 1 if self.physical_dsa >= 0 else 2


def integers(text):
    try:
        values = [int(v) for v in text.split(',')]
    except ValueError as error:
        raise argparse.ArgumentTypeError('需要用逗号分隔整数') from error
    if not values or len(values) != len(set(values)):
        raise argparse.ArgumentTypeError('列表不能为空或有重复值')
    return sorted(values)


def geometry(block, threads, qps, qd, cache_mib=128):
    return (threads % (32 * qps) == 0 and threads % 64 == 0
            and qps * 32 <= threads <= cache_mib * MIB // block)


def choose_cache(requested, free_by_gpu, qd):
    candidates = (256, 128) if requested == 'auto' else (requested,)
    for cache in candidates:
        required = mapped_bytes(cache * MIB, 256, qd) + BAR1_RESERVE
        if all(free >= required for free in free_by_gpu.values()):
            return cache
    raise RuntimeError(f'BAR1 空闲不足：{free_by_gpu}；256 MiB cache 在 QD<=1024、256 QP 下需约 352 MiB 空闲，'
                       '请先调整固件 ReBAR，或使用 --cache-mib 128')


def plan(args):
    points, skipped = [], []
    requested = getattr(args, 'cache_mib', 128)
    cache = getattr(args, 'resolved_cache_mib', 256 if requested == 'auto' else requested)
    for scenario in ('single', 'dual-equal', 'dual-contiguous', 'dual-weighted'):
        for gpu in args.gpus:
            for block in (512, 4096):
                for t in args.threads:
                    for q in (args.qps if scenario in ('single', 'dual-equal') else [256]):
                        for d in args.qds:
                            choices = ([(0, -1)] if scenario == 'single'
                                       else [(-1, w) for w in args.weights] if scenario == 'dual-weighted'
                                       else [(-1, 128 if scenario == 'dual-contiguous' else -1)])
                            for physical, weight in choices:
                                p = Point(scenario, gpu, block, t, q, d, physical, weight)
                                if geometry(block, t, q, d, cache):
                                    points.append(p)
                                else:
                                    skipped.append(asdict(p) | {'reason': f'warp/QP balance or {cache} MiB cache capacity'})
    return points, skipped


def diagnostic_points(rows, throughput_gpus, topology_gpus):
    """Hold geometry fixed; do not repeat Cartesian scans on remote paths."""
    scan = [r for r in rows if r['stage'] == 'scan']
    points = []
    for block in (512, 4096):
        candidates = [r for r in scan if r['scenario'] == 'single' and r['block_bytes'] == block]
        preferred = [r for r in candidates if r['gpu'] == 0]
        best = max(preferred or candidates, key=lambda r: r['median_iops'])
        for gpu in topology_gpus:
            for physical in (0, 2):
                points.append(Point('single', gpu, block, best['threads'], best['qps'], best['qd'], physical))
        for gpu in throughput_gpus:
            best = max((r for r in scan if r['scenario'] == 'dual-weighted'
                        and r['gpu'] == gpu and r['block_bytes'] == block), key=lambda r: r['median_iops'])
            weighted = Point(**{k: best[k] for k in Point.__dataclass_fields__})
            points.extend([replace(weighted, scenario='dual-equal', dsa0_qps=-1),
                           replace(weighted, scenario='dual-contiguous', dsa0_qps=128), weighted])
    return list({p.key: p for p in points}.values())


def read_latency(path):
    return {(int(r['physical_dsa']), int(r['wq']), r['stage']): r
            for r in csv.DictReader(io.StringIO(path.read_text()))}


def latency_difference(before, after):
    results = []
    for key, new in after.items():
        old = before.get(key, {})
        row = dict(zip(('physical_dsa', 'wq', 'metric'), key))
        for name in ('calls', 'samples', 'units', 'sum_ns', *(f'h{i}' for i in range(32))):
            row[name] = int(new[name]) - int(old.get(name, 0))
            if row[name] < 0:
                raise RuntimeError('阶段计数倒退，模块可能在本轮被外部重载')
        if not row['samples']:
            continue
        if sum(row[f'h{i}'] for i in range(32)) != row['samples']:
            raise RuntimeError('阶段直方图计数不一致')
        row['mean_ns_per_call'] = row['sum_ns'] / row['samples']
        row['mean_units_per_call'] = row['units'] / row['samples']
        row['amortized_ns_per_unit'] = row['sum_ns'] / row['units'] if row['units'] else ''
        for p in (50, 95, 99):
            cumulative = 0
            for bucket in range(32):
                cumulative += row[f'h{bucket}']
                if cumulative >= math.ceil(row['samples'] * p / 100):
                    # Bucket i: [2^(i-1), 2^i-1] ns; final bucket saturates.
                    row[f'p{p}_upper_ns'] = (2**bucket - 1) if bucket < 31 else 'overflow'
                    break
        results.append(row)
    return results


def summarize(out):
    records = []
    for file in sorted((out / 'trials').glob('**/record.json')):
        row = json.loads(file.read_text())
        if row['status'] == 'ok':
            records.append(row)
    csv_file(out / 'runs.csv', records)
    grouped = defaultdict(list)
    for r in records:
        if not r['stage'].startswith('warmup'):
            grouped[(r['point_id'], r['stage'])].append(r)
    summary = []
    for (point_id, stage), group in grouped.items():
        rates = [r['logical_iops'] for r in group]
        mean = statistics.mean(rates)
        first = group[0]
        row = {k: first[k] for k in (*Point.__dataclass_fields__, 'kernel', 'point_id')}
        row['gpu_numa'] = first.get('gpu_numa', '')
        row['cache_mib'] = first.get('cache_mib', '')
        row['reserved_sqs'] = first.get('reserved_sqs', '')
        row['reserved_cqs'] = first.get('reserved_cqs', '')
        row.update(stage=stage, runs=len(group), median_iops=statistics.median(rates),
                   mean_iops=mean, min_iops=min(rates), max_iops=max(rates),
                   stdev_iops=statistics.stdev(rates) if len(rates) > 1 else 0,
                   cv_pct=100 * statistics.stdev(rates) / mean if len(rates) > 1 else 0,
                   min_elapsed_s=min(r['elapsed_us'] / 1e6 for r in group),
                   median_extra_reads=statistics.median(r['extra_reads'] for r in group))
        row['median_gb_s'] = row['median_iops'] * first['block_bytes'] / 1e9
        row['median_gib_s'] = row['median_iops'] * first['block_bytes'] / 2**30
        summary.append(row)
    csv_file(out / 'summary.csv', summary)
    # Schema 2 uses three scan repetitions, without a second confirmation sweep.
    config_file = out / 'baseline.json'
    schema = json.loads(config_file.read_text()).get('schema', 1) if config_file.exists() else SCHEMA
    best_stage = 'scan' if schema >= 2 else 'confirm'
    envelope = []
    groups = defaultdict(list)
    for r in summary:
        if r['stage'] == best_stage:
            groups[(r['scenario'], r['gpu'], r['block_bytes'], r['physical_dsa'])].append(r)
    for group in groups.values():
        for axis in ('overall', 'threads', 'qps', 'qd', 'dsa0_qps'):
            for value in ([0] if axis == 'overall' else sorted({r[axis] for r in group})):
                candidates = group if axis == 'overall' else [r for r in group if r[axis] == value]
                best = max(candidates, key=lambda r: r['median_iops'])
                envelope.append(dict(best, axis=axis, axis_value=value,
                                     claim='best_observed_in_configured_grid' if schema >= 2 else 'best_confirmed_in_configured_grid'))
    csv_file(out / 'best.csv', envelope)
    latency = []
    for file in sorted((out / 'trials').glob('**/latency.csv')):
        metadata = json.loads((file.parent / 'record.json').read_text()) if (file.parent / 'record.json').exists() else None
        if metadata is not None and metadata['stage'] == 'latency':
            for row in csv.DictReader(io.StringIO(file.read_text())):
                latency.append({k: metadata[k] for k in (*Point.__dataclass_fields__, 'point_id', 'repeat', 'elapsed_us', 'logical_iops', 'gpu_numa')}
                               | row | {'trial': str(file.parent.relative_to(out)),
                                        'cache_mib': metadata.get('cache_mib', ''),
                                        'dsa_numa': 0 if int(row['physical_dsa']) == 0 else 1,
                                        'path': 'local' if metadata['gpu_numa'] == (0 if int(row['physical_dsa']) == 0 else 1) else 'remote'})
    csv_file(out / 'latency.csv', latency)
    # Pool sampled calls across WQs/repeats; report histogram bounds, not exact percentiles.
    pooled = defaultdict(list)
    for row in latency:
        pooled[(row['point_id'], row['physical_dsa'], row['metric'])].append(row)
    latency_summary = []
    for (point_id, device, metric), group in pooled.items():
        row = {k: group[0][k] for k in (*Point.__dataclass_fields__, 'point_id')}
        row.update(physical_dsa=device, metric=metric, runs=len({r['repeat'] for r in group}),
                   path=group[0]['path'], gpu_numa=group[0]['gpu_numa'], dsa_numa=group[0]['dsa_numa'])
        for name in ('samples', 'units', 'sum_ns', *(f'h{i}' for i in range(32))):
            row[name] = sum(int(r[name]) for r in group)
        row['mean_ns_per_call'] = row['sum_ns'] / row['samples']
        row['mean_units_per_call'] = row['units'] / row['samples']
        row['amortized_ns_per_unit'] = row['sum_ns'] / row['units'] if row['units'] else ''
        for percentile in (50, 95, 99):
            total = 0
            for bucket in range(32):
                total += row[f'h{bucket}']
                if total >= math.ceil(row['samples'] * percentile / 100):
                    row[f'p{percentile}_upper_ns'] = 2**bucket - 1 if bucket < 31 else 'overflow'
                    break
        latency_summary.append(row)
    csv_file(out / 'latency-summary.csv', latency_summary)
    controls = []
    for row in summary:
        if row['stage'] == 'control-after':
            before = next((r for r in summary if r['point_id'] == row['point_id'] and r['stage'] == 'control-before'), None)
            if before:
                controls.append(dict(row, before_iops=before['median_iops'],
                                     drift_pct=100 * (row['median_iops'] / before['median_iops'] - 1)))
    if controls:
        csv_file(out / 'controls.csv', controls)
    comparisons = []
    for row in summary:
        if row['stage'] != 'scan' or row['scenario'] != 'dual-weighted':
            continue
        matched = {name: next((r for r in summary if r['stage'] == 'scan' and r['scenario'] == name
                              and all(r[k] == row[k] for k in ('gpu', 'block_bytes', 'threads', 'qps', 'qd'))), None)
                   for name in ('dual-equal', 'dual-contiguous')}
        if all(matched.values()):
            comparisons.append(dict(row, original_equal_iops=matched['dual-equal']['median_iops'],
                               contiguous_equal_iops=matched['dual-contiguous']['median_iops'],
                               gain_vs_original_equal_pct=100 * (row['median_iops'] / matched['dual-equal']['median_iops'] - 1),
                               gain_vs_contiguous_equal_pct=100 * (row['median_iops'] / matched['dual-contiguous']['median_iops'] - 1)))
    csv_file(out / 'weight-comparison.csv', comparisons)
    overhead = []
    for row in summary:
        if row['stage'] == 'latency':
            baseline = next((r for r in summary if r['point_id'] == row['point_id'] and r['stage'] == 'topology-throughput'), None)
            if baseline is None:
                baseline = next((r for r in summary if r['point_id'] == row['point_id'] and r['stage'] == 'scan'), None)
            if baseline:
                overhead.append(dict(row, uninstrumented_iops=baseline['median_iops'],
                                     uninstrumented_stage=baseline['stage'],
                                     relative_iops_change_pct=100 * (row['median_iops'] / baseline['median_iops'] - 1)))
    csv_file(out / 'profile-overhead.csv', overhead)
    return summary


class Runner:
    def __init__(self, args, out):
        self.args, self.out = args, out
        self.cursor = raw_kernel()
        self.active = None
        self.sequence = len(list((out / 'transitions').glob('*.json')))
        self.binary = BUILD / 'bin/nvm-block-bench'
        self.modules = {}
        self.original = out / 'swarmio-original.ko'
        self.original_parameters = {}
        self.changed = False

    def check_kernel(self, path):
        current = raw_kernel()
        delta = kernel_delta(self.cursor, current)
        path.write_text(delta)
        self.cursor = current
        error = KERNEL_ERROR.search(delta)
        if error:
            raise RuntimeError(f'新内核异常 {error[0]}，停止：{path}')
        return delta

    def build_id(self, module):
        match = re.search(r'Build ID:\s*(\w+)', command(['readelf', '-n', module]))
        if not match:
            raise RuntimeError(f'模块缺少 Build ID：{module}')
        return match[1]

    def verify_module(self, module, name='swarmio'):
        note = Path(f'/sys/module/{name}/notes/.note.gnu.build-id').read_bytes()
        # Parse the ELF note rather than assume a fixed name/descriptor offset.
        import struct
        name_size, desc_size, _ = struct.unpack_from('=III', note)
        pos = 12 + (name_size + 3) // 4 * 4
        actual = note[pos:pos + desc_size].hex()
        expected = self.build_id(module)
        if actual != expected:
            raise RuntimeError(f'{name} Build ID 不符：运行 {actual}，磁盘 {expected}，文件 {module}')

    def stop_stack(self, log):
        if PARAMS.exists():
            mutate(['bash', 'scripts/load.sh', '--prepare-unload'], log)
        if Path('/sys/module/libnvm').exists():
            mutate(['bash', 'scripts/load_bam.sh', '-u', '--no-rebind', VIRTUAL_BDF], log)
        if PARAMS.exists():
            mutate(['bash', 'scripts/load.sh', '--unload'], log)

    def preflight(self):
        if os.geteuid() != 0:
            raise RuntimeError('执行测试需要 sudo bash script/baseline/run_paper_baseline.sh --run')
        installed = Path(command(['modinfo', '-n', 'swarmio']).strip()).resolve()
        if installed.suffix != '.ko':
            raise RuntimeError('需要未压缩的已安装 swarmio.ko，以保存并核对原模块')
        self.verify_module(installed)
        idxd = Path(command(['modinfo', '-n', 'idxd']).strip()).resolve()
        self.verify_module(idxd, 'idxd')
        if (PARAMS / 'prepare_unload').read_text().strip() != '0':
            raise RuntimeError('模块已进入 prepare-unload 状态，请先正常恢复双 DSA 配置')
        check_controller(self.binary, 2)
        self.original_parameters = parameters()
        if 'qid_dsa0_qps' in self.original_parameters or 'baseline_enabled' in self.original_parameters:
            raise RuntimeError('开始前必须加载正常原模块，不能从实验模块开始')
        if self.original.exists() and modules.digest(self.original) != modules.digest(installed):
            raise RuntimeError('恢复/续测原模块与记录不符')
        shutil.copy2(installed, self.original)
        write_json(self.out / 'original-runtime.json', self.original_parameters)
        self.gpus, budgets = {}, {}
        for gpu in sorted(set(self.args.gpus) | set(self.args.topology_gpus)):
            bdf, node = gpu_location(gpu)
            if node not in (0, 1):
                raise RuntimeError(f'GPU {gpu} 无法确认 NUMA 节点：{node}')
            self.gpus[gpu] = {'bdf': bdf, 'numa_node': node}
            total, free = bar1_bytes(bdf)
            budgets[gpu] = {'total_bytes': total, 'free_bytes': free}
        memory_file = self.out / 'memory-plan.json'
        previous = json.loads(memory_file.read_text()) if memory_file.exists() else None
        requested = previous['cache_mib'] if previous else self.args.cache_mib
        self.cache_mib = choose_cache(requested, {g: b['free_bytes'] for g, b in budgets.items()}, max(self.args.qds))
        self.args.resolved_cache_mib = self.cache_mib
        totals = {str(g): b['total_bytes'] for g, b in budgets.items()}
        if previous and (previous['cache_mib'] != self.cache_mib or previous['bar1_total_bytes'] != totals):
            raise RuntimeError('续测 BAR1 大小已改变，请创建新的 Baseline 目录')
        if not previous:
            write_json(memory_file, {'cache_mib': self.cache_mib, 'bar1_total_bytes': totals,
                                    'initial_budget': budgets,
                                    'required_free_bytes': mapped_bytes(self.cache_mib * MIB, 256, max(self.args.qds)) + BAR1_RESERVE})
        for physical, node in ((0, 0), (2, 1)):
            dev = Path(f'/sys/bus/dsa/devices/dsa{physical}')
            if numa_node(dev) != node:
                raise RuntimeError(f'dsa{physical} NUMA 与本机配置不符')
            first, last = map(int, CPU_LISTS[physical].split('-'))
            for cpu in range(first, last + 1):
                if not Path(f'/sys/devices/system/cpu/cpu{cpu}/node{node}').exists():
                    raise RuntimeError(f'CPU{cpu} 不属于 NUMA{node}')
        gpu_map = json.loads(json.dumps(self.gpus))
        gpu_file = self.out / 'gpu-map.json'
        if gpu_file.exists() and json.loads(gpu_file.read_text()) != gpu_map:
            raise RuntimeError('续测 GPU PCI/NUMA 映射已改变')
        write_json(gpu_file, gpu_map)
        host = {'machine_id': Path('/etc/machine-id').read_text().strip(),
                'gpu_identity': command(['nvidia-smi', '--query-gpu=index,uuid,pci.bus_id,driver_version', '--format=csv,noheader'])}
        host_file = self.out / 'host.json'
        if host_file.exists() and json.loads(host_file.read_text()) != host:
            raise RuntimeError('续测主机、GPU 或驱动已改变，请创建新 Baseline')
        write_json(host_file, host)
        self.inventory()

    def inventory(self):
        environment = self.out / 'environment' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        environment.mkdir(parents=True)
        commands = {
            'uname': ['uname', '-a'], 'cmdline': ['cat', '/proc/cmdline'],
            'lscpu': ['lscpu', '-e=CPU,NODE,SOCKET,CORE,ONLINE,MAXMHZ,MINMHZ'],
            'numa': ['numactl', '--hardware'], 'topology': ['nvidia-smi', 'topo', '-m'],
            'gpu': ['nvidia-smi', '-q'], 'pci': ['lspci', '-Dtv'],
            'iomem': ['cat', '/proc/iomem'], 'memory': ['cat', '/proc/meminfo'],
            'gcc': ['gcc-12', '--version'], 'cmake': ['cmake', '--version'],
            'idxd': ['modinfo', 'idxd'], 'libnvm': ['modinfo', 'libnvm'],
            'nvcc': ['nvcc', '--version'], 'lsmem': ['lsmem', '--json', '--output', 'RANGE,SIZE,STATE,NODE'],
        }
        for name, cmd in commands.items():
            path = environment / (name + '.txt')
            try:
                path.write_text(command(cmd, stderr=subprocess.STDOUT))
            except (OSError, subprocess.CalledProcessError) as error:
                path.write_text(str(error) + '\n' + getattr(error, 'output', ''))
        # Preserve NUMA distance, CPU governor and DSA/WQ state without changing them.
        state = {}
        for pattern in ('/sys/devices/system/node/node*/distance',
                        '/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor',
                        '/sys/bus/dsa/devices/dsa*/state', '/sys/bus/dsa/devices/wq*/state'):
            import glob
            for name in glob.glob(pattern):
                state[name] = Path(name).read_text().strip()
        write_json(environment / 'sysfs.json', state)
        for name in ('idxd', 'swarmio', 'libnvm', 'nvidia'):
            note = Path(f'/sys/module/{name}/notes/.note.gnu.build-id')
            if note.exists():
                (environment / f'{name}-running-build-id.hex').write_text(note.read_bytes().hex() + '\n')

    def prepare_modules(self):
        include = modules.headers()
        for kind in ('original', 'dsa2', 'weighted'):
            for profiling in (False, True):
                abort_requested()
                label = kind + ('-latency' if profiling else '')
                destination = self.out / 'modules' / label
                if destination.exists() and not (destination / 'build.json').exists():
                    destination.rename(destination.with_name(label + datetime.now().strftime('-incomplete-%Y%m%d-%H%M%S-%f')))
                if destination.exists():
                    manifest = json.loads((destination / 'build.json').read_text())
                    candidates = list(destination.glob('build-*/swarmio.ko'))
                    if len(candidates) != 1:
                        raise RuntimeError(f'续测构建目录没有唯一的 swarmio.ko：{destination}')
                    candidate = candidates[0]
                    if modules.digest(candidate) != manifest['module_sha256']:
                        raise RuntimeError(f'续测模块校验失败：{candidate}')
                else:
                    build = modules.stage_variant(kind, destination, weights=sorted({128, *self.args.weights}))
                    if profiling:
                        modules.instrument(build)
                    candidate = modules.compile_module(build, include, kind == 'weighted', destination / 'compile.log')
                self.modules[kind, profiling] = candidate
                if command(['modinfo', '-F', 'vermagic', candidate]).split()[0] != os.uname().release:
                    raise RuntimeError('隔离模块的 vermagic 与当前内核不符')

    def load(self, point, profiling=False):
        kind = ('dsa2' if point.physical_dsa == 2 else 'weighted'
                if point.scenario in ('dual-contiguous', 'dual-weighted') else 'original')
        state = (kind, profiling, point.physical_dsa, point.dsa0_qps)
        if state == self.active:
            return
        abort_requested()
        self.sequence += 1
        folder = self.out / 'transitions'
        folder.mkdir(exist_ok=True)
        log = folder / f'{self.sequence:04d}.log'
        self.changed = True
        self.stop_stack(log)
        params = dict(memmap_start='4G', memmap_size='24G', num_sched_insts=8,
                      io_unit_shift=9, read_min_delay=49900, write_min_delay=49900,
                      read_sched_delay=100, write_sched_delay=100,
                      num_service_units=4 if point.devices == 1 else 8,
                      num_workers_per_service_unit=1,
                      cpus=CPU_LISTS[point.physical_dsa] if point.devices == 1 else '8-15,28-35',
                      disp_using_dma=1, worker_using_dma=1,
                      dsa_num_devs=point.devices, dsa_num_grps_per_dev=4,
                      dsa_num_wqs_per_grp=2, dsa_num_engs_per_grp=1,
                      dsa_wq_sizes='2,30', dsa_wq_priorities='1,15',
                      num_dma_descs_per_disp=2, num_dma_descs_per_worker=30, dma_batch_size=32,
                      trace_first_io=0)
        if kind == 'weighted':
            params['qid_dsa0_qps'] = point.dsa0_qps
        if profiling:
            params.update(baseline_sample_every=self.args.sample_every, baseline_enabled=0)
        module = self.modules[kind, profiling]
        mutate(['modprobe', 'idxd'], log)
        mutate(['insmod', module, *(f'{k}={v}' for k, v in params.items())], log)
        self.verify_module(module)
        for physical in (0, 2):
            expected = 'enabled' if point.devices == 2 or point.physical_dsa == physical else 'disabled'
            if Path(f'/sys/bus/dsa/devices/dsa{physical}/state').read_text().strip() != expected:
                raise RuntimeError(f'dsa{physical} 的实际选择不符')
        mutate(['bash', 'scripts/load_bam.sh', VIRTUAL_BDF], log)
        check_controller(self.binary, point.devices)
        actual = parameters()
        for k, expected in params.items():
            if k in actual:
                wanted = {'1': 'Y', '0': 'N'}.get(str(expected), str(expected)) if k in ('disp_using_dma', 'worker_using_dma', 'baseline_enabled', 'trace_first_io') else str(expected)
                if k in ('memmap_start', 'memmap_size'):
                    wanted = str((4 if k.endswith('start') else 24) * 2**30)
                if actual[k] != wanted:
                    raise RuntimeError(f'模块参数 {k} 实际 {actual[k]}，预期 {wanted}')
        write_json(folder / f'{self.sequence:04d}.json', {'module': str(module), 'requested': params,
                   'actual': actual, 'build_id': self.build_id(module), 'state': state})
        self.active = state
        self.loaded_module, self.loaded_parameters = module, actual
        self.check_kernel(folder / f'{self.sequence:04d}-kernel.log')
        abort_requested()

    def telemetry(self, path):
        path.write_text(command(['nvidia-smi', '--query-gpu=index,pci.bus_id,temperature.gpu,power.draw,clocks.sm,clocks.mem,pstate,utilization.gpu', '--format=csv']))

    def trial(self, point, stage, repeat, ops, profiling=False):
        folder = self.out / 'trials' / point.key / f'{stage}-{repeat:02d}'
        record = folder / 'record.json'
        if record.exists():
            saved = json.loads(record.read_text())
            if saved['logical_ops'] != ops or saved['point_id'] != point.key or saved['kernel'] != 'author':
                raise RuntimeError(f'续测记录配置不一致：{record}')
            return saved
        abort_requested()
        self.load(point, profiling)
        self.verify_module(self.loaded_module)
        if parameters() != self.loaded_parameters:
            raise RuntimeError('运行模块参数被外部修改，停止本轮采集')
        check_controller(self.binary, point.devices)
        bdf = self.gpus[point.gpu]['bdf']
        _, free = bar1_bytes(bdf)
        required = mapped_bytes(self.cache_mib * MIB, point.qps, point.qd) + BAR1_RESERVE
        if free < required:
            raise RuntimeError(f'BAR1 空闲 {free/MIB:g} MiB，小于含余量需求 {required/MIB:g} MiB')
        if (PARAMS / 'prepare_unload').read_text().strip() != '0':
            raise RuntimeError('模块已准备卸载，停止测试')
        folder.mkdir(parents=True, exist_ok=True)
        attempt = folder / datetime.now().strftime('attempt-%Y%m%d-%H%M%S-%f')
        attempt.mkdir()
        self.telemetry(attempt / 'gpu-before.csv')
        cmd = ['env', 'BAM_SWARMIO_FAULT_EXIT=1', 'SWARMIO_COMPARE_KERNEL=author',
               str(self.binary), f'--threads={point.threads}', '--blk_size=64',
               f'--reqs={ops // point.threads}', f'--pages={self.cache_mib * MIB // point.block_bytes}',
               f'--page_size={point.block_bytes}', f'--num_blks={DATA_SIZE // point.block_bytes}',
               f'--queue_depth={point.qd}', f'--num_queues={point.qps}',
               f'--gpu={point.gpu}', '--n_ctrls=1', '--random=true', '--access_type=0']
        write_json(attempt / 'config.json', {'point': asdict(point), 'command': cmd,
                   'module': parameters(), 'stage': stage, 'repeat': repeat,
                   'bar1_free_bytes': free, 'bar1_required_with_reserve_bytes': required})
        print(f'{stage} GPU{point.gpu} {point.scenario} dsa={point.physical_dsa} '
              f'{point.block_bytes}B T={point.threads} QP={point.qps} QD={point.qd} '
              f'dsa0QP={point.dsa0_qps} repeat={repeat}', flush=True)
        latency_path = Path('/proc/swarmio_baseline_latency')
        before = {}
        if profiling:
            (PARAMS / 'baseline_enabled').write_text('0\n')
            shutil.copyfile(latency_path, attempt / 'latency-before.csv')
            before = read_latency(attempt / 'latency-before.csv')
            (PARAMS / 'baseline_enabled').write_text('1\n')
        cpu_before = cpu_snapshot()
        try:
            status = run_benchmark(cmd, attempt / 'benchmark.log')
        finally:
            if profiling:
                (PARAMS / 'baseline_enabled').write_text('0\n')
        csv_file(attempt / 'cpu-utilization.csv', cpu_difference(cpu_before, cpu_snapshot()))
        kernel = self.check_kernel(attempt / 'kernel.log')
        if status:
            raise RuntimeError(f'BaM 退出码 {status}：{attempt}')
        elapsed, iops, extra = benchmark_result(attempt / 'benchmark.log', 'author', point.qps, point.qd, ops)
        reserved_sqs, reserved_cqs = queue_inventory((attempt / 'benchmark.log').read_text(), point)
        csv_file(attempt / 'qid-assignment.csv', qid_assignment(kernel, point, reserved_sqs))
        self.telemetry(attempt / 'gpu-after.csv')
        row = asdict(point) | dict(point_id=point.key, kernel='author', stage=stage,
                  cache_mib=self.cache_mib,
                  reserved_sqs=reserved_sqs, reserved_cqs=reserved_cqs,
                  actual_qid_start=1, actual_qid_end=point.qps,
                  repeat=repeat, status='ok', logical_ops=ops, elapsed_us=elapsed,
                  logical_iops=iops, logical_gb_s=iops * point.block_bytes / 1e9,
                  logical_gib_s=iops * point.block_bytes / 2**30, extra_reads=extra,
                  extra_read_pct=100 * extra / ops,
                  source_working_set_upper_bytes=min(point.threads, DATA_SIZE // point.block_bytes) * point.block_bytes,
                  inferred_nvme_reads=ops + extra,
                  inferred_nvme_read_bytes=ops * point.block_bytes + extra * 512,
                  bar1_free_bytes=free, module_build_id=self.build_id(self.modules[
                      ('dsa2' if point.physical_dsa == 2 else 'weighted'
                       if point.dsa0_qps >= 0 else 'original'), profiling]),
                  gpu_numa=self.gpus[point.gpu]['numa_node'],
                  log=str((attempt / 'benchmark.log').relative_to(self.out)))
        if profiling:
            # A completed author kernel has no new commands. Wait for any sampled
            # CPU retirement callback to finish before taking the second snapshot.
            time.sleep(0.1)
            shutil.copyfile(latency_path, attempt / 'latency-after.csv')
            metrics = latency_difference(before, read_latency(attempt / 'latency-after.csv'))
            expected_devices = (point.physical_dsa,) if point.devices == 1 else (0, 2)
            for device in expected_devices:
                measured = {r['metric'] for r in metrics if r['physical_dsa'] == device}
                if measured != {'sq_sync', 'worker_wait', 'worker_reclaim'}:
                    raise RuntimeError(f'dsa{device} 缺少有效时延样本：{measured}')
            csv_file(folder / 'latency.csv', metrics)
        write_json(record, row)
        print(f'  {iops/1e6:.4f} MIOPS, {row["logical_gb_s"]:.4f} GB/s, {elapsed/1e6:.2f}s', flush=True)
        return row

    def restore(self):
        if not self.changed:
            return
        print('恢复开始前的原模块和运行参数；先排空，禁止强制卸载。', flush=True)
        log = self.out / 'restore.log'
        self.stop_stack(log)
        mutate(['insmod', self.original, *(f'{k}={v}' for k, v in self.original_parameters.items())], log)
        self.verify_module(self.original)
        mutate(['bash', 'scripts/load_bam.sh', VIRTUAL_BDF], log)
        check_controller(self.binary, int(self.original_parameters['dsa_num_devs']))
        actual = parameters()
        if actual != self.original_parameters:
            raise RuntimeError('恢复后的模块参数与开始前不符，详见 restore.log')
        self.check_kernel(self.out / 'restore-kernel.log')
        write_json(self.out / 'restored.json', {'parameters': actual, 'build_id': self.build_id(self.original)})
        print('已恢复原模块和运行参数。', flush=True)


def provenance():
    paths = [ROOT / 'Kbuild', ROOT / 'Makefile']
    for folder in ('src', 'nvmev', 'dsa', 'scripts', 'script/baseline'):
        paths.extend(p for p in (ROOT / folder).rglob('*')
                     if p.is_file() and p.suffix in ('.py', '.sh', '.c', '.h', '.cuh'))
    for name in ('benchmarks/block/main.cu', 'include/nvm_parallel_queue.h', 'include/page_cache.h'):
        paths.append(ROOT / 'bam' / name)
    cache_path = ROOT / 'bam/build/CMakeCache.txt'
    cache = {key: value for key, value in re.findall(r'^([^/#\n][^:\n]*):[^=\n]+=(.*)$',
                                                    cache_path.read_text() if cache_path.exists() else '', re.M)
             if key in CACHE_KEYS and value and not value.endswith('-NOTFOUND')}
    tools = {'gcc-12': 'gcc-12', 'cmake': 'cmake'}
    for key in ('CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER', 'CMAKE_CUDA_COMPILER', 'CMAKE_CUDA_HOST_COMPILER'):
        if cache.get(key):
            tools[key] = cache[key]
    return {'schema': SCHEMA, 'kernel_release': os.uname().release,
            'swarmio_revision': command(['git', 'rev-parse', 'HEAD']).strip(),
            'bam_revision': command(['git', '-C', 'bam', 'rev-parse', 'HEAD']).strip(),
            'bam_diff_sha256': hashlib.sha256(command(['git', '-C', 'bam', 'diff', '--binary', 'HEAD']).encode()).hexdigest(),
            'cmake_cache': cache,
            'tool_versions': {name: command([exe, '--version']) for name, exe in tools.items()},
            'build_environment': {key: os.environ.get(key, '') for key in
                                  ('CMAKE_ARGS', 'CUDA_MODULE_LOADING', 'CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER')},
            'source_sha256': {str(p.relative_to(ROOT)): modules.digest(p) for p in sorted(set(paths))}}


def archive(out):
    # Keep the complete reproducible dataset together, including logs/builds.
    archive_path = Path(str(out) + '.tar.gz')
    temporary = archive_path.with_suffix('.gz.tmp')
    with tarfile.open(temporary, 'w:gz') as file:
        file.add(out, arcname=out.name)
    temporary.replace(archive_path)
    return archive_path


def execute(args, out, points):
    previous_restore = out / 'restored.json'
    if previous_restore.exists():
        sessions = out / 'sessions'
        sessions.mkdir(exist_ok=True)
        previous_restore.rename(sessions / datetime.now().strftime('restored-%Y%m%d-%H%M%S-%f.json'))
    write_json(out / 'status.json', {'status': 'running', 'restored': False,
                                   'time': datetime.now().isoformat()})
    runner = Runner(args, out)
    result_status = 'failed'
    try:
        runner.preflight()
        points, skipped = plan(args)
        if {(p.scenario, p.block_bytes) for p in points} != {
                (s, b) for s in ('single', 'dual-equal', 'dual-contiguous', 'dual-weighted') for b in (512, 4096)}:
            raise RuntimeError('当前 cache/线程范围无法覆盖两种 READ 粒度；请保留 <=32768 的线程点，或扩大实际 BAR1 后使用 256 MiB cache')
        csv_file(out / 'plan.csv', [asdict(p) | {'point_id': p.key} for p in points])
        csv_file(out / 'skipped.csv', skipped)
        print(f'实际扫描 {len(points)} 个配置，每点 {args.repeats} 次；无额外预热、长测或首尾复测。', flush=True)
        runner.prepare_modules()

        def measure(point, stage, count, ops, profiling=False):
            for trial in range(1, count + 1):
                runner.trial(point, stage, trial, ops, profiling)

        # Group reloads, but shuffle points deterministically within each group.
        grouped = defaultdict(list)
        for p in points:
            grouped[(p.scenario, p.physical_dsa, p.dsa0_qps)].append(p)
        rng = random.Random(args.seed)
        for group in grouped.values():
            rng.shuffle(group)
            for point in group:
                measure(point, 'scan', args.repeats, args.ops_512 if point.block_bytes == 512 else args.ops_4096)
            summarize(out)
        scan = summarize(out)
        diagnostics = diagnostic_points(scan, args.gpus, args.topology_gpus)
        write_json(out / 'latency-plan.json', [asdict(p) for p in diagnostics])
        scanned = {p.key for p in points}
        # Finish normal measurements first, then group diagnostic-module loads.
        # This avoids switching normal/profile modules for every topology point.
        ordered = sorted(diagnostics, key=lambda p: (p.devices, p.physical_dsa,
                         p.scenario != 'dual-equal', p.dsa0_qps, p.gpu, p.block_bytes))
        for point in ordered:
            ops = args.ops_512 if point.block_bytes == 512 else args.ops_4096
            if point.key not in scanned:
                measure(point, 'topology-throughput', args.repeats, ops)
        for point in ordered:
            ops = args.ops_512 if point.block_bytes == 512 else args.ops_4096
            measure(point, 'latency', args.repeats, ops, True)
            summarize(out)
        result_status = 'complete'
    except KeyboardInterrupt:
        result_status = 'interrupted'
        raise
    finally:
        restored = False
        try:
            runner.restore()
            restored = True
        finally:
            summarize(out)
            write_json(out / 'status.json', {'status': result_status if restored else 'restore_failed',
                       'restored': restored, 'time': datetime.now().isoformat()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='load modules and execute; default only writes a plan')
    parser.add_argument('--quick', action='store_true', help='smoke test only; not publication data')
    parser.add_argument('--out', type=Path, help='new dataset directory or existing directory to resume')
    parser.add_argument('--gpus', type=integers, default=[0], help='GPUs for throughput Cartesian scans')
    parser.add_argument('--topology-gpus', type=integers, default=[0, 1], help='GPUs for fixed-geometry topology tests')
    parser.add_argument('--threads', type=integers, default=[8192, 16384, 32768, 65536])
    parser.add_argument('--qps', type=integers, default=[32, 64, 128, 256])
    parser.add_argument('--qds', type=integers, default=[256, 512, 1024])
    parser.add_argument('--weights', type=integers, default=[152, 156, 160])
    parser.add_argument('--cache-mib', type=lambda x: x if x == 'auto' else int(x), choices=('auto', 128, 256), default='auto',
                        help='auto: one fixed cache size per dataset, selected using actual BAR1 free space')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--ops-512', type=int, default=2**26)
    parser.add_argument('--ops-4096', type=int, default=2**24)
    parser.add_argument('--sample-every', type=int, default=64)
    parser.add_argument('--seed', type=int, default=20261125)
    parser.add_argument('--no-archive', action='store_true')
    parser.add_argument('--aggregate-only', action='store_true', help='regenerate tables without touching devices')
    args = parser.parse_args()
    if args.quick:
        args.threads, args.qps, args.qds, args.weights = [8192], [64, 256], [512, 1024], [156]
        args.repeats = 1
        args.ops_512 = args.ops_4096 = 2**20
    if (any(g < 0 for g in (*args.gpus, *args.topology_gpus)) or any(t < 64 or t & (t - 1) for t in args.threads)
            or any(q < 8 or q > 256 or q & (q - 1) for q in args.qps)
            or any(d < 2 or d > 1024 or d & (d - 1) for d in args.qds)
            or any(w < 4 or w > 252 or w % 4 for w in args.weights)
            or min(args.repeats, args.ops_512, args.ops_4096, args.sample_every) < 1):
        parser.error('线程/QP/QD 需要合法的 2 次幂；权重为 [4,252] 内 4 的倍数；计数为正')
    if not any(t >= 8192 for t in args.threads):
        parser.error('256 QP 加权测试至少需要 8192 线程')
    if 256 not in args.qps:
        parser.error('QP 列表必须包含 256，作为加权与连续均分的共同对照')
    if any(ops % max(args.threads) for ops in (args.ops_512, args.ops_4096)):
        parser.error('固定操作总数必须是最大线程数的整数倍')
    if Path.cwd().resolve() != ROOT:
        parser.error('请从 SwarmIO 项目目录执行，不需要 cd 或 HOME 路径')
    if args.run and os.geteuid() != 0:
        parser.error('正式执行需要 sudo bash script/baseline/run_paper_baseline.sh --run')
    os.environ['LC_ALL'] = 'C'
    out = (args.out or ROOT / 'results' / datetime.now().strftime('Baseline-%Y%m%d-%H%M%S')).resolve()
    if args.aggregate_only:
        if not out.exists() or not args.out:
            parser.error('--aggregate-only 必须指定现有 --out')
        summarize(out)
        print(f'已重新汇总 {out}')
        return
    points, skipped = plan(args)
    if not points or {p.scenario for p in points} != {'single', 'dual-equal', 'dual-contiguous', 'dual-weighted'}:
        parser.error('扫描范围无法覆盖所有测试场景')
    out.mkdir(parents=True, exist_ok=True)
    config = {k: v for k, v in vars(args).items() if k not in ('run', 'out', 'no_archive', 'aggregate_only')}
    config.update(schema=SCHEMA, kernel='author', data_mib=128, single_grid_physical_dsa=0,
                  batch_size=32, fault_exit=1, fault_poll_spins=4096,
                  gpu_block_threads=64, access='random-read', address_policy='fixed_random_address_per_thread',
                  timing_model='8 instances, 100 ns per 512 B, 49900 ns minimum delay',
                  cpus=CPU_LISTS)
    # JSON normalizes integer mapping keys; compare the normalized form on resume.
    config = json.loads(json.dumps(config))
    manifest = out / 'baseline.json'
    if manifest.exists() and json.loads(manifest.read_text()) != config:
        raise RuntimeError('续测配置不符，请使用完全相同的参数或新的 --out')
    write_json(manifest, config)
    csv_file(out / 'plan.csv', [asdict(p) | {'point_id': p.key} for p in points])
    csv_file(out / 'skipped.csv', skipped)
    print(f'Baseline 目录：{out}\n计划扫描 {len(points)} 个配置，每点实测 {args.repeats} 次，无额外预热、长测或首尾复测。\n'
          'cache=auto 的计划先按 256 MiB 生成；执行时按实际 BAR1 选择并固定 cache，重新保存有效矩阵。\n'
          'GPU1/远端只做固定几何拓扑对照；可用相同 --out 续测。', flush=True)
    if not args.run:
        print('仅生成计划，没有访问 GPU 或加载模块。正式执行需加 --run。')
        return
    lock_dir = ROOT / 'results/baseline'
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / '.bam-iops.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('已有 BaM 扫描运行') from error
        current = provenance()
        provenance_path = out / 'provenance.json'
        if provenance_path.exists() and json.loads(provenance_path.read_text()) != current:
            raise RuntimeError('续测内核或源码指纹已改变，请建立新的 Baseline 结果集')
        write_json(provenance_path, current)
        code = out / 'code'
        code.mkdir(exist_ok=True)
        for folder in ('script/baseline', 'scripts'):
            for path in (ROOT / folder).rglob('*'):
                if path.is_file() and path.suffix in ('.py', '.sh', '.c', '.h', '.cuh'):
                    destination = code / path.relative_to(ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
        shutil.copy2(ROOT / 'docs/paper-baseline-compact.zh-CN.md', out / 'README.zh-CN.md')
        (out / 'bam-working-tree.diff').write_text(command(['git', '-C', 'bam', 'diff', '--binary', 'HEAD']))
        (out / 'swarmio-working-tree.diff').write_text(command(['git', 'diff', '--binary', 'HEAD']))
        # Build before the first device mutation. Runtime selection is always author.
        build_benchmark()
        generated = BUILD
        shutil.copytree(generated, out / 'bam-build', dirs_exist_ok=True)
        global STOP

        def request_stop(_sig, _frame):
            global STOP
            STOP = True
            print('收到中断；等待当前控制操作或 I/O 排空后恢复原配置。', flush=True)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, request_stop)
        try:
            execute(args, out, points)
        finally:
            if not args.no_archive:
                print(f'结果归档：{archive(out)}', flush=True)
    print(f'Baseline 完成：{out}\n请提供归档；核心表为 summary.csv、best.csv、latency.csv。')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('已中断；保留已完成记录。确认 restored.json/status.json 后，用相同 --out 续测。') from None
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Baseline 停止：{error}') from error
