#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Measure BaM random-read IOPS on the currently loaded single-DSA baseline."""

import argparse
import csv
from datetime import datetime
import fcntl
import math
import os
from pathlib import Path
import re
import stat
from statistics import median
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PARAMS = Path('/sys/module/swarmio/parameters')
VDEV = Path('/sys/bus/pci/devices/0001:10:00.0')
MIB = 1024**2
BAR1_RESERVE = 64 * MIB
DATA_SIZE = 128 * MIB
THREADS = (256, 1024, 4096, 16384, 65536, 131072)
QUEUES = (8, 16, 32, 64, 128, 256)
KERNEL_ERROR = re.compile(
    r'Oops:|BUG:|kernel panic|general protection fault|blocked for more than|'
    r'NVRM:.*Xid|nvidia_p2p.*fail|DMAR:.*fault|'
    r'SwarmIO:.*(?:fail|error|timeout|timed out|disabling DMA|fallback)', re.I
)
CUDA_BDF_QUERY = '''
import ctypes as c
import sys

cuda = c.CDLL('libcuda.so.1')
dev = c.c_int()
bus = c.create_string_buffer(32)
for function, args in (
    (cuda.cuInit, (0,)),
    (cuda.cuDeviceGet, (c.byref(dev), int(sys.argv[1]))),
    (cuda.cuDeviceGetPCIBusId, (bus, len(bus), dev)),
):
    status = function(*args)
    if status:
        sys.exit(f'{function.__name__} failed: {status}')
print(bus.value.decode('ascii'))
'''


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', type=int, default=0, help='CUDA device ordinal (default: 0)')
    parser.add_argument('-l', '--block-size', type=int, choices=(512, 4096), default=512)
    parser.add_argument('--qd', type=int, choices=(2, 4, 8, 16, 32, 64, 128, 256, 512, 1024),
                        default=128, help='entries per NVMe queue (default: 128)')
    parser.add_argument('--cache-mib', type=int, default=64, help='GPU data cache (default: 64 MiB)')
    parser.add_argument('--max-threads', type=int, choices=THREADS, default=THREADS[-1])
    parser.add_argument('--max-queues', type=int, choices=QUEUES, default=QUEUES[-1])
    parser.add_argument('--reqs', type=int, default=256, help='requests per thread (default: 256)')
    parser.add_argument('--repeats', type=int, default=3, help='runs per point (default: 3)')
    parser.add_argument('--dry-run', action='store_true', help='show feasible points without issuing I/O')
    args = parser.parse_args()
    if args.gpu < 0 or min(args.cache_mib, args.reqs, args.repeats) < 1:
        parser.error('GPU must be nonnegative and cache, reqs and repeats must be positive')
    return args


def check_controller():
    for name, expected in {'disp_using_dma': 'Y', 'worker_using_dma': 'Y', 'dsa_num_devs': '1'}.items():
        if (PARAMS / name).read_text().strip() != expected:
            raise RuntimeError(f'需要单 DSA 且两条 DMA 路径均已启用，预期 {name}={expected}')
    if ((VDEV / 'vendor').read_text().strip(), (VDEV / 'device').read_text().strip()) != ('0x0c51', '0x0110'):
        raise RuntimeError('虚拟设备的 PCI ID 应为 0c51:0110')
    if not (Path('/sys/class/misc/libnvm0/device').samefile(VDEV)
            and stat.S_ISCHR(Path('/dev/libnvm0').stat().st_mode)):
        raise RuntimeError('/dev/libnvm0 必须绑定本次 SwarmIO 虚拟设备')
    if int((PARAMS / 'memmap_size').read_text()) - 4 * 1024**3 < DATA_SIZE + 4096:
        raise RuntimeError('SwarmIO namespace 容量不足以覆盖测试地址范围及请求余量')
    if not os.access(ROOT / 'bam/Release/bin/nvm-block-bench', os.X_OK):
        raise RuntimeError('缺少可执行文件 bam/Release/bin/nvm-block-bench')
    idle = subprocess.run(['pgrep', '-x', 'nvm-block-bench'], stdout=subprocess.DEVNULL)
    if idle.returncode != 1:
        raise RuntimeError('无法确认 BaM 空闲，请检查本机是否已有 nvm-block-bench 进程')


