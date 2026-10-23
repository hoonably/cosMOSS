"""Drag/confirm speaker merges against isolated real native-note and TXT APIs."""
import asyncio
import codecs
import sys
import tempfile
import uuid
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.store import NoteStore


async def main():
    base, data = sys.argv[1], Path(sys.argv[2]).resolve()
    engine = sys.argv[3] if len(sys.argv) > 3 else 'webkit'
    output = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    assert 'cosmoss-qa' in data.parts, 'Use an isolated QA data directory'
    store = NoteStore(data / 'moss-note.sqlite3')
    note_id = uuid.uuid4().hex
    upload = store.upload_directory(note_id)
    upload.mkdir(parents=True)
    audio = upload / 'fixture.wav'
    audio.write_bytes(b'RIFF')
    store.create({'id': note_id, 'title': '화자 병합 테스트', 'status': 'done', 'source_path': str(audio), 'original_filename': audio.name})
    original = [
        {'id':0, 'start':0, 'end':2, 'speaker':'S01', 'text':'첫 번째 발언입니다.'},
        {'id':1, 'start':2, 'end':4, 'speaker':'S02', 'text':'같은 사람의 다른 화자 표시입니다.'},
        {'id':2, 'start':4, 'end':6, 'speaker':'S03', 'text':'다른 사람의 발언입니다.'},
        {'id':3, 'start':6, 'end':8, 'speaker':'S01', 'text':'마지막 발언입니다.'},
    ]
    store.update(note_id, segments=original, raw_segments=original, speaker_names={})
    errors = []
    with tempfile.TemporaryDirectory(prefix='cosmoss-merge-ui-') as temporary:
        folder = Path(temporary).resolve()
        external = folder / '기존 TXT 병합.txt'
        text = '[00:00:01] 지민: 첫 발언\r\n본문의 수빈: 표기는 유지합니다.\r\n\r\n[00:00:03] 수빈: 두 번째 발언\r\n'
        original_bytes = codecs.BOM_UTF8 + text.encode('utf-8')
        external.write_bytes(original_bytes)
        async with async_playwright() as pw:
            options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
            browser = await getattr(pw, engine).launch(headless=True, **options)
            page = await browser.new_page(viewport={'width':1440,'height':950}, device_scale_factor=2)
            page.set_default_timeout(10000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            import json
            await page.add_init_script('window.webkit={messageHandlers:{getSaveLocation:{postMessage:async()=>('+json.dumps({'path':str(folder),'name':folder.name})+')}}};')
            await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[]))
            await page.route('**/api/devices', lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit','setup':{'phase':'idle'}}]))
            await page.route('**/api/local-model', lambda r:r.fulfill(json={'phase':'idle'}))
            await page.goto(base)
            await page.locator(f'.note-item[data-id="{note_id}"] .note-edit').click()
            await page.locator('#edit-speakers').click()
            initial = store.get(note_id)
            initial_text = Path(initial['text_path']).read_bytes() if initial['text_path'] else None
            assert await page.locator('#speaker-merge-help').inner_text() == '드래그로 화자를 병합할 수 있습니다.'

            async def drag(source, target):
                await page.locator(f'[data-select-speaker="{source}"] .speaker-choice-select').drag_to(page.locator(f'[data-select-speaker="{target}"]'))
                await page.locator('#speaker-merge-dialog').wait_for(state='visible')
                assert await page.evaluate('document.activeElement.id') == 'speaker-merge-title'

            async def confirm():
                await page.click('#confirm-speaker-merge')
                await page.locator('#speaker-merge-dialog').wait_for(state='hidden')

            await drag('S02','S01')
            assert 'S02' in await page.locator('#speaker-merge-question').inner_text()
            assert 'S01' in await page.locator('#speaker-merge-question').inner_text()
            assert await page.locator('.speaker-choice').count() == 3
            assert store.get(note_id)['speaker_merges'] == {}
            if output: await page.screenshot(path=str(output.with_name(output.stem+'-confirm.png')))
            await page.click('#cancel-speaker-merge')
            assert await page.locator('.speaker-choice').count() == 3
            await drag('S02','S01')
            await page.keyboard.press('Escape')
            await page.locator('#speaker-merge-dialog').wait_for(state='hidden')
            assert await page.locator('#speaker-dialog').is_visible()
            await drag('S02','S01')
            await confirm()
            assert await page.locator('.speaker-choice').count() == 2
            assert await page.locator('.speaker-preview-row.highlighted').count() == 3
            await drag('S01','S03')
            await confirm()
            assert await page.locator('.speaker-choice').count() == 1
            assert await page.locator('.speaker-preview-row.highlighted').count() == 4
            assert store.get(note_id)['speaker_merges'] == {}
            assert store.get(note_id)['text_path'] == initial['text_path']
            if initial_text is not None:
                assert Path(initial['text_path']).read_bytes() == initial_text
            await page.click('#cancel-speakers')
            await page.click('#edit-speakers')
            assert await page.locator('.speaker-choice').count() == 3, 'Cancelled draft merge persisted'
            await drag('S02','S01')
            await confirm()
            await page.fill('#speaker-name', '대표 화자')
            if output: await page.screenshot(path=str(output))
            await page.click('#save-speakers')
            await page.locator('#speaker-dialog').wait_for(state='hidden')
            note = store.get(note_id)
            assert note['speaker_merges'] == {'S02':'S01'}
            assert note['raw_segments'] == original
            assert {s['speaker'] for s in note['segments']} == {'S01','S03'}
            saved = Path(note['text_path'])
            assert saved.read_text().count('대표 화자:') == 3
            await page.click('#edit-speakers')
            assert await page.locator('.speaker-choice').count() == 2, 'Raw speaker reappeared after merge'
            await page.locator('[data-select-speaker="S01"]').click()
            await page.fill('#speaker-name','민수')
            await page.click('#save-speakers')
            await page.locator('#speaker-dialog').wait_for(state='hidden')
            assert store.get(note_id)['speaker_names']['S02'] == '민수'
            await page.click('#edit-speakers')
            await drag('S03','S01')
            await confirm()
            saved.write_text('외부에서 수정한 원문')
            await page.click('#save-speakers')
            await page.locator('#speaker-save-error').wait_for(state='visible')
            assert saved.read_text() == '외부에서 수정한 원문'
            assert store.get(note_id)['speaker_merges'] == {'S02':'S01'}
            await page.click('#cancel-speakers')
            await page.click('#close-note')
            await page.locator('#note-dialog').wait_for(state='hidden')
            await page.get_by_role('button',name='기존 TXT 병합 편집',exact=True).click()
            await page.click('#edit-speakers')
            await drag('S01','S02')
            assert '지민' in await page.locator('#speaker-merge-question').inner_text()
            assert '수빈' in await page.locator('#speaker-merge-question').inner_text()
            await confirm()
            assert external.read_bytes() == original_bytes
            await page.fill('#speaker-name','합친 화자')
            await page.click('#save-speakers')
            await page.locator('#speaker-dialog').wait_for(state='hidden')
            expected = text.replace('] 지민:', '] 합친 화자:').replace('] 수빈:', '] 합친 화자:')
            assert external.read_bytes() == codecs.BOM_UTF8 + expected.encode('utf-8')
            await page.click('#edit-speakers')
            assert await page.locator('.speaker-choice').count() == 1
            assert await page.locator('.speaker-choice').get_attribute('draggable') == 'false'
            assert not errors, errors
            await browser.close()
    store.delete(note_id)
    import shutil
    shutil.rmtree(upload)
    print(f'PASS {engine}: real drag/drop, confirmation/cancel/Escape, chained drafts, cancel-all, save/reopen, raw preservation, renaming, file conflicts, existing TXT BOM/CRLF and body preservation')


if __name__ == '__main__':
    asyncio.run(main())
