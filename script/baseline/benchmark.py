# SPDX-License-Identifier: GPL-2.0-only
"""Build and execute the author BaM workload in an isolated directory."""
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess

from common import ROOT

DIRECTORY = Path(__file__).resolve().parent
BAM = ROOT / "bam"
BUILD = ROOT / "results/baseline/build-author-poll4096"

MODULE_PARAMETERS = (
    'dma_batch_size', 'dsa_wq_sizes', 'dsa_wq_priorities',
    'num_dma_descs_per_worker', 'num_dma_descs_per_disp',
    'dsa_num_devs', 'dsa_num_grps_per_dev', 'dsa_num_wqs_per_grp',
    'dsa_num_engs_per_grp', 'num_service_units', 'num_workers_per_service_unit',
    'num_sched_insts', 'read_sched_delay', 'read_min_delay', 'io_unit_shift',
    'cpus', 'memmap_start', 'memmap_size', 'disp_using_dma', 'worker_using_dma',
)
CACHE_KEYS = (
    'CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER', 'CMAKE_CUDA_COMPILER',
    'CMAKE_CUDA_HOST_COMPILER', 'CMAKE_CUDA_ARCHITECTURES',
    'CUDA_TOOLKIT_ROOT_DIR', 'CUDA_NVCC_EXECUTABLE', 'cuda_cccl_include', 'no_cc',
)


def author_source(source):
    """Add host validation/reporting without replacing any author GPU kernel."""
    if 'settings.numQueues, swarmioFaultExit);' not in source:
        raise RuntimeError('需要含 SwarmIO 故障退出路径的 BaM，例如服务器已验证的 2a08f65')
    edits = (
        ('int main(int argc, char** argv) {',
         '#include "benchmark_checks.cuh"\n\nint main(int argc, char** argv) {'),
        ('        std::vector<Controller*> ctrls(settings.n_ctrls);',
         '''        if (!swarmioFaultExit || settings.n_ctrls != 1 || !settings.random ||
            settings.accessType != READ || (settings.pageSize != 512 && settings.pageSize != 4096) ||
            settings.blkSize != 64)
            throw error("baseline requires SwarmIO fault exit, one controller and 512 B or 4 KiB random READs");
        std::vector<Controller*> ctrls(settings.n_ctrls);'''),
        ('        //auto dma = createDma(ctrl.ctrl,',
         '''        require_queue_geometry(*ctrls[0], settings.numQueues, settings.queueDepth);
        if (ctrls[0]->blk_size != 512)
            throw error("baseline requires a 512 B namespace LBA");
        std::cout << "Compare kernel: author" << '\\n';

        //auto dma = createDma(ctrl.ctrl,'''),
        ('        h_pc.print_reset_stats();',
         '        std::cout << "Extra READs: " << read_extra_count(h_pc) << std::endl;'),
    )
    for before, after in edits:
        if source.count(before) != 1:
            raise RuntimeError(f'BaM 主程序与基线构建不匹配：{before.strip()}')
        source = source.replace(before, after, 1)
    return source


def fault_queue_source(source):
    before = '(++spins & 255) == 0'
    if source.count(before) != 1:
        raise RuntimeError('BaM 故障轮询源码不匹配，需要含故障退出路径的版本')
    return source.replace(before, '(++spins & 4095) == 0', 1)


