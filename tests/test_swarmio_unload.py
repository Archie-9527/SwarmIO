"""Ordinary unload after failed GPU runs; no real module operations in these tests."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from test_swarmio_dsa import pipeline

ROOT=Path(__file__).resolve().parents[1]

class FailedUnloadTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self,case='ok',first_code=0):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp); build=b/'build'; build.mkdir(); mods=b/'modules'
            (build/'manifest.json').write_text('{}')
            for name in ('swarmio','libnvm'):
                (mods/name/'parameters').mkdir(parents=True)
                (mods/name/'refcnt').write_text('1' if case=='busy' and name=='libnvm' else '0')
            (mods/'swarmio/parameters/sq_diag_faulted').write_text('Y' if case=='sq_fault' else 'N')
            boot=b/'boot'; boot.write_text('boot')
            taint=b/'taint'; taint.write_text('128' if case=='oops' else '0')
            session=dict(run_state='running' if case=='running' else 'incomplete',boot_id='other' if case=='boot' else 'boot',
                         manifest_sha256='wrong' if case=='manifest' else pipeline.digest(build/'manifest.json'))
            pipeline.save_json(build/'loaded.json',session)
            c=pipeline.read_config(ROOT/('configs/bam-cpu.json' if case=='cpu' else 'configs/bam-dsa-sq-lab230.json'))
            state=dict(version=3,device=0,borrowed=8,fault=0,mode=1,submitted=64,completed=64,bytes=32768,errors=0,timeouts=0,
                       batches_submitted=2,batches_retired=2,active_batches=0,pending_batches=0,
                       sq_submitted=4,sq_completed=4,sq_entries=64,sq_bytes=4096,sq_pending=0)
            if case=='unbalanced': state['completed']=63
            if case=='dma_error': state['errors']=1
            if case=='batch_mismatch': state['batches_retired']=1
            if case=='sq_mismatch': state['sq_completed']=3
            if case=='moving': second=dict(state,submitted=65,completed=65)
            else: second=dict(state)
            first=Mock(pid=111); first.wait.return_value=first_code
            second_process=Mock(pid=112); second_process.wait.return_value=0
            states={'libnvm':'Live','swarmio':'Unloading' if case=='going' else 'Live'}
            paths={'/proc/sys/kernel/random/boot_id':boot,'/proc/sys/kernel/tainted':taint}
            no_workload=Mock(side_effect=pipeline.Error('workload still exists') if case=='workload' else None)
            read_state=Mock(side_effect=[state,second])
            if case=='inflight': read_state.side_effect=pipeline.Error('DSA still active')
            with contextlib.ExitStack() as stack:
                for name,value in [('ROOT',b),('MODULES',mods)]: stack.enter_context(patch.object(pipeline,name,value))
                stack.enter_context(patch.object(pipeline,'Path',side_effect=lambda p:paths.get(str(p),Path(p))))
                stack.enter_context(patch.object(pipeline,'linux_only'))
                stack.enter_context(patch.object(pipeline,'check_no_workload',no_workload))
                stack.enter_context(patch.object(pipeline,'dmesg_text',return_value=''))
                stack.enter_context(patch.object(pipeline,'manifest_for',return_value={'config':c}))
                stack.enter_context(patch.object(pipeline,'module_state',return_value=states))
                stack.enter_context(patch.object(pipeline,'read_dsa_state',read_state))
                stack.enter_context(patch.object(pipeline.time,'sleep'))
                execute=stack.enter_context(patch.object(pipeline.subprocess,'Popen',side_effect=[first,second_process]))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                yield SimpleNamespace(build_dir=build,after_failure=True),execute,b,session
                first.kill.assert_not_called(); first.terminate.assert_not_called()
                second_process.kill.assert_not_called(); second_process.terminate.assert_not_called()

    def test_drained_failure_unloads_in_order_and_preserves_evidence(self):
        with self.fixture() as (args,execute,base,previous):
            pipeline.unload(args)
            self.assertEqual([call.args[0] for call in execute.call_args_list],[['rmmod','libnvm'],['rmmod','swarmio']])
            self.assertFalse((args.build_dir/'loaded.json').exists())
            evidence=json.loads(next((base/'results').glob('unload-check-*.json')).read_text())
            self.assertEqual(evidence['previous_session']['run_state'],'incomplete')
            self.assertEqual(evidence['module_refs'],{'libnvm':0,'swarmio':0})
            self.assertEqual(evidence['dsa_before'],evidence['dsa_after'])

    def test_uncertain_or_busy_failure_never_invokes_rmmod(self):
        for case in ('busy','sq_fault','oops','boot','manifest','going','workload','inflight',
                     'unbalanced','dma_error','batch_mismatch','sq_mismatch','moving','running','cpu'):
            with self.subTest(case=case), self.fixture(case) as (args,execute,base,previous):
                with self.assertRaises(pipeline.Error): pipeline.unload(args)
                execute.assert_not_called()
                self.assertEqual(json.loads((args.build_dir/'loaded.json').read_text()),previous)

    def test_libnvm_rejection_stops_without_unloading_swarmio_or_marking_success(self):
        with self.fixture(first_code=1) as (args,execute,base,previous):
            with self.assertRaises(pipeline.Error): pipeline.unload(args)
            self.assertEqual(execute.call_count,1)
            self.assertEqual(execute.call_args.args[0],['rmmod','libnvm'])
            self.assertEqual(json.loads((args.build_dir/'loaded.json').read_text())['run_state'],'incomplete')

if __name__=='__main__': unittest.main()
