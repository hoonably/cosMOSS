"""Speaker-only edits, stale-result protection, and actual SSH lease teardown."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import os
import select
import subprocess
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import ModuleType

import httpx

from src.diarization import Diarization, fingerprint, proposals
from src.store import NoteStore
from src.text_files import write_text


def fixture():
    segments = [dict(id=index, start=index * 4, end=index * 4 + 4, speaker=speaker, text='원문 ' + str(index))
                for index, speaker in enumerate(['S01', 'S02', 'S01', 'S02'])]
    note = dict(id='d' * 32, segments=segments, raw_segments=copy.deepcopy(segments),
                speaker_names={'S01':'가', 'S02':'나'}, speaker_merges={})
    turns = [dict(start=index * 4, end=index * 4 + 4, speaker=speaker)
             for index, speaker in enumerate(['a', 'b', 'a', 'c'])]
    return note, turns


class ProposalTests(unittest.TestCase):
    def test_merged_voice_becomes_new_identity_without_rewriting_text_or_raw(self):
        note, turns = fixture()
        before = copy.deepcopy(note)
        result = proposals(note, turns)
        self.assertEqual(result['speaker_count'], 3)
        self.assertEqual(result['rows'][-1]['after'], 'S03')
        self.assertEqual(note, before)
        self.assertEqual(len(result['voices']), 3)
        self.assertTrue(all(len(voice['samples']) <= 3 for voice in result['voices']))
        self.assertEqual([row['text'] for row in result['rows']], [s['text'] for s in note['segments']])

    def test_mixed_utterance_is_flagged_and_missing_audio_is_not_guessed(self):
        note, _ = fixture()
        result = proposals(note, [dict(start=0, end=2, speaker='a'), dict(start=2, end=4, speaker='b')])
        self.assertTrue(result['rows'][0]['mixed'])
        self.assertEqual(result['unmatched_segments'], 3)
        for invalid in (float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError): proposals(note, [dict(start=invalid, end=4, speaker='a')])

    def test_partial_approval_stale_note_and_forged_ids(self):
        note, turns = fixture()
        with tempfile.TemporaryDirectory() as temp:
            manager = Diarization(Path(temp))
            result = {**proposals(note, turns), 'proposal_id':'a' * 32, 'source_fingerprint':fingerprint(note)}
            manager.states['note:' + note['id']] = {'phase':'done', 'result':result}
            changes = manager.approved(note, 'a' * 32, [3])
            self.assertEqual([s['speaker'] for s in changes['segments']], ['S01','S02','S01','S03'])
            self.assertNotIn('raw_segments', changes)
            for ids in ([], [3,3], [999], [0]):
                with self.assertRaises(ValueError): manager.approved(note, 'a' * 32, ids)
            note['segments'][0]['text'] = '수정된 문장'
            with self.assertRaisesRegex(ValueError, '변경'): manager.approved(note, 'a' * 32, [3])


class AuthenticationTests(unittest.TestCase):
    def test_first_download_requires_login_but_cached_model_works_without_token(self):
        from src import diarization_worker as worker
        class CacheMiss(Exception): pass
        class Gate(Exception): pass
        class HTTPError(Exception): pass
        hub, errors = ModuleType('huggingface_hub'), ModuleType('huggingface_hub.errors')
        errors.LocalEntryNotFoundError, errors.GatedRepoError, errors.HfHubHTTPError = CacheMiss, Gate, HTTPError
        hub.get_token = Mock(return_value=None)
        hub.snapshot_download = Mock(side_effect=CacheMiss)
        with patch.dict(sys.modules, {'huggingface_hub':hub, 'huggingface_hub.errors':errors}), patch.object(worker, 'emit') as emit:
            self.assertIsNone(worker.setup('pyannote-community-1'))
            self.assertEqual(emit.call_args.kwargs['phase'], 'auth_required')
            self.assertEqual(hub.snapshot_download.call_count, 1)
            hub.snapshot_download.side_effect = None
            hub.snapshot_download.return_value = '/cached/model'
            self.assertEqual(worker.setup('pyannote-community-1'), '/cached/model')
            hub.snapshot_download.side_effect = [CacheMiss(), '/downloaded/model']
            self.assertEqual(worker.setup('nemotron-3'), '/downloaded/model')
            self.assertFalse(hub.snapshot_download.call_args.kwargs['local_files_only'])
            hub.get_token.return_value = 'test-only'
            hub.snapshot_download.side_effect = [CacheMiss(), Gate()]
            self.assertIsNone(worker.setup('pyannote-community-1'))
            self.assertEqual(emit.call_args.kwargs['phase'], 'auth_required')


class APITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = NoteStore(self.root / 'notes.sqlite3')
        self.manager = Diarization(self.root)
        self.enterContext(patch.object(main, 'STORE', self.store))
        self.enterContext(patch.object(main, 'DIARIZATION', self.manager))
        note, turns = fixture()
        folder = self.store.upload_directory(note['id']); folder.mkdir(parents=True)
        source = folder / 'audio.wav'; source.write_bytes(b'RIFF')
        self.store.create(dict(id=note['id'], source_path=str(source), original_filename='audio.wav', title='검증', status='done'))
        self.note = self.store.update(note['id'], **{key:note[key] for key in ('segments','raw_segments','speaker_names','speaker_merges')})
        self.note = self.store.update(note['id'], **write_text(self.note, str(self.root)))
        result = {**proposals(self.note, turns), 'proposal_id':'a' * 32, 'source_fingerprint':fingerprint(self.note)}
        self.manager.states['note:' + note['id']] = {'phase':'done', 'result':result}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def test_preview_never_writes_approval_saves_subset_and_preserves_original(self):
        original = Path(self.note['text_path']).read_bytes()
        base = '/api/notes/' + self.note['id'] + '/diarization'
        self.assertEqual((await self.client.get(base)).status_code, 200)
        self.assertEqual(Path(self.note['text_path']).read_bytes(), original)
        response = await self.client.post(base + '/approve', json={'proposal_id':'a' * 32, 'segment_ids':[3]})
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.store.get(self.note['id'])
        self.assertEqual(saved['raw_segments'], self.note['raw_segments'])
        self.assertEqual(saved['segments'][-1]['speaker'], 'S03')
        self.assertEqual(saved['segments'][-1]['text'], self.note['segments'][-1]['text'])
        self.assertNotEqual(Path(saved['text_path']).read_bytes(), original)
        self.assertEqual((await self.client.post(base + '/approve', json={'proposal_id':'a'*32,'segment_ids':[3]})).status_code, 409)

    async def test_external_file_edits_and_cross_origin_requests_are_preserved(self):
        base = '/api/notes/' + self.note['id'] + '/diarization'
        Path(self.note['text_path']).write_text('외부 변경')
        response = await self.client.post(base + '/approve', json={'proposal_id':'a'*32,'segment_ids':[3]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get(self.note['id'])['segments'], self.note['segments'])
        response = await self.client.post(base, headers={'Origin':'https://external.example'}, json={})
        self.assertEqual(response.status_code, 403)
        response = await self.client.post('/api/diarization/setup', headers={'Origin':'https://external.example'}, json={})
        self.assertEqual(response.status_code, 403)

    async def test_local_nemotron_uses_no_ssh_and_rejects_other_models(self):
        base = '/api/notes/' + self.note['id'] + '/diarization'
        payload = {'host':'@local','model_key':'nemotron-3','gpu_uuid':'local'}
        with patch.object(self.main, 'probe_host', new=AsyncMock()) as ssh, \
             patch.object(self.main.MEDIA_TOOLS, 'require_ready', new=AsyncMock()), \
             patch.object(self.main.MEDIA_TOOLS, 'executable', return_value='ffmpeg'), \
             patch.object(self.manager, 'start', return_value={'phase':'preparing'}) as start:
            response = await self.client.post(base,json=payload)
            self.assertEqual(response.status_code,202,response.text)
            ssh.assert_not_awaited()
            self.assertEqual(start.call_args.args[3:6],('@local','nemotron-3','local'))
            response = await self.client.post(base,json={**payload,'model_key':'pyannote-community-1'})
            self.assertEqual(response.status_code,409)
        response = await self.client.post('/api/diarization/setup',json={'host':'@local','model_key':'pyannote-community-1'})
        self.assertEqual(response.status_code,400)
        catalog = (await self.client.get('/api/diarization/models')).json()
        self.assertEqual(catalog['default_model_key'],'nemotron-3')
        self.assertEqual(catalog['hosts'][0],'@local')


class LocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_dispatch_and_actual_subprocess_cancellation(self):
        from src.local_diarization import LocalDiarization
        with tempfile.TemporaryDirectory() as temp:
            manager = Diarization(Path(temp))
            manager.local.execute = AsyncMock(return_value={'phase':'idle'})
            with patch('src.diarization.ssh_arguments',side_effect=AssertionError('SSH used')):
                self.assertEqual(await manager.execute('@local','nemotron-3','inspect',lambda _:None),{'phase':'idle'})
            engine = LocalDiarization(Path(temp))
            with self.assertRaises(ValueError):
                await engine.execute('pyannote-community-1','inspect',lambda _:None)
            ready, pid = asyncio.Event(), []
            def event(value): pid.append(value['pid']);ready.set()
            program = "import os,time,json; print('COSMOSS_DIARIZATION:'+json.dumps({'pid':os.getpid()}),flush=True); time.sleep(60)"
            task=asyncio.create_task(engine.command([sys.executable,'-u','-c',program],event))
            await asyncio.wait_for(ready.wait(),3)
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            with self.assertRaises(ProcessLookupError): os.kill(pid[0],0)
class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_closes_ssh_input_and_stops_actual_owned_child(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = Diarization(Path(temp))
            real_spawn = asyncio.create_subprocess_exec
            processes = []
            async def local_ssh(*args, **kwargs):
                child = await real_spawn(sys.executable, '-u', '-c',
                    "import sys,json; json.loads(sys.stdin.buffer.readline()); print('COSMOSS_DIARIZATION:'+json.dumps({'phase':'running'}),flush=True); [None for line in sys.stdin.buffer]", **kwargs)
                processes.append(child)
                return child
            ready = asyncio.Event()
            with patch('src.diarization.discover_hosts', return_value=['test']), patch('src.diarization.asyncio.create_subprocess_exec', new=local_ssh):
                task = asyncio.create_task(manager.execute('test', 'nemotron-3', 'run', lambda _:ready.set()))
                await asyncio.wait_for(ready.wait(), 3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(processes[0].returncode, 0)


class BootstrapTests(unittest.TestCase):
    def test_eof_and_silent_lease_stop_child_and_remove_audio(self):
        from src import diarization_bootstrap as bootstrap
        root = Path(__file__).resolve().parents[1]
        for disconnected in (True, False):
            with self.subTest(eof=disconnected), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                runtime = home / '.local/share/cosmoss/diarization' / bootstrap.VERSION
                (runtime / 'runtime/bin').mkdir(parents=True)
                (runtime / 'runtime/bin/python').symlink_to(sys.executable)
                child_pid = home / 'child.pid'
                worker = "import os,time; from pathlib import Path; Path(" + repr(str(child_pid)) + ").write_text(str(os.getpid())); print('COSMOSS_DIARIZATION:{\"phase\":\"running\"}',flush=True); time.sleep(60)"
                source = Path(bootstrap.__file__).read_text().replace('> 60:', '> .4:')
                program = "from pathlib import Path; Path.home=classmethod(lambda cls:Path(" + repr(temp) + ")); exec(compile(" + repr(source) + ", 'bootstrap.py', 'exec'), {'__name__':'__main__'})"
                process = subprocess.Popen([sys.executable,'-u','-c',program], stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                try:
                    payload = dict(action='run',model_key='nemotron-3',audio_bytes=4,gpu_uuid='GPU-0123',worker_source=worker,lifecycle_source='')
                    process.stdin.write(json.dumps(payload).encode()+b'\nRIFF');process.stdin.flush()
                    deadline=time.monotonic()+3
                    while not child_pid.exists() and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue(child_pid.exists())
                    pid=int(child_pid.read_text())
                    if disconnected:process.stdin.close()
                    self.assertEqual(process.wait(timeout=5),1)
                    with self.assertRaises(ProcessLookupError):os.kill(pid,0)
                    self.assertFalse(list(runtime.glob('job-*')))
                    self.assertTrue((runtime/'runtime/bin/python').exists())
                finally:
                    if process.poll() is None:process.kill();process.wait()
                    process.stdin.close();process.stdout.close()


if __name__ == '__main__': unittest.main()
