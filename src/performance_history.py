"""Local numeric CUDA measurements, independent of recordings and device IDs."""
import json
import math
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

ENGINE = 'cosmoss-cuda-v1'
ORIGINAL_MODEL = 'OpenMOSS-Team/MOSS-Transcribe-Diarize'


def gpu_name(name):
    return ' '.join(name.lower().removeprefix('nvidia ').removeprefix('geforce ').split()) if isinstance(name, str) else ''


def memory_class(mib):
    return round(mib / 1024 / 8) * 8 if isinstance(mib, (int, float)) and math.isfinite(mib) and mib > 0 else None


@lru_cache(maxsize=1)
def seed_profiles():
    return json.loads(Path(__file__).with_name('performance_seeds.json').read_text())


def matching_profile(device, gpu, profiles):
    """Never mix GPU memory classes, precision, models or software environments."""
    if device.get('backend') != 'cuda' or not gpu:
        return None
    options = device.get('options', {})
    if device.get('model_key') == 'custom' and not (device.get('model_id') or device.get('model')):
        return None
    model = device.get('model_id') or device.get('model') or ORIGINAL_MODEL
    precision = options.get('precision', 'auto')
    capacity = memory_class(gpu.get('memory_total_mib'))
    compatible = []
    for profile in profiles:
        if not any(sample['kind'] == 'main' for sample in profile['samples']):
            continue
        if (profile['engine'] != ENGINE or profile['gpu_name'] != gpu_name(gpu.get('name'))
                or profile['model'] != model
                or (capacity is not None and profile['memory_class_gib'] != capacity)
                or (precision == 'auto' and profile.get('requested_precision', 'auto') != 'auto')
                or (precision != 'auto' and profile['precision'] != precision)
                or profile['attention'] != options.get('attention', 'sdpa')
                or profile['use_cache'] != options.get('use_cache', True)
                or profile['max_tokens'] != options.get('max_tokens', 32768)):
            continue
        compatible.append(profile)
    # A software upgrade starts a new group. The newest observed environment
    # wins over older reference data; different environments are never pooled.
    return max(compatible, key=lambda item:item.get('updated_at', 0), default=None)


class PerformanceHistory:
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, profile TEXT NOT NULL, measurement TEXT NOT NULL, created_at REAL NOT NULL)')

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def record(self, metrics, *, model):
        """Only completed, non-truncated worker inference can calibrate a profile."""
        if not isinstance(metrics, dict) or metrics.get('engine') != ENGINE or metrics.get('model') != model:
            return False
        if metrics.get('hit_token_limit') is not False or metrics.get('precision') not in {'bf16', 'fp16'}:
            return False
        if metrics.get('requested_precision', 'auto') not in {'auto', 'bf16', 'fp16'}:
            return False
        values = [metrics.get(key) for key in ('audio_seconds', 'inference_seconds', 'peak_reserved_gib', 'memory_total_mib', 'max_tokens')]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 for value in values):
            return False
        if metrics.get('kind') not in {'main', 'bridge'} or not gpu_name(metrics.get('gpu_name')):
            return False
        software = metrics.get('software', {})
        if not isinstance(software, dict) or not all(isinstance(software.get(key), str) and 0 < len(software[key]) <= 100 for key in ('torch', 'transformers', 'model_revision')):
            return False
        profile = dict(engine=ENGINE, gpu_name=gpu_name(metrics['gpu_name']), memory_class_gib=memory_class(metrics['memory_total_mib']),
                       model=model, precision=metrics['precision'], requested_precision=metrics.get('requested_precision', 'auto'), attention=metrics.get('attention', 'sdpa'),
                       use_cache=metrics.get('use_cache', True), max_tokens=metrics['max_tokens'],
                       software={key:software[key] for key in ('torch', 'transformers', 'model_revision')})
        # Whitelist numerical fields: no transcript, audio, filename, host,
        # credential, note ID or GPU UUID is persisted here.
        sample = {key:metrics[key] for key in ('audio_seconds', 'inference_seconds', 'peak_reserved_gib', 'kind')}
        if isinstance(metrics.get('generated_tokens'), int):
            sample['generated_tokens'] = metrics['generated_tokens']
        if isinstance(metrics.get('cold_start'), bool):
            sample['cold_start'] = metrics['cold_start']
        key = json.dumps(profile, sort_keys=True)
        with self._connect() as connection:
            connection.execute('INSERT INTO samples(profile,measurement,created_at) VALUES(?,?,?)', (key, json.dumps(sample), time.time()))
            connection.execute('DELETE FROM samples WHERE profile=? AND id NOT IN (SELECT id FROM samples WHERE profile=? ORDER BY id DESC LIMIT 200)', (key, key))
        return True

    def profiles(self):
        groups = {}
        for profile in seed_profiles():
            base = {key:value for key,value in profile.items() if key not in {'samples', 'provenance'}}
            groups[json.dumps(base, sort_keys=True)] = {**base, 'samples':list(profile['samples']), 'observed_samples':0}
        with self._connect() as connection:
            rows = connection.execute('SELECT profile,measurement,created_at FROM samples ORDER BY id').fetchall()
        for key, measurement, created_at in rows:
            group = groups.setdefault(key, {**json.loads(key), 'samples':[], 'observed_samples':0})
            group['samples'].append(json.loads(measurement))
            group['observed_samples'] += 1
            group['updated_at'] = created_at
        return list(groups.values())
