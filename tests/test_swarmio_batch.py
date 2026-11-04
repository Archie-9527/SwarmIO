"""Exercise batch scheduling and CQ recycling on CPU; target CUDA checks still required."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import swarmio_bam as pipeline
import swarmio_microbench as microbench


class BatchTests(unittest.TestCase):
    def config(self):
        return pipeline.read_config(ROOT / 'configs/bam-cpu.json')

    def test_binary_parser_checks_batch_geometry_before_device_access(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        source = (ROOT / 'copilotio/microbenchmark/swarmio_read.cu').read_text()
        parser = source[source.index('struct Config {'):source.index('// No admin reset')]
        prelude = '#include <algorithm>\n#include <cstdint>\n#include <string>\n#include <stdexcept>\n#define CPU_SETSIZE 1024\n'
        cases = [([], True), (['--page_size=0'], False), (['--batch_size=0'], False),
                 (['--batch_size=33'], False), (['--reqs=18446744073709551615'], False)]
        batch = ['--threads=64', '--reqs=8', '--num_queues=4', '--queue_depth=128', '--batch_size=3']
        with tempfile.TemporaryDirectory() as d:
            unit, binary = Path(d) / 'parser.cpp', Path(d) / 'parser'
            unit.write_text(prelude + parser + '\nint main(int argc,char** argv) {\n'
                            'try { parse(argc,argv); return 0; } catch(const std::exception&) { return 1; } }\n')
            for defines, copilot in (([], False), (['-DSWARMIO_COPILOT'], True)):
                subprocess.run(['c++', '-std=c++17', '-fsanitize=undefined', *defines,
                                str(unit), '-o', str(binary)], check=True)
                for options, ok in cases + [(batch + ['--pages=192'], copilot),
                                           (batch + ['--pages=191'], False),
                                           (batch + ['--pages=192', '--num_queues=3', '--queue_depth=64'], False)]:
                    with self.subTest(copilot=copilot, options=options):
                        result = subprocess.run([str(binary), *options], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)

    def test_exclusive_buffers_queue_capacity_and_tail_batch(self):
        config = self.config()
        bench = dict(config['benchmark'], threads=512, reqs=2048, num_queues=16,
                     batch_size=8, pages=4096)
        pipeline.validate_benchmark(bench, config)
        for change, message in (({'pages': 4095}, 'pages'),
                                ({'threads': 4096, 'pages': 32768}, '队列容量'),
                                ({'batch_size': 33, 'pages': 16896}, 'batch_size'),
                                ({'threads': 1024, 'pages': 8192, 'queue_depth': 512}, '队列容量')):
            with self.subTest(change=change), self.assertRaisesRegex(pipeline.Error, message):
                pipeline.validate_benchmark(dict(bench, **change), config)
        # The final batch can be shorter; only two buffers per thread are needed.
        pipeline.validate_benchmark(dict(bench, reqs=2, batch_size=32, pages=1024), config)
        # Non-divisible mapping must use ceil(threads / num_queues).
        with self.assertRaisesRegex(pipeline.Error, '队列容量'):
            pipeline.validate_benchmark(dict(bench, threads=64, num_queues=3, batch_size=3,
                                            pages=192, queue_depth=64), config)

    def test_wrapper_defaults_and_4k_range(self):
        args = microbench.arguments(['--build-dir', 'build-68-batch', '--dry-run'])
        command = microbench.command(args)
        self.assertEqual(command[2], 'compare')
        for key, value in (('--batch-size', '8'), ('--pages', '4096'),
                           ('--threads', '512'), ('--reqs', '2048'), ('--num-blks', '2097152')):
            self.assertEqual(command[command.index(key) + 1], value)
        args.page_size = 4096
        command = microbench.command(args)
        self.assertEqual(command[command.index('--num-blks') + 1], '262144')
        with patch.object(pipeline, 'main') as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(microbench.main(['--build-dir', '/nonexistent/build', '--dry-run']), 0)
            execute.assert_not_called()

    def test_compare_preflights_and_records_different_submission_modes(self):
        config = self.config()
        args = SimpleNamespace(command='compare', threads=512, reqs=17, pages=4096, num_queues=16,
                               queue_depth=512, page_size=512, num_blks=2097152, batch_size=8,
                               doorbell_mode='mmio', random=True, access='read')
        manifest = {'benchmark_protocol': 2, 'config': config}
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            observed = []

            def run(a):
                bench = pipeline.workload_for(a, config, a.engine)
                batch = bench.get('batch_size', 1)
                observed.append((a.engine, batch, bench['pages'], bench['threads'] * bench['reqs']))
                directory = base / a.engine
                directory.mkdir()
                (directory / 'bench.log').write_text(
                    f'Doorbell mode: mmio\nNotification memory: host\nOps/sec: 100\n'
                    f'Submission mode: {"batch" if batch > 1 else "sync"}\nBatch size: {batch}\n')
                return directory

            with patch.object(pipeline, 'ROOT', base), patch.object(pipeline, 'manifest_for', return_value=manifest), \
                    patch.object(pipeline, 'run', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                pipeline.compare(args)
            self.assertEqual(observed, [('bam', 1, 4096, 8704), ('copilotio', 8, 4096, 8704)])
            rows = json.loads(next((base / 'results').glob('compare-*/comparison.json')).read_text())['runs']
            self.assertEqual([(r['submission_mode'], r['batch_size']) for r in rows], [('sync', 1), ('batch', 8)])
            args.threads, args.pages = 4096, 32768
            with patch.object(pipeline, 'manifest_for', return_value=manifest), patch.object(pipeline, 'run') as execute:
                with self.assertRaisesRegex(pipeline.Error, '队列容量'):
                    pipeline.compare(args)
                execute.assert_not_called()
            args.threads, args.pages = 512, 4096
            with patch.object(pipeline, 'manifest_for', return_value={'config': config}), patch.object(pipeline, 'run') as execute:
                with self.assertRaisesRegex(pipeline.Error, '重新 build/load'):
                    pipeline.compare(args)
                execute.assert_not_called()

    def test_actual_kernel_batches_and_reclaims_reordered_completions(self):
        if not shutil.which('c++'):
            self.skipTest('no C++ compiler')
        source = (ROOT / 'copilotio/microbenchmark/swarmio_read.cu').read_text()
        kernel = source[source.index('__global__ void read_kernel('):source.index('int main(')]
        # Replace only the CUDA trap instruction; execute the actual loop body.
        kernel = kernel.replace('asm("trap;")', 'throw std::runtime_error("NVMe status")')
        header = (ROOT / 'copilotio/microbenchmark/swarmio_batch.cuh').read_text()
        drain = header[header.index('inline __device__ void drain_batch_cq('):]
        prelude = r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <vector>
#include <set>
#include <copilot_types.h>
#define __device__
#define __global__
#define SWARMIO_COPILOT
#define LOCKED 1
#define UNLOCKED 0
namespace cuda { namespace std=::std; }
static void __threadfence_system() {}
struct Slot { std::atomic<uint32_t> val{0}; };
struct nvm_queue_t {
    std::atomic<uint32_t> head{0},tail{0},head_lock{0};
    uint32_t qs_minus_1=15;
    Slot tickets[16];
};
struct QueuePair { nvm_queue_t sq,cq; uint32_t nvmNamespace=1; };
struct Controller { QueuePair* d_qps; uint32_t n_qps=1; };
struct page_cache_d_t { uint64_t *prp1,*prp2; bool prps=true; };
struct nvm_cmd_t { uint16_t cid; uint64_t page,lba; };
struct Dim { uint32_t x; };
static Dim blockIdx{0},blockDim{64},threadIdx{0};
static copilot_notify_entry notices[65536];
static copilot_cq_table table;
static std::vector<nvm_cmd_t> pending,commands;
static std::set<unsigned> active;
static unsigned issued=0,published=0,allocated=0,expected=0,total=0,window=0,doorbells=0;
static bool inject_error=false;
static uint16_t get_cid(nvm_queue_t*) {
    unsigned cid=allocated++%8;
    assert(active.insert(cid).second && !notices[cid].ready);
    return cid;
}
static void put_cid(nvm_queue_t*,uint16_t cid) { assert(active.erase(cid)==1); }
static void nvm_cmd_header(nvm_cmd_t* c,uint16_t cid,int,unsigned) { c->cid=cid; }
static void nvm_cmd_data_ptr(nvm_cmd_t* c,uint64_t page,uint64_t second) { c->page=page; assert(second==page+10000); }
static void nvm_cmd_rw_blks(nvm_cmd_t* c,uint64_t lba,uint64_t blocks) { c->lba=lba; assert(blocks==1); }
constexpr int NVM_IO_READ=2;
static void sq_enqueue(nvm_queue_t*,nvm_cmd_t* c) {
    pending.push_back(*c); commands.push_back(*c); ++issued;
    // No notification is produced until the whole expected batch was submitted.
    if(pending.size()!=expected) return;
    std::set<uint64_t> pages;
    for(auto& cmd:pending) assert(pages.insert(cmd.page).second);
    // Reverse CID completion order. CQ order and request order deliberately differ.
    for(auto it=pending.rbegin();it!=pending.rend();++it) {
        unsigned pos=published++ & 15;
        assert(!table.ready[pos]);
        table.entries[pos].sq_head=issued&15;
        table.ready[pos]=1;
        notices[it->cid].status=inject_error ? 1 : 0;
        notices[it->cid].ready=1;
    }
    pending.clear();
    expected=(total-issued<window ? total-issued : window);
}
static void __nanosleep(unsigned) { throw std::runtime_error("wait before full batch submission"); }
static void batch_cq_doorbell(nvm_queue_t* cq,uint32_t head) {
    assert(cq->head.load()==head);
    ++doorbells;
}
''' + drain + kernel
        checks = r'''
int main() {
    for(unsigned batch: {1u,3u,8u}) for(unsigned reqs: {2u,8u,101u}) for(bool random: {false,true}) {
        QueuePair qp;
        Controller ctrl{&qp}; Controller* controllers[]={&ctrl};
        uint64_t prp1[512],prp2[512];
        for(unsigned i=0;i<512;++i) { prp1[i]=i; prp2[i]=i+10000; }
        page_cache_d_t pc{prp1,prp2};
        copilot_notify_entry* ptrs[]={notices}; copilot_cq_table* tables[]={&table};
        issued=published=allocated=doorbells=0;
        total=reqs; window=(reqs<batch ? reqs : batch); expected=window;
        commands.clear();
        threadIdx.x=17;
        read_kernel(controllers,&pc,64,reqs,1024,1,random,batch,ptrs,tables);
        assert(issued==reqs && active.empty() && pending.empty());
        assert(qp.cq.head==reqs && qp.cq.tail==reqs && qp.sq.head==reqs);
        assert(doorbells==(reqs+window-1)/window);
        uint64_t seed=17*6364136223846793005ULL+1442695040888963407ULL;
        for(unsigned i=0;i<reqs;++i) {
            seed^=seed<<13; seed^=seed>>7; seed^=seed<<17;
            assert(commands[i].lba==(random ? seed%1024 : (17+i)%1024));
            assert(commands[i].page==17*window+(i%window));
        }
    }
    // An NVMe error must abort instead of publishing a successful IOPS result.
    QueuePair qp; Controller ctrl{&qp}; Controller* controllers[]={&ctrl};
    uint64_t p1[64]={},p2[64]; for(auto& p:p2) p=10000;
    page_cache_d_t pc{p1,p2}; copilot_notify_entry* ptrs[]={notices}; copilot_cq_table* tables[]={&table};
    issued=published=allocated=0; expected=total=window=1; threadIdx.x=0; inject_error=true;
    try { read_kernel(controllers,&pc,64,1,1024,1,false,1,ptrs,tables); assert(false); }
    catch(const std::runtime_error& e) { assert(std::string(e.what())=="NVMe status"); }
}
'''
        with tempfile.TemporaryDirectory() as d:
            source, binary = Path(d) / 'batch.cpp', Path(d) / 'batch'
            source.write_text(prelude + checks)
            subprocess.run(['c++', '-std=c++17', '-fsanitize=undefined',
                            '-I' + str(ROOT / 'copilotio/copilot-io/include'), str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    unittest.main()
