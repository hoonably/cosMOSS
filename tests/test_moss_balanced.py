"""Minimal balanced contexts, explicit consent, sequential inference and offsets."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch

from src import speech_engines as engines
from src.speech_engines import balanced_moss_ranges, transcribe_moss_balanced, missing_moss_partition_tail
from src.speech_pipeline import estimate_pipeline


class BalancedMossTests(unittest.TestCase):
    def test_selected_context_limit_stays_balanced_without_gaps(self):
        self.assertEqual(balanced_moss_ranges(5400,1800),[(0,1800),(1800,3600),(3600,5400)])
        self.assertEqual(balanced_moss_ranges(6000,3600),[(0,3000),(3000,6000)])
        for invalid in (0,-1,5401,float('nan')):
            with self.assertRaises(ValueError):balanced_moss_ranges(5400,invalid)

    def test_oom_restarts_only_after_completed_partition_and_halves_failed_range(self):
        calls=[];events=[]
        def run(**options):
            calls.append(options)
            if len(calls)==1:raise RuntimeError(engines.CUDA_OOM_MESSAGE)
            return dict(items=[{'start':3600,'end':7200}],chunks=[{'offset':3600}],
                        ranges=[[3600,5400],[5400,7200]],load_seconds=3,inference_seconds=4)
        saved=dict(phase='processing',start_offset=0,completed_until=3600,items=[{'start':0,'end':3600}],
                   chunks=[{'offset':0}],ranges=[[0,3600]],load_seconds=1,inference_seconds=2)
        result=engines.run_moss_with_memory_retries(7200,run,lambda:saved,lambda **e:events.append(e),max_seconds=3600)
        self.assertEqual(calls[1]['start_offset'],3600)
        self.assertEqual(calls[1]['moss_chunk_seconds'],1800)
        self.assertEqual(result['ranges'],[[0,3600],[3600,5400],[5400,7200]])
        self.assertEqual(result['partition_count'],3)
        self.assertEqual(result['load_seconds'],4)
        self.assertEqual(result['inference_seconds'],6)
        self.assertEqual(events[0]['resume_offset'],3600)

    def test_short_file_oom_halves_actual_input_not_the_ninety_minute_ceiling(self):
        calls=[]
        def run(**options):
            calls.append(options)
            if len(calls)==1:raise RuntimeError(engines.CUDA_OOM_MESSAGE)
            return dict(items=[],chunks=[],ranges=[[0,1800],[1800,3600]],load_seconds=0,inference_seconds=1)
        saved=dict(phase='processing',start_offset=0,completed_until=0)
        engines.run_moss_with_memory_retries(3600,run,lambda:saved,lambda **_:None)
        self.assertEqual(calls[1]['moss_chunk_seconds'],1800)

    def test_engine_capacity_rejections_retry_but_normal_token_truncation_does_not(self):
        self.assertTrue(engines.is_moss_capacity_error('Request prompt exceeds the KV memory budget: input_len=60000.'))
        self.assertTrue(engines.is_moss_capacity_error('Input length (71687 tokens) exceeds the maximum allowed length (69136 tokens). Use a shorter input or enable --allow-auto-truncate.'))
        self.assertFalse(engines.is_moss_capacity_error('Requested token count (10 input + 65536 new) exceeds the context. Truncating max_new_tokens.'))
        self.assertFalse(engines.is_moss_capacity_error('ServerArgs(max_total_tokens=131072, context_length=131072)'))
        run=Mock(side_effect=[RuntimeError(engines.MOSS_CAPACITY_MESSAGE),
            dict(items=[],chunks=[],ranges=[[0,2700],[2700,5400]],load_seconds=1,inference_seconds=2)])
        result=engines.run_moss_with_memory_retries(5400,run,
            lambda:dict(phase='processing',start_offset=0,completed_until=0),lambda **_:None)
        self.assertEqual(result['moss_chunk_seconds'],2700)

    def test_non_memory_and_startup_errors_do_not_repeat_and_retries_are_bounded(self):
        for message,phase in [('invalid audio','processing'),(engines.CUDA_OOM_MESSAGE,'loading')]:
            run=Mock(side_effect=RuntimeError(message))
            with self.assertRaises(RuntimeError):
                engines.run_moss_with_memory_retries(5400,run,lambda:dict(phase=phase,start_offset=0),lambda **_:None)
            self.assertEqual(run.call_count,1)
        run=Mock(side_effect=RuntimeError(engines.CUDA_OOM_MESSAGE))
        with self.assertRaises(RuntimeError):
            engines.run_moss_with_memory_retries(5400,run,
                lambda:dict(phase='processing',start_offset=0,completed_until=0),lambda **_:None,max_retries=2)
        self.assertEqual(run.call_count,3)

    def test_minimum_equal_ranges_include_exact_boundary_and_fractional_durations(self):
        for duration,count,seconds in [(5400,1,5400),(6000,2,3000),(12600,3,4200),(5400.1,2,2700.05)]:
            parts=balanced_moss_ranges(duration)
            self.assertEqual(len(parts),count)
            self.assertEqual(parts[0][0],0)
            self.assertEqual(parts[-1][1],duration)
            for i,(start,end) in enumerate(parts):
                self.assertAlmostEqual(end-start,seconds)
                self.assertLessEqual(end-start,5400)
                if i:self.assertEqual(start,parts[i-1][1])

    def test_210_minutes_are_requested_sequentially_with_global_timestamps(self):
        calls=[];events=[]
        def infer(start,end,maximum):
            calls.append((start,end))
            return {'text':f'[0][S01]part {len(calls)}[{end-start}]'}
        rows,chunks=transcribe_moss_balanced(12600,infer,lambda **event:events.append(event),allow_split=True)
        self.assertEqual(calls,[(0,4200),(4200,8400),(8400,12600)])
        self.assertEqual([(r['start'],r['end']) for r in rows],calls)
        self.assertEqual(len(chunks),3)
        self.assertEqual(events[-1]['completed'],3)
        with self.assertRaisesRegex(ValueError,'확인'):
            transcribe_moss_balanced(6000,infer,lambda **_:None)

    def test_truncated_first_partition_only_retries_its_remaining_audio(self):
        calls=[]
        def infer(start,end,maximum):
            calls.append((start,end))
            if start==0:return {'text':'[0][S01]prefix[2500][2500'}
            return {'text':f'[0][S01]tail[{end-start}]'}
        rows,_=transcribe_moss_balanced(6000,infer,lambda **_:None,allow_split=True)
        self.assertEqual(calls,[(0,3000),(2500,3000),(3000,6000)])
        self.assertEqual([r['end'] for r in rows],[2500,3000,6000])

    def test_missing_middle_tail_is_detected_and_recovered_without_repeating_later_parts(self):
        items=[dict(start=0,end=4100),dict(start=4200,end=8400),dict(start=8400,end=12600)]
        turns=[dict(start=0,end=4200),dict(start=4200,end=8400),dict(start=8400,end=12600)]
        self.assertEqual(missing_moss_partition_tail(items,turns,12600),(4100,4200))
        calls=[]
        def infer(start,end,maximum):
            calls.append((start,end));return {'text':f'[0][S01]recovered[{end-start}]'}
        rows,_=transcribe_moss_balanced(12600,infer,lambda **_:None,allow_split=True,start_offset=4100,end_offset=4200)
        self.assertEqual(calls,[(4100,4200)])
        self.assertIsNone(missing_moss_partition_tail(items+rows,turns,12600))

    def test_long_memory_is_peak_per_partition_and_time_is_sequential_sum(self):
        gpu=dict(name='NVIDIA H200',memory_total_mib=143771,memory_free_mib=140000)
        short=estimate_pipeline('moss-sglang',4200,gpu)
        long=estimate_pipeline('moss-sglang',12600,gpu)
        self.assertEqual(long['chunk_count'],3)
        self.assertEqual(long['memory_expected_gib'],short['memory_expected_gib'])
        self.assertAlmostEqual(long['time_seconds'],3*(short['time_seconds']-short['setup_seconds'])+short['setup_seconds'],delta=1)

    def test_worker_crops_only_each_partition_and_loads_the_model_once(self):
        spec=importlib.util.spec_from_file_location('moss_worker_fixture',Path(engines.__file__).with_name('speech_engine_worker.py'))
        worker=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'speech_engines':engines}):spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input.wav';output=root/'result.json'
            # Low sample rate keeps this protocol test small while exercising
            # the same sample-accurate crop offsets as the real 16 kHz input.
            with wave.open(str(source),'wb') as stream:
                stream.setparams((1,2,2,12000,'NONE','not compressed'))
                stream.writeframes(b'\x01\0'*6000+b'\x02\0'*6000)
            seen=[]
            def post(url,**kwargs):
                with wave.open(kwargs['files']['file'][1],'rb') as stream:
                    seen.append((stream.getnframes(),stream.readframes(1)))
                return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'text':'[0][S01]part[3000]'})
            client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
            client.get.return_value=SimpleNamespace(status_code=200);client.post.side_effect=post
            child=Mock();child.poll.return_value=None
            with patch('httpx.Client',return_value=client),patch.object(worker.subprocess,'Popen',return_value=child) as launch:
                worker.moss(dict(duration=6000,audio=str(source),output=str(output),paths={'moss':'fixture'},
                                 allow_moss_split=True,ipc_path=str(root/'ipc')))
            self.assertEqual(seen,[(6000,b'\x01\0'),(6000,b'\x02\0')])
            self.assertEqual(launch.call_count,1)
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index('--endpoints.base_path')+1], str(root/'ipc'))
            self.assertFalse(output.with_suffix('.remaining.wav').exists())
            result=json.loads(output.read_text())
            self.assertEqual(result['items'][-1]['end'],6000)

    def test_worker_checkpoint_and_fresh_restart_preserve_completed_audio(self):
        spec=importlib.util.spec_from_file_location('moss_checkpoint_fixture',Path(engines.__file__).with_name('speech_engine_worker.py'))
        worker=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'speech_engines':engines}):spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input.wav';output=root/'result.json';seen=[]
            with wave.open(str(source),'wb') as stream:
                stream.setparams((1,2,2,12000,'NONE','not compressed'))
                stream.writeframes(b'\x01\0'*6000+b'\x02\0'*6000)
            def post(url,**kwargs):
                with wave.open(kwargs['files']['file'][1],'rb') as audio:
                    sample=audio.readframes(1);seconds=audio.getnframes()/audio.getframerate()
                seen.append((sample,seconds))
                if len(seen)==2:raise RuntimeError(engines.CUDA_OOM_MESSAGE)
                return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'text':f'[0][S01]part[{seconds}]'})
            client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
            client.get.return_value=SimpleNamespace(status_code=200);client.post.side_effect=post
            children=[Mock(),Mock()]
            for child in children:child.poll.return_value=None
            def run(**options):
                worker.moss(dict(duration=6000,audio=str(source),output=str(output),paths={'moss':'fixture'},
                                 ipc_path=str(root/'ipc'),**options))
                return json.loads(output.read_text())
            with patch('httpx.Client',return_value=client),patch.object(worker.subprocess,'Popen',side_effect=children) as launch:
                result=engines.run_moss_with_memory_retries(6000,run,
                    lambda:json.loads(output.with_suffix('.checkpoint.json').read_text()),lambda **_:None,max_seconds=3000)
            self.assertEqual(seen,[(b'\x01\0',3000),(b'\x02\0',3000),(b'\x02\0',1500),(b'\x02\0',1500)])
            self.assertEqual([(r['start'],r['end']) for r in result['items']],[(0,3000),(3000,4500),(4500,6000)])
            self.assertEqual(result['partition_count'],3)
            self.assertEqual(launch.call_count,2)
            for child in children:child.terminate.assert_called_once()


class LongMossConsentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.test_audio_join import AudioJoinTests
        self.case=AudioJoinTests();self.case.setUp()
        self.addCleanup(self.case.doCleanups);self.addAsyncCleanup(self.case.client.aclose)

    async def submit(self, identity, **changes):
        from tests.test_audio_join import GPU
        return await self.case.client.post('/api/notes',data=dict(upload_id=identity,
            device_id=self.case.device['id'],gpu_uuid=GPU,speech_pipeline='moss-sglang',**changes))

    async def test_no_confirmation_never_consumes_upload_and_confirmation_is_snapshotted_and_forwarded(self):
        case=self.case;identity,_=case.upload(10);case.store.update_upload(identity,duration=12600)
        response=await self.submit(identity)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['detail']['code'],'moss_split_confirmation_required')
        self.assertEqual(response.json()['detail']['count'],3)
        self.assertEqual(response.json()['detail']['seconds'],4200)
        self.assertTrue(case.queue.empty());self.assertEqual(len(case.store.list_uploads()),1)
        response=await self.submit(identity,allow_moss_split='true')
        self.assertEqual(response.status_code,202,response.text)
        note=case.store.get(identity);snapshot=json.loads(note['device_snapshot_json'])
        self.assertTrue(snapshot['options']['allow_moss_split'])
        self.assertEqual(note['chunk_count'],3)
        async def execute(host,key,action,event,**options):
            if action=='inspect':return {'phase':'installed'}
            self.assertTrue(options['allow_moss_split'])
            event(dict(phase='transcribing',partition_index=2,partition_count=3,chunk_count=3,completed=1))
            self.assertEqual(case.store.get(identity)['model_progress']['partition_index'],2)
            return {'result':dict(segments=[dict(id=0,start=0,end=12600,speaker='S01',text='complete')],chunk_count=3)}
        with patch.object(case.main.SPEECH,'execute',side_effect=execute),patch.object(case.main.SPEECH,'record'):
            await case.main.transcribe_fast(note,snapshot,time.monotonic())
        self.assertEqual(case.store.get(identity)['status'],'done')

    async def test_joined_long_inputs_are_not_copied_or_consumed_before_consent(self):
        case=self.case;first,_=case.upload(10);second,_=case.upload(20)
        case.store.update_upload(first,duration=3000);case.store.update_upload(second,duration=3000)
        with patch.object(case.main,'join_recordings') as join:
            response=await case.submit([first,second])
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['detail']['count'],2)
        join.assert_not_called()
        self.assertEqual(len(case.store.list_uploads()),2)
        self.assertTrue(case.queue.empty())

    async def test_gpu_specific_split_below_ninety_minutes_requires_the_actual_plan(self):
        case=self.case;identity,_=case.upload(10);case.store.update_upload(identity,duration=5400)
        with patch('src.moss_context.recommend_moss_context',return_value={'seconds':1800,'measured':False}):
            response=await self.submit(identity)
            self.assertEqual(response.status_code,409,response.text)
            self.assertEqual(response.json()['detail']['count'],3)
            self.assertEqual(response.json()['detail']['max_seconds'],1800)
            self.assertTrue(case.queue.empty())
            response=await self.submit(identity,allow_moss_split='true')
            self.assertEqual(response.status_code,202,response.text)
            note=case.store.get(identity)
            self.assertEqual(note['chunk_count'],3)
            self.assertEqual(json.loads(note['device_snapshot_json'])['options']['moss_chunk_seconds'],1800)
