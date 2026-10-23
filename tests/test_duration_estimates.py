"""Regression against held-out measurements, hardware boundaries and UI plans."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from src.devices import DeviceStore
from src.duration_estimates import calibrations, mac_estimate, server_estimate
from src.local_models import RUNTIME_VERSION as MLX_RUNTIME
from src.processing_estimates import estimate_processing
from src.speech_engines import ENGINE_VERSION
from src.speech_pipeline import estimate_pipeline

M4 = {'name':'Apple M4','memory_gib':16}
H200 = {'name':'NVIDIA H200','memory_total_mib':143771,'memory_free_mib':140000}


def mac(key='mlx-8bit', **options):
    return {'id':'local','backend':'mlx','model_key':key,'options':options}


class CalibrationTests(unittest.TestCase):
    def test_one_hour_mac_chunking_matches_recorded_timings_and_uses_max_memory(self):
        actual = {4:(635.7269,2.65946),2:(1037.5885,4.11733),1:(2197.2690,7.06257)}
        for count,(elapsed,peak) in actual.items():
            estimate=estimate_processing(mac(),3600,count,local_hardware_info=M4,local_runtime=MLX_RUNTIME)
            self.assertLess(abs(estimate['time_seconds']/elapsed-1),.03)
            self.assertAlmostEqual(estimate['memory_raw_gib'],peak,places=4)
            self.assertGreater(estimate['memory_expected_gib'],peak)
            self.assertEqual(estimate['capacity_gib'],16)
            self.assertEqual(estimate['calibration_status'],'provisional')
        no_bridge=mac_estimate(mac(bridge_seconds=0),3600,4,M4,MLX_RUNTIME)
        bridge=mac_estimate(mac(),3600,4,M4,MLX_RUNTIME)
        self.assertLess(no_bridge['time_seconds'],bridge['time_seconds'])
        self.assertEqual(no_bridge['memory_raw_gib'],bridge['memory_raw_gib'])

    def test_held_out_server_time_and_raw_peak_errors_remain_within_measured_bounds(self):
        measured={'moss-sglang':(124.5587,20.05664,.02),
                  'qwen-transformers':(68.58135,13.33984,.05),
                  'moss-qwen-sequential':(261.45834,20.05664,.15)}
        for key,(elapsed,peak,tolerance) in measured.items():
            estimate=estimate_pipeline(key,1320,H200)
            self.assertLess(abs(estimate['time_seconds']/elapsed-1),tolerance)
            self.assertLess(abs(estimate['memory_raw_gib']/peak-1),.022)
            self.assertGreater(estimate['memory_expected_gib'],peak)
            self.assertEqual(estimate['calibration_status'],'checked')

    def test_mac_validation_limits_are_exposed_instead_of_claiming_accuracy(self):
        checked=mac_estimate(mac('mlx-16bit'),1320,1,M4,MLX_RUNTIME)
        self.assertLess(abs(checked['time_seconds']/356.6304-1),.10)
        self.assertEqual(checked['calibration_status'],'checked')
        low=mac_estimate(mac('mlx-4bit'),1320,1,M4,MLX_RUNTIME)
        self.assertGreater(low['time_seconds']/279.5945-1,.25)
        self.assertEqual(low['calibration_status'],'provisional')
        long=mac_estimate(mac('mlx-4bit'),3600,1,M4,MLX_RUNTIME)
        self.assertIsNone(long['time_seconds'])
        self.assertGreater(long['memory_expected_gib'],0)
        self.assertTrue(long['calibration_extrapolated'])

    def test_mac_combined_time_adds_one_full_recording_pass_and_hides_unmeasured_total_memory(self):
        duration = 3937.1306875
        for count in (1, 2, 4):
            with self.subTest(count=count):
                asr = estimate_processing(mac(), duration, count, local_hardware_info=M4, local_runtime=MLX_RUNTIME)
                combined = estimate_processing(mac(local_diarization='nemotron-3'), duration, count,
                                               local_hardware_info=M4, local_runtime=MLX_RUNTIME)
                self.assertAlmostEqual(combined['diarization_time_seconds'], 36.90151841700026)
                self.assertAlmostEqual(combined['diarization_inference_seconds'], 28.70422870799939)
                self.assertAlmostEqual(combined['inference_seconds'] - asr['inference_seconds'], 28.70422870799939)
                self.assertEqual(combined['time_seconds'], round(combined['inference_seconds'] + combined['setup_seconds']))
                self.assertEqual(combined['asr_memory_expected_gib'], asr['memory_expected_gib'])
                for field in ('memory_expected_gib', 'memory_low_gib', 'memory_high_gib', 'memory_raw_gib'):
                    self.assertIsNone(combined[field])
                self.assertFalse(combined['exceeds_capacity'])
                self.assertEqual(combined['calibration_status'], 'provisional')
                self.assertIsNone(combined['validation_time_error_percent'])
                self.assertEqual(combined['diarization_time_source'], 'single-observation-projection')
                self.assertIn('1회 측정', combined['calibration_note'])
                self.assertIn('Nemotron 포함', combined['note'])

    def test_mac_combined_nemotron_startup_is_fixed_and_projection_scales_by_whole_recording(self):
        device = mac('mlx-16bit', local_diarization='nemotron-3')
        short = estimate_processing(device, 300, 1, local_hardware_info=M4, local_runtime=MLX_RUNTIME)
        long = estimate_processing(device, 3600, 4, local_hardware_info=M4, local_runtime=MLX_RUNTIME)
        self.assertEqual(short['diarization_setup_seconds'], long['diarization_setup_seconds'])
        self.assertAlmostEqual(long['diarization_inference_seconds'] / short['diarization_inference_seconds'], 12)
        self.assertTrue(short['calibration_extrapolated'])
        self.assertTrue(long['calibration_extrapolated'])
        # The ASR-only 16-bit curve is checked, but the combined pipeline is not.
        self.assertEqual(short['calibration_status'], 'provisional')

    def test_mac_combined_unknown_conditions_do_not_fall_back_to_asr_only_predictions(self):
        device = mac(local_diarization='nemotron-3')
        cases = [
            (device, {**M4, 'name':'Apple M2'}, MLX_RUNTIME),
            (device, {**M4, 'memory_gib':32}, MLX_RUNTIME),
            (device, None, MLX_RUNTIME),
            (device, M4, 'other-runtime'),
            (mac(local_diarization='nemotron-3', kv_bits=0), M4, MLX_RUNTIME),
            ({**device, 'model_source':'folder', 'model_path':'/custom/model'}, M4, MLX_RUNTIME),
        ]
        for config, hardware, runtime in cases:
            with self.subTest(config=config, hardware=hardware, runtime=runtime):
                estimate = estimate_processing(config, 3600, 2, local_hardware_info=hardware, local_runtime=runtime)
                self.assertIsNone(estimate['time_seconds'])
                self.assertIsNone(estimate['memory_expected_gib'])
                self.assertEqual(estimate['time_source'], 'unmeasured')
                self.assertEqual(estimate['diarization_time_source'], 'unmeasured')
        low = estimate_processing(mac('mlx-4bit', local_diarization='nemotron-3'), 3600, 1,
                                  local_hardware_info=M4, local_runtime=MLX_RUNTIME)
        self.assertIsNone(low['time_seconds'])
        self.assertIsNone(low['inference_seconds'])

    def test_local_marker_does_not_change_saved_asr_only_or_remote_mlx_estimates(self):
        remote = {**mac(local_diarization='nemotron-3'), 'id':'remote-mac'}
        remote_result = estimate_processing(remote, 1800, 1, local_hardware_info=M4, local_runtime=MLX_RUNTIME)
        asr_result = estimate_processing(mac(), 1800, 1, local_hardware_info=M4, local_runtime=MLX_RUNTIME)
        self.assertEqual(remote_result['time_seconds'], asr_result['time_seconds'])
        self.assertEqual(remote_result['memory_expected_gib'], asr_result['memory_expected_gib'])
        self.assertNotIn('diarization_time_source', asr_result)

    def test_mac_does_not_mix_hardware_models_precision_or_software(self):
        for hardware in ({**M4,'name':'Apple M4 Pro'},{**M4,'name':'Apple M2'},{**M4,'memory_gib':32},None):
            self.assertIsNone(mac_estimate(mac(),1800,1,hardware,MLX_RUNTIME))
        for options in ({'kv_bits':0},{'prefill_step_size':256},{'max_tokens':4096}):
            self.assertIsNone(mac_estimate(mac(**options),1800,1,M4,MLX_RUNTIME))
        for device in ({**mac(),'model_id':'someone/another-model'},
                       {**mac(),'model_source':'folder','model_path':'/a/custom/model'},
                       {**mac(),'model_path':'/cache/snapshots/another-revision'}):
            self.assertIsNone(mac_estimate(device,1800,1,M4,MLX_RUNTIME))
        self.assertIsNone(mac_estimate(mac(),1800,1,M4,'different-runtime'))

    def test_server_does_not_transfer_to_other_gpus_or_manual_batch(self):
        for gpu in ({**H200,'name':'NVIDIA H100'}, {**H200,'memory_total_mib':80*1024}, {**H200,'name':'H200 MIG 1g.18gb'}):
            self.assertIsNone(estimate_pipeline('moss-sglang',1800,gpu)['time_seconds'])
        self.assertIsNone(server_estimate('moss-sglang',1800,H200,'different-runtime',1))
        self.assertIsNone(estimate_pipeline('qwen-transformers',3937,H200,4)['time_seconds'])
        many=estimate_pipeline('qwen-transformers',10800,H200)
        self.assertTrue(many['calibration_extrapolated'])
        self.assertGreater(many['time_seconds'],estimate_pipeline('qwen-transformers',3937,H200)['time_seconds'])

    def test_short_observation_does_not_replace_the_full_length_curve(self):
        row={'pipeline':'moss-sglang','runtime':ENGINE_VERSION,'batch':1,'gpu_name':'h200',
             'capacity_gib':143771/1024,'audio_seconds':60,'inference_seconds':100,'load_seconds':10,'peak_process_gib':20}
        seed=estimate_pipeline('moss-sglang',3937.13,H200)
        self.assertEqual(estimate_pipeline('moss-sglang',3937.13,H200,observations=[row])['time_seconds'],seed['time_seconds'])
        row.update(audio_seconds=3937.13,inference_seconds=110,load_seconds=90)
        observed=estimate_pipeline('moss-sglang',3937.13,H200,observations=[row])
        self.assertEqual(observed['time_seconds'],200)
        self.assertEqual(observed['time_source'],'observed')

    def test_short_server_inputs_reduce_peak_and_keep_fixed_startup(self):
        short=estimate_pipeline('moss-sglang',60,H200)
        long=estimate_pipeline('moss-sglang',3937,H200)
        self.assertLess(short['memory_expected_gib'],long['memory_expected_gib'])
        self.assertLess(short['time_seconds'],long['time_seconds'])
        self.assertEqual(short['setup_seconds'],long['setup_seconds'])
        self.assertGreater(short['time_seconds'],90)
        self.assertEqual(estimate_pipeline('qwen-transformers',180,H200)['calibration_status'],'caution')

    def test_export_contains_only_numeric_profiles_and_public_model_identity(self):
        data=calibrations();text=json.dumps(data)
        self.assertEqual(len(data['profiles']),6)
        for forbidden in ('/Users/','/home/','juyu','hoon','token_ids','speaker_names','text_path','source_sha256'):
            self.assertNotIn(forbidden,text)
        for profile in data['profiles'].values():
            for name in ('time_knots','memory_knots'):
                self.assertTrue(all(x>0 and y>0 for x,y in profile[name]))
                self.assertEqual(profile[name],sorted(profile[name]))


class CalibrationAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_local_preview_and_job_progress_share_the_same_prediction(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            devices=DeviceStore(Path(folder)/'devices.db')
            device=devices.configure('local',model_key='mlx-8bit',model_id='vanch007/mlx-MOSS-Transcribe-Diarize-8bit')
            with patch.object(main,'DEVICES',devices),patch.object(main,'local_hardware',return_value=M4),patch.object(main.LOCAL_MODELS,'saved',{}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    response=await client.get('/api/devices/local/estimate',params={'duration':3600,'count':2})
                self.assertEqual(response.status_code,200,response.text)
                preview=response.json()
                self.assertIsNone(preview['memory_expected_gib'])
                self.assertEqual(preview['asr_memory_expected_gib'],4.9)
                snapshot = {**device, 'options':{**device.get('options', {}), 'local_diarization':'nemotron-3'}}
                frozen=main.estimate_note_inference({'device_snapshot_json':json.dumps(snapshot)},3600,2)
                self.assertEqual(frozen,preview['time_seconds'])
                self.assertEqual(preview['calibration_model'],'mlx-8bit')
                historical = main.estimate_note_inference({'device_snapshot_json':json.dumps(device)},3600,2)
                self.assertLess(historical, frozen)
                self.assertAlmostEqual(frozen - historical, preview['diarization_time_seconds'], delta=1)

    async def test_local_custom_folder_does_not_inherit_catalog_model_timings(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            devices=DeviceStore(Path(folder)/'devices.db')
            devices.configure('local',model_key='mlx-8bit',model_id='vanch007/mlx-MOSS-Transcribe-Diarize-8bit')
            saved={'model_key':'mlx-8bit','source':'folder','model_path':str(Path(folder)/'custom')}
            with patch.object(main,'DEVICES',devices),patch.object(main,'local_hardware',return_value=M4),patch.object(main.LOCAL_MODELS,'saved',saved):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    response=await client.get('/api/devices/local/estimate',params={'duration':3600,'count':2})
                self.assertEqual(response.status_code,200,response.text)
                self.assertNotIn('calibration_model',response.json())
                self.assertNotIn(folder,response.text)

    async def test_server_selected_gpu_is_used_without_starting_any_worker(self):
        from src import main
        with tempfile.TemporaryDirectory() as folder:
            devices=DeviceStore(Path(folder)/'devices.db')
            device=devices.add_ssh(name='fixture',host='fixture',backend='cuda',remote_port=8001,model='moss')
            inventory={device['id']:(time.monotonic(),{'gpus':[{**H200,'uuid':'GPU-fixture'}]})}
            with patch.object(main,'DEVICES',devices),patch.object(main,'GPU_INVENTORIES',inventory),patch.object(main.SPEECH,'observations',return_value=[]),patch.object(main.SPEECH,'execute') as execute:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                    response=await client.get(f'/api/devices/{device["id"]}/speech-estimate',params={'pipeline':'moss-sglang','duration':1320,'gpu_uuid':'GPU-fixture'})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['time_seconds'],124)
                self.assertEqual(response.json()['memory_expected_gib'],23.4)
                execute.assert_not_called()


if __name__=='__main__':unittest.main()
