"""Common setup, retry and remote-only model preparation in a real browser."""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    base = sys.argv[1]
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    engine = sys.argv[3] if len(sys.argv) > 3 else 'chromium'
    setup = {'phase':'idle', 'ready':False, 'busy':False, 'components':{
        key:{'ready':False, 'version':'', 'source':'app'} for key in ('ffmpeg','ffprobe')}}
    polls, fail_next = 0, False
    remote_prepared, requests, errors = [], [], []

    async def common(route):
        nonlocal polls, setup, fail_next
        if route.request.method == 'POST':
            polls = 1
            setup = {**setup, 'phase':'checking', 'ready':False, 'busy':True}
            setup.pop('error', None)
            return await route.fulfill(status=202, json=setup)
        if polls:
            polls += 1
            if fail_next:
                fail_next, polls = False, 0
                setup = {**setup, 'phase':'error', 'ready':False, 'busy':False, 'error':'네트워크 연결을 확인하고 다시 준비해 주세요.'}
            elif polls >= 4:
                polls = 0
                setup = {**setup, 'phase':'ready', 'ready':True, 'busy':False, 'components':{
                    key:{'ready':True,'version':'9.0.1','source':'app'} for key in ('ffmpeg','ffprobe')}}
            else:
                setup = {**setup, 'phase':'downloading', 'download':{'percent':50,'downloaded_bytes':5*1024**2,'total_bytes':10*1024**2}}
        await route.fulfill(json=setup)

    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine == 'chromium' else {}
        browser = await getattr(pw, engine).launch(headless=True, **options)
        page = await browser.new_page(viewport={'width':1440,'height':960}, device_scale_factor=1)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append((request.method, request.url)))
        await page.route('**/api/setup', common)
        await page.route('**/api/notes', lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**', lambda r:r.fulfill(json=[]))
        await page.route('**/api/summary/providers', lambda r:r.fulfill(json=[]))
        devices = [
            {'id':'local','kind':'local','name':'이 Mac','backend':'mlx','setup':{'phase':'idle'}},
            {'id':'remote','kind':'ssh','name':'원격 GPU','host':'lab','backend':'cuda','setup':{'phase':'installed'}}]
        await page.route('**/api/devices', lambda r:r.fulfill(json=devices))
        async def save_chunk_default(route):
            values=route.request.post_data_json
            device=devices[1]
            device['options']={**device.get('options',{}),'chunk_minutes':values['chunk_minutes']}
            await route.fulfill(json=device)
        await page.route('**/api/devices/remote/chunk-default',save_chunk_default)
        await page.route('**/api/devices/*/environment', lambda r:r.fulfill(json={'ready':False}))
        await page.route('**/api/devices/remote/worker', lambda r:r.fulfill(json={'phase':'installed'}))
        metadata = await (await page.request.get(base + '/api/model-catalog')).json()
        remote_metadata = {**metadata, 'phase':'installed', 'model_key':'cuda-original', 'source':'cache',
                           'options':{**metadata['recommendation']['options'], 'chunk_minutes':60},
                           'recommendation':{'model_key':'cuda-original','options':{**metadata['recommendation']['options'],'chunk_minutes':60}},
                           'cache_dir':'/home/test/.cache/huggingface/hub'}
        await page.route('**/api/devices/remote/model', lambda r:r.fulfill(json=remote_metadata))
        async def prepare_remote(route):
            remote_prepared.append(route.request.post_data_json)
            await route.fulfill(status=202, json={'phase':'installed'})
        await page.route('**/api/devices/remote/model/prepare', prepare_remote)
        await page.goto(base)
        await page.click('#open-app-settings')
        await page.wait_for_function("document.querySelectorAll('#app-settings-components .environment-component-status').length===2")
        assert await page.locator('#app-settings-components').get_by_text('준비 필요',exact=True).count() == 2
        assert not any(method == 'POST' and url.endswith('/api/setup') for method,url in requests)
        await page.click('#prepare-app-settings')
        await page.locator('#app-settings-progress progress').wait_for(state='visible')
        assert await page.locator('#prepare-app-settings').is_disabled()
        await page.click('#close-app-settings')
        await page.wait_for_function('state.appSetup?.ready && !state.appSetup.busy')
        await page.click('#open-app-settings')
        await page.wait_for_function("document.querySelector('#app-settings-status').textContent.includes('준비됐습니다')")
        await page.screenshot(path=str(output))
        fail_next = True
        await page.click('#prepare-app-settings')
        await page.wait_for_function("document.querySelector('#app-settings-status').textContent.includes('네트워크')")
        assert await page.locator('#prepare-app-settings').is_enabled()
        await page.click('#prepare-app-settings')
        await page.wait_for_function('state.appSetup?.ready && !state.appSetup.busy')
        await page.click('#close-app-settings')
        await page.locator('[data-device-id="remote"] [data-device-action="install"]').click()
        await page.locator('#prepare-local-model:not([disabled])').wait_for()
        assert await page.locator('[data-knob="chunk_minutes"]').input_value() == '60'
        await page.click('#model-knobs > summary')
        await page.fill('[data-knob="chunk_minutes"]','90')
        await page.click('#save-chunk-default')
        await page.wait_for_function('document.querySelector("#chunk-default-status").textContent.includes("저장됨")')
        assert devices[1]['options']['chunk_minutes'] == 90
        assert remote_prepared == [], 'Saving the split default started model preparation'
        await page.click('#prepare-local-model')
        await page.wait_for_function('!state.localModelSubmitting')
        assert len(remote_prepared) == 1
        assert not any(method == 'POST' and ('/api/local-model' in url or '/devices/local/environment' in url) for method,url in requests)
        await page.click('#close-local-model')
        await page.wait_for_function('state.appSetup?.ready && !state.appSetup.busy')
        await page.set_viewport_size({'width':390,'height':844})
        await page.click('#open-app-settings')
        bounds = await page.locator('#app-settings-dialog').bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 390, bounds
        await page.screenshot(path=str(output.with_name(output.stem + '-mobile.png')))
        assert not errors, errors
        await browser.close()
    print(f'PASS {engine}: common setup, progress, background polling, error/retry, remote-only model setup, mobile dialog')


if __name__ == '__main__':
    asyncio.run(main())
