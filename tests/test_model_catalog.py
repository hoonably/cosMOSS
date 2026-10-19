import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.model_catalog import MODELS, catalog, model_selection, recommendation


class CatalogTests(unittest.TestCase):
    def test_backends_reject_incompatible_weights_and_bad_custom_ids(self):
        self.assertEqual(model_selection('mlx', 'mlx-4bit')['repo'], 'vanch007/mlx-MOSS-Transcribe-Diarize-4bit')
        with self.assertRaises(ValueError):
            model_selection('cuda', 'mlx-4bit')
        with self.assertRaises(ValueError):
            model_selection('mlx', 'custom', '../invalid/model')
        self.assertEqual(model_selection('cuda','custom','example/moss-finetune')['repo'], 'example/moss-finetune')

    def test_vram_profiles_and_mac_memory_change_recommendations(self):
        self.assertEqual(recommendation('mlx', 8)['model_key'], 'mlx-4bit')
        self.assertEqual(recommendation('mlx', 32)['model_key'], 'mlx-8bit')
        self.assertEqual(recommendation('cuda', 6)['model_key'], 'cuda-4bit')
        self.assertEqual(recommendation('cuda', 10)['model_key'], 'cuda-8bit')
        self.assertEqual(recommendation('cuda', 16)['options']['chunk_minutes'], 15)
        self.assertEqual(recommendation('cuda', 24)['options']['chunk_minutes'], 30)
        self.assertEqual(recommendation('cuda', 48)['options']['chunk_minutes'], 60)
        result = catalog('cuda', {'gpus':[{'name':'RTX 3090','index':0,'memory_gib':24}, {'name':'small','index':1,'memory_gib':6}]})
        self.assertEqual([p['model_key'] for p in result['profiles']], ['cuda-original','cuda-4bit'])
        self.assertEqual(result['recommendation']['model_key'], 'cuda-4bit')
        self.assertFalse(next(m for m in result['models'] if m['key']=='mlx-8bit')['supported'])

    def test_job_token_limit_comes_from_pinned_device_configuration(self):
        from src.main import transcription_form
        form = transcription_form({'device_snapshot_json':json.dumps({'options':{'max_tokens':4096}})})
        self.assertEqual(form['max_completion_tokens'], '4096')

    def test_local_tuning_survives_device_store_restart(self):
        from src.devices import DeviceStore
        with tempfile.TemporaryDirectory() as temporary:
            database=Path(temporary)/'devices.sqlite3'
            DeviceStore(database).configure('local', model_key='mlx-4bit', options={'chunk_minutes':10})
            local=DeviceStore(database).get('local')
            self.assertEqual(local['options']['chunk_minutes'],10)
            self.assertEqual(local['model_key'],'mlx-4bit')

    def test_disabling_speaker_bridges_skips_extra_audio_processing(self):
        from src.main import make_bridge_chunks
        with patch('src.main.run_command') as command:
            self.assertEqual(make_bridge_chunks(Path('/clip.wav'),120,[(Path('/a'),0),(Path('/b'),60)],60,0),[])
        command.assert_not_called()


class CUDAOptionsTests(unittest.TestCase):
    def test_quantized_load_keeps_audio_frontend_and_avoids_dtype_casting(self):
        from src import cuda_server
        torch = types.ModuleType('torch')
        torch.bfloat16, torch.float16 = 'bf16','fp16'
        torch.cuda=Mock()
        torch.cuda.is_available.return_value=True
        torch.cuda.device_count.return_value=1
        torch.cuda.is_bf16_supported.return_value=True
        torch.cuda.get_device_capability.return_value=(8,6)
        transformers=types.ModuleType('transformers')
        model=Mock();model.eval.return_value=model
        transformers.AutoModelForCausalLM=Mock(from_pretrained=Mock(return_value=model))
        transformers.AutoProcessor=Mock()
        transformers.BitsAndBytesConfig=Mock(return_value='quantization')
        with tempfile.TemporaryDirectory() as temporary, patch.dict('sys.modules',{'torch':torch,'transformers':transformers}), \
             patch.object(cuda_server,'MODEL_PATH',temporary), patch.object(cuda_server,'GPU_UUID','GPU-test'), \
             patch.object(cuda_server,'OPTIONS',{'precision':'int4','attention':'eager','use_cache':False}), \
             patch.object(cuda_server.Path,'home',return_value=Path(temporary)), patch.object(cuda_server,'MODEL',None), \
             patch.object(cuda_server,'PROCESSOR',None), patch.object(cuda_server,'DTYPE',None), patch.object(cuda_server,'GPU_LOCK',None):
            try:
                cuda_server.load_model()
                kwargs=transformers.AutoModelForCausalLM.from_pretrained.call_args.kwargs
                self.assertEqual(kwargs['device_map'],{'':'cuda:0'})
                self.assertEqual(kwargs['attn_implementation'],'eager')
                quant=transformers.BitsAndBytesConfig.call_args.kwargs
                self.assertTrue(quant['load_in_4bit'])
                self.assertIn('whisper_encoder',quant['llm_int8_skip_modules'])
                self.assertFalse(model.generation_config.use_cache)
                model.to.assert_not_called()
            finally:
                if cuda_server.GPU_LOCK: cuda_server.GPU_LOCK.close()
