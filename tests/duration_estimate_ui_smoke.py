"""Mac calibrated estimates, model/count changes and unsupported settings."""
import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.processing_estimates import estimate_processing
from src.local_models import RUNTIME_VERSION


async def main():
    base,engine,png=sys.argv[1:4]
    hardware={'name':'Apple M4','memory_gib':16}
    device={'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit',
            'model_selected':True,'setup':{'phase':'ready'},'options':{'chunk_minutes':30}}
    calls=[];errors=[]
    async def estimate(route):
        query=parse_qs(urlsplit(route.request.url).query);calls.append(query)
        value=estimate_processing(device,float(query['duration'][0]),int(query['count'][0]),local_hardware_info=hardware,local_runtime=RUNTIME_VERSION)
        await route.fulfill(json=value)
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[device]))
        await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'ready','model_key':device['model_key']}))
        await page.route('**/api/setup',lambda r:r.fulfill(json={'ready':True,'phase':'ready'}))
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[]))
        await page.route('**/api/devices/local/estimate?*',estimate)
        await page.goto(base)
        await page.locator('[data-device-id="local"] .use-device').click()
        await page.evaluate('''() => {
          state.uploadPreparation={duration:3600,status:'ready',controller:new AbortController()};
          document.querySelector('#audio-file').required=false;
          document.querySelector('#drop-title').textContent='1시간 녹음.m4a';
          renderFilePreparation();
        }''')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 17:17"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='약 4.9 GiB'
        assert '검증 보완' in await page.locator('#upload-estimate-note').inner_text()
        assert await page.locator('#upload-estimate-note').evaluate('e=>e.classList.contains("estimate-caution")')
        await page.locator('#upload-chunk-count').fill('4')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 10:22"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='약 3.3 GiB'
        await page.screenshot(path=png,animations='disabled')
        async def configure(model, count, **options):
            device.update(model_key=model,options={'chunk_minutes':30,**options})
            await page.evaluate('''device=>{
              Object.assign(state.devices.find(d=>d.id==='local'),device);
              state.uploadChunkCount='''+str(count)+''';
              clearUploadEstimate();renderFilePreparation();
            }''',device)
        await configure('mlx-4bit',1)
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="추가 측정 필요"')
        assert '긴 청크' in await page.locator('#upload-estimate-note').inner_text()
        await configure('mlx-16bit',2)
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 20:29"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='약 5.5 GiB'
        assert await page.locator('#upload-estimate-note').inner_text()=='실측 기반'
        await configure('mlx-8bit',2,kv_bits=0)
        await page.wait_for_function('document.querySelector("#upload-estimate-memory").textContent==="추가 측정 필요"')
        assert '설정은 실측 준비' in await page.locator('#upload-estimate-note').inner_text()
        for width in (1440,1100,390):
            await page.set_viewport_size({'width':width,'height':1000})
            assert await page.locator('#upload-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        assert len(calls)>=5 and not errors,(calls,errors)
        await browser.close()
    print(engine,'PASS: measured Mac values, nonlinear chunking, uncertainty, model changes, unsupported options and responsive layout')


asyncio.run(main())
