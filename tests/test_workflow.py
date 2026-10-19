"""TXT synchronization and CLI summaries without inference or external requests."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from src.store import NoteStore
from src.text_files import save_text, speaker_changes, TextConflict
from src.summaries import Summaries, command, provider_status


def seed(store):
    note_id = 'a' * 32
    directory = store.upload_directory(note_id)
    directory.mkdir(parents=True)
    source = directory / 'recording.wav'
    source.write_bytes(b'RIFF')
    note = store.create({'id': note_id, 'title': '주간 회의', 'original_filename': 'recording.wav',
                         'source_path': str(source), 'status': 'done'})
    segments = [{'id': 0, 'start': 0, 'end': 2, 'speaker': 'S01', 'text': '금요일에 배포합니다.'},
                {'id': 1, 'start': 2, 'end': 4, 'speaker': 'S02', 'text': '제가 테스트할게요.'},
                {'id': 2, 'start': 4, 'end': 6, 'speaker': 'S01', 'text': '좋습니다.'}]
    return store.update(note_id, segments=segments, raw_segments=segments, speaker_names={})


class TextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = NoteStore(self.root / 'data' / 'notes.sqlite3')
        self.note = seed(self.store)
        self.folder = self.root / 'exports'

    def test_names_save_updates_actual_txt_and_preserves_audio(self):
        note = save_text(self.store, self.note['id'], str(self.folder), {'speaker_names': {'S01': '민수'}})
        saved = Path(note['text_path'])
        self.assertIn('민수: 금요일', saved.read_text())
        note2 = save_text(self.store, note['id'], str(self.root / 'other'), {'speaker_names': {'S01': '수진'}})
        self.assertEqual(note['text_path'], note2['text_path'])
        self.assertNotEqual(note['text_hash'], note2['text_hash'])
        self.assertIn('수진: 좋습니다.', saved.read_text())
        self.assertNotIn('민수', saved.read_text())
        self.assertTrue(self.store.resolve_path(note['id'], note['source_path']).exists())
        self.assertFalse((self.root / 'other').exists())

    def test_outside_edit_is_not_overwritten_or_committed(self):
        note = save_text(self.store, self.note['id'], str(self.folder))
        Path(note['text_path']).write_text('외부에서 수정한 내용')
        with self.assertRaises(TextConflict):
            save_text(self.store, note['id'], changes={'speaker_names': {'S01': '새 이름'}})
        self.assertEqual(self.store.get(note['id'])['speaker_names'], {})
        self.assertEqual(Path(note['text_path']).read_text(), '외부에서 수정한 내용')

    def test_failed_write_does_not_commit_names(self):
        with patch('src.text_files.os.replace', side_effect=PermissionError('쓰기 권한 없음')):
            with self.assertRaises(PermissionError):
                save_text(self.store, self.note['id'], str(self.folder), {'speaker_names': {'S01': '홍길동'}})
        self.assertEqual(self.store.get(self.note['id'])['speaker_names'], {})
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_existing_files_and_symlinks_are_preserved(self):
        self.folder.mkdir()
        target = self.folder / f"주간 회의-{self.note['id'][:8]}.txt"
        outside = self.root / 'outside.txt'
        outside.write_text('keep me')
        target.symlink_to(outside)
        note = save_text(self.store, self.note['id'], str(self.folder))
        self.assertNotEqual(str(target), note['text_path'])
        self.assertEqual(outside.read_text(), 'keep me')
        Path(note['text_path']).unlink()
        Path(note['text_path']).symlink_to(outside)
        with self.assertRaises(ValueError):
            save_text(self.store, note['id'])

    def test_unknown_speakers_and_newline_names_rejected(self):
        for names in ({'S99': '없는 화자'}, {'S01': 'a\nb'}, {'S01': 'a' * 61}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                save_text(self.store, self.note['id'], str(self.folder), {'speaker_names': names})
        self.assertFalse(self.folder.exists())

    def test_merge_preserves_original_segments_and_survives_reopen_and_rename(self):
        note = save_text(self.store, self.note['id'], str(self.folder), {
            'speaker_names': {'S01': '지민', 'S02': '수빈'}, 'speaker_merges': {'S02': 'S01'}})
        self.assertEqual({s['speaker'] for s in note['segments']}, {'S01'})
        self.assertEqual(note['raw_segments'], self.note['raw_segments'])
        self.assertEqual(note['speaker_merges'], {'S02': 'S01'})
        self.assertEqual([s['text'] for s in note['segments']], [s['text'] for s in self.note['segments']])
        reopened = NoteStore(self.store.database).get(note['id'])
        self.assertEqual(reopened['speaker_merges'], {'S02': 'S01'})
        renamed = save_text(self.store, note['id'], changes={'speaker_names': {'S01': '민수'}})
        self.assertEqual(renamed['speaker_names']['S02'], '민수')
        self.assertEqual(Path(note['text_path']).read_text().count('민수:'), 3)
        self.assertNotIn('수빈:', Path(note['text_path']).read_text())

    def test_chained_merges_resolve_to_one_speaker_and_invalid_merges_do_not_write(self):
        segment = {**self.note['segments'][0], 'id': 3, 'speaker': 'S03'}
        note = self.store.update(self.note['id'], segments=self.note['segments']+[segment], raw_segments=self.note['raw_segments']+[segment])
        changes = speaker_changes(note, {'S03': '대표 화자'}, {'S01': 'S02', 'S02': 'S03'})
        self.assertEqual(changes['speaker_merges'], {'S01': 'S03', 'S02': 'S03'})
        self.assertEqual({s['speaker'] for s in changes['segments']}, {'S03'})
        for merges in ({'S01': 'S01'}, {'S99': 'S01'}, {'S01': 'S99'}, {'S01':'S02','S02':'S01'}):
            with self.subTest(merges=merges), self.assertRaises(ValueError):
                save_text(self.store, note['id'], str(self.folder), {'speaker_names':{}, 'speaker_merges':merges})
        self.assertFalse(self.folder.exists())

    def test_merge_write_conflict_keeps_both_speakers(self):
        note = save_text(self.store, self.note['id'], str(self.folder))
        Path(note['text_path']).write_text('외부에서 편집한 원문')
        with self.assertRaises(TextConflict):
            save_text(self.store, note['id'], changes={'speaker_names':{}, 'speaker_merges':{'S02':'S01'}})
        restored = self.store.get(note['id'])
        self.assertEqual(restored['speaker_merges'], {})
        self.assertEqual(restored['segments'], self.note['segments'])
        self.assertEqual(Path(note['text_path']).read_text(), '외부에서 편집한 원문')


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = NoteStore(self.root / 'data' / 'notes.sqlite3')
        self.note = save_text(self.store, seed(self.store)['id'], str(self.root / 'txt'))
        self.manager = Summaries(self.root / 'summaries')
        self.addAsyncCleanup(self.manager.close)

    def start(self, note, provider, prompt, directory=None):
        return self.manager.start(note, provider, prompt, str(directory or self.root / 'txt'))

    def fake_cli(self, body):
        binary = self.root / 'fake cli'
        binary.write_text('#!' + sys.executable + '\n' + body)
        binary.chmod(0o755)
        self.enterContext(patch('src.summaries.executable', return_value=str(binary)))
        return binary

    async def completed(self):
        task = self.manager.tasks.get(self.note['id'])
        if task:
            await asyncio.wait_for(task, 5)
        return self.manager.status(self.store.get(self.note['id']))

    async def test_codex_uses_fixed_snapshot_via_stdin_without_shell(self):
        capture = self.root / 'capture.txt'
        self.fake_cli(f'''import pathlib,sys,time
source=sys.stdin.read()
pathlib.Path({str(capture)!r}).write_text(source)
time.sleep(.15)
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('금요일 배포. S02가 테스트.')
''')
        self.start(self.note, 'codex', '요약해 줘; $(touch should-not-exist)')
        with self.assertRaises(ValueError):
            self.start(self.note, 'codex', '중복 실행')
        updated = save_text(self.store, self.note['id'], changes={'speaker_names': {'S01': '지민'}})
        result = await self.completed()
        self.assertEqual(result['phase'], 'done')
        self.assertIn('S01:', capture.read_text())
        self.assertNotIn('지민:', capture.read_text())
        self.assertIn('$(touch should-not-exist)', capture.read_text())
        self.assertIn('summary in Markdown', capture.read_text())
        markdown = Path(result['result']['markdown_path'])
        self.assertEqual(markdown.parent, self.root / 'txt')
        self.assertEqual(markdown.suffix, '.md')
        self.assertEqual(markdown.read_text(), result['result']['text'] + '\n')
        self.assertEqual(result['result']['source_hash'], self.note['text_hash'])
        self.assertTrue(result['stale'])
        restored = Summaries(self.manager.directory)
        self.assertEqual(restored.status(updated)['result']['text'], result['result']['text'])

    async def test_claude_json_result_and_error_are_distinguished(self):
        self.fake_cli("import sys,json\nsys.stdin.read()\nprint(json.dumps({'result':'요약 완료','is_error':False}))\n")
        self.start(self.note, 'claude', '요약')
        result = await self.completed()
        self.assertEqual(result['result']['text'], '요약 완료')
        markdown = Path(result['result']['markdown_path'])
        self.assertEqual(markdown.read_text(), '요약 완료\n')
        self.fake_cli("import sys,json\nsys.stdin.read()\nprint(json.dumps({'result':'로그인 필요','is_error':True}))\n")
        self.start(self.note, 'claude', '다시 요약')
        result = await self.completed()
        self.assertEqual(result['phase'], 'error')
        self.assertIn('로그인 필요', result['error'])
        self.assertEqual(result['result']['text'], '요약 완료')
        self.assertEqual(list(markdown.parent.glob('*.md')), [markdown])

    async def test_markdown_uses_selected_folder_and_never_overwrites_existing_files(self):
        self.fake_cli("import sys,json\nsys.stdin.read()\nprint(json.dumps({'result':'# 요약\\n\\n- 할 일','is_error':False}))\n")
        folder = self.root / 'chosen'
        folder.mkdir()
        existing = folder / (Path(self.note['text_path']).stem + '-요약.md')
        outside = self.root / 'outside.md'
        outside.write_text('사용자 문서')
        existing.symlink_to(outside)
        source = Path(self.note['text_path']).read_bytes()
        self.start(self.note, 'claude', '요약', folder)
        first = (await self.completed())['result']
        self.assertNotEqual(first['markdown_path'], str(existing))
        self.assertEqual(Path(first['markdown_path']).parent, folder)
        self.start(self.note, 'claude', '요약', folder)
        second = (await self.completed())['result']
        self.assertNotEqual(first['markdown_path'], second['markdown_path'])
        self.assertEqual(Path(first['markdown_path']).read_text(), '# 요약\n\n- 할 일\n')
        self.assertEqual(outside.read_text(), '사용자 문서')
        self.assertEqual(Path(self.note['text_path']).read_bytes(), source)
        self.assertFalse(list(folder.glob('.cosmoss-*')))

    async def test_default_is_downloads_and_save_failure_keeps_new_result(self):
        self.fake_cli("import sys,json\nsys.stdin.read()\nprint(json.dumps({'result':'# 완료','is_error':False}))\n")
        downloads = self.root / 'Downloads'
        downloads.mkdir()
        with patch('src.summaries.Path.home', return_value=self.root):
            self.manager.start(self.note, 'claude', '요약')
            result = await self.completed()
        self.assertEqual(Path(result['result']['markdown_path']).parent, downloads)
        with patch('src.summaries.save_markdown', side_effect=PermissionError('권한 없음')):
            self.start(self.note, 'claude', '다시 요약')
            result = await self.completed()
        self.assertEqual(result['phase'], 'error')
        self.assertIn('MD 파일을 저장하지 못했습니다', result['error'])
        self.assertEqual(result['result']['text'], '# 완료')
        self.assertNotIn('markdown_path', result['result'])

    async def test_invalid_output_directory_does_not_start_cli(self):
        self.fake_cli('pass\n')
        for directory in ('relative', str(self.root / 'missing')):
            with self.assertRaises(ValueError):
                self.start(self.note, 'claude', '요약', directory)
        self.assertFalse(self.manager.tasks)

    async def test_cancel_kills_cli_process_and_keeps_previous_result(self):
        marker = self.root / 'pid'
        self.fake_cli(f'import os,pathlib,time\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n')
        self.start(self.note, 'codex', '요약')
        for _ in range(100):
            if marker.exists(): break
            await asyncio.sleep(.02)
        self.assertTrue(marker.exists())
        await self.manager.cancel(self.note['id'])
        self.assertEqual(self.manager.status(self.note)['phase'], 'cancelled')
        with self.assertRaises(ProcessLookupError): os.kill(int(marker.read_text()), 0)
        self.assertFalse(self.manager.busy(self.note['id']))
        self.assertFalse(list((self.root / 'txt').glob('*.md')))

    async def test_immediate_cancel_and_interrupted_session(self):
        self.fake_cli('import time\ntime.sleep(60)\n')
        self.start(self.note, 'codex', '요약')
        await self.manager.cancel(self.note['id'])
        self.assertEqual(self.manager.status(self.note)['phase'], 'cancelled')
        self.manager._save(self.note['id'], {'phase': 'running'})
        restored = Summaries(self.manager.directory)
        self.assertEqual(restored.status(self.note)['phase'], 'error')

    async def test_provider_missing_does_not_start_job(self):
        with patch('src.summaries.executable', return_value=None):
            self.assertFalse((await provider_status('claude'))['installed'])
            with self.assertRaises(ValueError): self.start(self.note, 'claude', '요약')
        self.assertEqual(self.manager.status(self.note)['phase'], 'idle')

    async def test_large_source_is_rejected_not_silently_cut(self):
        self.fake_cli('pass\n')
        Path(self.note['text_path']).write_bytes(b'x' * 1_500_001)
        with self.assertRaises(ValueError): self.start(self.note, 'codex', '요약')
        self.assertFalse(self.manager.tasks)

    def test_cli_args_disable_tools_and_never_use_permission_bypass(self):
        for provider in ('codex', 'claude'):
            args = command(provider, '/path with spaces/' + provider, self.root / 'result.txt')
            self.assertNotIn('--dangerously-skip-permissions', args)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
            if provider == 'codex':
                self.assertIn('read-only', args)
                self.assertIn('--ignore-user-config', args)
            else:
                self.assertEqual(args[args.index('--tools') + 1], '')
                self.assertIn('--strict-mcp-config', args)


class WorkflowAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = NoteStore(self.root / 'data' / 'notes.sqlite3')
        self.note = seed(self.store)
        self.enterContext(patch.object(main, 'STORE', self.store))
        self.manager = Summaries(self.root / 'summaries')
        self.enterContext(patch.object(main, 'SUMMARIES', self.manager))
        self.addAsyncCleanup(self.manager.close)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def test_speaker_save_and_later_body_edits_update_same_txt(self):
        base = '/api/notes/' + self.note['id']
        response = await self.client.post(base + '/speakers', json={'speaker_names': {'S02': '수빈'}, 'directory': str(self.root / 'txt')})
        self.assertEqual(response.status_code, 200, response.text)
        target = Path(response.json()['text_path'])
        self.assertIn('수빈:', target.read_text())
        segments = self.store.get(self.note['id'])['segments']
        segments[1]['text'] = '내일까지 테스트합니다.'
        response = await self.client.patch(base, json={'segments': segments})
        self.assertEqual(response.status_code, 200)
        self.assertIn('수빈: 내일까지 테스트합니다.', target.read_text())
        self.assertEqual(response.json()['text_path'], str(target))
        response = await self.client.post(base + '/speakers', json={'speaker_names': {'S99': '없음'}})
        self.assertEqual(response.status_code, 409)

    async def test_merge_route_persists_identity_and_rejects_bad_targets(self):
        base = '/api/notes/' + self.note['id']
        response = await self.client.post(base+'/speakers', json={'speaker_names':{'S01':'지민'},'speaker_merges':{'S02':'S01'},'directory':str(self.root/'txt')})
        self.assertEqual(response.status_code, 200, response.text)
        note = response.json()
        self.assertEqual({s['speaker'] for s in note['segments']}, {'S01'})
        self.assertEqual(note['raw_segments'], self.note['raw_segments'])
        target = Path(note['text_path'])
        self.assertEqual(target.read_text().count('지민:'), 3)
        before = target.read_bytes()
        response = await self.client.post(base+'/speakers', json={'speaker_names':{},'speaker_merges':{'S01':'S99'}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(target.read_bytes(), before)

    async def test_cross_site_cannot_write_files_or_launch_cli(self):
        for suffix in ('speakers', 'text-file', 'summary'):
            response = await self.client.post('/api/notes/' + self.note['id'] + '/' + suffix, json={}, headers={'sec-fetch-site': 'cross-site'})
            self.assertEqual(response.status_code, 403)
        with patch.object(self.main, 'provider_status', new=AsyncMock(return_value={'ready': False, 'name': 'Claude', 'message': '설치 필요', 'login': 'claude auth login', 'installed': False, 'install': '설치'})):
            response = await self.client.post('/api/notes/' + self.note['id'] + '/summary', json={'provider': 'claude', 'directory': str(self.root / 'txt')})
            self.assertEqual(response.status_code, 409)
            self.assertFalse((self.root / 'txt').exists())

    async def test_summary_route_reads_named_txt_and_detects_subsequent_edits(self):
        binary = self.root / 'codex-fixture'
        capture = self.root / 'stdin.txt'
        binary.write_text('#!' + sys.executable + '\n' + f'''import pathlib,sys
pathlib.Path({str(capture)!r}).write_text(sys.stdin.read())
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('금요일 배포')
''')
        binary.chmod(0o755)
        base = '/api/notes/' + self.note['id']
        self.store.update(self.note['id'], speaker_names={'S01': '지민'})
        with patch.object(self.main, 'provider_status', new=AsyncMock(return_value={'ready': True})), patch('src.summaries.executable', return_value=str(binary)):
            response = await self.client.post(base + '/summary', json={'provider': 'codex', 'prompt': '할 일 정리', 'directory': str(self.root / 'txt')})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()['phase'], 'running')
            task = self.manager.tasks.get(self.note['id'])
            if task: await task
        result = (await self.client.get(base + '/summary')).json()
        self.assertEqual(result['result']['text'], '금요일 배포')
        markdown = Path(result['result']['markdown_path'])
        self.assertEqual(markdown.parent, self.root / 'txt')
        self.assertEqual(markdown.read_text(), '금요일 배포\n')
        self.assertFalse(result['stale'])
        self.assertIn('지민: 금요일', capture.read_text())
        self.assertIn('할 일 정리', capture.read_text())
        await self.client.patch(base, json={'speaker_names': {'S01': '새 이름'}})
        self.assertTrue((await self.client.get(base + '/summary')).json()['stale'])
        exported = Path(self.store.get(self.note['id'])['text_path'])
        self.assertEqual((await self.client.delete(base)).status_code, 204)
        self.assertTrue(exported.exists(), 'Deleting the app note deleted an exported user TXT')
        self.assertTrue(markdown.exists(), 'Deleting the app note deleted an exported summary')
        self.assertFalse(self.manager._path(self.note['id']).exists())


if __name__ == '__main__': unittest.main()
