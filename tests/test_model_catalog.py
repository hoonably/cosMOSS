import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.model_catalog import MODELS, DEFAULT_OPTIONS, catalog, model_selection, recommendation


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
        self.assertEqual(recommendation('cuda', 6)['model_key'], 'cuda-original')
        self.assertEqual(recommendation('cuda', 10)['model_key'], 'cuda-original')
        self.assertEqual(recommendation('cuda', 16)['options']['chunk_minutes'], 30)
        self.assertEqual(recommendation('cuda', 24)['options']['chunk_minutes'], 30)
        self.assertEqual(recommendation('cuda', 48)['options']['chunk_minutes'], 60)
        self.assertEqual(recommendation('cuda', 79.1)['options']['chunk_minutes'], 90)
        self.assertEqual(recommendation('cuda', 141)['options']['chunk_minutes'], 90)
        self.assertEqual(recommendation('mlx', 8)['options']['chunk_minutes'], 30)
        result = catalog('cuda', {'gpus':[{'name':'RTX 3090','index':0,'memory_gib':24}, {'name':'small','index':1,'memory_gib':6}]})
        self.assertEqual([p['model_key'] for p in result['profiles']], ['cuda-original','cuda-original'])
        self.assertEqual(result['recommendation']['model_key'], 'cuda-original')
        self.assertFalse(any(m['key'] in {'cuda-4bit', 'cuda-8bit'} for m in result['models']))
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


class ModelSelectionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_mac_choice_survives_restart_without_preparing_or_loading_model(self):
        from unittest.mock import AsyncMock
        from src.local_models import LocalModels
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manager=LocalModels(root,root/'data',8001,'moss-mtd')
            selected=model_selection('mlx','mlx-4bit')
            with patch.object(manager,'download',new=AsyncMock()) as download, patch.object(manager,'launch',new=AsyncMock()) as launch:
                result=await manager.select(selected,DEFAULT_OPTIONS.copy())
                self.assertEqual(result['phase'],'idle')
                await manager.restore()
                download.assert_not_awaited()
                launch.assert_not_awaited()
            restored=LocalModels(root,root/'data',8001,'moss-mtd')
            self.assertEqual(restored.status()['model_key'],'mlx-4bit')
            self.assertFalse(restored.saved.get('model_path'))


class CUDAOptionsTests(unittest.TestCase):
    def test_original_load_uses_selected_dtype_and_unquantized_cache(self):
        from src import cuda_server
        torch = types.ModuleType('torch')
        torch.bfloat16, torch.float16 = 'bf16','fp16'
        torch.cuda=Mock()
        torch.cuda.is_available.return_value=True
        torch.cuda.device_count.return_value=1
        torch.cuda.is_bf16_supported.return_value=True
        torch.cuda.get_device_capability.return_value=(8,6)
        transformers=types.ModuleType('transformers')
        model=Mock();model.eval.return_value=model;model.to.return_value=model
        transformers.AutoModelForCausalLM=Mock(from_pretrained=Mock(return_value=model))
        transformers.AutoProcessor=Mock()
        with tempfile.TemporaryDirectory() as temporary, patch.dict('sys.modules',{'torch':torch,'transformers':transformers}), \
             patch.object(cuda_server,'MODEL_PATH',temporary), patch.object(cuda_server,'GPU_UUID','GPU-test'), \
             patch.object(cuda_server,'OPTIONS',{'precision':'fp16','attention':'eager','use_cache':False}), \
             patch.object(cuda_server.Path,'home',return_value=Path(temporary)), patch.object(cuda_server,'MODEL',None), \
             patch.object(cuda_server,'PROCESSOR',None), patch.object(cuda_server,'DTYPE',None), patch.object(cuda_server,'GPU_LOCK',None):
            try:
                (Path(temporary) / 'config.json').write_text('{}')
                cuda_server.load_model()
                kwargs=transformers.AutoModelForCausalLM.from_pretrained.call_args.kwargs
                self.assertEqual(kwargs['dtype'],'fp16')
                self.assertNotIn('quantization_config',kwargs)
                self.assertEqual(kwargs['attn_implementation'],'eager')
                self.assertFalse(model.generation_config.use_cache)
                self.assertIsNone(model.generation_config.cache_implementation)
                self.assertIsNone(model.generation_config.cache_config)
                self.assertEqual(model.to.call_args_list[0].kwargs, {'dtype':'fp16'})
                self.assertEqual(model.to.call_args_list[1].args, ('cuda:0',))
            finally:
                if cuda_server.GPU_LOCK: cuda_server.GPU_LOCK.close()
