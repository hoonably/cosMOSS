import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from src.devices import DeviceStore
from src.model_catalog import default_chunk_minutes
from src.processing_estimates import estimate_processing
from src import main


class EstimateTests(unittest.TestCase):
    def test_vram_bands_and_local_default(self):
        for memory, expected in ((None,30),(24,30),(39.9,30),(47.5,60),(64,60),(79.1,90),(141,90)):
            self.assertEqual(default_chunk_minutes('cuda',memory),expected)
        self.assertEqual(default_chunk_minutes('mlx',128),30)

    def test_context_and_settings_affect_peak_and_time_without_claiming_measurements(self):
        device={'backend':'cuda','model_key':'cuda-original','options':{}}
        gpu={'name':'H200','memory_total_mib':141*1024,'memory_free_mib':140*1024}
        long=estimate_processing(device,5400,1,gpu)
        short=estimate_processing(device,5400,3,gpu)
        self.assertGreater(long['memory_high_gib'],short['memory_high_gib'])
        self.assertGreater(long['time_high_seconds'],short['time_high_seconds'])
        self.assertFalse(long['exceeds_capacity'])
        self.assertIn('초기 추정',long['note'])
        occupied=estimate_processing(device,5400,3,{**gpu,'memory_free_mib':0})
        self.assertTrue(occupied['exceeds_free'])
        eager=estimate_processing({**device,'options':{'attention':'eager'}},5400,1,{**gpu,'memory_total_mib':24*1024})
        self.assertTrue(eager['exceeds_capacity'])
        quantized=estimate_processing({'backend':'mlx','model_key':'mlx-4bit','options':{'kv_bits':8}},1800,1,local_memory_gib=32)
        full=estimate_processing({'backend':'mlx','model_key':'mlx-16bit','options':{'kv_bits':0}},1800,1,local_memory_gib=32)
        self.assertLess(quantized['memory_high_gib'],full['memory_high_gib'])
        unknown=estimate_processing(device,1800,1,{'name':'GPU','memory_total_mib':None,'memory_free_mib':None})
        self.assertIsNone(unknown['capacity_gib'])
        custom=estimate_processing({**device,'model_key':'custom'},1800,1,gpu)
        self.assertIsNone(custom['memory_high_gib'])


class EstimateAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.main=main
        self.devices=DeviceStore(self.root/'devices.sqlite3')
        self.enterContext(patch.object(main,'DEVICES',self.devices))
        self.enterContext(patch.object(main,'GPU_INVENTORIES',{}))
        from src.remote_workers import RemoteWorkers
        self.workers=RemoteWorkers()
        self.enterContext(patch.object(main,'REMOTE_WORKERS',self.workers))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        self.device=self.devices.add_ssh(name='GPU',host='qa-host',remote_port=18400,model='moss',backend='cuda')

    async def test_hardware_initialization_persistence_and_save_without_install(self):
        device_id=self.device['id']
        self.workers.states[device_id]={'phase':'installed','hardware':{'gpus':[{'name':'H200','index':0,'memory_gib':141}]}}
        result=await self.client.get('/api/devices')
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.devices.get('local')['options']['chunk_minutes'],30)
        self.assertEqual(self.devices.get(device_id)['options']['chunk_minutes'],90)
        saved=await self.client.patch(f'/api/devices/{device_id}/chunk-default',json={'chunk_minutes':60})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertNotIn('api_key',saved.json())
        self.workers.states[device_id]['hardware']['gpus'][0]['memory_gib']=48
        await self.client.get('/api/devices')
        self.assertEqual(DeviceStore(self.devices.database).get(device_id)['options']['chunk_minutes'],60)
        self.assertFalse(self.workers.tasks)
        bad=await self.client.patch(f'/api/devices/{device_id}/chunk-default',json={'chunk_minutes':91})
        self.assertEqual(bad.status_code,422)

    async def test_mixed_gpu_default_is_conservative_and_manual_options_survive_races(self):
        device_id=self.device['id']
        hardware={'gpus':[{'name':'H200','index':0,'memory_gib':141},{'name':'3090','index':1,'memory_gib':24}]}
        initialized=self.main.initialize_device_chunks(self.device,hardware)
        self.assertEqual(initialized['options']['chunk_minutes'],30)
        self.devices.set_chunk_default(device_id,60)
        self.devices.initialize_chunk_default(device_id,90,hardware)
        self.assertEqual(self.devices.get(device_id)['options']['chunk_minutes'],60)

    async def test_capacity_is_server_inventory_and_stale_free_memory_is_not_used(self):
        device_id=self.device['id']; uuid='GPU-11111111-2222-3333-4444-555555555555'
        self.devices.configure(device_id,options={'attention':'eager'})
        gpu={'uuid':uuid,'name':'RTX 3090','memory_total_mib':24*1024,'memory_free_mib':0}
        self.main.GPU_INVENTORIES[device_id]=(time.monotonic(),{'gpus':[gpu]})
        url=f'/api/devices/{device_id}/estimate'
        result=await self.client.get(url,params={'duration':5400,'count':1,'gpu_uuid':uuid,'capacity_gib':9999})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['capacity_gib'],24)
        self.assertTrue(result.json()['exceeds_capacity'])
        self.main.GPU_INVENTORIES[device_id]=(time.monotonic()-20,{'gpus':[gpu]})
        result=await self.client.get(url,params={'duration':1800,'count':1,'gpu_uuid':uuid})
        self.assertIsNone(result.json()['free_gib'])
        for values in ({'duration':'nan','count':1},{'duration':1800,'count':0}):
            self.assertEqual((await self.client.get(url,params=values)).status_code,422)
        self.assertEqual((await self.client.get(url,params={'duration':1800,'count':1,'gpu_uuid':'unknown'})).status_code,409)
