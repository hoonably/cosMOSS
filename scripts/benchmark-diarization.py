"""Four independent speaker-diarization ablations; keep private audio/results out of Git."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.diarization import Diarization
from src.ssh_devices import probe_host


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    inventory = await probe_host(args.host)
    (args.output / 'inventory.json').write_text(json.dumps(inventory, indent=2))
    jobs = [
        ('nemotron-fp32-offline', 'nemotron-3', {'precision': 'fp32', 'mode': 'offline'}),
        ('nemotron-bf16-offline', 'nemotron-3', {'precision': 'bf16', 'mode': 'offline'}),
        ('pyannote-auto', 'pyannote-community-1', {}),
        ('pyannote-3-speakers', 'pyannote-community-1', {'num_speakers': 3}),
    ]
    service = Diarization(args.output)
    async def run(index, name, model, options):
        gpu = next(gpu for gpu in inventory['gpus'] if gpu['index'] == index)
        started = time.monotonic()
        try:
            def event(value):
                print(name, value['phase'], value.get('metrics') or value.get('error', ''), flush=True)
            event_result = await service.execute(args.host, model, 'run', event, audio=args.audio,
                                                 gpu_uuid=gpu['uuid'], **options)
            result = event_result['result']
            result['wall_seconds'] = time.monotonic() - started
            result['gpu_index'] = index
            result['baseline_gpu_memory_mib'] = gpu['memory_used_mib']
            result['baseline_gpu_utilization_percent'] = gpu['utilization_percent']
            (args.output / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False))
            print('SUCCESS', name, {key: value for key, value in result.items() if key != 'turns'}, flush=True)
        except Exception as error:
            (args.output / (name + '-error.json')).write_text(json.dumps({'error': str(error)}))
            print('FAILED', name, str(error), flush=True)
    await asyncio.gather(*(run(index, *job) for index, job in enumerate(jobs)))
    if list(args.output.glob('*-error.json')):
        raise SystemExit('One or more benchmark configurations failed; inspect the private error files.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