def bar1_bytes(gpu_bdf):
    output = subprocess.check_output(['nvidia-smi', '-i', gpu_bdf, '-q', '-d', 'MEMORY'], text=True)
    bar1 = re.search(r'^    BAR1 Memory Usage\s*\n((?:^ {8}[^\n]*\n)+)', output + '\n', re.M)
    if not bar1:
        raise RuntimeError(f'无法读取 GPU {gpu_bdf} 的 BAR1')
    counters = dict(re.findall(r'^\s*(Total|Free)\s*:\s*(\d+) MiB\s*$', bar1[1], re.M))
    if not {'Total', 'Free'} <= counters.keys():
        raise RuntimeError(f'GPU {gpu_bdf} 未报告有效的 BAR1 容量')
    total, free = int(counters['Total']) * MIB, int(counters['Free']) * MIB
    if not 0 <= free <= total or total == 0:
        raise RuntimeError(f'GPU {gpu_bdf} 的 BAR1 容量异常')
    return total, free


def mapped_bytes(cache_bytes, queues, qd):
    # The cache and each GPU SQ/CQ are mapped independently in 64 KiB units.
    granule = 64 * 1024
    cache = (cache_bytes + granule - 1) // granule * granule
    sq = (qd * 64 + granule - 1) // granule * granule
    cq = (qd * 16 + granule - 1) // granule * granule
    return cache + queues * (sq + cq)


def check_kernel(started, path):
    log = subprocess.check_output(['sudo', 'dmesg', '--since', started, '--color=never'], text=True)
    path.write_text(log)
    error = KERNEL_ERROR.search(log)
    if error:
        raise RuntimeError(f'内核日志出现异常信号 {error[0]}，详见 {path}')


def run_point(args, threads, queues, path, started, out):
    env = dict(os.environ, BAM_GPU=str(args.gpu), BAM_CACHE_SIZE=f'{args.cache_mib}M',
               BAM_DATA_SIZE='128M', BAM_REQS=str(args.reqs), BAM_QUEUE_DEPTH=str(args.qd), BAM_RANDOM='true')
    command = ['bash', 'scripts/run_bam-block-bench.sh', '-j', str(threads), '-q', str(queues),
               '-l', str(args.block_size)]
    with path.open('w') as output:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    check_kernel(started, out / 'kernel.log')
    if result.returncode:
        raise RuntimeError(f'BaM 退出码 {result.returncode}，详见 {path}')

    log = path.read_text(errors='replace')
    qp = re.search(r'SQs:\s*(\d+)\s+CQs:\s*(\d+)\s+n_qps:\s*(\d+)', log)
    ops = re.search(r'Number of Ops:\s*(\d+)', log)
    rate = re.search(r'Ops/sec:\s*(\S+)', log)
    try:
        iops = float(rate[1]) if rate else 0.0
    except ValueError:
        iops = 0.0
    if (not qp or int(qp[3]) != queues or not ops or int(ops[1]) != threads * args.reqs
            or not math.isfinite(iops) or iops <= 0):
        raise RuntimeError(f'实际 QP、完成请求数或 IOPS 与预期不符，详见 {path}')
    return min(int(qp[1]), int(qp[2])), iops


