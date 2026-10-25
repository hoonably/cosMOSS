"""Public preset contracts, private auxiliary text, and engine-specific estimates."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.speech_engines import ENGINE_VERSION, stages
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

    def test_full_context_never_becomes_independent_chunks_and_memory_is_not_summed(self):
        moss=estimate_pipeline('moss-sglang',3937.13,GPU,24)
        dual=estimate_pipeline('moss-qwen-sequential',3937.13,GPU)
        self.assertEqual((moss['chunk_count'],moss['batch']),(1,1))
        self.assertEqual(dual['chunk_count'],1)
        self.assertEqual(dual['batch'],4)
        self.assertLess(dual['memory_expected_gib'],45)
        with self.assertRaises(ValueError): estimate_pipeline('moss-sglang',5401,GPU)
        self.assertGreater(estimate_pipeline('qwen-transformers',5401,GPU)['chunk_count'],1)
        self.assertTrue(estimate_pipeline('moss-sglang',100,{**GPU,'memory_total_mib':24576})['exceeds_capacity'])

    def test_history_does_not_mix_old_engine_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=SpeechJobs(Path(folder)/'samples.db')
            sample=dict(pipeline='moss-sglang',runtime=ENGINE_VERSION,segments=[{'text':'secret'}],
                peak_process_mib=25000,batch=1,audio_seconds=100,inference_seconds=10,load_seconds=20,
                capacity_gib=48,gpu_name='NVIDIA RTX 6000 Ada Generation')
            jobs.record(sample)
            observations=jobs.observations()
            self.assertNotIn('secret',json.dumps(observations))
            gpu={**GPU,'name':sample['gpu_name'],'memory_total_mib':48*1024}
            result=estimate_pipeline('moss-sglang',200,gpu,observations=observations)
            self.assertEqual(result['time_seconds'],40)
            self.assertIsNone(estimate_pipeline('moss-qwen-sequential',200,gpu,observations=observations)['time_seconds'])


class EngineAPITests(unittest.IsolatedAsyncioTestCase):
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
                on_event({'phase':'transcribing'})
                on_event({'phase':'loading','engine':'qwen'})
                return dict(phase='done',result=result)
            with patch.object(main,'STORE',store),patch.object(main.SPEECH,'execute',side_effect=execute), \
                 patch.object(main.SPEECH,'observations',return_value=[]),patch.object(main.SPEECH,'record'), \
                 patch.object(main.VOICE_PROFILES,'settings',return_value={'enabled':False}):
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
