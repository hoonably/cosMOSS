"""Registration and model selection are read-only until explicit download."""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright
from ui_helpers import choose_option


async def main():
    base,png,engine=sys.argv[1:4]
    devices=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_selected':False,'setup':{'phase':'unselected'}}]
    installs=[];selections=[];setup_posts=[];errors=[]
    prepared=False
    async def device_list(route):
        if route.request.method=='POST':
            body=route.request.post_data_json
            devices.append({'id':'new-server','name':body['name'],'host':body['host'],'kind':'ssh','backend':'cuda',
                            'model_selected':False,'model_prepared':False,'setup':{'phase':'unselected'}})
            return await route.fulfill(status=201,json=devices[-1])
        await route.fulfill(json=devices)
    async def select(route):
        body=route.request.post_data_json;selections.append(body)
        device=next(d for d in devices if d['id']==route.request.url.split('/api/devices/')[1].split('/')[0])
        device.update(model_selected=True,model_key=body.get('model_key','cuda-original'),options={'speech_pipeline':body['pipeline']} if 'pipeline' in body else {})
        device['setup']={'phase':'unknown' if device['backend']=='cuda' else 'idle'}
        await route.fulfill(json=device)
    async def install(route):
        nonlocal prepared
        installs.append(route.request.post_data_json);prepared=True
        await route.fulfill(status=202,json={'phase':'downloading'})
    async def setup(route):
        if route.request.method=='POST':setup_posts.append(route.request.url)
        await route.fulfill(json={'ready':True,'phase':'ready'})
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.set_default_timeout(15000)
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.route('**/api/devices',device_list)
        await page.route('**/api/devices/*/model/selection',select)
        await page.route('**/api/devices/*/environment',lambda r:r.fulfill(json={'ready':False}))
        await page.route('**/api/devices/new-server/worker',lambda r:r.fulfill(json={'phase':'installed' if prepared else 'unknown'}))
        await page.route('**/api/devices/new-server/status',lambda r:r.fulfill(json={'ok':prepared,'setup':{'phase':'installed' if prepared else 'idle'}}))
        await page.route('**/api/devices/new-server/speech-setup',install)
        await page.route('**/api/devices/new-server/speech-setup?*',lambda r:r.fulfill(json={'phase':'installed'}))
        await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'idle'}))
        await page.route('**/api/ssh/hosts',lambda r:r.fulfill(json={'hosts':['test-h200']}))
        await page.route('**/api/setup',setup)
        await page.route('**/api/health',lambda r:r.fulfill(json={'moss':{'ok':False},'max_upload_bytes':5*1024**3,'default_chunk_seconds':1800}))
        await page.route('**/api/devices/new-server/gpus',lambda r:r.fulfill(json={'gpus':[{
            'index':0,'uuid':'GPU-11111111-2222-3333-4444-555555555555','name':'NVIDIA H200',
            'memory_total_mib':143771,'memory_free_mib':143000,'memory_used_mib':0,'utilization_percent':0,
            'processes':[],'process_info_known':True,'idle_candidate':True}], 'checked_at':datetime.now(timezone.utc).isoformat()}))
        await page.goto(base)
        await page.wait_for_function('state.catalog?.models?.length > 0')
        local=page.locator('[data-device-id="local"]')
        assert await local.locator('[data-device-model]').input_value()==''
        assert await local.locator('[data-device-action="download"]').is_disabled()
        await choose_option(page,'[data-device-model="local"]','mlx-4bit')
        await page.wait_for_function('state.devices[0].model_selected===true')
        assert not installs and not setup_posts
        assert not await page.locator('#local-model-dialog').evaluate('(e)=>e.open')
        await page.locator('#manage-devices').click()
        await page.locator('[data-ssh-host="test-h200"]').click()
        await page.locator('#add-device').click()
        server=page.locator('[data-device-id="new-server"]')
        await server.wait_for()
        assert await server.locator('[data-device-model]').input_value()==''
        assert await server.locator('.use-device').is_disabled()
        assert not installs and not setup_posts
        await choose_option(page,'[data-device-model="new-server"]','speech:qwen-transformers')
        await server.locator('[data-device-action="download"]:not([disabled])').wait_for()
        assert selections[-1]=={'pipeline':'qwen-transformers'}
        assert not installs and not setup_posts
        await page.screenshot(path=png.replace('.png','-selected.png'),full_page=True)
        await server.locator('[data-device-action="download"]').click()
        await server.locator('.device-card-state.ready').wait_for()
        assert installs==[{'pipeline':'qwen-transformers'}]
        assert await server.locator('.use-device').is_enabled()
        await server.locator('.use-device').click()
        assert await page.locator('#speech-pipeline').is_hidden()
        assert await page.locator('#speech-pipeline').input_value()=='qwen-transformers'
        assert await page.locator('#speech-pipeline').is_enabled()
        assert await page.locator('#prepare-speech').count()==0
        for width in (1440,1100,390):
            await page.set_viewport_size({'width':width,'height':1000})
            await page.locator('#upload-dialog').screenshot(path=png.replace('.png',f'-{width}.png'))
            assert await page.locator('#upload-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        assert not errors,errors
        await browser.close()
    print(engine,'PASS: connect → choose on left → explicit download → use selected model; no automatic setup')


asyncio.run(main())
