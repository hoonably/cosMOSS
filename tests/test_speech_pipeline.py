"""Batch planning, uncertain attribution, compatibility and note persistence."""
import asyncio
import copy
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx

from src.speech_pipeline import estimate_pipeline, attribute_speakers, utterances, RUNTIME_VERSION
from src.speech_jobs import SpeechJobs
from src.store import NoteStore

H200 = {'name':'NVIDIA H200', 'memory_total_mib':143771, 'memory_free_mib':140000}


class PlanningTests(unittest.TestCase):
    def test_batching_uses_measured_waves_and_separate_models(self):
        moss = estimate_pipeline('moss-batch',3937.1306875,H200)
        qwen = estimate_pipeline('qwen-batch',3937.1306875,H200)
        self.assertEqual(moss['batch'],23)
        self.assertEqual(moss['time_seconds'],87)
        self.assertEqual(qwen['time_seconds'],57)
        self.assertGreater(qwen['memory_expected_gib'],moss['memory_expected_gib'])
        self.assertFalse(moss['exceeds_budget'])
        self.assertGreater(estimate_pipeline('qwen-batch',3937.13,H200,4)['time_seconds'],qwen['time_seconds'])

    def test_smaller_gpu_recommends_safe_batch_and_flags_override(self):
        gpu={**H200,'name':'NVIDIA RTX 6000 Ada Generation','memory_total_mib':24*1024,'memory_free_mib':1000}
        automatic=estimate_pipeline('qwen-batch',3937.13,gpu)
        self.assertLessEqual(automatic['memory_expected_gib'],24*.7)
        self.assertTrue(automatic['exceeds_free'])
        forced=estimate_pipeline('qwen-batch',3937.13,gpu,24)
        self.assertTrue(forced['exceeds_budget'])
        self.assertTrue(forced['exceeds_capacity'])
        self.assertIsNone(forced['time_seconds'])

    def test_new_gpu_uses_its_own_observation_not_h200_timing(self):
        gpu={**H200,'name':'NVIDIA RTX 6000 Ada Generation','memory_total_mib':48*1024}
        observation={'pipeline':'moss-batch','batch':23,'gpu_name':'rtx 6000 ada generation','capacity_gib':48,
                     'runtime':RUNTIME_VERSION,'audio_seconds':2000,'inference_seconds':100,'load_seconds':12,'peak_process_gib':20}
        self.assertIsNone(estimate_pipeline('moss-batch',4000,gpu)['time_seconds'])
        result=estimate_pipeline('moss-batch',4000,gpu,24,[observation])
        self.assertEqual(result['time_seconds'],212)
        self.assertEqual(result['time_source'],'observed')
        self.assertIsNone(estimate_pipeline('qwen-batch',4000,gpu,24,[observation])['time_seconds'])
        self.assertIsNone(estimate_pipeline('moss-batch',4000,gpu,8,[observation])['time_seconds'])

    def test_bad_batch_is_rejected_and_short_audio_keeps_model_memory(self):
        with self.assertRaises(ValueError):estimate_pipeline('moss-batch',60,H200,3)
        with self.assertRaises(ValueError):estimate_pipeline('moss-batch',float('nan'),H200)
        self.assertEqual(estimate_pipeline('moss-batch',10,H200)['batch'],1)
        self.assertGreater(estimate_pipeline('moss-batch',10,H200)['memory_expected_gib'],8)


