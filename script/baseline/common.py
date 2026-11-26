# SPDX-License-Identifier: GPL-2.0-only
"""Read-only environment checks and BAR1 accounting for the baseline."""
import os
from pathlib import Path
import re
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PARAMS = Path('/sys/module/swarmio/parameters')
VDEV = Path('/sys/bus/pci/devices/0001:10:00.0')
MIB = 1024**2
BAR1_RESERVE = 64 * MIB
DATA_SIZE = 128 * MIB
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


def check_controller(benchmark, dsa_devs):
    for name, expected in {'disp_using_dma': 'Y', 'worker_using_dma': 'Y', 'dsa_num_devs': str(dsa_devs)}.items():
        if (PARAMS / name).read_text().strip() != expected:
            raise RuntimeError(f'需要 {dsa_devs} 个 DSA 且两条 DMA 路径均已启用，预期 {name}={expected}')
    if ((VDEV / 'vendor').read_text().strip(), (VDEV / 'device').read_text().strip()) != ('0x0c51', '0x0110'):
        raise RuntimeError('虚拟设备的 PCI ID 应为 0c51:0110')
    if not (Path('/sys/class/misc/libnvm0/device').samefile(VDEV)
            and stat.S_ISCHR(Path('/dev/libnvm0').stat().st_mode)):
        raise RuntimeError('/dev/libnvm0 必须绑定本次 SwarmIO 虚拟设备')
    if int((PARAMS / 'memmap_size').read_text()) - 4 * 1024**3 < DATA_SIZE + 4096:
        raise RuntimeError('SwarmIO namespace 容量不足以覆盖测试地址范围及请求余量')
    if not os.access(benchmark, os.X_OK):
        raise RuntimeError(f'缺少可执行文件 {benchmark}')
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
