"""CUDA timing/peak collection contract, without a GPU or model downloads."""
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from src import cuda_server as worker


class WorkerMetricsTests(unittest.TestCase):
    def test_only_inference_is_timed_and_peaks_reset_for_every_chunk(self):
        torch=types.ModuleType('torch'); torch.bfloat16='bf16'; torch.__version__='torch-fixture'
        torch.device=lambda name:name; torch.cuda=Mock()
        torch.cuda.get_device_name.return_value='NVIDIA RTX 3090'
        torch.cuda.get_device_properties.return_value=types.SimpleNamespace(total_memory=24*1024**3)
        torch.cuda.max_memory_reserved.return_value=6*1024**3
        torch.cuda.max_memory_allocated.return_value=5*1024**3
        transformers=types.ModuleType('transformers'); transformers.__version__='transformers-fixture'
        inference=types.ModuleType('moss_transcribe_diarize.inference_utils')
        inference.build_transcription_messages=Mock(return_value='prepared')
        inference.generate_transcription=Mock(return_value={'text':'speech','generated_tokens':100})
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); audio=root/'clip.wav'
            with wave.open(str(audio),'wb') as stream:
                stream.setparams((1,2,16000,0,'NONE','not compressed')); stream.writeframes(b'\0\0'*16000)
            (root/'config.json').write_text('{}')
            with patch.dict('sys.modules',{'torch':torch,'transformers':transformers,'moss_transcribe_diarize.inference_utils':inference}), \
                 patch.object(worker,'MODEL_PATH',str(root)), patch.object(worker,'DTYPE','bf16'), \
                 patch.object(worker,'GENERATION_COUNT',0), patch.object(worker.time,'monotonic',side_effect=[10,12,20,23]):
                first=worker.generate(str(audio),'prompt',200)
                second=worker.generate(str(audio),'prompt',100)
            sample=first['performance']
            self.assertEqual(sample['audio_seconds'],1)
            self.assertEqual(sample['inference_seconds'],2)
            self.assertEqual(sample['peak_reserved_gib'],6)
            self.assertEqual(sample['gpu_name'],'NVIDIA RTX 3090')
            self.assertTrue(sample['cold_start'])
            self.assertFalse(sample['hit_token_limit'])
            self.assertFalse(second['performance']['cold_start'])
            self.assertTrue(second['performance']['hit_token_limit'])
            self.assertEqual(torch.cuda.reset_peak_memory_stats.call_count,2)
            self.assertNotIn('clip.wav',str(sample))


if __name__ == '__main__':
    unittest.main()
