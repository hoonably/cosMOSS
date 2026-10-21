"""Server quantization retirement preserves settings and rejects stale jobs."""
import json
import tempfile
import unittest
from pathlib import Path

from src.cuda_server import cuda_precision, load_model
from src.devices import DeviceStore
from unittest.mock import patch


class CUDAPrecisionTests(unittest.TestCase):
    def test_retired_options_fail_before_importing_torch_or_acquiring_a_gpu(self):
        for options in ({'precision':'int4'}, {'precision':'int8'}, {'cuda_kv_bits':8}, {'cuda_kv_bits':4}):
            with self.subTest(options=options), patch('src.cuda_server.OPTIONS', options):
                with self.assertRaisesRegex(ValueError, '양자화 지원이 종료'):
                    load_model()
        for precision in ('auto', 'bf16', 'fp16'):
            self.assertEqual(cuda_precision({'precision':precision, 'kv_bits':8}), precision)

    def test_legacy_device_choices_migrate_and_survive_restart_without_affecting_mac(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DeviceStore(Path(temporary) / 'devices.sqlite3')
            mac_options = {'kv_bits':8, 'chunk_minutes':10}
            store.configure('local', model_key='mlx-4bit', options=mac_options)
            devices = []
            for bits in (4, 8):
                saved = store.add_ssh(name='legacy', host=f'qa-{bits}', remote_port=18400, model='OpenMOSS-Team/MOSS-Transcribe-Diarize', backend='cuda')
                saved.update(model_key=f'cuda-{bits}bit', model_source='folder', model_path='/cached/model', model_prepared=True,
                             options={'precision':f'int{bits}', 'cuda_kv_bits':bits, 'chunk_minutes':60, 'max_tokens':4096, 'bridge_seconds':0})
                with store._connect() as connection:
                    connection.execute('UPDATE devices SET config=? WHERE id=?', (json.dumps(saved), saved['id']))
                devices.append(saved)
            migrated = DeviceStore(store.database)
            for old in devices:
                actual = migrated.get(old['id'])
                expected = {**old, 'model_key':'cuda-original', 'options':{**old['options'], 'precision':'auto'}}
                expected['options'].pop('cuda_kv_bits')
                self.assertEqual(actual, expected)
                with migrated._connect() as connection:
                    persisted = json.loads(connection.execute('SELECT config FROM devices WHERE id=?', (old['id'],)).fetchone()[0])
                self.assertEqual(persisted, expected)
            self.assertEqual(migrated.get('local')['options'], mac_options)
            self.assertEqual(migrated.get('local')['model_key'], 'mlx-4bit')


if __name__ == '__main__':
    unittest.main()