def build_benchmark():
    """Build only the original workload with the tested 4096-spin fault polling."""
    BUILD.mkdir(parents=True, exist_ok=True)
    manifest = BUILD / 'benchmark.json'
    manifest.unlink(missing_ok=True)
    source = author_source((BAM / 'benchmarks/block/main.cu').read_text())
    include = '#include <nvm_parallel_queue.h>'
    if source.count(include) != 1 or source.index(include) > source.index('#include <page_cache.h>'):
        raise RuntimeError('无法在 page_cache.h 之前接入故障轮询头文件')
    source = source.replace(include, '#include "baseline_queue.cuh"', 1)
    (BUILD / 'baseline_main.cu').write_text(source)
    (BUILD / 'baseline_queue.cuh').write_text(fault_queue_source((BAM / 'include/nvm_parallel_queue.h').read_text()))
    shutil.copy2(DIRECTORY / 'benchmark_checks.cuh', BUILD / 'benchmark_checks.cuh')
    hook = BUILD / 'baseline.cmake'
    hook.write_text('''cmake_language(DEFER CALL set_property TARGET block-benchmark-module PROPERTY
    SOURCES "${CMAKE_BINARY_DIR}/baseline_main.cu")
cmake_language(DEFER CALL target_include_directories block-benchmark-module PRIVATE
    "${CMAKE_SOURCE_DIR}/benchmarks/block")
''')
    cache = {}
    cache_path = BAM / 'build/CMakeCache.txt'
    if cache_path.is_file():
        cache = {key: value for key, value in re.findall(r'^([^/#\n][^:\n]*):[^=\n]+=(.*)$', cache_path.read_text(), re.M)
                 if key in CACHE_KEYS and value and not value.endswith('-NOTFOUND')}
    cache.setdefault('CMAKE_CUDA_ARCHITECTURES', '120')
    args = [f'-D{k}={v}' for k, v in cache.items()]
    args.extend(shlex.split(os.environ.get('CMAKE_ARGS', '')))
    args.extend(('-Dno_module=ON', '-Dno_smartio=ON', '-DCMAKE_BUILD_TYPE=Release',
                 f'-DCMAKE_PROJECT_libnvm_INCLUDE={hook}'))
    subprocess.run(['cmake', '-S', str(BAM), '-B', str(BUILD), *args], check=True)
    subprocess.run(['cmake', '--build', str(BUILD), '--parallel', '4', '--target', 'block-benchmark-module'], check=True)
    binary = BUILD / 'bin/nvm-block-bench'
    if not os.access(binary, os.X_OK):
        raise RuntimeError(f'未生成基线程序：{binary}')
    revision = subprocess.check_output(['git', '-C', str(BAM), 'rev-parse', 'HEAD'], text=True).strip()
    manifest.write_text(json.dumps({'bam_revision': revision, 'kernel': 'author',
                                    'cq_loads': 'original', 'cq_lock': 'original',
                                    'fault_exit': 1, 'fault_poll_spins': 4096}, indent=2) + '\n')
    print(f'Author 基线程序：{binary}；原始 CQ/锁，故障轮询间隔 4096', flush=True)


def run_benchmark(command, output):
    interrupted = False

    def stop_requested(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    previous = {sig: signal.signal(sig, stop_requested) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if interrupted:
            raise KeyboardInterrupt
        with output.open('w') as log, subprocess.Popen(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        ) as process:
            while process.poll() is None:
                if interrupted:
                    print('请求 BaM 排空，等待程序退出', flush=True)
                    process.send_signal(signal.SIGTERM)
                    process.wait()
                    break
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
            if interrupted:
                raise KeyboardInterrupt
            return process.returncode
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def benchmark_result(path, kernel, queues, depth, expected_ops):
    if kernel != 'author':
        raise RuntimeError('本基线只接受 author kernel')
    log = path.read_text(errors='replace')
    geometry = re.search(r'^Actual QPs: (\d+) SQD: (\d+) CQD: (\d+)$', log, re.M)
    protocol = re.search(r'^Compare kernel: (\S+)$', log, re.M)
    ops = re.search(r'Elapsed Time: (\S+)\s+Number of Ops: (\d+)', log)
    rate = re.search(r'^Ops/sec: (\S+)', log, re.M)
    extra = re.search(r'^Extra READs: (\d+)$', log, re.M)
    if (not all((geometry, protocol, ops, rate, extra)) or protocol[1] != kernel
            or tuple(map(int, geometry.groups())) != (queues, depth, depth)
            or int(ops[2]) != expected_ops):
        raise RuntimeError(f'kernel、实际队列配置或请求数与预期不符，详见 {path}')
    elapsed, iops = float(ops[1]), float(rate[1])
    extra_reads = int(extra[1])
    if (not all(math.isfinite(v) and v > 0 for v in (elapsed, iops))
            or not math.isclose(iops, expected_ops * 1e6 / elapsed, rel_tol=0.001)):
        raise RuntimeError(f'计时、IOPS 或额外 READ 计数异常，详见 {path}')
    return elapsed, iops, extra_reads
