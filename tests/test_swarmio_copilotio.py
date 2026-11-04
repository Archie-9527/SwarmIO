"""CPU-side protocol regression; CUDA execution remains a target-machine check."""
import importlib.util
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
struct copilot_notify_entry { volatile uint32_t ready; uint32_t pos,loc,cq_head; };
struct copilot_cq_table_entry { uint16_t cid,sq_head; };
struct copilot_cq_table { volatile uint32_t ready[4096]; copilot_cq_table_entry entries[4096]; };
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
            subprocess.run(['c++','-std=c++17','-pthread',str(source),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True,timeout=10)

    def test_compare_pairs_same_workload_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            calls=[]
            def run(args):
                calls.append((args.engine,args.threads,args.reqs,args.random,args.queue_depth))
                p=base/args.engine; p.mkdir()
                (p/'bench.log').write_text('Ops/sec: '+('100' if args.engine=='bam' else '200'))
                return p
            args=SimpleNamespace(threads=None,reqs=None,num_queues=None,queue_depth=None)
            with patch.object(pipeline,'ROOT',base), patch.object(pipeline,'run',side_effect=run):
                pipeline.compare(args)
            self.assertEqual(calls,[('bam',1024,8,True,512),('copilotio',1024,8,True,512)])
            self.assertEqual(len(list((base/'results').glob('compare-*/comparison.csv'))),1)
            with patch.object(pipeline,'run',side_effect=pipeline.Error('failed')) as failed:
                with self.assertRaises(pipeline.Error): pipeline.compare(args)
                self.assertEqual(failed.call_count,1)

    def test_exclusive_buffer_pages(self):
        c=pipeline.read_config(ROOT/'configs/bam-cpu.json')
        c['benchmark']['threads']=8192
        with self.assertRaises(pipeline.Error): pipeline.validate_benchmark(c['benchmark'],c)
