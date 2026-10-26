#!/usr/bin/env python3
"""SwarmIO + BaM 的统一入口：doctor/build/load/run/status/unload。

仅支持当前 CPU full-I/O、512 B 基线。所有实现来自仓库源码，不生成源码补丁。
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PCI = Path('/sys/bus/pci/devices')
MODULES = Path('/sys/module')
ARTIFACTS = ('swarmio/swarmio.ko', 'libnvm/libnvm.ko',
             'bam/lib/libnvm.so', 'bam/bin/nvm-block-bench')


class Error(RuntimeError):
    pass


def output(command, **kwargs):
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def parse_size(value):
    match = re.fullmatch(r'(\d+)([KMG]?)', str(value), re.I)
    if not match:
        raise Error('无效内存大小：' + str(value))
    return int(match[1]) << (10 * ('', 'K', 'M', 'G').index(match[2].upper()))


def cpu_set(value):
    result = set()
    for group in str(value).split(','):
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', group)
        if not match:
            raise Error('无效 CPU 列表：' + str(value))
        first, last = int(match[1]), int(match[2] or match[1])
        if last < first:
            raise Error('CPU 范围倒置')
        result.update(range(first, last + 1))
    return result


def read_symvers(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if len(fields) >= 4:
            result[fields[1]] = {'crc': int(fields[0], 16), 'export': fields[3]}
    return result


def validate_sources(root):
    """兼容实现必须已在 BaM 源文件中；此处不改写源码或符号表。"""
    root = Path(root)
    files = {name: (root / 'bam/module' / name).read_text()
             for name in ('ctrl.h', 'ctrl.c', 'pci.c')}
    if 'struct miscdevice' not in files['ctrl.h'] or 'misc_register(' not in files['ctrl.c']:
        raise Error('BaM 子模块尚未包含 miscdevice 兼容，请检查其提交版本')
    forbidden = r'\b(?:class_create|class_destroy|device_create|device_destroy|cdev_add|cdev_del|cdev_init)\s*\('
    if any(re.search(forbidden, text) for text in files.values()):
        raise Error('BaM 仍引用旧 cdev/class 注册接口')
    return files


def validate_benchmark(bench, config):
    for name in ('threads', 'blk_size', 'reqs', 'pages', 'page_size', 'num_blks', 'queue_depth', 'num_queues'):
        if type(bench[name]) is not int or bench[name] <= 0:
            raise Error(name + ' 必须为正整数')
    if bench['page_size'] != 512 or bench['blk_size'] != 64 or bench['threads'] % 64:
        raise Error('当前基线仅支持 512 B、block=64、线程数为 64 的倍数')
    depth = bench['queue_depth']
    if depth < 2 or depth > 1024 or depth & (depth - 1):
        raise Error('queue_depth 必须是 2..1024 内的 2 的幂')
    if bench['num_queues'] > int(config['build']['NR_MAX_IO_QUEUE']):
        raise Error('队列数超过 SwarmIO 编译上限')
    if type(bench['random']) is not bool or type(bench['access_type']) is not int or bench['access_type'] not in (0, 1):
        raise Error('仅支持顺序/随机读或写，不支持混合负载')
    capacity = parse_size(config['module']['memmap_size']) - (4 << 30)
    if bench['num_blks'] * 512 > capacity or bench['threads'] > bench['num_blks']:
        raise Error('访问范围超过虚拟 namespace 容量或不足以容纳顺序线程起始 LBA')


def read_config(path):
    config = json.loads(Path(path).read_text())
    build, params = config['build'], config['module']
    required = {'CONFIG_SWARMIO_WORKER_SKIP_IO': 'n', 'CONFIG_SWARMIO_DMA_DISPATCHER': 'n',
                'CONFIG_SWARMIO_DMA_WORKER': 'n', 'CONFIG_SWARMIO_COMMIT': 'n',
                'CONFIG_SWARMIO_DISP_ALLOC_WORKER_RR': 'n',
                'CONFIG_SWARMIO_SQ_DIAGNOSTICS': 'y', 'CONFIG_SWARMIO_QUEUE_DIAGNOSTICS': 'y'}
    if any(build.get(k) != v for k, v in required.items()):
        raise Error('当前入口要求 CPU full-I/O / single-owner / 诊断开启配置')
    if not 1 < int(build['NR_WORKER_QUEUE_SIZE']) <= 65535:
        raise Error('worker 本地队列必须能由 16 位索引表示，范围为 2..65535')
    if not 1 <= int(build['NR_MAX_IO_QUEUE']) <= 64 or not 1 <= params['qdiag_qid'] <= int(build['NR_MAX_IO_QUEUE']):
        raise Error('当前入口支持最多 64 个 I/O 队列，诊断 QID 必须在范围内')
    if params['disp_using_dma'] or params['worker_using_dma'] or params['io_unit_shift'] != 9:
        raise Error('运行参数必须关闭 DSA，并使用 512 B 时序单位')
    if params['num_service_units'] <= 0 or params['num_workers_per_service_unit'] <= 0:
        raise Error('服务单元和 worker 数必须为正数')
    needed = params['num_service_units'] * (1 + params['num_workers_per_service_unit'])
    if len(cpu_set(params['cpus'])) != needed:
        raise Error('cpus 必须为每个 dispatcher/worker 各分配一个 CPU')
    if parse_size(params['memmap_size']) <= 4 << 30:
        raise Error('预留内存必须大于内部区域占用的 4 GiB')
    validate_benchmark(config['benchmark'], config)
    return config


def linux_only(root=False):
    if not sys.platform.startswith('linux'):
        raise Error('请在目标 Linux 服务器执行；本机不编译或加载 Linux 内核模块')
    if root and os.geteuid() != 0:
        raise Error('此子命令需要 sudo')


def find_nvcc(cuda_root=None):
    """显式选择工具链，避免 sudo 的 secure_path 隐藏 nvcc 或误选其他 CUDA 版本。"""
    if cuda_root is not None:
        candidates = [Path(cuda_root) / 'bin/nvcc']
    else:
        found = shutil.which('nvcc')
        candidates = [Path(found)] if found else []
        for name in ('CUDA_HOME', 'CUDA_PATH'):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name]) / 'bin/nvcc')
        candidates.append(Path('/usr/local/cuda/bin/nvcc'))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise Error('找不到可执行 nvcc；请在 doctor/build 后传入 --cuda-root /usr/local/cuda-12.4（按实际版本替换）')


def require_readable_file(path):
    """实际打开输入；路径某一级不可遍历时 Path.is_file 也可能直接抛 PermissionError。"""
    try:
        with Path(path).open('rb') as stream:
            stream.read(1)
    except PermissionError as exc:
        raise Error('没有权限读取构建输入：' + str(path) +
                    '；请用 sudo namei -l 检查该路径各级目录权限，并为构建用户开放必要的读取/遍历权限。'
                    '只做环境检查可使用 sudo python3 scripts/swarmio_bam.py doctor --cuda-root <CUDA目录>') from exc
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise Error('缺少构建输入文件：' + str(path)) from exc


def gpu_info(selector):
    if not re.fullmatch(r'\d+|GPU-[A-Fa-f0-9-]+', str(selector)):
        raise Error('gpu 必须是单个 nvidia-smi index 或 GPU UUID')
    fields = 'index,name,uuid,pci.bus_id,compute_cap,driver_version'
    row = next(csv.reader([output(['nvidia-smi', '--id=' + str(selector),
                                  '--query-gpu=' + fields, '--format=csv,noheader,nounits'])]))
    if len(row) != 6:
        raise Error('无法读取目标 GPU 的唯一信息')
    result = dict(zip(('index', 'name', 'uuid', 'pci', 'compute_cap', 'driver'), map(str.strip, row)))
    if not re.fullmatch(r'\d+\.\d+', result['compute_cap']):
        raise Error('无法确定 CUDA 架构，请检查 nvidia-smi 输出')
    result['cuda_arch'] = result['compute_cap'].replace('.', '')
    return result


def cmake_info():
    """只查询一次，验证和 manifest 使用相同的版本输出。显式 CUDA 架构无需 native。"""
    text = output(['cmake', '--version'])
    match = re.search(r'cmake version (\d+)\.(\d+)', text)
    if not match or tuple(map(int, match.groups())) < (3, 22):
        raise Error('需要 CMake >= 3.22')
    return text


def environment(args, config):
    linux_only()
    tools = ('git', 'cmake', 'make', 'cc', 'nvidia-smi', 'nm', 'objcopy', 'modinfo',
             'modprobe', 'insmod', 'rmmod', 'udevadm', 'ldd', 'stdbuf')
    missing = [name for name in tools if not shutil.which(name)]
    if missing:
        raise Error('缺少工具：' + ', '.join(missing))
    nvcc = find_nvcc(args.cuda_root)
    cmake = cmake_info()
    release = os.uname().release
    kernel = Path('/lib/modules') / release / 'build'
    driver_text = Path('/proc/driver/nvidia/version').read_text()
    match = re.search(r'Kernel Module\s+(\d+\.\d+(?:\.\d+)?)', driver_text)
    if not match:
        raise Error('无法确定当前加载的 NVIDIA 驱动版本')
    version = match[1]
    symvers = (args.nvidia_symvers or Path('/var/lib/dkms/nvidia') / version / release /
               os.uname().machine / 'module/Module.symvers').resolve()
    include = (args.nvidia_include or Path('/usr/src') / ('nvidia-' + version) / 'nvidia').resolve()
    input_errors = []
    for p in (kernel / 'Makefile', kernel / 'Module.symvers', symvers, include / 'nv-p2p.h'):
        try:
            require_readable_file(p)
        except Error as exc:
            input_errors.append(str(exc))
    if input_errors:
        raise Error('\n'.join(input_errors))
    for path in (ROOT, args.build_dir, kernel, symvers, include):
        if not re.fullmatch(r'[/A-Za-z0-9_.+\-]+', str(path)):
            raise Error('构建路径不能包含空格或 Makefile 特殊字符：' + str(path))
    if output(['modinfo', '-F', 'version', 'nvidia']) != version:
        raise Error('磁盘上的 NVIDIA 驱动版本与已加载版本不同，请先完成驱动切换')
    gpu = gpu_info(args.gpu or config['gpu'])
    if gpu['driver'] != version:
        raise Error('nvidia-smi 与内核 NVIDIA 驱动版本不一致')
    if 'sm_' + gpu['cuda_arch'] not in output([str(nvcc), '--list-gpu-code']).split():
        raise Error('当前 CUDA 工具链不支持目标 GPU 架构 sm_' + gpu['cuda_arch'])
    nvtable, ktable = read_symvers(symvers), read_symvers(kernel / 'Module.symvers')
    if 'nvidia_p2p_get_pages' not in nvtable:
        raise Error('所选 NVIDIA DKMS 符号表没有 P2P 导出')
    if any(ktable.get(name, {}).get('export') != 'EXPORT_SYMBOL'
           for name in ('misc_register', 'misc_deregister')):
        raise Error('目标内核没有所需的非 GPL-only miscdevice 导出')
    validate_sources(ROOT)
    expected = 'memmap=' + str(config['module']['memmap_size']) + '$' + str(config['module']['memmap_start'])
    online = cpu_set(Path('/sys/devices/system/cpu/online').read_text().strip())
    return {'kernel': release, 'kernel_build': str(kernel.resolve()), 'nvidia_version': version,
            'nvidia_symvers': str(symvers), 'nvidia_include': str(include),
            'nvidia_symvers_sha256': digest(symvers), 'gpu': gpu,
            'boot_reservation': expected, 'boot_reservation_ready': expected in Path('/proc/cmdline').read_text().split(),
            'service_cpus_online': cpu_set(config['module']['cpus']) <= online,
            'nvcc_path': str(nvcc), 'cuda_root': str(nvcc.parent.parent),
            'nvcc': output([str(nvcc), '--version']), 'cmake': cmake}


def source_inventory(root):
    paths = output(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).split('\0')
    return {name: digest(root / name) for name in sorted(set(paths)) if name and (root / name).is_file()
            and ((root / name).suffix in {'.c', '.h', '.cu', '.cuh', '.cpp', '.py', '.in', '.json'}
                 or (root / name).name in {'Makefile', 'Kbuild', 'CMakeLists.txt'})}


def audit_libnvm(module, kernel_symbols, nvidia_symbols):
    undefined = {line.split()[-1] for line in output(['nm', '-u', str(module)]).splitlines() if line.split()}
    forbidden = sorted(name for name in undefined if 'GPL' in kernel_symbols.get(name, {}).get('export', ''))
    if forbidden:
        raise Error('libnvm 仍引用 GPL-only 符号：' + ', '.join(forbidden))
    imported = {}
    for line in output(['modprobe', '--show-modversions', str(module)]).splitlines():
        fields = line.split()
        if len(fields) == 2:
            imported[fields[1]] = int(fields[0], 16)
    names = sorted(name for name in undefined if name.startswith('nvidia_p2p_'))
    if 'nvidia_p2p_get_pages' not in names:
        raise Error('libnvm 没有编入 NVIDIA P2P 支持')
    for name in names:
        if name not in nvidia_symbols or imported.get(name) != nvidia_symbols[name]['crc']:
            raise Error('NVIDIA P2P 导入 CRC 不匹配：' + name)
    return {name: hex(imported[name]) for name in names}


def check_library(binary, library):
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = str(library.parent) + (os.pathsep + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    text = output(['ldd', str(binary)], env=env)
    match = re.search(r'libnvm\.so\s+=>\s+(\S+)', text)
    if not match or Path(match[1]).resolve() != library.resolve() or 'not found' in text:
        raise Error('benchmark 没有链接本次构建的 libnvm.so：\n' + text)
    return text


def build(args, config):
    env = environment(args, config)
    out = args.build_dir
    if args.jobs < 1:
        raise Error('jobs 必须为正数')
    if out.exists() or out == ROOT or out in ROOT.parents or out.is_relative_to(ROOT / 'bam'):
        raise Error('构建目录必须是一个新目录，不能覆盖源码或位于 bam 子模块内')
    out.mkdir(parents=True)
    manifest = {'state': 'building', 'config': config, 'environment': env, 'commands': [],
                'source_transformations': False, 'source_sha256': {
                    'swarmio': source_inventory(ROOT), 'bam': source_inventory(ROOT / 'bam')},
                'git': {name: {'commit': output(['git', '-C', str(path), 'rev-parse', 'HEAD']),
                               'status': output(['git', '-C', str(path), 'status', '--short'])}
                        for name, path in [('swarmio', ROOT), ('bam', ROOT / 'bam')]}}

    def execute(command):
        manifest['commands'].append(list(map(str, command)))
        save_json(out / 'manifest.json', manifest)
        print('执行：', ' '.join(map(str, command)), flush=True)
        with (out / 'build.log').open('a') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                print(line, end='', flush=True)
                log.write(line)
            if process.wait():
                raise Error('构建失败；日志：' + str(out / 'build.log'))

    try:
        # 只复制源文件和 Kbuild，不带入对象文件或旧生成文件，不改动复制后的 C/CUDA 内容。
        source = out / 'swarmio/source'
        source.mkdir(parents=True)
        for directory in ('src', 'nvmev', 'dsa'):
            destination = source / directory
            destination.mkdir()
            for p in (ROOT / directory).iterdir():
                if p.suffix in ('.c', '.h'):
                    shutil.copy2(p, destination / p.name)
        shutil.copy2(ROOT / 'Kbuild', source / 'Kbuild')
        command = ['make', '-C', env['kernel_build'], 'M=' + str(source), '-j' + str(args.jobs),
                   'CONFIG_DEBUG_INFO_BTF_MODULES=n', 'KBUILD_EXTRA_SYMBOLS=']
        command += [key + '=' + str(value) for key, value in config['build'].items()]
        execute(command + ['modules'])
        swarmio = out / ARTIFACTS[0]
        execute(['objcopy', '--remove-section=.BTF', '--remove-section=.BTF.ext', str(source / 'swarmio.ko'), str(swarmio)])
        for field in ('swarmio_skip_io', 'swarmio_sq_diag', 'swarmio_queue_diag'):
            expected = '0' if field == 'swarmio_skip_io' else '1'
            if output(['modinfo', '-F', field, str(swarmio)]) != expected:
                raise Error('SwarmIO 编译模式不符：' + field)
        if re.search(r'\b(?:idxd_\w+|dsa_bus_type)\b', output(['nm', '-u', str(swarmio)])):
            raise Error('CPU SwarmIO 仍然依赖 idxd 私有符号')
        execute(['cmake', '-S', str(ROOT / 'bam'), '-B', str(out / 'bam'),
                 '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CUDA_ARCHITECTURES=' + env['gpu']['cuda_arch'],
                 '-DCMAKE_CUDA_COMPILER=' + env['nvcc_path'], '-DCUDA_TOOLKIT_ROOT_DIR=' + env['cuda_root'],
                 '-Dno_module=ON', '-Dno_cuda=OFF', '-Dno_smartio=ON'])
        execute(['cmake', '--build', str(out / 'bam'), '--target', 'block-benchmark-module', '--parallel', str(args.jobs)])
        module_source = out / 'libnvm/source'
        module_source.mkdir(parents=True)
        for p in (ROOT / 'bam/module').iterdir():
            if p.suffix in ('.c', '.h'):
                shutil.copy2(p, module_source / p.name)
        shutil.copy2(ROOT / 'bam/src/linux/ioctl.h', module_source / 'ioctl.h')
        # 只生成 Kbuild 文件；C 实现与 bam/module 中一致。
        (module_source / 'Makefile').write_text('obj-m := libnvm.o\nlibnvm-objs := pci.o list.o ctrl.o map.o\n'
                                               'ccflags-y += -D_CUDA -I' + env['nvidia_include'] + '\n')
        execute(['make', '-C', env['kernel_build'], 'M=' + str(module_source),
                 'KBUILD_EXTRA_SYMBOLS=' + env['nvidia_symvers'], 'CONFIG_DEBUG_INFO_BTF_MODULES=n',
                 '-j' + str(args.jobs), 'modules'])
        libnvm = out / ARTIFACTS[1]
        execute(['objcopy', '--remove-section=.BTF', '--remove-section=.BTF.ext',
                 str(module_source / 'libnvm.ko'), str(libnvm)])
        for module, name in ((swarmio, 'swarmio'), (libnvm, 'libnvm')):
            if output(['modinfo', '-F', 'name', str(module)]) != name:
                raise Error('模块名错误：' + str(module))
            if output(['modinfo', '-F', 'vermagic', str(module)]).split()[0] != env['kernel']:
                raise Error('模块 vermagic 不匹配当前内核')
        manifest['nvidia_import_crcs'] = audit_libnvm(libnvm,
            read_symvers(Path(env['kernel_build']) / 'Module.symvers'), read_symvers(env['nvidia_symvers']))
        manifest['ldd'] = check_library(out / ARTIFACTS[3], out / ARTIFACTS[2])
        manifest['artifacts_sha256'] = {name: digest(out / name) for name in ARTIFACTS}
        manifest['state'] = 'built'
    except BaseException:
        manifest['state'] = 'failed'
        raise
    finally:
        save_json(out / 'manifest.json', manifest)
    print('构建完成，未安装或加载模块：', out)


def manifest_for(args):
    manifest = json.loads((args.build_dir / 'manifest.json').read_text())
    if manifest['state'] != 'built':
        raise Error('构建未成功完成，不能加载或运行')
    if manifest['environment']['kernel'] != os.uname().release:
        raise Error('当前内核与构建内核不同')
    if set(manifest['artifacts_sha256']) != set(ARTIFACTS):
        raise Error('构建记录缺少必要产物')
    for name, expected in manifest['artifacts_sha256'].items():
        if digest(args.build_dir / name) != expected:
            raise Error('构建产物已变化：' + name)
    return manifest


def module_state():
    return {fields[0]: fields[4] for line in Path('/proc/modules').read_text().splitlines()
            if len(fields := line.split()) >= 5 and fields[0] in ('swarmio', 'libnvm')}


def workload_pids():
    result = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            name = (p / 'comm').read_text().strip()
            if name.startswith('nvm-block-bench') or name == 'fio':
                result.append(int(p.name))
        except (OSError, ProcessLookupError):
            pass
    return result


def dmesg_text():
    return output(['dmesg', '-T'])


def check_kernel_health(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r'BUG:|Oops:|exited with irqs disabled', line):
            context = '\n'.join(lines[max(0, i - 20):i + 90])
            if re.search(r'swarmio|libnvm', context, re.I):
                raise Error('本次启动已记录 SwarmIO/BaM 内核异常；保存 status 后正常重启，不继续加载/运行/卸载')


def virtual_device(bdf=None):
    candidates = []
    for p in PCI.iterdir():
        try:
            if (p / 'vendor').read_text().strip() == '0x0c51' and (p / 'device').read_text().strip() == '0x0110':
                candidates.append(p)
        except OSError:
            pass
    if bdf:
        candidates = [p for p in candidates if p.name == bdf.lower()]
    if len(candidates) != 1:
        raise Error('必须存在唯一的 SwarmIO 虚拟控制器；可用 --bdf 指定其地址')
    return candidates[0]


def verify_node(device):
    misc = Path('/sys/class/misc/libnvm0')
    node = Path('/dev/libnvm0')
    if not misc.exists() or (misc / 'device').resolve() != device.resolve():
        raise Error('/dev/libnvm0 不属于目标 SwarmIO 控制器')
    major, minor = map(int, (misc / 'dev').read_text().split(':'))
    if not node.exists() or not stat.S_ISCHR(node.stat().st_mode) or node.stat().st_rdev != os.makedev(major, minor):
        raise Error('/dev/libnvm0 节点缺失或设备号错误')


def verify_gpu(manifest):
    gpu = manifest['environment']['gpu']
    current = gpu_info(gpu['uuid'])
    if current['driver'] != manifest['environment']['nvidia_version'] or current['cuda_arch'] != gpu['cuda_arch']:
        raise Error('目标 GPU 或 NVIDIA 驱动与构建记录不同')


def check_no_workload():
    pids = workload_pids()
    if pids:
        raise Error('仍有 I/O 进程运行：' + ', '.join(map(str, pids)))


def load(args):
    linux_only(root=True)
    manifest = manifest_for(args)
    config = manifest['config']
    check_no_workload()
    check_kernel_health(dmesg_text())
    if module_state():
        raise Error('已有 swarmio/libnvm；load 不自动卸载或替换活动模块')
    verify_gpu(manifest)
    params = config['module']
    expected = 'memmap=' + str(params['memmap_size']) + '$' + str(params['memmap_start'])
    if expected not in Path('/proc/cmdline').read_text().split():
        raise Error('未发现配置要求的启动预留：' + expected)
    if not cpu_set(params['cpus']) <= cpu_set(Path('/sys/devices/system/cpu/online').read_text().strip()):
        raise Error('配置中的服务 CPU 不全在线')
    for module in (args.build_dir / ARTIFACTS[0], args.build_dir / ARTIFACTS[1]):
        for dependency in filter(None, output(['modinfo', '-F', 'depends', str(module)]).split(',')):
            subprocess.run(['modprobe', dependency], check=True)
    subprocess.run(['insmod', str(args.build_dir / ARTIFACTS[0])] +
                   [key + '=' + str(value) for key, value in params.items()], check=True)
    device = virtual_device(args.bdf)
    # libnvm 使用 NVMe class 匹配；其他未绑定盘可能被一起接管，必须在解绑目标前拒绝。
    for other in PCI.iterdir():
        if other != device and (other / 'class').read_text().strip() == '0x010802' and not (other / 'driver').exists():
            raise Error('发现其他未绑定的 NVMe 控制器：' + other.name)
    previous = (device / 'driver').resolve() if (device / 'driver').exists() else None
    if previous and previous.name != 'nvme':
        raise Error('虚拟控制器已绑定未知驱动：' + previous.name)
    if previous:
        (device / 'driver/unbind').write_text(device.name)
    try:
        subprocess.run(['insmod', str(args.build_dir / ARTIFACTS[1]), 'max_num_ctrls=64'], check=True)
    except subprocess.CalledProcessError:
        if previous and not (device / 'driver').exists():
            (previous / 'bind').write_text(device.name)
        raise
    subprocess.run(['udevadm', 'settle'], check=True)
    verify_node(device)
    for name, expected_value in (('skip_io', '0'), ('sq_diag_faulted', 'N')):
        if (MODULES / 'swarmio/parameters' / name).read_text().strip() != expected_value:
            raise Error('模块状态不符：' + name)
    check_kernel_health(dmesg_text())
    save_json(args.build_dir / 'loaded.json', {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               'bdf': device.name, 'manifest_sha256': digest(args.build_dir / 'manifest.json'),
               'run_state': 'not_started'})
    print('已就绪：', device.name, '-> /dev/libnvm0；CPU full-I/O；GPU', manifest['environment']['gpu']['name'])


def verify_session(args):
    manifest = manifest_for(args)
    session = json.loads((args.build_dir / 'loaded.json').read_text())
    if session['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise Error('加载记录属于上一次启动，请先 load')
    if session['manifest_sha256'] != digest(args.build_dir / 'manifest.json'):
        raise Error('加载记录与当前构建不一致')
    if session.get('run_state') not in ('not_started', 'completed'):
        raise Error('上一轮没有正常结束；先保存现场并重启，不自动复用队列')
    if module_state() != {'swarmio': 'Live', 'libnvm': 'Live'}:
        raise Error('两个模块必须均处于 Live 状态')
    device = virtual_device(session['bdf'])
    verify_node(device)
    verify_gpu(manifest)
    if (MODULES / 'swarmio/parameters/sq_diag_faulted').read_text().strip() != 'N':
        raise Error('SQ 故障已锁存，请保存状态，不继续运行')
    return manifest


def snapshot():
    parts = ['time=' + dt.datetime.now().astimezone().isoformat()]
    # 故障现场不调用 nvidia-smi，避免 GPU 驱动失去响应时采集程序也被阻塞。
    commands = (['uname', '-a'], ['ps', '-eo', 'pid,ppid,stat,wchan:32,comm,args'])
    for command in commands:
        try:
            parts += ['\n$ ' + ' '.join(command), output(command)]
        except (OSError, subprocess.CalledProcessError) as exc:
            parts.append(str(exc))
    for name in ('skip_io', 'sq_diag_faulted', 'qdiag_sq_state', 'qdiag_cq_state'):
        path = MODULES / 'swarmio/parameters' / name
        if path.is_file():
            parts += ['\n' + name + '=', path.read_text().strip()]
    try:
        parts += ['\n/proc/modules', Path('/proc/modules').read_text(), '\ndmesg', dmesg_text()]
    except (OSError, subprocess.CalledProcessError) as exc:
        parts.append(str(exc))
    # 在内核线程或 rmmod 卡住时保留栈，不发送信号、不触碰硬件寄存器。
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try:
                if (p / 'comm').read_text().strip().startswith(('swarmio_', 'rmmod')):
                    parts += ['\n/proc/' + p.name + '/stack', (p / 'stack').read_text()]
            except OSError:
                pass
    return '\n'.join(parts) + '\n'


def status(args):
    linux_only()
    text = snapshot()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print('状态已保存：', args.output)
    else:
        print(text, end='')


def benchmark_command(binary, bench):
    return ['stdbuf', '-oL', '-eL', str(binary)] + [
        '--' + key + '=' + (str(value).lower() if type(value) is bool else str(value))
        for key, value in bench.items()] + ['--gpu=0', '--n_ctrls=1']


def run(args):
    linux_only(root=True)
    check_kernel_health(dmesg_text())
    check_no_workload()
    manifest = verify_session(args)
    bench = dict(manifest['config']['benchmark'])
    for name in ('threads', 'reqs', 'queue_depth', 'num_queues'):
        if getattr(args, name) is not None:
            bench[name] = getattr(args, name)
    if args.random:
        bench['random'] = True
    if args.access:
        bench['access_type'] = {'read': 0, 'write': 1}[args.access]
    validate_benchmark(bench, manifest['config'])
    check_library(args.build_dir / ARTIFACTS[3], args.build_dir / ARTIFACTS[2])
    directory = ROOT / 'results' / dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    directory.mkdir(parents=True)
    command = benchmark_command(args.build_dir / ARTIFACTS[3], bench)
    child_env = dict(os.environ, CUDA_VISIBLE_DEVICES=manifest['environment']['gpu']['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID')
    # 本次 libnvm 优先；保留可能必需的 CUDA/驱动兼容库路径。
    child_env['LD_LIBRARY_PATH'] = str(args.build_dir / 'bam/lib') + (
        os.pathsep + child_env['LD_LIBRARY_PATH'] if child_env.get('LD_LIBRARY_PATH') else '')
    record = {'command': command, 'gpu': manifest['environment']['gpu'], 'state': 'running',
              'build_manifest_sha256': digest(args.build_dir / 'manifest.json')}
    print('输出目录：', directory, flush=True)
    (directory / 'before.log').write_text(snapshot())
    interrupted = [None]
    with (directory / 'bench.log').open('wb') as log, (directory / 'bench.log').open('r') as tail:
        process = subprocess.Popen(command, env=child_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record['pid'] = process.pid
        save_json(directory / 'run.json', record)
        session = json.loads((args.build_dir / 'loaded.json').read_text())
        session.update(run_state='running', workload_pid=process.pid)
        save_json(args.build_dir / 'loaded.json', session)

        def interrupt(signum, frame):
            if interrupted[0] is None:
                interrupted[0] = time.monotonic()
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                print('\n已发送 SIGINT；不强杀进程或自动卸载模块。', flush=True)
        previous = signal.signal(signal.SIGINT, interrupt)
        next_message = time.monotonic() + 10
        try:
            while True:
                chunk = tail.read()
                if chunk:
                    print(chunk, end='', flush=True)
                code = process.poll()
                if code is not None:
                    print(tail.read(), end='', flush=True)
                    break
                now = time.monotonic()
                if interrupted[0] is not None and now - interrupted[0] >= 5:
                    code = 130
                    print('进程仍未退出，PID=' + str(process.pid) + '；保留现场，使用 status 采样。', flush=True)
                    break
                if now >= next_message:
                    print('[仍在运行，PID=' + str(process.pid) + '] 可在另一终端使用 status 保存状态。', flush=True)
                    next_message = now + 10
                time.sleep(0.2)
        finally:
            signal.signal(signal.SIGINT, previous)
    text = (directory / 'bench.log').read_text(errors='replace')
    record.update(returncode=code, state='completed' if code == 0 and 'Ops/sec:' in text else 'incomplete')
    try:
        check_kernel_health(dmesg_text())
    except Error:
        record['state'] = 'kernel_fault'
    session['run_state'] = 'completed' if record['state'] == 'completed' else 'incomplete'
    save_json(args.build_dir / 'loaded.json', session)
    save_json(directory / 'run.json', record)
    (directory / 'after.log').write_text(snapshot())
    if record['state'] != 'completed':
        raise Error('本轮没有正常完成；日志位于 ' + str(directory) + '；不自动重跑或卸载')
    print('本轮吞吐统计已完成；不代表内容校验或随机负载问题已修复。')


def unload(args):
    linux_only(root=True)
    check_no_workload()
    check_kernel_health(dmesg_text())
    states = module_state()
    if any(state != 'Live' for state in states.values()):
        raise Error('模块已经在卸载或异常状态；保存 status 后正常重启')
    if 'swarmio' in states and (MODULES / 'swarmio/parameters/sq_diag_faulted').read_text().strip() != 'N':
        raise Error('SQ 故障已锁存，不执行自动卸载；先保存现场')
    if states:
        session = json.loads((args.build_dir / 'loaded.json').read_text())
        if session.get('run_state') not in ('not_started', 'completed'):
            raise Error('存在未正常结束的负载记录；保存现场并重启，不继续卸载')
        if session['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
            raise Error('没有本次启动的加载记录，拒绝卸载未知模块')
        for name in states:
            refcount = MODULES / name / 'refcnt'
            if refcount.exists() and int(refcount.read_text()) != 0:
                raise Error(name + ' 仍有引用，拒绝卸载')
        session['run_state'] = 'unloading'
        save_json(args.build_dir / 'loaded.json', session)
    # 有活动文件引用时不尝试卸载；不使用 rmmod -f，不通过超时发送 SIGKILL。
    for name in ('libnvm', 'swarmio'):
        if name not in states:
            continue
        process = subprocess.Popen(['rmmod', name])
        try:
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            path = ROOT / 'results' / ('unload-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.log')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snapshot())
            raise Error('正常卸载仍在等待，rmmod PID=' + str(process.pid) + '；未强杀。现场：' + str(path) + '；请正常重启')
        if code:
            raise Error('卸载 ' + name + ' 失败；不继续卸载后续模块')
        check_kernel_health(dmesg_text())
    session = args.build_dir / 'loaded.json'
    if session.exists():
        session.unlink()
    print('模块已正常卸载。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/bam-cpu.json')
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'build'):
        p = commands.add_parser(name, help='检查环境' if name == 'doctor' else '编译全部产物')
        p.add_argument('--gpu', help='nvidia-smi 的目标 index/UUID，默认使用配置中的 GPU')
        p.add_argument('--nvidia-symvers', type=Path)
        p.add_argument('--nvidia-include', type=Path)
        p.add_argument('--cuda-root', type=Path, help='CUDA 工具链目录，例如 /usr/local/cuda-12.4；sudo 下也可显式选择')
        if name == 'build':
            p.add_argument('--jobs', type=int, default=8)
    p = commands.add_parser('load', help='加载两个模块并绑定虚拟控制器')
    p.add_argument('--bdf', help='可选；必须属于 SwarmIO 虚拟设备')
    p = commands.add_parser('run', help='运行单轮读/写检查')
    for name in ('threads', 'reqs', 'queue-depth', 'num-queues'):
        p.add_argument('--' + name, type=int)
    p.add_argument('--random', action='store_true')
    p.add_argument('--access', choices=('read', 'write'))
    p = commands.add_parser('status', help='只读收集队列、进程、线程栈和内核日志')
    p.add_argument('--output', type=Path)
    commands.add_parser('unload', help='仅在无负载和无内核异常时正常卸载')
    args = parser.parse_args()
    args.build_dir = args.build_dir.resolve()
    try:
        if args.command in ('doctor', 'build'):
            config = read_config(args.config)
            if args.command == 'doctor':
                report = environment(args, config)
                print(json.dumps(report, indent=2, ensure_ascii=False))
                if not report['boot_reservation_ready'] or not report['service_cpus_online']:
                    raise Error('编译环境检查已输出，但启动预留或服务 CPU 不符合运行配置；修正后重启检查')
            else:
                build(args, config)
        else:
            {'load': load, 'run': run, 'status': status, 'unload': unload}[args.command](args)
        return 0
    except (Error, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        if args.command == 'load':
            print('加载失败后不自动卸载已成功加载的模块；先用 status 保存现场。', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n操作已中断；没有执行强制卸载。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
