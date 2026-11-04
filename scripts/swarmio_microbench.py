#!/usr/bin/env python3
"""随机读微基准：BaM 同步提交，对比 CoPilotIO 每线程批量提交。

复用 swarmio_bam.py 的构建记录、加载状态检查、监控和结果归档。
默认两轮各 1,048,576 次 512 B READ；不自动构建、加载或修改 hugepages。
"""
import argparse
import json
from pathlib import Path
import shlex
import sys

import swarmio_bam as pipeline
from swarmio_monitor import interval_value


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build-dir', type=Path, required=True)
    p.add_argument('--threads', type=int, default=512)
    p.add_argument('--reqs', type=int, default=2048)
    p.add_argument('--batch-size', type=int, default=8)
    p.add_argument('--pages', type=int, help='默认 threads * min(batch-size,reqs)，两引擎分配相同页数')
    p.add_argument('--num-queues', type=int, default=16)
    p.add_argument('--queue-depth', type=int, default=512)
    p.add_argument('--page-size', type=int, choices=(512, 4096), default=512)
    p.add_argument('--num-blks', type=int, help='以 page-size 为单位；默认覆盖 1 GiB 地址范围')
    p.add_argument('--doorbell-mode', choices=('mmio', 'relay'), default='mmio')
    p.add_argument('--copilot-base-core', type=int, default=0)
    p.add_argument('--copilot-cores', type=int, default=4)
    p.add_argument('--monitor-interval', type=interval_value)
    p.add_argument('--bar-latency', action='store_true')
    p.add_argument('--dry-run', action='store_true', help='只检查负载参数并显示命令，不访问设备')
    return p.parse_args(argv)


def command(args):
    pages = args.pages if args.pages is not None else args.threads * min(args.batch_size, args.reqs)
    blocks = args.num_blks if args.num_blks is not None else (1 << 30) // args.page_size
    result = ['--build-dir', str(args.build_dir), 'compare',
              '--threads', str(args.threads), '--reqs', str(args.reqs),
              '--batch-size', str(args.batch_size), '--pages', str(pages),
              '--num-queues', str(args.num_queues), '--queue-depth', str(args.queue_depth),
              '--page-size', str(args.page_size), '--num-blks', str(blocks),
              '--doorbell-mode', args.doorbell_mode,
              '--copilot-base-core', str(args.copilot_base_core), '--copilot-cores', str(args.copilot_cores)]
    if args.monitor_interval is not None:
        result += ['--monitor-interval', str(args.monitor_interval)]
    if args.bar_latency:
        result += ['--bar-latency']
    return result


def main(argv=None):
    args = arguments(argv)
    forwarded = command(args)
    # Validate without GPU/kernel operations, including before --dry-run output.
    manifest = args.build_dir / 'manifest.json'
    config = (json.loads(manifest.read_text())['config'] if manifest.exists()
              else pipeline.read_config(pipeline.ROOT / 'configs/bam-cpu.json'))
    bench = dict(config['benchmark'])
    bench.update(threads=args.threads, reqs=args.reqs, batch_size=args.batch_size,
                 pages=args.pages if args.pages is not None else args.threads * min(args.batch_size, args.reqs),
                 num_queues=args.num_queues, queue_depth=args.queue_depth, page_size=args.page_size,
                 num_blks=args.num_blks if args.num_blks is not None else (1 << 30) // args.page_size)
    pipeline.validate_benchmark(bench, config)
    print(f'BaM: sync, batch=1；CoPilotIO: {"batch" if args.batch_size > 1 else "sync"}, '
          f'batch={args.batch_size}；每轮 {args.threads * args.reqs} 次 READ', flush=True)
    if args.dry_run:
        print(shlex.join(['sudo', 'python3', 'scripts/swarmio_bam.py', *forwarded]))
        return 0
    return pipeline.main(forwarded)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (pipeline.Error, OSError, ValueError, KeyError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
