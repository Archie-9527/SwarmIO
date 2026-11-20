"""Local provenance/build checks; never load modules or run a GPU workload."""
from pathlib import Path
import os
import shutil
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
AUTHOR = '25594a1a763044d3d459fd15ccebc5d44d1690ca'
AUTHOR_BAM_PATCHED = '98c3a3a'

DOXYGEN_COMMENTS = re.compile(
    r'^[ \t]*/\*\*(?!<)[\s\S]*?\*/[ \t]*\n'
    r'|^[ \t]*///[^\n]*\n'
    r'|[ \t]+/\*\*<[^\n]*?\*/(?=[ \t]*$)',
    re.MULTILINE,
)


def original(repo, ref, path):
    return subprocess.check_output(['git', '-C', str(repo), 'show', f'{ref}:{path}'], text=True)


def without_doxygen(source):
    """Keep the byte-for-byte provenance comparison outside Doxygen comments."""
    return DOXYGEN_COMMENTS.sub('', source)


class BaselineTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'C preprocessor unavailable')
    def test_nvidia_api_conditionals_for_old_and_persistent_headers(self):
        source = (ROOT / 'bam/module/map.c').read_text()
        source = re.sub(r'^#include[^\n]*', '', source, flags=re.M)
        prefix = '#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))\n#define LINUX_VERSION_CODE KERNEL_VERSION(6,8,0)\n#define _CUDA\n'
        for persistent in (False, True):
            code = prefix + ('#define NVIDIA_P2P_CAP_GET_PAGES_PERSISTENT_API\n' if persistent else '') + source
            result = subprocess.run(['cc', '-E', '-P', '-x', 'c', '-'], input=code,
                                    text=True, capture_output=True, check=True).stdout
            self.assertEqual('nvidia_p2p_get_pages_persistent(' in result, persistent)
            self.assertEqual('nvidia_p2p_put_pages_persistent(' in result, persistent)
            self.assertEqual(bool(re.search(r'err\s*=\s*nvidia_p2p_get_pages\(', result)), not persistent)
            self.assertEqual('force_release_gpu_memory(' in result, not persistent)

    def test_swarmio_algorithms_are_author_code(self):
        for name in ('dsa/config.c', 'src/queue.h', 'src/simple_timing_model.c',
                     'nvmev/nvme_vdev.c'):
            with self.subTest(file=name):
                self.assertEqual(without_doxygen((ROOT / name).read_text()),
                                 without_doxygen(original(ROOT, AUTHOR, name)))

    def test_bam_workload_kernels_are_author_patch(self):
        name = 'benchmarks/block/main.cu'
        source = without_doxygen((ROOT / 'bam' / name).read_text())
        authored = original(ROOT / 'bam', AUTHOR_BAM_PATCHED, name)
        for kernel in ('sequential_access_kernel', 'random_access_kernel'):
            with self.subTest(kernel=kernel):
                pattern = rf'^void {kernel}\(.*?^\}}'
                self.assertEqual(re.search(pattern, source, re.M | re.S).group(),
                                 re.search(pattern, authored, re.M | re.S).group())

    def test_shell_syntax(self):
        for path in (ROOT / 'scripts').glob('*.sh'):
            subprocess.run(['bash', '-n', str(path)], check=True)

    @unittest.skipUnless(shutil.which('make'), 'make unavailable')
    def test_make_forwards_author_features_and_kernel_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'Makefile').write_text('modules:\n\t@echo kernel-build-placeholder\n')
            out = subprocess.check_output(['make', '-n', 'KERNELDIR=' + tmp,
                                           'coalesce_sqe=y', 'batch_worker_dma=y',
                                           'use_disp_dma=y', 'use_worker_dma=y'], cwd=ROOT, text=True)
            for flag in ('CONFIG_SWARMIO_DISP_COALESCE_SQE=y',
                         'CONFIG_SWARMIO_WORKER_BATCH_IO=y',
                         'CONFIG_SWARMIO_DMA_DISPATCHER=y', 'CONFIG_SWARMIO_DMA_WORKER=y'):
                self.assertIn(flag, out)

    @unittest.skipUnless(shutil.which('make'), 'make unavailable')
    def test_kbuild_only_links_idxd_when_dma_is_enabled(self):
        makefile = ('include Kbuild\nshow:\n'
                    '\t@echo $(swarmio-objs) $(ccflags-y)\n')
        for dispatcher in ('n', 'y'):
            for worker in ('n', 'y'):
                with self.subTest(dispatcher=dispatcher, worker=worker):
                    args = ['make', '-sf', '-', 'show', 'src=.',
                            'IDXD_INCLUDE=/private-idxd-headers',
                            f'CONFIG_SWARMIO_DMA_DISPATCHER={dispatcher}',
                            f'CONFIG_SWARMIO_DMA_WORKER={worker}']
                    flags = subprocess.check_output(args, input=makefile, cwd=ROOT,
                                                    text=True).split()
                    enabled = 'y' in (dispatcher, worker)
                    for flag in ('dsa/config.o', 'dsa/dsa.o', 'dsa/batch.o',
                                 'dsa/single.o', '-I/private-idxd-headers',
                                 '-DCONFIG_SWARMIO_DSA=1'):
                        self.assertEqual(flag in flags, enabled)

    def test_shell_benchmark_overrides_without_running_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            mocks = {'getopt': "#!/bin/sh\nprintf '%s\\n' --\n",
                     'sudo': "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
                     'numfmt': "#!/bin/sh\ncase \"$2\" in 512M) echo 536870912;; 128M) echo 134217728;; *) exit 1;; esac\n"}
            for name, source in mocks.items():
                p = Path(tmp, name); p.write_text(source); p.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                       BAM_CACHE_SIZE='512M', BAM_DATA_SIZE='128M', BAM_REQS='2048',
                       BAM_QUEUE_DEPTH='512', BAM_GPU='1', BAM_RANDOM='false')
            env.pop('CUDA_MODULE_LOADING', None)
            for loading in (None, 'EAGER'):
                with self.subTest(cuda_module_loading=loading):
                    preserve_env = '--preserve-env=BAM_SWARMIO_FAULT_EXIT'
                    if loading:
                        env['CUDA_MODULE_LOADING'] = loading
                        preserve_env += ',CUDA_MODULE_LOADING'
                    out = subprocess.check_output(['bash', 'scripts/run_bam-block-bench.sh'],
                                                  cwd=ROOT, env=env, text=True,
                                                  stderr=subprocess.DEVNULL)
                    for arg in (preserve_env, 'bam/Release/bin/nvm-block-bench', '--reqs=2048',
                                '--queue_depth=512', '--gpu=1', '--random=false',
                                '--pages=1048576', '--num_blks=262144'):
                        self.assertIn(arg, out.splitlines())


if __name__ == '__main__':
    unittest.main()
