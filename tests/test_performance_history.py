"""Learning on any CUDA GPU, without inference or private recording contents."""
import json
import tempfile
import unittest
from pathlib import Path

from src.performance_history import PerformanceHistory, ORIGINAL_MODEL
from src.processing_estimates import estimate_processing


def measurement(**values):
    return dict(dict(engine='cosmoss-cuda-v1', gpu_name='NVIDIA Unlisted Future GPU', memory_total_mib=24*1024,
                model=ORIGINAL_MODEL, precision='bf16', attention='sdpa', use_cache=True, max_tokens=32768,
                software={'torch':'2.8.0+cu128', 'transformers':'5.20.2', 'model_revision':'fixture'},
                audio_seconds=1800, inference_seconds=240, peak_reserved_gib=8, generated_tokens=12000,
                kind='main', hit_token_limit=False), **values)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'performance.sqlite3'
        self.history=PerformanceHistory(self.path)
        self.device={'backend':'cuda','model_key':'cuda-original','model':ORIGINAL_MODEL,'options':{}}
        self.gpu={'name':'NVIDIA Unlisted Future GPU','memory_total_mib':24*1024}

    def test_unknown_gpu_learns_after_completion_and_survives_restart(self):
        before=estimate_processing(self.device,1800,1,self.gpu,profiles=self.history.profiles())
        self.assertEqual(before['time_source'],'initial-heuristic')
        self.assertTrue(self.history.record(measurement(),model=ORIGINAL_MODEL))
        result=estimate_processing(self.device,1800,1,self.gpu,profiles=PerformanceHistory(self.path).profiles())
        self.assertEqual(result['time_source'],'measured-profile')
        self.assertEqual(result['time_seconds'],240)
        self.assertEqual(result['memory_expected_gib'],9)
        self.assertEqual(result['observed_samples'],1)

    def test_context_interpolation_and_sequential_memory_use_measurements(self):
        for audio,elapsed,reserved in ((900,90,5),(1800,240,8)):
            metrics=measurement(); metrics.update(audio_seconds=audio,inference_seconds=elapsed,peak_reserved_gib=reserved)
            self.history.record(metrics,model=ORIGINAL_MODEL)
        full=estimate_processing(self.device,1350,1,self.gpu,profiles=self.history.profiles())
        self.assertEqual(full['time_seconds'],165)
        self.assertEqual(full['memory_expected_gib'],7.5)
        split=estimate_processing(self.device,2700,2,self.gpu,profiles=self.history.profiles())
        self.assertEqual(split['memory_expected_gib'],7.5)
        self.assertGreater(split['time_seconds'],330)  # boundary inference included

    def test_settings_gpu_capacity_models_and_software_do_not_pool(self):
        self.history.record(measurement(),model=ORIGINAL_MODEL)
        for gpu in ({**self.gpu,'name':'Other GPU'},{**self.gpu,'memory_total_mib':8*1024}):
            self.assertEqual(estimate_processing(self.device,1800,1,gpu,profiles=self.history.profiles())['time_source'],'initial-heuristic')
        for options in ({'precision':'fp16'},{'attention':'eager'},{'use_cache':False},{'max_tokens':4096}):
            result=estimate_processing({**self.device,'options':options},1800,1,self.gpu,profiles=self.history.profiles())
            self.assertEqual(result['time_source'],'initial-heuristic')
        changed=measurement(); changed['software']={**changed['software'],'transformers':'new-version'}; changed['inference_seconds']=400
        self.history.record(changed,model=ORIGINAL_MODEL)
        result=estimate_processing(self.device,1800,1,self.gpu,profiles=self.history.profiles())
        self.assertEqual(result['time_seconds'],400)
        self.assertEqual(result['observed_samples'],1)

    def test_invalid_truncated_results_and_content_are_not_saved(self):
        for changes in ({'hit_token_limit':True},{'audio_seconds':float('nan')},{'inference_seconds':-1},{'precision':'int8'},{'engine':'unknown'},{'software':'bad'},{'gpu_name':None}):
            metrics=measurement(); metrics.update(changes)
            self.assertFalse(self.history.record(metrics,model=ORIGINAL_MODEL))
        self.assertFalse(self.history.record(measurement(),model='other/model'))
        metrics=measurement();metrics.update(text='private contents',filename='secret.wav',api_key='secret-token',host='private-host')
        self.history.record(metrics,model=ORIGINAL_MODEL)
        with self.history._connect() as connection:
            saved=json.dumps(connection.execute('SELECT * FROM samples').fetchall())
        for private in ('private contents','secret.wav','secret-token','private-host'):
            self.assertNotIn(private,saved)

    def test_manual_precision_is_not_assigned_to_auto_and_bridge_cache_does_not_inflate_short_jobs(self):
        self.history.record(measurement(requested_precision='fp16',precision='fp16'),model=ORIGINAL_MODEL)
        result=estimate_processing(self.device,1800,1,self.gpu,profiles=self.history.profiles())
        self.assertEqual(result['time_source'],'initial-heuristic')
        for seconds,reserved in ((900,5),(1800,8)):
            self.history.record(measurement(audio_seconds=seconds,peak_reserved_gib=reserved),model=ORIGINAL_MODEL)
        self.history.record(measurement(kind='bridge',audio_seconds=30,peak_reserved_gib=20),model=ORIGINAL_MODEL)
        result=estimate_processing(self.device,1800,2,self.gpu,profiles=self.history.profiles())
        self.assertEqual(result['memory_expected_gib'],6)


if __name__ == '__main__':
    unittest.main()
