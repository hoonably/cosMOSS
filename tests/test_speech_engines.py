"""Public preset contracts, private auxiliary text, and engine-specific estimates."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.speech_engines import CUDA_OOM_MESSAGE, ENGINE_VERSION, stages
from src.speech_pipeline import PIPELINES, DEFAULT_PIPELINE, estimate_pipeline
from src.speech_jobs import SpeechJobs
from src.store import NoteStore
from src.devices import DeviceStore

GPU = dict(name='NVIDIA H200', memory_total_mib=143771, memory_free_mib=140000)


class PlanningTests(unittest.TestCase):
    def test_device_defaults_upgrade_on_restart_without_changing_mac_or_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'devices.db'
            devices=DeviceStore(path)
            remote=devices.add_ssh(name='server',host='fixture',remote_port=8001,backend='cuda',model='old')
            devices.configure(remote['id'],model_selected=True,options={'speech_pipeline':'moss-batch'})
            devices.configure('local',model_key='mlx-8bit',options={'kv_bits':8})
            reopened=DeviceStore(path)
            self.assertEqual(reopened.get(remote['id'])['options']['speech_pipeline'],'moss-sglang')
            self.assertEqual(reopened.get('local')['options'],{'kv_bits':8})

    def test_only_three_new_server_choices_and_moss_full_default(self):
        self.assertEqual([p['key'] for p in PIPELINES], ['moss-sglang','qwen-transformers','moss-qwen-sequential'])
        self.assertEqual(DEFAULT_PIPELINE,'moss-sglang')
        self.assertEqual(stages('moss-qwen-sequential'), [('moss','sglang'),('qwen','qwen'),('nemotron','nemotron')])
        self.assertEqual(stages('qwen-transformers'), [('qwen','qwen'),('aligner','qwen'),('nemotron','nemotron')])

    def test_full_context_up_to_90_minutes_and_balanced_long_plans(self):
        moss=estimate_pipeline('moss-sglang',3937.13,GPU,24)
        dual=estimate_pipeline('moss-qwen-sequential',3937.13,GPU)
        self.assertEqual((moss['chunk_count'],moss['batch']),(1,1))
        self.assertEqual(dual['chunk_count'],1)
        self.assertEqual(dual['batch'],4)
        self.assertLess(dual['memory_expected_gib'],45)
        long = estimate_pipeline('moss-sglang',6000,GPU)
        self.assertEqual((long['chunk_count'],long['chunk_seconds']),(2,3000))
        self.assertTrue(long['calibration_extrapolated'])
        self.assertGreater(estimate_pipeline('qwen-transformers',5401,GPU)['chunk_count'],1)
        self.assertFalse(estimate_pipeline('moss-sglang',100,{**GPU,'memory_total_mib':24576})['exceeds_capacity'])

    def test_split_prediction_charges_setup_once_and_learns_without_polluting_short_curve(self):
        part = estimate_pipeline('moss-sglang', 4200, GPU)
        long = estimate_pipeline('moss-sglang', 12600, GPU)
        self.assertAlmostEqual(long['time_seconds'],
            (part['time_seconds'] - part['setup_seconds'])*3 + part['setup_seconds'], delta=1)
        with tempfile.TemporaryDirectory() as folder:
            jobs = SpeechJobs(Path(folder)/'samples.db')
            jobs.record(dict(pipeline='moss-sglang', runtime=ENGINE_VERSION,
                segments=[{'text':'not stored'}], peak_process_mib=25000, batch=1,
                audio_seconds=12600, inference_seconds=570, load_seconds=30,
                capacity_gib=GPU['memory_total_mib']/1024, gpu_name='NVIDIA H200', moss_partition_count=3))
            rows = jobs.observations()
            self.assertEqual(len(rows), 1)
            learned = estimate_pipeline('moss-sglang',12600,GPU,observations=rows)
            self.assertEqual(learned['time_seconds'],600)
            self.assertEqual(learned['time_source'],'observed')
            self.assertEqual(estimate_pipeline('moss-sglang',4200,GPU,observations=rows)['time_seconds'],part['time_seconds'])
            self.assertEqual(estimate_pipeline('moss-sglang',12600,{**GPU,'name':'RTX 3090'},observations=rows)['time_source'],'unmeasured')

    def test_other_gpu_split_estimate_also_charges_observed_setup_once(self):
        gpu = {**GPU, 'name':'NVIDIA RTX 6000 Ada Generation', 'memory_total_mib':48*1024}
        row = dict(pipeline='moss-sglang',runtime=ENGINE_VERSION,batch=1,
            gpu_name='rtx 6000 ada generation',capacity_gib=48,audio_seconds=4200,
            inference_seconds=300,load_seconds=30,peak_process_gib=25)
        result = estimate_pipeline('moss-sglang',12600,gpu,observations=[row])
        self.assertEqual(result['time_seconds'],930)
        self.assertEqual(result['setup_seconds'],30)
        self.assertEqual(result['inference_seconds'],900)

    def test_history_does_not_mix_old_engine_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=SpeechJobs(Path(folder)/'samples.db')
            sample=dict(pipeline='moss-sglang',runtime=ENGINE_VERSION,segments=[{'text':'secret'}],
                peak_process_mib=25000,batch=1,audio_seconds=100,inference_seconds=10,load_seconds=20,
                capacity_gib=48,gpu_name='Fixture GPU')
            jobs.record(sample)
            observations=jobs.observations()
            self.assertNotIn('secret',json.dumps(observations))
            gpu={**GPU,'name':sample['gpu_name'],'memory_total_mib':48*1024}
            result=estimate_pipeline('moss-sglang',200,gpu,observations=observations)
            self.assertEqual(result['time_seconds'],40)
            self.assertIsNone(estimate_pipeline('moss-qwen-sequential',200,gpu,observations=observations)['time_seconds'])


class EngineAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_cuda_oom_cause_survives_generic_remote_stage_failure(self):
        class Process:
            returncode = 1
            stdin = None

            async def wait(self):
                return self.returncode

        async def lines(detail):
            for line in ['COSMOSS_SPEECH:{"phase":"loading","engine":"moss"}', detail,
                         'COSMOSS_SPEECH:{"phase":"error","error":"전사 엔진 단계가 실패했습니다."}']:
                yield (line+'\n').encode()

        with tempfile.TemporaryDirectory() as folder:
            jobs=SpeechJobs(Path(folder)/'samples.db')
            for detail,expected in [('torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1 GiB',CUDA_OOM_MESSAGE),
                                    ('ValueError: invalid audio','전사 엔진 단계가 실패했습니다.')]:
                process=Process();process.stdout=lines(detail)
                with patch('src.speech_jobs.discover_hosts',return_value=['fixture']), \
                     patch('src.speech_jobs.asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)):
                    with self.assertRaisesRegex(RuntimeError,'메모리 부족' if expected==CUDA_OOM_MESSAGE else '엔진 단계') as caught:
                        await jobs.execute('fixture','moss-sglang','run',lambda _:None,batch=1)
                    self.assertEqual(str(caught.exception),expected)

    async def test_memory_failure_remains_a_saved_job_after_reopening(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=NoteStore(root/'notes.db');identity='a'*32
            audio=store.upload_directory(identity)/'audio.wav';audio.parent.mkdir(parents=True);audio.write_bytes(b'fixture')
            device={'id':'remote','backend':'cuda','host':'fixture','gpu_uuid':'GPU-fixture',
                    'options':{'speech_pipeline':'moss-sglang'}}
            store.create({'id':identity,'title':'메모리 오류 작업','original_filename':'audio.wav','source_path':str(audio),
                          'duration':3600,'status':'queued','device_id':'remote','device_snapshot_json':json.dumps(device)})
            with patch.object(main,'STORE',store),patch.object(main.DEVICES,'get',return_value=device), \
                 patch.object(main.MEDIA_TOOLS,'require_ready',new=AsyncMock()), \
                 patch.object(main,'transcribe_fast',new=AsyncMock(side_effect=RuntimeError(CUDA_OOM_MESSAGE))):
                await main.transcribe(identity)
            saved=NoteStore(root/'notes.db').get(identity)
            self.assertEqual(saved['status'],'error')
            self.assertEqual(saved['error'],CUDA_OOM_MESSAGE)
            self.assertEqual(saved['title'],'메모리 오류 작업')

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch('src.job_artifacts.DEFAULT_DOWNLOADS', Path(temporary.name) / 'Downloads'))

    async def test_download_endpoint_starts_real_background_task_on_event_loop(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            devices = DeviceStore(root / 'devices.db')
            device = devices.add_ssh(name='server', host='fixture', remote_port=8001,
                                     backend='cuda', model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
            devices.configure(device['id'], model_selected=True, model_key='cuda-original',
                              options={'speech_pipeline':'moss-sglang'})
            jobs = SpeechJobs(root / 'samples.db')
            entered, release = asyncio.Event(), asyncio.Event()
            async def execute(host, pipeline, action, on_event):
                entered.set()
                await release.wait()
                on_event({'phase':'installed'})
                return {'phase':'installed'}
            with patch.object(main, 'DEVICES', devices), patch.object(main, 'SPEECH', jobs), \
                 patch('src.speech_jobs.discover_hosts', return_value=['fixture']), \
                 patch.object(jobs, 'execute', side_effect=execute):
                try:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost') as client:
                        for preset in PIPELINES:
                            key = preset['key']
                            response = await client.post(f"/api/devices/{device['id']}/speech-setup", json={'pipeline':key})
                            self.assertEqual(response.status_code, 202, response.text)
                            await asyncio.wait_for(entered.wait(), 1)
                            task = jobs.tasks[('fixture', key)]
                            # Repeated clicks share the same running installation.
                            response = await client.post(f"/api/devices/{device['id']}/speech-setup", json={'pipeline':key})
                            self.assertEqual(response.status_code, 202, response.text)
                            self.assertIs(jobs.tasks[('fixture', key)], task)
                            release.set()
                            await asyncio.wait_for(task, 1)
                            self.assertEqual(jobs.status('fixture', key)['phase'], 'installed')
                            entered.clear()
                            release.clear()
                finally:
                    await jobs.close()

    async def test_per_note_choice_and_download_leave_device_default_unchanged(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); store=NoteStore(root/'notes.db'); devices=DeviceStore(root/'devices.db')
            device=devices.add_ssh(name='server',host='fixture',remote_port=8001,backend='cuda',model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
            devices.configure(device['id'],model_selected=True,model_key='cuda-original',options={'speech_pipeline':'moss-sglang'})
            gpu={**GPU,'uuid':'GPU-11111111-2222-3333-4444-555555555555','index':0,'idle_candidate':True}
            with patch.object(main,'STORE',store),patch.object(main,'DEVICES',devices),patch.object(main,'QUEUE',asyncio.Queue()), \
                 patch.object(main,'probe_host',new=AsyncMock(return_value={'gpus':[gpu]})), \
                 patch.object(main.SPEECH,'observations',return_value=[]), \
                 patch.object(main.SPEECH,'install',return_value={'phase':'connecting'}) as install:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    for i,preset in enumerate(PIPELINES):
                        key=preset['key']; identity=f'{i+1:032x}'
                        audio=store.upload_directory(identity)/'a.wav'; audio.parent.mkdir(parents=True); audio.write_bytes(b'fixture')
                        store.create_upload(dict(id=identity,original_filename='a.wav',source_path=str(audio),
                            normalized_path=str(audio),status='ready',duration=60))
                        response=await client.post(f"/api/devices/{device['id']}/speech-setup",json={'pipeline':key})
                        self.assertEqual(response.status_code,202,response.text)
                        install.assert_called_with('fixture',key)
                        response=await client.post('/api/notes',data=dict(upload_id=identity,device_id=device['id'],
                            gpu_uuid=gpu['uuid'],speech_pipeline=key))
                        self.assertEqual(response.status_code,202,response.text)
                        snapshot=json.loads(store.get(response.json()['id'])['device_snapshot_json'])
                        self.assertEqual(snapshot['options']['speech_pipeline'],key)
                        expected='Qwen/Qwen3-ASR-1.7B' if key=='qwen-transformers' else 'OpenMOSS-Team/MOSS-Transcribe-Diarize'
                        self.assertEqual(snapshot['model'],expected)
                        self.assertEqual(devices.get(device['id'])['options'],{'speech_pipeline':'moss-sglang'})

    async def test_select_each_server_choice_without_install_and_mac_rejects_cuda(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            devices=DeviceStore(Path(folder)/'devices.db')
            device=devices.add_ssh(name='server',host='fixture',remote_port=8001,backend='cuda',model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
            with patch.object(main,'DEVICES',devices),patch.object(main.STORE,'list',return_value=[]), \
                 patch.object(main.SPEECH,'install') as install,patch.object(main.SPEECH,'tasks',{}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    for preset in PIPELINES:
                        response=await client.post(f"/api/devices/{device['id']}/model/selection",json={'pipeline':preset['key']})
                        self.assertEqual(response.status_code,200,response.text)
                        self.assertEqual(response.json()['options']['speech_pipeline'],preset['key'])
                    response=await client.post('/api/devices/local/model/selection',json={'pipeline':'moss-sglang'})
                    self.assertEqual(response.status_code,400)
                    self.assertEqual((await client.get('/api/speech/pipelines')).json()['default'],'moss-sglang')
                install.assert_not_called()

    async def test_auxiliary_is_preserved_separately_downloadable_and_not_in_normal_note(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); store=NoteStore(root/'notes.db')
            identity='f'*32; audio=store.upload_directory(identity)/'audio.wav'
            audio.parent.mkdir(parents=True); audio.write_bytes(b'fixture')
            device=dict(id='fixture',backend='cuda',host='fixture',gpu_uuid='GPU-fixture',gpu_hardware=GPU,
                options=dict(speech_pipeline='moss-qwen-sequential'))
            note=store.create(dict(id=identity,title='테스트',status='queued',source_path=str(audio),normalized_path=str(audio),
                original_filename='audio.wav',device_id='fixture',duration=60,device_snapshot_json=json.dumps(device)))
            segments=[dict(id=0,start=0,end=10,speaker='S01',text='MOSS 원본 유지')]
            result=dict(segments=segments,words=[],turns=[],asr_chunks=[],pipeline='moss-qwen-sequential',
                batch=1,chunk_count=1,inference_seconds=10,load_seconds=5,has_auxiliary_transcript=True,
                auxiliary_transcript=dict(text='Qwen 별도 대본',chunks=[dict(text='Qwen 별도 대본')]))
            async def execute(host,key,action,on_event,**kwargs):
                if action=='inspect': return {'phase':'installed'}
                on_event({'phase':'loading','engine':'moss'})
                self.assertFalse(store.get(identity)['model_progress']['continuing'])
                on_event({'phase':'loading','engine':'moss','resume_offset':30})
                self.assertTrue(store.get(identity)['model_progress']['continuing'])
                on_event({'phase':'transcribing','resume_offset':30})
                self.assertTrue(store.get(identity)['model_progress']['continuing'])
                on_event({'phase':'loading','engine':'qwen'})
                self.assertFalse(store.get(identity)['model_progress']['continuing'])
                return dict(phase='done',result=result)
            with patch.object(main,'STORE',store),patch.object(main.SPEECH,'execute',side_effect=execute), \
                 patch.object(main.SPEECH,'observations',return_value=[]),patch.object(main.SPEECH,'record'):
                await main.transcribe_fast(note,device,0)
                self.assertEqual(store.get(identity)['segments'],segments)
                self.assertNotIn('Qwen 별도 대본',json.dumps(store.get(identity)['pipeline_result'],ensure_ascii=False))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    response=await client.get(f'/api/notes/{identity}/auxiliary-transcript')
                    self.assertEqual(response.json()['text'],'Qwen 별도 대본')
                    self.assertFalse(response.json()['aligned'])
                    response=await client.get(f'/api/notes/{identity}/auxiliary-transcript?download=true')
                    self.assertEqual(response.text,'Qwen 별도 대본')
                    self.assertIn('attachment',response.headers['content-disposition'])
                    self.assertEqual((await client.get('/api/notes/'+'a'*32+'/auxiliary-transcript')).status_code,404)


if __name__=='__main__': unittest.main()