def sweep(args, gpu_bdf, out):
    cache_bytes = args.cache_mib * MIB
    started = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    queue_limit = args.max_queues
    best = None
    feasible = 0
    if not args.dry_run:
        check_kernel(started, out / 'kernel.log')
    with (out / 'runs.csv').open('w', newline='', buffering=1) as runs, \
            (out / 'summary.csv').open('w', newline='', buffering=1) as summary:
        trials = csv.writer(runs)
        results = csv.writer(summary)
        trials.writerow(['block_bytes', 'threads', 'qps', 'qd', 'run', 'bar1_free_mib',
                         'estimated_mapped_mib', 'status', 'logical_iops', 'log'])
        results.writerow(['block_bytes', 'threads', 'qps', 'qd', 'completed_runs',
                          'median_logical_iops', 'min_logical_iops', 'max_logical_iops'])
        for threads in THREADS:
            if threads > min(args.max_threads, cache_bytes // args.block_size):
                break
            for queues in QUEUES:
                if queues > min(queue_limit, threads // 32):
                    break
                point = (args.block_size, threads, queues, args.qd)
                estimate = mapped_bytes(cache_bytes, queues, args.qd)
                measurements = []
                for trial in range(1, (1 if args.dry_run else args.repeats) + 1):
                    _, free = bar1_bytes(gpu_bdf)
                    row = (*point, trial, free // MIB, estimate / MIB)
                    label = f'{threads} 线程 / {queues} QP / QD {args.qd}'
                    if estimate + BAR1_RESERVE > free:
                        trials.writerow([*row, 'skip_bar1', '', ''])
                        needed = (estimate + BAR1_RESERVE) / MIB
                        print(f'跳过 {label}，BAR1 剩余 {free // MIB} MiB，需 {needed:g} MiB（含余量）')
                        break
                    if args.dry_run:
                        trials.writerow([*row, 'planned', '', ''])
                        feasible += 1
                        print(f'可测 {label}，预计映射 {estimate / MIB:g} MiB')
                        continue
                    path = out / f't{threads}-q{queues}-qd{args.qd}-run{trial}.log'
                    print(f'运行 {label}，第 {trial}/{args.repeats} 轮，BAR1 剩余 {free // MIB} MiB', flush=True)
                    try:
                        cap, iops = run_point(args, threads, queues, path, started, out)
                    except (OSError, RuntimeError, subprocess.CalledProcessError):
                        trials.writerow([*row, 'failed', '', path.name])
                        raise
                    queue_limit = min(queue_limit, cap)
                    trials.writerow([*row, 'ok', iops, path.name])
                    measurements.append(iops)
                if len(measurements) == args.repeats:
                    middle = median(measurements)
                    results.writerow([*point, len(measurements), middle, min(measurements), max(measurements)])
                    if best is None or middle > best[0]:
                        best = (middle, label)
    if args.dry_run and feasible:
        return
    if best is None:
        raise RuntimeError('没有完整完成的测试组合，请检查 runs.csv 中的 BAR1 或缓存限制')
    print(f'本次扫描最高中位数 {best[0]:,.0f} 逻辑 IOPS（{best[1]}）')


def main():
    args = options()
    os.environ['LC_ALL'] = 'C'
    results = ROOT / 'results/baseline'
    results.mkdir(parents=True, exist_ok=True)
    with (results / '.bam-iops.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('已有 BaM 扫描进程') from error
        check_controller()
        subprocess.run(['sudo', '-v'], check=True)
        gpu_bdf = subprocess.check_output(['sudo', 'python3', '-c', CUDA_BDF_QUERY, str(args.gpu)], text=True).strip()
        total, free = bar1_bytes(gpu_bdf)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        out = results / f'single-dsa-iops-{timestamp}'
        out.mkdir()
        info = (f'GPU {args.gpu} ({gpu_bdf}), BAR1 {free // MIB}/{total // MIB} MiB free, '
                f'reserve {BAR1_RESERVE // MIB} MiB\n{args}\n')
        for name in ('num_service_units', 'num_sched_insts', 'read_sched_delay', 'io_unit_shift', 'cpus'):
            value = (PARAMS / name).read_text().strip()
            info += f'{name}={value}\n'
        (out / 'config.txt').write_text(info)
        print(info, end='')
        print(f'结果目录 {out}', flush=True)
        sweep(args, gpu_bdf, out)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('已请求停止扫描，请检查 BaM 进程及排空状态。') from None
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'扫描停止：{exc}') from exc
