"""Fixture UI: GPU first, calibrated point estimates, desktop/mobile ordering.

Run against an isolated backend: uv run --no-project --with playwright python
tests/processing_estimate_ui_smoke.py URL PNG. No audio upload or GPU inference.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.processing_estimates import estimate_processing
from ui_helpers import choose_option


async def main():
    base, screenshot=sys.argv[1:3]
    remote=dict(id='remote', name='juyu-h200', kind='ssh', host='fixture-host', backend='cuda', options={'chunk_minutes':90})
    gpus=[dict(index=index,uuid=f'GPU-{index}-fixture',name='NVIDIA H200',memory_total_mib=143771,
               memory_used_mib=0,memory_free_mib=143771,utilization_percent=0,processes=[],
               process_info_known=True,idle_candidate=True) for index in range(4)]
    calls=[]; errors=[]
    async def estimate(route):
        query=parse_qs(urlsplit(route.request.url).query); calls.append(query)
        gpu=next(item for item in gpus if item['uuid']==query['gpu_uuid'][0])
        value=estimate_processing(remote,float(query['duration'][0]),int(query['count'][0]),gpu)
        await route.fulfill(json=value)
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[dict(id='local',name='이 Mac',kind='local',backend='mlx'),remote]))
        await page.route('**/api/devices/*/environment',lambda r:r.fulfill(json={'ready':True}))
        await page.route('**/api/devices/remote/worker',lambda r:r.fulfill(json={'phase':'installed'}))
        await page.route('**/api/devices/remote/gpus',lambda r:r.fulfill(json={'gpus':gpus,'checked_at':datetime.now(timezone.utc).isoformat(),'message':'현재 GPU 상태','chunk_default_minutes':90}))
        await page.route('**/api/devices/remote/estimate?*',estimate)
        await page.route('**/api/setup',lambda r:r.fulfill(json={'phase':'ready','ready':True}))
        await page.goto(base)
        await page.locator('[data-device-id="remote"] .use-device').click()
        await page.locator('input[name="selected-gpu"]').first.wait_for()
        assert await page.locator('#probe-ssh-host').count()==0
        assert '3초마다 갱신' in await page.locator('#execution-gpu-meta').inner_text()
        assert await page.locator('#upload-estimate-memory').inner_text()=='-'
        assert await page.locator('#upload-estimate-time').inner_text()=='-'
        await page.evaluate('''() => {
            state.uploadPreparation={duration:3937.1306875,status:'ready',controller:new AbortController()};
            document.querySelector('#audio-file').required=false;
            document.querySelector('#drop-title').textContent='녹음 파일.m4a';
            document.querySelector('#drop-copy').textContent='31.8 MB';
            renderFilePreparation();
        }''')
        assert await page.locator('#upload-chunk-count').is_disabled()
        assert await page.locator('#upload-estimates').is_visible()
        await page.locator('#upload-dialog').screenshot(path=screenshot.replace('.png','-choose-gpu.png'))
        assert not calls
        await page.locator('input[name="selected-gpu"]').first.check()
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 08:49"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='약 15.5 GiB'
        assert await page.locator('#upload-estimate-note').is_hidden()
        assert await page.locator('#upload-chunk-count').is_enabled()
        await page.locator('#upload-dialog').screenshot(path=screenshot)
        for width in (1440,1100,390):
            await page.set_viewport_size({'width':width,'height':1000})
            gpu_panel=await page.locator('#execution-gpu').bounding_box()
            details=await page.locator('.upload-details').bounding_box()
            submit=await page.locator('.submit-upload').bounding_box()
            if width>850:
                assert gpu_panel['x']+gpu_panel['width']<=details['x']
                assert abs(gpu_panel['y']-details['y'])<1
                assert abs(submit['x']-details['x'])<1 and abs(submit['width']-details['width'])<1
                assert 0<=submit['y']-details['y']-details['height']<=20
            else:
                assert gpu_panel['y']+gpu_panel['height']<=details['y']
                assert details['y']+details['height']<=submit['y']
            assert gpu_panel['x']>=0 and details['x']+details['width']<=width
            assert await page.locator('#upload-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
        await page.locator('#upload-chunk-count').fill('2')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 07:08"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='약 9.1 GiB'
        await page.set_viewport_size({'width':1440,'height':1000})
        async with page.expect_response(lambda response:'/estimate?' in response.url and gpus[1]['uuid'] in response.url):
            await page.locator('input[name="selected-gpu"]').nth(1).check()
        await page.wait_for_function('state.selectedGpu?.index===1')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 07:08"')
        assert calls[-1]['gpu_uuid']==[gpus[1]['uuid']]
        gpus[:]=[gpu for gpu in gpus if gpu['index']!=1]
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="-"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='-'
        assert await page.locator('#upload-memory-warning').is_hidden()
        assert not errors,errors
        await page.click('#close-upload')
        await browser.close()
    print('PASS: GPU selection first, H200 seed profile points, count changes, GPU identity, 1440/1100/390px layout')


if __name__=='__main__':
    asyncio.run(main())