class AttributionTests(unittest.TestCase):
    def test_point_words_use_only_unique_active_speaker_not_nearest(self):
        turns=[{'start':1,'end':2,'speaker':'a'},{'start':3,'end':4,'speaker':'b'},
               {'start':3.5,'end':5,'speaker':'a'}]
        items=[{'start':t,'end':t,'text':'원문'} for t in (1.5,2.5,3.2,3.7)]
        before=copy.deepcopy(items)
        rows=attribute_speakers(items,turns)
        self.assertEqual([r['speaker'] for r in rows],['S01','미확인','S02','미확인'])
        self.assertEqual(items,before)
        self.assertEqual([(r['start'],r['end'],r['text']) for r in rows],[(r['start'],r['end'],r['text']) for r in items])

    def test_mixed_utterance_is_not_forced_into_dominant_or_three_speakers(self):
        items=[{'start':0,'end':4,'text':'두 사람이 말함'}]
        turns=[{'start':0,'end':2,'speaker':'a'},{'start':2,'end':4,'speaker':'b'}]
        self.assertTrue(attribute_speakers(items,turns)[0]['speaker_uncertain'])
        turns=[{'start':i,'end':i+1,'speaker':str(i)} for i in range(6)]
        rows=attribute_speakers([{'start':i,'end':i+1,'text':str(i)} for i in range(6)],turns)
        self.assertEqual(len({r['speaker'] for r in rows}),6)

    def test_word_grouping_retains_order_and_uncertainty_boundaries(self):
        items=[{'start':i,'end':i+1,'text':word,'speaker':'S01','speaker_uncertain':i==2}
               for i,word in enumerate(['오늘','결정했습니다.','확인','다음'])]
        rows=utterances(items)
        self.assertEqual([r['text'] for r in rows],['오늘 결정했습니다.','확인','다음'])
        self.assertEqual(rows[0]['end'],2)

    def test_unknown_group_is_not_a_voice_profile_or_diarization_identity(self):
        from src.voice_profiles import VoiceProfiles
        from src.transcript import parse_transcript
        segments=[{'id':0,'start':0,'end':4,'text':'불분명한 말','speaker':'미확인','speaker_uncertain':True}]
        self.assertEqual(VoiceProfiles.ranges(segments),{})
        raw='[0.00][미확인]불분명한 말[4.00]\n[4.00][S01]다음 발화[5.00]'
        self.assertEqual([s['speaker'] for s in parse_transcript(raw)],['미확인','S01'])


class PersistenceTests(unittest.TestCase):
    def test_old_notes_default_to_empty_metrics_and_numeric_history_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=NoteStore(root/'notes.sqlite3')
            identity='a'*32;audio=store.upload_directory(identity)/'a.wav'
            note=store.create({'id':identity,'title':'기존 노트','original_filename':'a.wav','source_path':str(audio),'status':'done'})
            self.assertEqual(note['pipeline_result'],{})
            self.assertEqual(store.update(identity,pipeline_result={'pipeline':'moss-batch'})['pipeline_result']['pipeline'],'moss-batch')
            jobs=SpeechJobs(root/'performance.sqlite3')
            result={'runtime':RUNTIME_VERSION,'segments':[{'text':'private transcript'}],'audio_seconds':10,
                    'inference_seconds':2,'load_seconds':1,'capacity_gib':140,'gpu_name':'NVIDIA H200',
                    'peak_process_mib':12000,'batch':1,'pipeline':'moss-batch'}
            jobs.record(result)
            encoded=json.dumps(jobs.observations())
            self.assertNotIn('private transcript',encoded)
            self.assertNotIn('segments',encoded)
            self.assertEqual(jobs.observations()[0]['gpu_name'],'h200')


class FastJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_model_stays_ready_after_device_list_refresh(self):
        from src import main
        from src.devices import DeviceStore
        with tempfile.TemporaryDirectory() as folder:
            devices=DeviceStore(Path(folder)/'devices.sqlite3')
            device=devices.add_ssh(name='test',host='fixture',remote_port=8001,backend='cuda',model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
            devices.configure(device['id'],model_selected=True,options={'speech_pipeline':'moss-batch'})
            with patch.object(main,'DEVICES',devices),patch.object(main.SPEECH,'states',{}), \
                 patch.object(main.SPEECH,'execute',new=AsyncMock(return_value={'phase':'installed'})):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    response=await client.post(f"/api/devices/{device['id']}/status")
                    self.assertTrue(response.json()['ok'],response.text)
                    listed=(await client.get('/api/devices')).json()
                    self.assertEqual(next(d for d in listed if d['id']==device['id'])['setup']['phase'],'installed')
                    self.assertEqual((await client.get(f"/api/devices/{device['id']}/worker")).json()['phase'],'installed')
                    self.assertEqual(main.SPEECH.status('fixture','qwen-batch')['phase'],'idle')

    async def test_old_chunk_controls_do_not_block_selected_fast_pipeline(self):
        from src import main
        from src.devices import DeviceStore
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=NoteStore(root/'notes.sqlite3');devices=DeviceStore(root/'devices.sqlite3')
            device=devices.add_ssh(name='test',host='fixture',remote_port=8001,backend='cuda',model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
            gpu={**H200,'uuid':'GPU-11111111-2222-3333-4444-555555555555','index':0,'idle_candidate':True}
            queue=asyncio.Queue()
            with patch.object(main,'STORE',store),patch.object(main,'DEVICES',devices),patch.object(main,'QUEUE',queue), \
                 patch.object(main,'probe_host',new=AsyncMock(return_value={'gpus':[gpu]})), \
                 patch.object(main.SPEECH,'observations',return_value=[]):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    for pipeline in ('moss-batch','qwen-batch'):
                        devices.configure(device['id'],model_selected=True,options={'speech_pipeline':pipeline})
                        for legacy in ({'chunk_count':'1'}, {'chunk_minutes':'90'},
                                       {'chunk_count':'2','chunk_minutes':'30','speech_batch':'8'}):
                            with self.subTest(pipeline=pipeline,legacy=legacy):
                                identity=uuid.uuid4().hex
                                audio=store.upload_directory(identity)/'a.wav';audio.parent.mkdir(parents=True);audio.write_bytes(b'fixture')
                                store.create_upload({'id':identity,'original_filename':'a.wav','source_path':str(audio),
                                    'normalized_path':str(audio),'status':'ready','duration':3937.13})
                                response=await client.post('/api/notes',data={
                                    'upload_id':identity,'device_id':device['id'],'gpu_uuid':gpu['uuid'],**legacy})
                                self.assertEqual(response.status_code,202,response.text)
                                note=store.get(response.json()['id'])
                                self.assertEqual(note['chunk_seconds'],180)
                                # Queue placeholders start at one; the fast
                                # worker supplies its actual automatic count.
                                self.assertEqual(note['chunk_count'],1)
                                snapshot=json.loads(note['device_snapshot_json'])
                                self.assertEqual(snapshot['options']['speech_pipeline'],pipeline)
                                self.assertEqual(snapshot['options']['speech_batch'],int(legacy.get('speech_batch',0)))
                                self.assertEqual(queue.get_nowait(),note['id'])

    async def test_api_requires_memory_acknowledgement_and_never_overrides_capacity(self):
        from src import main
        from src.devices import DeviceStore
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=NoteStore(root/'notes.sqlite3');devices=DeviceStore(root/'devices.sqlite3')
            device=devices.add_ssh(name='test',host='fixture',remote_port=8001,backend='cuda',model='OpenMOSS-Team/MOSS-Transcribe-Diarize')
            identity='c'*32;audio=store.upload_directory(identity)/'a.wav';audio.parent.mkdir(parents=True);audio.write_bytes(b'fixture')
            store.create_upload({'id':identity,'original_filename':'a.wav','source_path':str(audio),'normalized_path':str(audio),
                                 'status':'ready','duration':3937.13})
            gpu={**H200,'uuid':'GPU-11111111-2222-3333-4444-555555555555','index':0,'idle_candidate':True,
                 'memory_total_mib':24576,'name':'NVIDIA RTX 6000 Ada Generation'}
            body={'upload_id':identity,'device_id':device['id'],'gpu_uuid':gpu['uuid'],'speech_pipeline':'qwen-batch','speech_batch':'8'}
            with patch.object(main,'STORE',store),patch.object(main,'DEVICES',devices),patch.object(main,'QUEUE',asyncio.Queue()), \
                 patch.object(main,'probe_host',new=AsyncMock(return_value={'gpus':[gpu]})), \
                 patch.object(main.SPEECH,'observations',return_value=[]):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    response=await client.post('/api/notes',data=body)
                    self.assertEqual(response.status_code,409,response.text)
                    self.assertEqual(store.list(),[])
                    response=await client.post('/api/notes',data={**body,'speech_batch':'24','allow_memory_risk':'true'})
                    self.assertEqual(response.status_code,422,response.text)
                    self.assertEqual(store.list(),[])
                    response=await client.post('/api/notes',data={**body,'allow_memory_risk':'true'})
                    self.assertEqual(response.status_code,202,response.text)
                    snapshot=json.loads(store.get(response.json()['id'])['device_snapshot_json'])
                    self.assertEqual(snapshot['options']['speech_batch'],8)
                    self.assertEqual(snapshot['options']['speech_pipeline'],'qwen-batch')

    async def test_fast_job_skips_old_server_and_keeps_raw_and_export(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=NoteStore(root/'notes.sqlite3')
            identity='b'*32;audio=store.upload_directory(identity)/'a.wav';audio.parent.mkdir(parents=True);audio.write_bytes(b'fixture')
            device={'id':'remote','backend':'cuda','host':'fixture','gpu_uuid':'GPU-fixture',
                    'gpu_hardware':H200,'options':{'speech_pipeline':'moss-batch','speech_batch':8}}
            note=store.create({'id':identity,'title':'전사','original_filename':'a.wav','source_path':str(audio),
                'normalized_path':str(audio),'duration':60,'status':'queued','device_id':'remote',
                'device_snapshot_json':json.dumps(device),'output_directory':str(root/'output')})
            segments=[{'id':0,'start':0,'end':5,'text':'원문 그대로','speaker':'S01'}]
            result={'segments':segments,'words':[],'asr_chunks':[],'turns':[], 'pipeline':'moss-batch',
                    'inference_seconds':1,'chunk_count':1,'batch':1}
            async def execute(host,key,action,event,**kwargs):
                if action=='inspect':return {'phase':'installed'}
                event({'phase':'transcribing','chunk_count':1})
                return {'phase':'done','result':result}
            with patch.object(main,'STORE',store),patch.object(main.DEVICES,'get',return_value=device), \
                 patch.object(main.MEDIA_TOOLS,'require_ready',new=AsyncMock()), \
                 patch.object(main.SPEECH,'execute',side_effect=execute), \
                 patch.object(main.SPEECH,'record'),patch.object(main.SPEECH,'observations',return_value=[]), \
                 patch.object(main.REMOTE_WORKERS,'perform',new=AsyncMock()) as old, \
                 patch.object(main.VOICE_PROFILES,'settings',return_value={'enabled':False}):
                await main.transcribe(identity)
            saved=store.get(identity)
            self.assertEqual(saved['status'],'done',saved.get('error'))
            self.assertEqual(saved['segments'],segments)
            self.assertEqual(saved['raw_segments'],segments)
            self.assertTrue(Path(saved['text_path']).is_file())
            old.assert_not_awaited()


class UIResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ui_modules_revalidate_including_conditional_responses(self):
        from src import main
        class Resources(HTMLParser):
            def __init__(self):
                super().__init__()
                self.paths=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='script' and attrs.get('src'): self.paths.append(attrs['src'])
                if tag=='link' and attrs.get('rel')=='stylesheet': self.paths.append(attrs['href'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
            document=await client.get('/')
            parser=Resources();parser.feed(document.text)
            self.assertIn('/speech.js?v=model-sizes-20261025',parser.paths)
            for path in ('/',*parser.paths):
                with self.subTest(path=path):
                    response=await client.get(path)
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.headers['cache-control'],'no-cache')
                    cached=await client.get(path,headers={'If-None-Match':response.headers['etag']})
                    self.assertEqual(cached.status_code,304)
                    self.assertEqual(cached.headers['cache-control'],'no-cache')


if __name__=='__main__':unittest.main()
