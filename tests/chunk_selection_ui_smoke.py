"""Per-note chunk selection with a real user-owned recording, without inference.

Usage: python tests/chunk_selection_ui_smoke.py URL RECORDING PNG [webkit|chromium]
Run against an isolated data directory. Only the final transcription request is
intercepted; upload, ffprobe and audio preparation use the actual backend.
"""
import asyncio
import hashlib
import math
import sys
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base, recording, screenshot = sys.argv[1:4]
    source = Path(recording).resolve(strict=True)
    with source.open('rb') as stream:
        original_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    original_mtime = source.stat().st_mtime_ns
    engine = sys.argv[4] if len(sys.argv) > 4 else 'webkit'
    devices = [dict(id='local', name='이 Mac', kind='local', backend='mlx', options={'chunk_minutes':30}),
               dict(id='remote', name='GPU', kind='ssh', host='qa-host', backend='cuda', options={'chunk_minutes':90})]
    submitted, errors = [], []
    note_id = 'f' * 32
    note = dict(id=note_id, title=source.stem, original_filename=source.name, status='queued',
                created_at=datetime.now(timezone.utc).isoformat(), segments=[], speaker_names={})

    async def notes(route):
        if route.request.method == 'POST':
            raw = b'Content-Type: ' + route.request.headers['content-type'].encode() + b'\r\n\r\n' + route.request.post_data_buffer
            parts = BytesParser(policy=default).parsebytes(raw)
            submitted.append({part.get_param('name', header='content-disposition'):part.get_payload(decode=True).decode() for part in parts.iter_parts()})
            return await route.fulfill(status=202, json=note)
        await route.fulfill(json=[note] if submitted else [])

    async def remote_estimate(route):
        from urllib.parse import urlsplit, parse_qs
        query=parse_qs(urlsplit(route.request.url).query)
        high=40 if int(query['count'][0])==1 else 10
        selected='gpu_uuid' in query
        await route.fulfill(json=dict(memory_low_gib=8,memory_high_gib=high,time_low_seconds=20,time_high_seconds=60,
                                     capacity_gib=24,free_gib=4,capacity_kind='VRAM',exceeds_capacity=selected and high>24,
                                     exceeds_free=selected and high>4,note='초기 추정 · 모델 준비·파일 전송 제외'))

    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':1000}, device_scale_factor=2)
        page.on('pageerror', lambda error:errors.append(str(error)))
        await page.route('**/api/devices', lambda r:r.fulfill(json=devices))
        await page.route('**/api/devices/*/environment', lambda r:r.fulfill(json={'ready':True}))
        await page.route('**/api/devices/remote/worker', lambda r:r.fulfill(json={'phase':'installed'}))
        await page.route('**/api/devices/remote/gpus', lambda r:r.fulfill(json={'gpus':[], 'checked_at':datetime.now(timezone.utc).isoformat(),'message':''}))
        await page.route('**/api/devices/remote/estimate?*',remote_estimate)
        await page.route('**/api/notes', notes)
        await page.route('**/api/notes/' + note_id, lambda r:r.fulfill(json=note))
        await page.route('**/api/text-library**', lambda r:r.fulfill(json=[]))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[]))
        await page.goto(base)
        await page.locator('[data-device-id="local"] .use-device').click()
        count_field = page.locator('#upload-chunk-count')
        assert await page.locator('#upload-chunk-minutes').count() == 0
        assert await count_field.is_disabled()
        assert await page.locator('#increase-upload-chunks').is_disabled()
        await page.locator('#audio-file').set_input_files(source)
        await page.wait_for_function('state.uploadPreparation?.status === "ready"', timeout=120000)
        duration = await page.evaluate('state.uploadPreparation.duration')
        assert duration > 0
        assert int(await count_field.input_value()) == math.ceil(duration / 1800)
        assert await page.locator('#upload-chunk-preview').inner_text()
        await page.screenshot(path=screenshot)
        # Boundary fixtures use the same prepared real recording, without
        # manufacturing audio files or running another transcription.
        for seconds, count in ((3660,3),(3540,2),(3600,2),(30,1),(1800,1),(1800.001,2),(7200,4),(7260,5)):
            await page.evaluate('(seconds)=>{state.uploadPreparation.duration=seconds; state.uploadChunkCount=null; renderFilePreparation();}', seconds)
            plan = await page.evaluate('uploadChunkPlan()')
            assert plan['count'] == count, plan
            assert plan['seconds'] <= 1800
            assert await count_field.input_value() == str(count)
        await page.evaluate('state.uploadPreparation.duration=3660; state.uploadChunkCount=null; renderFilePreparation();')
        assert await count_field.input_value() == '3'
        assert await page.locator('#upload-chunk-preview').inner_text() == '20분 20초'
        await page.click('#increase-upload-chunks')
        assert await count_field.input_value() == '4'
        assert await page.locator('#upload-chunk-preview').inner_text() == '15분 15초'
        await page.click('#decrease-upload-chunks')
        assert await count_field.input_value() == '3'
        await count_field.fill('2')
        assert await page.locator('#upload-chunk-preview').inner_text() == '30분 30초'
        await count_field.press('ArrowUp')
        assert await count_field.input_value() == '3'
        await count_field.press('ArrowDown')
        assert await count_field.input_value() == '2'
        for invalid in ('0','1001','1.5',''):
            await count_field.fill(invalid)
            assert not await count_field.evaluate('el=>el.checkValidity()'), invalid
        await count_field.fill('1')
        assert await page.locator('#upload-chunk-preview').inner_text() == '1시간 1분'
        assert await page.locator('#decrease-upload-chunks').is_disabled()
        await page.evaluate('state.uploadPreparation.duration=10801; renderFilePreparation();')
        assert not await count_field.evaluate('el=>el.checkValidity()')
        await count_field.fill('3')
        assert await count_field.evaluate('el=>el.checkValidity()')
        await page.evaluate('(seconds)=>{state.uploadPreparation.duration=seconds; renderFilePreparation();}', duration)
        await count_field.fill('2')
        await page.evaluate('renderFilePreparation(); syncExecutionGpu();')
        assert await count_field.input_value() == '2', 'Refreshing replaced a manual override'
        assert abs((await page.evaluate('uploadChunkPlan()'))['seconds'] - duration / 2) < .000001
        for width in (1100,390):
            await page.set_viewport_size({'width':width,'height':950})
            left = await count_field.bounding_box()
            right = await page.locator('#upload-chunk-preview').bounding_box()
            assert left['x'] >= 0 and right['x'] + right['width'] <= width
            assert right['x'] > left['x'] and abs(left['y'] - right['y']) < 1
        await page.set_viewport_size({'width':1440,'height':1000})
        upload_id = await page.evaluate('state.uploadPreparation.id')
        await page.click('.submit-upload')
        await page.locator('#upload-dialog').wait_for(state='hidden')
        assert submitted[0]['chunk_count'] == '2' and submitted[0]['upload_id'] == upload_id
        assert 'chunk_minutes' not in submitted[0]
        assert submitted[0]['device_id'] == 'local'
        await page.request.delete(base+'/api/uploads/'+upload_id)
        await page.click('#close-note')
        await page.locator('[data-device-id="local"] .use-device').click()
        assert await count_field.input_value() == '' and await count_field.is_disabled()
        await page.click('#close-upload')
        await page.locator('[data-device-id="remote"] .use-device').click()
        assert await count_field.is_disabled()
        await page.locator('#audio-file').set_input_files(source)
        await page.wait_for_function('state.uploadPreparation?.status === "ready"', timeout=120000)
        assert int(await count_field.input_value()) == math.ceil(duration / 5400), 'The server default was ignored'
        await page.evaluate('state.selectedGpu={uuid:"GPU-estimate-test", memory_total_mib:24*1024}; renderFilePreparation();')
        await page.locator('#upload-estimates.is-over-capacity').wait_for()
        assert '전체 VRAM 24.0' in await page.locator('#upload-memory-warning').inner_text()
        assert '초기 추정' in await page.locator('#upload-estimate-note').inner_text()
        await page.locator('#upload-dialog').screenshot(path=screenshot.replace('.png','-memory-warning.png'))
        await count_field.fill('2')
        await page.wait_for_function('!document.querySelector("#upload-estimates").classList.contains("is-over-capacity") && document.querySelector("#upload-estimate-memory").textContent.includes("10.0")')
        assert '현재 여유 VRAM' in await page.locator('#upload-memory-warning').inner_text()
        await page.click('#probe-ssh-host')
        assert await count_field.input_value() == '2'
        # GPU memory and device settings must not replace per-note input.
        await page.evaluate('state.selectedGpu={memory_total_mib:96*1024}; renderFilePreparation();')
        assert await count_field.input_value() == '2'
        assert devices[0]['options']['chunk_minutes'] == 30
        assert not errors, errors
        await page.click('#close-upload')
        await browser.close()
    with source.open('rb') as stream:
        assert hashlib.file_digest(stream,'sha256').hexdigest() == original_hash
    assert source.stat().st_mtime_ns == original_mtime
    print(f'PASS {engine}: real recording ({duration:.3f}s), count-only equal splits, 61/59-minute defaults, direct input/buttons/arrow keys, side-by-side duration, count-only request, GPU refresh, original unchanged')


if __name__ == '__main__':
    asyncio.run(main())
