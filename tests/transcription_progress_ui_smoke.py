"""Estimated model-only progress, clock updates and terminal states; no inference.

Usage: uv run --no-project --with playwright python
tests/transcription_progress_ui_smoke.py URL PNG [chromium|webkit]
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base, output = sys.argv[1], Path(sys.argv[2])
    engine = sys.argv[3] if len(sys.argv) > 3 else 'chromium'
    note = dict(id='e' * 32, title='음성 전사 진행률', original_filename='녹음.m4a', status='processing',
                created_at='2026-10-21T00:00:00+00:00', duration=3937.13, chunk_count=1,
                processed_chunks=0, processed_bridges=0, bridge_count=0, segments=[], raw_segments=[], speaker_names={},
                progress_phase='preparing_model', processing_elapsed_seconds=194, inference_elapsed_seconds=None,
                inference_started_at=None, inference_finished_at=None, estimated_inference_seconds=None)
    errors = []
    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':1000}, device_scale_factor=2)
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error:errors.append(str(error)))
        await page.clock.install(time=datetime(2026, 10, 21, tzinfo=timezone.utc))
        await page.route('**/api/devices', lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx'}]))
        await page.route('**/api/local-model', lambda r:r.fulfill(json={'phase':'idle'}))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**', lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes', lambda r:r.fulfill(json=[note]))
        await page.route('**/api/notes/' + note['id'], lambda r:r.fulfill(json=note))
        await page.goto(base)
        await page.locator('.background-job').wait_for()
        assert await page.locator('#note-dialog').is_hidden()
        assert await page.locator('.job-percent').inner_text() == '준비 중'
        assert await page.locator('.job-timing').inner_text() == ''
        assert await page.locator('#processing-steps, #progress-count, #progress-audio, #model-progress').count() == 0
        async def put(**changes):
            note.update(changes)
            await page.evaluate('''note => {
                state.notes = [note];
                Object.defineProperty(note, '_elapsedReceivedAt', {value:performance.now(), configurable:true});
                renderBackgroundJobs();
            }''', note)
        await put(progress_phase='transcribing', estimated_inference_seconds=529,
                  inference_started_at='2026-10-21T00:00:00+00:00', inference_elapsed_seconds=194)
        assert await page.locator('.job-percent').inner_text() == '36%'
        assert '예상 08:49' in await page.locator('.job-timing').inner_text()
        assert '경과 03:14' in await page.locator('.job-timing').inner_text()
        await page.clock.fast_forward(6000)
        await page.wait_for_function('Number.parseInt(document.querySelector(".job-percent").textContent)>=37')
        for width in (1440, 1100, 390):
            await page.set_viewport_size({'width':width,'height':1000})
            assert await page.locator('.background-job').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            await page.locator('.background-job').screenshot(path=str(output.with_stem(output.stem + '-' + str(width))))
        await page.set_viewport_size({'width':1440,'height':1000})
        await put(inference_elapsed_seconds=600)
        assert await page.locator('.job-percent').inner_text() == '99%'
        await page.clock.fast_forward(120000)
        assert await page.locator('.job-percent').inner_text() == '99%'
        assert '기다리고' in await page.locator('.job-detail').inner_text()
        await put(status='cancelled', progress_phase='cancelled', inference_elapsed_seconds=720,
                  inference_finished_at='2026-10-21T00:12:00+00:00')
        frozen = await page.locator('.job-timing').inner_text()
        await page.clock.fast_forward(10000)
        assert await page.locator('.job-timing').inner_text() == frozen
        await put(status='queued', progress_phase='queued', inference_elapsed_seconds=None,
                  inference_started_at=None, inference_finished_at=None, estimated_inference_seconds=None)
        assert await page.locator('.job-percent').inner_text() == '대기'
        await put(status='processing', progress_phase='transcribing', inference_elapsed_seconds=0,
                  inference_started_at='2026-10-21T00:15:00+00:00', estimated_inference_seconds=100)
        assert await page.locator('.job-percent').inner_text() == '0%'
        await put(progress_phase='identifying_speakers', inference_elapsed_seconds=70,
                  inference_finished_at='2026-10-21T00:16:10+00:00')
        assert await page.locator('.job-percent').inner_text() == '100%'
        assert '노트 마무리 중' in await page.locator('.background-job').inner_text()
        assert await page.locator('#note-dialog').is_hidden()
        frozen = await page.locator('.job-timing').inner_text()
        await page.clock.fast_forward(10000)
        assert await page.locator('.job-timing').inner_text() == frozen
        await put(status='error', progress_phase='error', error='처리 중 오류', inference_elapsed_seconds=40,
                  inference_finished_at='2026-10-21T00:15:40+00:00')
        assert await page.locator('.job-percent').inner_text() == '오류'
        assert await page.locator('[data-job-action=restart]').is_visible()
        assert not errors, errors
        await browser.close()
    print(f'PASS {engine}: model-only estimated %, one-second clock, 99% cap, completion before postprocessing, cancellation/retry/error, desktop/mobile')


if __name__ == '__main__':
    asyncio.run(main())
