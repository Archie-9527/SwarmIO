"""CPU-side protocol regression; CUDA execution remains a target-machine check."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline', ROOT / 'scripts/swarmio_bam.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)

class CoPilotTests(unittest.TestCase):
    def test_probe_only_maps_and_releases_without_queue_apis(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        source=(ROOT/'copilotio/microbenchmark/swarmio_read.cu').read_text()
        probe=source[source.index('static int probe_mapping('):source.index('__global__ void read_kernel(')]
        prelude=r'''
#include <memory>
#include <stdexcept>
#include <string>
#include <cstdio>
#include <cassert>
#include <cstdint>
#include <cstddef>
using error=std::runtime_error;
constexpr int O_RDWR=2,cudaSuccess=0;
static int bar[2048];
static int controls=0,dmas=0,aliases=0,notifications=0,doorbells_active=0;
static bool gpu_failure=false;
struct Config { bool relay_doorbells=false,host_notify=true; size_t num_queues=1; int gpu=0; };
struct nvm_ctrl_t { void* mm_ptr=bar; };
struct dma { unsigned n_ioaddrs=16; };
static int open(const char*,int) { return 42; }
static int close(int fd) { assert(fd==42); return 0; }
static int nvm_ctrl_init(nvm_ctrl_t** p,int fd) { assert(fd==42); *p=new nvm_ctrl_t; ++controls; return 0; }
static void nvm_ctrl_free(nvm_ctrl_t* p) { delete p; --controls; }
static bool nvm_ok(int s) { return s==0; }
static const char* nvm_strerror(int) { return "error"; }
static int cudaHostGetDevicePointer(void** d,void* h,int) { *d=h; ++aliases; return 0; }
static const char* cudaGetErrorString(int) { return "error"; }
static int cudaDeviceSynchronize() { return 0; }
#define cuda_err_chk(s) assert((s)==0)
class DoorbellMapping {
public:
    void init(nvm_ctrl_t*,size_t n,bool) { assert(n==1); ++doorbells_active; }
    ~DoorbellMapping() { --doorbells_active; }
};
class CoPilotNotify {
    bool initialized=false;
public:
    void init(bool host) { assert(host); initialized=true; ++notifications; }
    ~CoPilotNotify() { if(initialized) --notifications; }
};
static std::shared_ptr<dma> createDma(nvm_ctrl_t*,size_t n,int gpu=-1) {
    assert(n==65536);
    if(gpu>=0 && gpu_failure) throw error("P2P failure");
    ++dmas; return std::shared_ptr<dma>(new dma,[](dma* p){ delete p; --dmas; });
}
// Deliberately provide no reset, admin queue, read/write, or CPU agent APIs.
'''
        checks=r'''
int main() {
    Config c;
    assert(probe_mapping(c)==0);
    assert(aliases==1 && !controls && !dmas && !notifications && !doorbells_active);
    c.relay_doorbells=true;
    assert(probe_mapping(c)==0);
    assert(aliases==1 && !controls && !dmas && !notifications && !doorbells_active);
    gpu_failure=true;
    try { probe_mapping(c); assert(false); } catch(const std::runtime_error&) {}
    assert(!controls && !dmas && !notifications && !doorbells_active);
}
'''
        with tempfile.TemporaryDirectory() as d:
            unit,binary=Path(d)/'probe.cpp',Path(d)/'probe'
            unit.write_text(prelude+probe+checks)
            for defines in ([], ['-DSWARMIO_COPILOT']):
                subprocess.run(['c++','-std=c++17',*defines,str(unit),'-o',str(binary)],check=True)
                subprocess.run([str(binary)],check=True,stdout=subprocess.DEVNULL,timeout=10)

    def test_doorbell_relay_stride_admin_isolation_and_cleanup(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        runtime = r'''
#pragma once
#include <cstdlib>
#include <cstddef>
using cudaError_t=int;
constexpr int cudaSuccess=0,cudaHostAllocMapped=2,cudaHostRegisterMapped=2,cudaHostRegisterIoMemory=4;
inline int allocations=0,frees=0,registrations=0,unregistrations=0;
inline bool fail_register=false,fail_alias=false;
inline unsigned last_flags=0;
inline int cudaHostAlloc(void** p,size_t n,unsigned flags) {
    if(flags!=cudaHostAllocMapped) abort();
    *p=calloc(1,n); ++allocations; return 0;
}
inline int cudaHostGetDevicePointer(void** d,void* h,unsigned) { *d=h; return fail_alias ? 1 : 0; }
inline int cudaFreeHost(void* p) { free(p); ++frees; return 0; }
inline int cudaHostRegister(void*,size_t,unsigned flags) { ++registrations; last_flags=flags; return fail_register?1:0; }
inline int cudaHostUnregister(void*) { ++unregistrations; return 0; }
inline const char* cudaGetErrorString(int) { return "invalid argument"; }
'''
        unit = r'''
#include <cstdint>
#include <cassert>
#include <string>
#define __NVM_TYPES_H__
struct nvm_ctrl_t { volatile void* mm_ptr; size_t mm_size; uint8_t dstrd; };
#include <doorbell_mapping.h>
int main() {
    uint32_t bar[2048]={};
    nvm_ctrl_t ctrl{bar,sizeof(bar),2};
    auto real=bar+0x1000/4;
    real[0]=71; real[4]=72; // Preserve both CPU admin doorbells.
    {
        DoorbellMapping d; d.init(&ctrl,2,true);
        assert(registrations==0);
        auto shadow=d.device_doorbells();
        for(int i=0;i<6;i++) shadow[i]=i+10;
        d.poll_once();
        assert(real[0]==71 && real[4]==72);
        for(int i=2;i<6;i++) assert(real[i*4]==uint32_t(i+10));
        real[8]=99; d.poll_once(); assert(real[8]==99); // No repeated writes.
        shadow[2]=0; d.poll_once(); assert(real[8]==0); // Ring wrap to zero.
        d.start(); d.destroy(); d.destroy();
    }
    assert(allocations==1 && frees==1 && unregistrations==0);
    { DoorbellMapping d; d.init(&ctrl,2,true); d.device_doorbells()[5]=77; }
    assert(real[20]==77);
    assert(allocations==2 && frees==2);
    { DoorbellMapping d; d.init(&ctrl,2,false); assert(!d.device_doorbells()); }
    assert(registrations==1 && last_flags==6 && unregistrations==1);
    fail_register=true;
    try { DoorbellMapping d; d.init(&ctrl,2,false); assert(false); }
    catch(const std::runtime_error& e) { assert(std::string(e.what()).find("--doorbell-mode relay")!=std::string::npos); }
    assert(unregistrations==1);
    fail_alias=true;
    try { DoorbellMapping d; d.init(&ctrl,2,true); assert(false); }
    catch(const std::runtime_error&) {}
    assert(allocations==3 && frees==3);
    ctrl.mm_size=0x1004;
    try { DoorbellMapping d; d.init(&ctrl,2,true); assert(false); }
    catch(const std::runtime_error&) {}
    assert(allocations==3);
}
'''
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            (base/'cuda_runtime.h').write_text(runtime)
            source=base/'relay.cpp'
            source.write_text('#include <cstddef>\n'+unit)
            for engine in ('bam','copilot-io'):
                binary=base/'relay'
                subprocess.run(['c++','-std=c++17','-pthread','-I'+str(base),
                    '-I'+str(ROOT/'copilotio'/engine/'include'),str(source),'-o',str(binary)],check=True)
                subprocess.run([str(binary)],check=True,timeout=10)

    def test_virtual_bar_layout_restored_after_cxl_header_fixup(self):
        if not shutil.which('cc'):
            self.skipTest('no C compiler')
        text = (ROOT / 'nvmev/pci.c').read_text()
        helper = text[text.index('static int __restore_nvme_bar_resources('):
                      text.index('static int __create_pci_bus(')]
        bar_size = next(line for line in (ROOT / 'nvmev/pci.h').read_text().splitlines()
                        if line.startswith('#define VDEV_PCI_BAR0_SIZE '))
        unit = r'''
#include <assert.h>
#include <errno.h>
#include <string.h>
#define PCI_STD_NUM_BARS 6
#define IORESOURCE_MEM 0x200
#define IORESOURCE_MEM_64 0x100000
#define IORESOURCE_PREFETCH 0x2000
#define SWARMIO_ERROR(...) ((void)0)
#define SWARMIO_INFO(...) ((void)0)
struct resource {
    const char *name;
    unsigned long long start, end, flags;
    struct resource *parent, *child, *sibling;
};
struct pci_dev { struct resource resource[7]; };
struct nvme_vdev { struct { unsigned long long memmap_start; } config; };
static struct nvme_vdev state;
static struct nvme_vdev *vdev=&state;
static struct resource iomem_resource;
static const char* pci_name(struct pci_dev* dev) { (void)dev; return "virtual"; }
''' + bar_size + '\n' + helper + r'''
int main(void) {
    struct pci_dev dev={0}, before;
    state.config.memmap_start=0x100000000ULL;
    dev.resource[0].start=0x100000000ULL;
    dev.resource[0].end=0x10000ffffULL;
    dev.resource[0].flags=IORESOURCE_MEM|IORESOURCE_MEM_64|IORESOURCE_PREFETCH;
    dev.resource[2]=dev.resource[0]; dev.resource[2].start+=0x10000;
    dev.resource[4]=dev.resource[0]; dev.resource[4].start+=0x20000;
    dev.resource[6].start=123; /* ROM resource must stay untouched. */
    assert(!__restore_nvme_bar_resources(&dev));
    assert(dev.resource[0].start==0x100000000ULL);
    assert(dev.resource[0].end==0x1000fffffULL); /* Includes MSI-X at +0x81000. */
    assert(dev.resource[0].flags==(IORESOURCE_MEM|IORESOURCE_MEM_64));
    assert(dev.resource[0].parent==&iomem_resource);
    for(int i=1;i<6;i++) {
        struct resource zero={0};
        assert(!memcmp(&dev.resource[i], &zero, sizeof(zero)));
    }
    assert(dev.resource[6].start==123);
    memset(&dev,0,sizeof(dev));
    dev.resource[2].parent=&iomem_resource;
    before=dev;
    assert(__restore_nvme_bar_resources(&dev)==-EBUSY);
    assert(!memcmp(&dev,&before,sizeof(dev)));
}
'''
        with tempfile.TemporaryDirectory() as d:
            source, binary = Path(d)/'bars.c', Path(d)/'bars'
            source.write_text(unit)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_gdr_audit_rejects_gpl_only_with_proprietary_nvidia(self):
        kernel = {'__vma_start_write': {'crc': 1, 'export': 'EXPORT_SYMBOL_GPL'}}
        nvidia = {'nvidia_p2p_get_pages': {'crc': 2, 'export': 'EXPORT_SYMBOL'}}
        def output(command, **kwargs):
            return ('U __vma_start_write\nU nvidia_p2p_get_pages' if command[0] == 'nm'
                    else '0x2 nvidia_p2p_get_pages')
        with patch.object(pipeline, 'read_symvers', side_effect=[kernel, nvidia]), \
             patch.object(pipeline, 'output', side_effect=output):
            with self.assertRaisesRegex(pipeline.Error, '__vma_start_write'):
                pipeline.audit_gdrdrv(Path('gdrdrv.ko'), {'kernel_build': '/kernel',
                    'nvidia_symvers': '/nvidia.symvers', 'nvidia_license': 'NVIDIA'})
        self.assertNotIn(pipeline.ARTIFACTS[7], pipeline.artifacts_for({'notify_memory': 'host'}))
        self.assertIn(pipeline.ARTIFACTS[7], pipeline.artifacts_for({}))

    def test_host_notification_allocation_alias_and_cleanup(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        include = ROOT / 'copilotio/copilot-io/include'
        header = (include / 'copilot.h').read_text()
        cls = header[header.index('class CoPilotNotify {'):header.index('class CoPilotCQTable {')]
        unit = r'''
#include <copilot_types.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstddef>
#include <cassert>
#include <stdexcept>
using cudaError_t = int;
using gdr_t = void*;
using gdr_mh_t = int;
struct gdr_info_t { unsigned long va; };
constexpr int cudaSuccess=0, cudaHostAllocMapped=2;
constexpr size_t GPU_PAGE_SIZE=65536, GPU_PAGE_MASK=~(GPU_PAGE_SIZE-1);
static int allocations=0, releases=0;
static bool pointer_failure=false;
static cudaError_t cudaHostAlloc(void** p,size_t n,int flags) {
    assert(flags==cudaHostAllocMapped); *p=malloc(n); ++allocations; return 0;
}
static cudaError_t cudaHostGetDevicePointer(void** p,void* host,int) {
    *p=(char*)host+4096; return pointer_failure ? 1 : 0;
}
static int cudaFreeHost(void* p) { free(p); ++releases; return 0; }
static const char* cudaGetErrorString(int) { return "failure"; }
static void copilot_sfence() {}
// No CUDA device allocation or GDR API should be reached in host mode.
static int cudaMalloc(void**,size_t) { abort(); }
static int cudaMemset(void*,int,size_t) { abort(); }
static int cudaFree(void*) { abort(); }
static int cudaDeviceSynchronize() { abort(); }
static gdr_t gdr_open() { abort(); }
static int gdr_pin_buffer(gdr_t,unsigned long,size_t,int,int,gdr_mh_t*) { abort(); }
static int gdr_map(gdr_t,gdr_mh_t,void**,size_t) { abort(); }
static int gdr_get_info(gdr_t,gdr_mh_t,gdr_info_t*) { abort(); }
static int gdr_unmap(gdr_t,gdr_mh_t,void*,size_t) { abort(); }
static int gdr_unpin_buffer(gdr_t,gdr_mh_t) { abort(); }
static int gdr_close(gdr_t) { abort(); }
''' + cls + r'''
int main() {
    {
        CoPilotNotify n; n.init(true);
        assert((char*)n.gpu_ptr==(char*)n.cpu_ptr+4096);
        assert(n.cpu_ptr[65535].ready==0);
        n.cpu_ptr[65535].ready=1;
        n.destroy(); n.destroy();
        assert(!n.cpu_ptr && !n.gpu_ptr);
    }
    assert(allocations==1 && releases==1);
    pointer_failure=true;
    { CoPilotNotify n; try { n.init(true); assert(false); } catch(const std::runtime_error&) {} }
    assert(allocations==2 && releases==2);
}
'''
        with tempfile.TemporaryDirectory() as d:
            source, binary = Path(d)/'host.cpp', Path(d)/'host'
            source.write_text(unit)
            subprocess.run(['c++', '-std=c++17', '-I'+str(include), str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_notification_types_available_before_device_functions(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        include = ROOT / 'copilotio/copilot-io/include'
        # Compile the real header declaration sections. CUDA function bodies
        # require nvcc; unrelated SDK dependencies are empty test doubles here.
        page = (include / 'page_cache.h').read_text().split('#define FREE 2', 1)[0] + '\n#endif\n'
        agent = (include / 'copilot.h').read_text().split('class CoPilotNotify {', 1)[0] + '\n#endif\n'
        checks = '''
#include <cstddef>
static_assert(sizeof(copilot_notify_entry) == 16);
static_assert(offsetof(copilot_notify_entry, ready) == 0);
static_assert(offsetof(copilot_notify_entry, cq_head) == 12);
static_assert(sizeof(copilot_cq_table_entry) == 4);
static_assert(sizeof(copilot_cq_table) == COPILOT_CQ_MAX_DEPTH * 8 + 8);
void access(copilot_notify_entry* notify, copilot_cq_table* table) {
    notify[1].ready = 1; notify[1].pos = 2; notify[1].loc = 3; notify[1].cq_head = 4;
    table->entries[0].cid = 1; table->entries[0].sq_head = 2;
    table->ready[0] = 1; table->drain_lock = 0; table->gpu_head = 0;
}
'''
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for name in ('util.h', 'host_util.h', 'nvm_types.h', 'nvm_util.h', 'buffer.h',
                         'ctrl.h', 'nvm_parallel_queue.h', 'nvm_cmd.h', 'gdrapi.h'):
                (base / name).write_text('')
            for prefix in (page, agent, page + agent, agent + page):
                unit = base / 'types.cpp'
                unit.write_text(prefix + checks)
                compiled = subprocess.run(['c++', '-std=c++17', '-fsyntax-only',
                    '-I' + str(base), '-I' + str(include), str(unit)], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_public_headers_compile_without_cuda_device_qualifiers(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / 'cuda/std').mkdir(parents=True)
            # Only replace SDK dependencies: compile the actual project headers.
            # Deliberately do not define __device__ or __forceinline__ here.
            (base / 'cuda.h').write_text('')
            (base / 'cuda/atomic').write_text(
                '#pragma once\n#include <atomic>\nnamespace cuda {\n'
                'enum thread_scope { thread_scope_system, thread_scope_device };\n'
                'template<class T, thread_scope S=thread_scope_system> using atomic=::std::atomic<T>;\n'
                'namespace std=::std;\n}\n')
            (base / 'cuda/std/atomic').write_text('#include <cuda/atomic>\n')
            unit = base / 'host.cpp'
            unit.write_text('#include <nvm_util.h>\n#include <nvm_queue.h>\n#include <nvm_cmd.h>\n')
            for engine in ('bam', 'copilot-io'):
                includes = ['-I' + str(base), '-I' + str(ROOT / 'copilotio' / engine / 'include')]
                command = ['c++', '-std=c++17', '-D_CUDA', '-D__CUDA__', *includes]
                compiled = subprocess.run(command + ['-c', str(unit), '-o', str(base / 'host.o')],
                                          capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                # Also check the device branch stays available to CUDA compilation.
                for defines, expected in [([], False), (['-D__CUDACC__'], True)]:
                    preprocessed = subprocess.check_output(command + defines + ['-E', str(unit)], text=True)
                    for helper in ('lane_id()', 'warp_id()', 'get_smid()'):
                        self.assertEqual(helper in preprocessed, expected)

    def test_empty_btf_override_disables_kbuild_ifdef(self):
        if not shutil.which('make'):
            self.skipTest('no make')
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / 'auto.conf').write_text('CONFIG_DEBUG_INFO_BTF_MODULES=y\n')
            # Match Makefile.modfinal: include the kernel config, then use ifdef.
            (base / 'Makefile').write_text(
                'include auto.conf\nifdef CONFIG_DEBUG_INFO_BTF_MODULES\n'
                '$(error BTF enabled with missing base BTF)\nendif\nall:\n\t@:\n')
            for value, expected in [('n', False), ('', True)]:
                result = subprocess.run(['make', 'CONFIG_DEBUG_INFO_BTF_MODULES=' + value],
                                        cwd=base, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_poll_phase_wrap_and_metadata(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        header = (ROOT / 'copilotio/copilot-io/include/copilot.h').read_text()
        poll = header[header.index('struct copilot_cq_info'):header.index('class CoPilotAgent')]
        unit = r'''
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <pthread.h>
#include <sched.h>
#include <cassert>
#ifndef __linux__
struct cpu_set_t { int unused; };
#define CPU_ZERO(p) ((void)(p))
#define CPU_SET(c,p) ((void)(c),(void)(p))
static int pthread_setaffinity_np(pthread_t,size_t,const cpu_set_t*) { return 0; }
#endif
struct nvm_cpl_t { uint32_t dword[4]; };
#include <copilot_types.h>
static void copilot_sfence() { std::atomic_thread_fence(std::memory_order_seq_cst); }
static void copilot_pause() {}
''' + poll + r'''
int main() {
    nvm_cpl_t cq[4] = {};
    copilot_notify_entry notices[8] = {};
    copilot_cq_info ci = {cq,3,2,notices,nullptr,0,0};
    std::atomic<bool> stop(false);
    copilot_thread_args args = {&ci,1,0,&stop};
    pthread_t thread;
    assert(!pthread_create(&thread,nullptr,copilot_poll_thread,&args));
    for (uint32_t h=0;h<100;h++) {
        uint32_t cid=h%8,pos=h%4,phase=(~(h>>2))&1;
        static_cast<volatile nvm_cpl_t*>(cq)[pos].dword[3]=cid|(phase<<16);
        while(!notices[cid].ready) {}
        copilot_sfence();
        assert(notices[cid].pos==pos && notices[cid].loc==h && notices[cid].cq_head==h);
        notices[cid].ready=0;
    }
    stop.store(true);
    pthread_join(thread,nullptr);
    assert(ci.completions==100);
}
'''
        with tempfile.TemporaryDirectory() as d:
            source, binary = Path(d)/'poll.cpp', Path(d)/'poll'
            source.write_text(unit)
            subprocess.run(['c++','-std=c++17','-pthread',
                            '-I'+str(ROOT/'copilotio/copilot-io/include'),str(source),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True,timeout=10)

    def test_compare_pairs_same_workload_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            calls=[]
            def run(args):
                calls.append((args.engine,args.threads,args.reqs,args.random,args.queue_depth))
                p=base/args.engine; p.mkdir()
                (p/'bench.log').write_text('Doorbell mode: relay\nOps/sec: '+('100' if args.engine=='bam' else '200\nNotification memory: host'))
                return p
            args=SimpleNamespace(threads=None,reqs=None,num_queues=None,queue_depth=None,doorbell_mode='relay')
            with patch.object(pipeline,'ROOT',base), patch.object(pipeline,'run',side_effect=run):
                pipeline.compare(args)
            self.assertEqual(calls,[('bam',1024,8,True,512),('copilotio',1024,8,True,512)])
            self.assertEqual(len(list((base/'results').glob('compare-*/comparison.csv'))),1)
            summary = json.loads(next((base/'results').glob('compare-*/comparison.json')).read_text())
            self.assertEqual([row['notify_memory'] for row in summary['runs']], ['none', 'host'])
            self.assertEqual([row['doorbell_mode'] for row in summary['runs']], ['relay', 'relay'])
            with patch.object(pipeline,'run',side_effect=pipeline.Error('failed')) as failed:
                with self.assertRaises(pipeline.Error): pipeline.compare(args)
                self.assertEqual(failed.call_count,1)

    def test_compare_rejects_mixed_doorbell_modes(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for engine, mode in [('bam', 'relay'), ('copilotio', 'mmio')]:
                directory = base / engine
                directory.mkdir()
                (directory / 'bench.log').write_text(
                    f'Doorbell mode: {mode}\nNotification memory: host\nOps/sec: 100\n')
            args = SimpleNamespace(threads=1024, reqs=8, num_queues=8,
                                   queue_depth=512, doorbell_mode='relay')
            with patch.object(pipeline, 'ROOT', base), \
                 patch.object(pipeline, 'run', side_effect=lambda a: base / a.engine):
                with self.assertRaisesRegex(pipeline.Error, 'doorbell'):
                    pipeline.compare(args)
            self.assertFalse((base / 'results').exists())

    def test_exclusive_buffer_pages(self):
        c=pipeline.read_config(ROOT/'configs/bam-cpu.json')
        c['benchmark']['threads']=8192
        with self.assertRaises(pipeline.Error): pipeline.validate_benchmark(c['benchmark'],c)
