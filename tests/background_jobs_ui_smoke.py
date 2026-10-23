"""Background queue, non-disruptive completion, job actions and summary badges.

Uses synthetic notes and mocked API responses; never starts inference or an AI CLI.
"""
import asyncio
import copy
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base, engine, screenshot = sys.argv[1:4]
    def make(letter, title, status='done', **values):
        return dict(id=letter*32, title=title, status=status, original_filename=title+'.m4a',
                    created_at='2026-10-24T03:00:00Z', device_name='juyu-h200',
                    text_path='/tmp/'+title+'.txt' if status == 'done' else None,
                    output_format='txt', text_hash=letter, duration=300, chunk_count=1,
                    speaker_names={}, raw_segments=[],
                    segments=[dict(id=0, start=0, end=5, speaker='S01', text='선택한 노트의 편집 내용입니다.')],
                    **values)
    notes = [make('a', '다른 노트 정리', summary_status='not_started'),
             make('b', '요약을 마친 노트', summary_status='done', summary_completed_at='2026-10-24T03:01:00Z'),
             make('c', '수정한 노트', summary_status='stale'),
             make('d', '첫 번째 녹음', 'processing', progress_phase='transcribing',
                  inference_started_at='2026-10-24T03:00:00Z', inference_elapsed_seconds=194,
                  estimated_inference_seconds=529),
             make('e', '두 번째 녹음', 'queued', progress_phase='queued'),
             make('f', '다시 시도할 녹음', 'error', progress_phase='error', error='연결이 끊어졌습니다.')]
    summaries = {n['id']: {'phase':'idle'} for n in notes}
    calls, errors, trashed = [], [], []
    fail_restart = True

    async def route_note(route):
        nonlocal fail_restart
        identity, *parts = route.request.url.split('/api/notes/')[1].split('/')
        action = '/'.join(parts)
        note = next(n for n in notes if n['id'] == identity)
        if action == 'trash':
            assert note['status'] not in ('queued', 'processing')
            calls.append((identity, 'trash'))
            notes.remove(note);trashed.append(note)
            return await route.fulfill(status=204)
        if action == 'summary':
            if route.request.method == 'POST':
                note['summary_status'] = 'running'
                summaries[identity] = {'phase':'running', 'provider':'codex'}
                calls.append((identity, 'summary'))
            return await route.fulfill(json=summaries[identity])
        if action == 'cancel':
            calls.append((identity, 'cancel'))
            note.update(status='cancelled', progress_phase='cancelled')
        if action == 'restart':
            calls.append((identity, 'restart'))
            if fail_restart:
                fail_restart = False
                return await route.fulfill(status=409, json={'detail':'작업을 정리 중입니다. 잠시 후 다시 시작해 주세요.'})
            note.update(status='queued', progress_phase='queued', error=None)
        if route.request.method == 'PATCH':
            calls.append((identity, 'edit'))
            note.update(route.request.post_data_json)
        await route.fulfill(json=copy.deepcopy(note))

    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':950}, device_scale_factor=2)
        page.set_default_timeout(12000)
        page.on('pageerror', lambda e:errors.append(str(e)))
        async def trash_route(route):
            if route.request.method == 'GET':
                now = datetime.now(timezone.utc)
                return await route.fulfill(json=[{'id':n['id'],'title':n['title'],'kind':'note','can_restore':True,'deleted_at':now.isoformat(),'expires_at':(now+timedelta(days=7)).isoformat()} for n in trashed])
            identity = route.request.url.split('/api/trash/')[1].split('/')[0]
            note = next(n for n in trashed if n['id']==identity)
            trashed.remove(note);notes.append(note)
            await route.fulfill(json={'restored':True})
        await page.route('**/api/trash',trash_route)
        await page.route('**/api/trash/*/restore',trash_route)
        await page.route('**/api/devices', lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes', lambda r:r.fulfill(json=notes))
        await page.route('**/api/text-library**', lambda r:r.fulfill(json=[{
            **n, 'id':'txt_'+n['id'], 'source_type':'text_file', 'managed_note_id':n['id']
        } for n in notes if n['status'] == 'done']))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[{'id':'codex','name':'Codex','ready':True,'installed':True,'message':'로그인됨'}]))
        await page.route('**/api/notes/*', route_note)
        await page.route('**/api/notes/*/**', route_note)
        await page.goto(base)
        await page.locator('.background-job').first.wait_for()
        assert await page.locator('.note-item').count() == 3
        assert await page.locator('.background-job').count() == 3
        assert await page.locator('#note-count').inner_text() == '3'
        assert await page.locator('#note-dialog').is_hidden()
        assert await page.locator('.note-summary-badge').all_text_contents() == ['요약 전','요약 완료','다시 요약 필요']
        first = page.locator(f'.background-job[data-id="{notes[3]["id"]}"]')
        queued = page.locator(f'.background-job[data-id="{notes[4]["id"]}"]')
        failed = page.locator(f'.background-job[data-id="{notes[5]["id"]}"]')
        assert await first.locator('.job-percent').inner_text() == '36%'
        assert '예상 08:49' in await first.locator('.job-timing').inner_text()
        assert await queued.locator('.job-percent').inner_text() == '대기'
        assert await failed.locator('.job-percent').inner_text() == '오류'

        await page.locator('.note-item').first.locator('.note-select').click()
        await page.wait_for_function('!state.openingNote && state.current?.title === "다른 노트 정리"')
        await page.locator('#summary-prompt').fill('추가 요청 초안이 유지되어야 합니다.')
        await page.locator('#run-summary').click()
        await page.locator('#cancel-summary').wait_for(state='visible')
        assert await page.locator('.note-summary-badge').first.inner_text() == '요약 중'
        await queued.locator('[data-job-action=cancel]').click()
        await page.locator('#confirm-cancel-transcription').click()
        await queued.locator('[data-job-action=restart]').wait_for()
        assert (notes[4]['id'], 'cancel') in calls
        assert await page.evaluate('state.current.id') == notes[0]['id']
        assert await page.locator('#summary-prompt').input_value() == '추가 요청 초안이 유지되어야 합니다.'
        assert await page.locator('#cancel-summary').is_visible()
        assert await page.locator('#note-dialog').is_hidden()

        # Failed/cancelled jobs use the reversible bin and restore without
        # changing the other note currently selected for summary.
        cancelled_id = 'e'*32
        await queued.locator('[data-job-action=remove]').click()
        assert '7일' in await page.locator('#delete-dialog-description').inner_text()
        await page.locator('#confirm-delete-note').click()
        await page.wait_for_function('!state.fileActionId && !state.jobActions.size')
        assert await queued.count() == 0
        assert (cancelled_id,'trash') in calls
        await page.locator('#open-trash').click()
        await page.locator('[data-restore-trash]').wait_for()
        assert '7일' in await page.locator('#trash-dialog').inner_text()
        await page.locator('[data-restore-trash]').click()
        await page.wait_for_function('state.trashRestoring.size===0')
        assert await page.locator('.trash-item').count() == 0
        await page.locator('#close-trash').click()
        assert await queued.count() == 1
        assert await page.evaluate('state.current.id') == notes[0]['id']

        await failed.locator('[data-job-action=restart]').click()
        await page.wait_for_function('!state.jobActions.size')
        assert '작업을 정리 중' in await page.locator('#toast').inner_text()
        assert await failed.locator('[data-job-action=restart]').is_enabled()
        await failed.locator('[data-job-action=restart]').click()
        await failed.locator('[data-job-action=cancel]').wait_for()
        assert await page.evaluate('state.current.id') == notes[0]['id']

        # Job polling and completion must not overwrite an editor or steal its selection.
        await page.locator('.note-item').first.locator('.note-edit').click()
        await page.locator('#note-dialog').wait_for(state='visible')
        await page.locator('.segment-text').fill('입력 중인 내용이 유지됩니다.')
        await page.evaluate('clearTimeout(state.saveTimer)')
        next(n for n in notes if n['id']=='d'*32).update(status='done', progress_phase='done', text_path='/tmp/첫 번째 녹음.txt',
                        inference_finished_at='2026-10-24T03:09:00Z', summary_status='not_started')
        await page.evaluate('loadNotes()')
        assert await page.locator('.segment-text').input_value() == '입력 중인 내용이 유지됩니다.'
        assert await page.evaluate('state.current.id') == notes[0]['id']
        assert await page.locator('.note-item').count() == 4
        assert await first.count() == 0
        assert '전사가 완료' in await page.locator('#background-job-announcement').inner_text()
        assert await page.locator('#note-dialog').is_visible()
        await page.locator('#close-note').click()
        await page.locator('#note-dialog').wait_for(state='hidden')
        assert await page.locator('#summary-prompt').input_value() == '추가 요청 초안이 유지되어야 합니다.'

        # Nonselected summary completion is reflected through list polling.
        await page.locator('.note-item').nth(1).locator('.note-select').click()
        notes[0]['summary_status'] = 'done'
        notes[0]['summary_completed_at'] = '2026-10-24T03:10:00Z'
        await page.evaluate('loadNotes()')
        assert await page.locator('.note-summary-badge').first.inner_text() == '요약 완료'
        assert await page.evaluate('state.current.id') == notes[1]['id']

        for i in range(9):
            notes.append(make(str(i), f'대기 녹음 {i+1}', 'queued', progress_phase='queued'))
        await page.evaluate('loadNotes()')
        await page.locator('#toast').evaluate("e=>e.className='toast'")
        for width in (1440, 1100, 390):
            await page.set_viewport_size({'width':width,'height':950})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.locator('.background-job').evaluate_all('els=>els.every(e=>e.scrollWidth<=e.clientWidth+1)')
            assert await page.locator('#background-job-list').evaluate('e=>e.scrollHeight>e.clientHeight')
            assert await page.locator('#note-list').evaluate('e=>e.clientHeight>100')
            await page.screenshot(path=str(Path(screenshot).with_stem(Path(screenshot).stem+'-'+str(width))))
        assert not errors, errors
        await browser.close()
    print(engine, 'PASS: background stack, estimated progress, cancellation/retry by ID, retained editor/summary drafts, completion and badges, responsive scrolling')


asyncio.run(main())
