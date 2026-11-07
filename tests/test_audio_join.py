"""Ordered, lossless model-input joins and atomic publication through HTTP."""
import asyncio
import json
import inspect
import os
from pathlib import Path
import struct
import tempfile
import unittest
import uuid
import wave
from unittest.mock import AsyncMock, patch

import httpx

from src.audio_join import join_recordings
from src.devices import DeviceStore
from src.store import NoteStore


GPU = 'GPU-11111111-2222-3333-4444-555555555555'


class AudioJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cosmoss-join-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        with patch.dict(os.environ, {'MOSS_DATA_DIR': str(self.root/'data')}):
            from src import main
        self.main = main
        self.store = NoteStore(self.root/'data/notes.sqlite3')
        devices = DeviceStore(self.root/'data/devices.sqlite3')
        self.device = devices.add_ssh(name='GPU',host='fixture',remote_port=8001,backend='cuda',model='MOSS')
        devices.configure(self.device['id'],model_selected=True,model_key='cuda-original',options={'speech_pipeline':'moss-sglang'})
        self.queue = asyncio.Queue()
        for key,value in dict(STORE=self.store,DEVICES=devices,QUEUE=self.queue).items():
            self.enterContext(patch.object(main,key,value))
        self.enterContext(patch('src.job_artifacts.DEFAULT_DOWNLOADS',self.root/'Downloads'))
        self.enterContext(patch.object(main.SPEECH,'observations',return_value=[]))
        self.enterContext(patch.object(main,'probe_host',new=AsyncMock(return_value={'gpus':[
            dict(uuid=GPU,index=0,name='NVIDIA H200',memory_total_mib=143771,idle_candidate=True)]})))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    def upload(self, sample, frames=1600, *, rate=16000):
        identity=uuid.uuid4().hex
        folder=self.store.upload_directory(identity);folder.mkdir(parents=True)
        audio=folder/'normalized.wav'
        content=struct.pack('<h',sample)*frames
        with wave.open(str(audio),'wb') as stream:
            stream.setparams((1,2,rate,frames,'NONE','not compressed'));stream.writeframes(content)
        (folder/'source.m4a').write_bytes(b'original copy')
        self.store.create_upload(dict(id=identity,original_filename=f'녹음{sample}.m4a',status='ready',
            source_path=str(folder/'source.m4a'),normalized_path=str(audio),duration=frames/rate))
        return identity,content

    async def submit(self, identities, **changes):
        fields=dict(upload_ids=identities,device_id=self.device['id'],gpu_uuid=GPU,
                    speech_pipeline='moss-sglang',**changes)
        return await self.client.post('/api/notes',data=fields)

    async def test_ordered_frames_and_size_are_preserved_in_one_job_then_cleaned(self):
        first,a=self.upload(100,1600);second,b=self.upload(-300,3200);third,c=self.upload(900,4800)
        response=await self.submit([third,first,second],title='이어진 회의')
        self.assertEqual(response.status_code,202,response.text)
        note=self.store.get(response.json()['id'])
        self.assertEqual(note['duration'],.6)
        self.assertEqual(note['title'],'이어진 회의')
        self.assertEqual(json.loads(note['device_snapshot_json'])['gpu_uuid'],GPU)
        audio=self.store.resolve_path(note['id'],note['normalized_path'])
        with wave.open(str(audio),'rb') as stream:
            self.assertEqual(stream.readframes(stream.getnframes()),c+a+b)
        self.assertEqual(audio.stat().st_size,44+len(a+b+c))
        self.assertEqual(self.store.list_uploads(),[])
        self.assertEqual(self.queue.qsize(),1)
        self.assertEqual(self.queue.get_nowait(),note['id'])
        for key in (first,second,third):self.assertFalse(self.store.upload_directory(key).exists())
        result=dict(segments=[dict(id=0,start=0,end=.6,speaker='S01',text='합친 전사')],chunk_count=1)
        import time
        await self.main.complete_pipeline_transcription(note,result,audio,time.monotonic())
        self.assertFalse(audio.parent.exists())
        self.assertIn('합친 전사',Path(self.store.get(note['id'])['text_path']).read_text())

    async def test_duplicate_missing_or_unready_inputs_never_consume_others(self):
        first,_=self.upload(10);second,_=self.upload(20)
        self.assertEqual((await self.submit([first,first])).status_code,422)
        self.assertEqual((await self.submit([first])).status_code,422)
        self.assertEqual((await self.submit([first,uuid.uuid4().hex])).status_code,404)
        self.store.update_upload(second,status='preparing')
        self.assertEqual((await self.submit([first,second])).status_code,409)
        self.assertEqual(len(self.store.list_uploads()),2)
        self.assertEqual(self.store.list(),[])
        self.assertTrue(self.queue.empty())

    async def test_invalid_audio_cleans_join_scratch_and_keeps_inputs_for_retry(self):
        first,_=self.upload(10);second,_=self.upload(20,rate=8000)
        response=await self.submit([first,second])
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual({p.name for p in (self.root/'data/uploads').iterdir()},{first,second})
        self.assertEqual(len(self.store.list_uploads()),2)
        self.assertEqual(self.store.list(),[])

    async def test_cancellation_after_copy_cleans_scratch_without_consuming_inputs(self):
        first,_=self.upload(10);second,_=self.upload(20)
        async def cancelled(function,*args):
            self.assertTrue((await self.main.update_status())['busy'])
            function(*args)
            raise asyncio.CancelledError
        with patch.object(self.main,'run_audio_work',side_effect=cancelled):
            arguments={name:parameter.default.default for name,parameter in inspect.signature(self.main.create_note).parameters.items()}
            arguments.update(upload_ids=[first,second],device_id=self.device['id'],gpu_uuid=GPU,speech_pipeline='moss-sglang')
            with self.assertRaises(asyncio.CancelledError):await self.main.create_note(**arguments)
        self.assertEqual({p.name for p in (self.root/'data/uploads').iterdir()},{first,second})
        self.assertEqual(len(self.store.list_uploads()),2)
        self.assertTrue(self.queue.empty())
        self.assertFalse(self.main.AUDIO_JOIN_TASKS)

    async def test_concurrent_consumption_rejects_join_without_removing_other_job(self):
        first,_=self.upload(10);second,_=self.upload(20)
        def race(store,uploads,identity):
            result=join_recordings(store,uploads,identity)
            store.consume_upload(first,{'title':'Already submitted'})
            return result
        with patch.object(self.main,'join_recordings',side_effect=race):
            response=await self.submit([first,second])
        self.assertEqual(response.status_code,404,response.text)
        self.assertEqual([n['id'] for n in self.store.list()],[first])
        self.assertTrue(self.store.upload_directory(first).exists())
        self.assertEqual([u['id'] for u in self.store.list_uploads()],[second])
        self.assertEqual({p.name for p in (self.root/'data/uploads').iterdir()},{first,second})
