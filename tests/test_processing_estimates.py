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
    def test_four_observations_produce_point_predictions_and_reserved_memory_budget(self):
        device = {'backend':'cuda', 'model_key':'cuda-original', 'options':{}}
        observations = ((1800, 1, 188, 8.2), (1800, 2, 186, 5.3),
                        (3937.1306875, 1, 529, 15.5), (3937.1306875, 2, 428, 9.1))
        for duration, count, expected_time, expected_memory in observations:
            with self.subTest(duration=duration, count=count):
                estimated = estimate_processing(device, duration, count, {'name':'NVIDIA H200'})
                self.assertEqual(estimated['time_source'], 'reference-profile')
                self.assertEqual(estimated['time_seconds'], expected_time)
                self.assertEqual(estimated['memory_expected_gib'], expected_memory)
                self.assertNotIn('time_low_seconds', estimated)
                self.assertNotIn('time_high_seconds', estimated)
                self.assertLess(estimated['memory_expected_gib'], estimated['memory_high_gib'])

    def test_chunk_context_cost_is_nonlinear_and_bridges_are_counted(self):
        device = {'backend':'cuda', 'model_key':'cuda-original', 'options':{}}
        gpu = {'name':'NVIDIA H200'}
        prefix = estimate_processing(device, 1800, 1, gpu)
        full = estimate_processing(device, 3937.1306875, 1, gpu)
        split = estimate_processing(device, 3937.1306875, 2, gpu)
        self.assertGreater(full['time_seconds'], prefix['time_seconds'] * 3937.1306875 / 1800 * 1.2)
        self.assertLess(split['time_seconds'], full['time_seconds'])
        self.assertLess(split['memory_expected_gib'], full['memory_expected_gib'])
        single = estimate_processing(device, 1800, 1, gpu)
        triple = estimate_processing(device, 5400, 3, gpu)
        self.assertEqual(triple['memory_expected_gib'], single['memory_expected_gib'])
        self.assertEqual(triple['time_seconds'], round(3 * 187.64271568134427 + 2 * 2.673874121159315))
        no_bridge = estimate_processing({**device, 'options':{'bridge_seconds':0}}, 5400, 3, gpu)
        self.assertLess(no_bridge['time_seconds'], triple['time_seconds'])
        times = [estimate_processing(device, seconds, 1, gpu)['time_seconds'] for seconds in (900, 1500, 1800, 1900, 2400, 3937, 5400)]
        self.assertEqual(times, sorted(times))
        self.assertIn('외삽', estimate_processing(device, 5400, 1, gpu)['note'])
        # More splits must not abruptly switch to the old, much faster guess.
        three_short_chunks = estimate_processing(device, 1800, 3, gpu)
        self.assertEqual(three_short_chunks['time_source'], 'reference-profile')
        self.assertGreater(three_short_chunks['time_seconds'], 180)
        self.assertIn('외삽', three_short_chunks['note'])
        before = estimate_processing(device, 899.9, 1, gpu)
        after = estimate_processing(device, 900.1, 1, gpu)
        self.assertLessEqual(abs(after['time_seconds'] - before['time_seconds']), 1)
        self.assertGreater(estimate_processing(device, 10, 1, gpu)['memory_expected_gib'], 2.8)

    def test_calibration_scope_and_retired_quantization(self):
        device = {'backend':'cuda', 'model_key':'cuda-original', 'options':{}}
        for gpu in ({'name':'RTX 3090'}, {'name':'H200 MIG 1g.18gb'}):
            self.assertEqual(estimate_processing(device, 1800, 1, gpu)['time_source'], 'initial-heuristic')
        for options in ({'precision':'fp16'}, {'attention':'eager'}, {'use_cache':False}, {'max_tokens':4096}):
            self.assertEqual(estimate_processing({**device, 'options':options}, 1800, 1, {'name':'H200'})['time_source'], 'initial-heuristic')
        for options in ({'precision':'int4'}, {'precision':'int8'}, {'cuda_kv_bits':8}, {'cuda_kv_bits':4}):
            unknown = estimate_processing({**device, 'options':options}, 1800, 1, {'name':'H200'})
            self.assertIsNone(unknown['time_seconds'])
            self.assertIsNone(unknown['memory_expected_gib'])

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
        self.assertGreater(long['time_seconds'],short['time_seconds'])
        self.assertFalse(long['exceeds_capacity'])
        self.assertIn('H200 실측 보정',long['note'])
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
        from src.performance_history import PerformanceHistory
        self.enterContext(patch.object(main,'PERFORMANCE',PerformanceHistory(self.root/'performance.sqlite3')))
        from src.remote_workers import RemoteWorkers
        self.workers=RemoteWorkers()
        self.enterContext(patch.object(main,'REMOTE_WORKERS',self.workers))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost')
        self.addAsyncCleanup(self.client.aclose)
        self.device=self.devices.add_ssh(name='GPU',host='qa-host',remote_port=18400,model='OpenMOSS-Team/MOSS-Transcribe-Diarize',backend='cuda')

    async def test_selected_h200_uses_calibrated_points_through_api(self):
        uuid='GPU-11111111-2222-3333-4444-555555555555'
        gpu={'uuid':uuid,'name':'NVIDIA H200','memory_total_mib':143771,'memory_free_mib':130000}
        self.main.GPU_INVENTORIES[self.device['id']]=(time.monotonic(),{'gpus':[gpu]})
        for duration, count, seconds, memory in ((1800,1,188,8.2),(1800,2,186,5.3),
                                                  (3937.1306875,1,529,15.5),(3937.1306875,2,428,9.1)):
            result=await self.client.get(f"/api/devices/{self.device['id']}/estimate", params={'duration':duration,'count':count,'gpu_uuid':uuid})
            self.assertEqual(result.status_code,200,result.text)
            value=result.json()
            self.assertEqual(value['time_seconds'],seconds)
            self.assertEqual(value['memory_expected_gib'],memory)
            self.assertFalse(value['exceeds_capacity'])

    async def test_unselected_homogeneous_server_uses_persisted_hardware_after_restart(self):
        device_id=self.device['id']
        hardware={'gpus':[{'name':'NVIDIA H200','index':index,'memory_gib':140.4} for index in range(4)]}
        # Matches registration without a manually saved model/settings form.
        self.devices.configure(device_id,hardware=hardware,options={'chunk_minutes':90})
        result=await self.client.get(f'/api/devices/{device_id}/estimate',params={'duration':3937.1306875,'count':1})
        self.assertEqual(result.status_code,200,result.text)
        value=result.json()
        self.assertEqual(value['time_seconds'],529)
        self.assertEqual(value['memory_expected_gib'],15.5)
        self.assertEqual(value['gpu_source'],'server-hardware')
        self.assertIn('번호 선택 전',value['note'])
        self.assertIsNone(value['free_gib'])
        self.assertFalse(self.workers.tasks)

    async def test_inventory_before_selection_and_selected_gpu_override_server_profile(self):
        device_id=self.device['id']
        self.devices.configure(device_id,hardware={'gpus':[{'name':'RTX 3090','memory_gib':24}]})
        first={'uuid':'GPU-one','name':'NVIDIA H200','memory_total_mib':143771,'memory_free_mib':0}
        second={**first,'uuid':'GPU-two','memory_free_mib':130000}
        self.main.GPU_INVENTORIES[device_id]=(time.monotonic(),{'gpus':[first,second]})
        url=f'/api/devices/{device_id}/estimate'
        response=await self.client.get(url,params={'duration':3937.1306875,'count':1})
        value=response.json()
        self.assertEqual(value['time_seconds'],529)
        self.assertIsNone(value['free_gib'])
        self.assertFalse(value['exceeds_free'])
        # Mixed servers cannot adopt the first GPU's hardware for every GPU.
        self.main.GPU_INVENTORIES[device_id][1]['gpus'].append({'uuid':'GPU-three','name':'RTX 3090','memory_total_mib':24*1024})
        response=await self.client.get(url,params={'duration':3937.1306875,'count':1})
        self.assertNotEqual(response.json()['time_source'],'reference-profile')
        response=await self.client.get(url,params={'duration':3937.1306875,'count':1,'gpu_uuid':'GPU-one'})
        self.assertEqual(response.json()['time_seconds'],529)
        self.assertEqual(response.json()['gpu_source'],'selected')
        self.assertTrue(response.json()['exceeds_free'])

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
