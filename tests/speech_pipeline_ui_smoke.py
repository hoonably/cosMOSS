"""Preset selection, VRAM warnings, submit payload and responsive Mac UI."""
import asyncio
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.speech_pipeline import estimate_pipeline
from ui_helpers import choose_option


async def main():
    base,png,engine=sys.argv[1:4]
    remote={'id':'fixture','name':'GPU 서버','host':'fixture-host','kind':'ssh','backend':'cuda','model_key':'cuda-original','model_selected':True,'options':{'chunk_minutes':90,'speech_pipeline':'moss-batch'}}
    gpus=[{'index':i,'uuid':f'GPU-{i}-fixture','name':'NVIDIA H200' if i==0 else 'NVIDIA RTX 6000 Ada Generation',
           'memory_total_mib':143771 if i==0 else 24576,'memory_free_mib':143000 if i==0 else 24000,
           'memory_used_mib':0,'utilization_percent':0,'processes':[],'process_info_known':True,'idle_candidate':True} for i in range(2)]
    errors=[];submitted=[]
    note={'id':'f'*32,'title':'녹음','original_filename':'녹음.m4a','status':'queued',
          'created_at':datetime.now(timezone.utc).isoformat(),'segments':[],'speaker_names':{}}
    async def notes(route):
        if route.request.method=='POST':
            raw=b'Content-Type: '+route.request.headers['content-type'].encode()+b'\r\n\r\n'+route.request.post_data_buffer
            parts=BytesParser(policy=default).parsebytes(raw)
            submitted.append({p.get_param('name',header='content-disposition'):p.get_payload(decode=True).decode() for p in parts.iter_parts()})
            return await route.fulfill(status=202,json=note)
        await route.fulfill(json=[note] if submitted else [])
    async def estimate(route):
        query={k:v[0] for k,v in parse_qs(urlsplit(route.request.url).query).items()}
        gpu=next(g for g in gpus if g['uuid']==query['gpu_uuid'])
        await route.fulfill(json=estimate_pipeline(query['pipeline'],float(query['duration']),gpu,int(query['batch'])))
    async def selection(route):
        remote['options']['speech_pipeline']=route.request.post_data_json['pipeline']
        await route.fulfill(json={**remote,'setup':{'phase':'installed'}})
    async def switch_pipeline(page,key):
        if await page.locator('#upload-dialog').evaluate('(e)=>e.open'):
            await page.locator('#close-upload').click()
        await choose_option(page,'[data-device-model="fixture"]','speech:'+key)
        await page.wait_for_function('(key)=>state.devices.find(d=>d.id==="fixture").options.speech_pipeline===key',arg=key)
        await page.locator('[data-device-id="fixture"] .use-device').click()
        await page.evaluate('''() => {
          state.uploadPreparation={id:'fixture-upload',duration:3937.1306875,status:'ready',controller:new AbortController(),file:{name:'녹음.m4a',size:12345}};
          document.querySelector('#audio-file').required=false;
          renderFilePreparation();
        }''')
        await page.locator('input[name="selected-gpu"]').first.check()
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.set_default_timeout(12000)
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx'},remote]))
        await page.route('**/api/devices/fixture/model/selection',selection)
        await page.route('**/api/devices/fixture/environment',lambda r:r.fulfill(json={'ready':True}))
        await page.route('**/api/devices/fixture/worker',lambda r:r.fulfill(json={'phase':'installed'}))
        await page.route('**/api/devices/fixture/gpus',lambda r:r.fulfill(json={'gpus':gpus,'checked_at':datetime.now(timezone.utc).isoformat(),'chunk_default_minutes':90}))
        await page.route('**/api/devices/fixture/speech-setup?*',lambda r:r.fulfill(json={'phase':'installed'}))
        await page.route('**/api/devices/fixture/speech-estimate?*',estimate)
        await page.route('**/api/setup',lambda r:r.fulfill(json={'ready':True,'phase':'ready'}))
        await page.route('**/api/notes',notes)
        await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
        await page.goto(base)
        await page.locator('[data-device-id="fixture"] .use-device').click()
        await page.locator('input[name="selected-gpu"]').first.wait_for()
        assert await page.locator('#speech-pipeline').input_value()=='moss-batch'
        assert await page.locator('#speech-options .custom-select-trigger').count()==1
        assert await page.locator('#upload-chunk-count').is_disabled()
        assert await page.locator('#speech-batch').is_disabled()
        assert await page.locator('#upload-estimate-memory').inner_text()=='-'
        await page.evaluate('''() => {
          state.uploadPreparation={id:'fixture-upload',duration:3937.1306875,status:'ready',controller:new AbortController(),file:{name:'녹음.m4a',size:12345}};
          document.querySelector('#audio-file').required=false;
          document.querySelector('#drop-title').textContent='녹음.m4a';
          renderFilePreparation();
        }''')
        await page.locator('input[name="selected-gpu"]').first.check()
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 01:27"')
        assert '23개 청크' in await page.locator('#speech-batch-plan').inner_text()
        await page.locator('#upload-dialog').screenshot(path=png.replace('.png','-moss.png'))
        await switch_pipeline(page,'qwen-batch')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 00:57"')
        await page.locator('input[name="selected-gpu"]').nth(1).check()
        await choose_option(page,'#speech-batch','8')
        await page.wait_for_function('!document.querySelector("#speech-memory-consent").classList.contains("hidden")')
        assert await page.locator('#allow-speech-memory-risk').get_attribute('required') is not None
        assert '70%' in await page.locator('#upload-memory-warning').inner_text()
        assert await page.locator('#upload-estimate-time').inner_text()=='업데이트 예정입니다.'
        await choose_option(page,'#speech-batch','0')
        await page.wait_for_function('document.querySelector("#speech-memory-consent").classList.contains("hidden")')
        assert not await page.locator('#allow-speech-memory-risk').is_checked()
        for width in (1440,1100,390):
            await page.set_viewport_size({'width':width,'height':1000})
            await page.screenshot(path=png.replace('.png',f'-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.locator('#upload-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        await page.set_viewport_size({'width':1440,'height':1000})
        await switch_pipeline(page,'moss-original')
        assert await page.locator('.upload-chunk-row').is_visible()
        assert await page.locator('#speech-batch-controls').is_hidden()
        await switch_pipeline(page,'moss-batch')
        await page.locator('#upload-form button[type="submit"]').click()
        await page.wait_for_function('!document.querySelector("#upload-dialog").open')
        assert len(submitted)==1,submitted
        values=submitted[0]
        assert values['speech_pipeline']=='moss-batch'
        assert values['speech_batch']=='0'
        assert 'chunk_count' not in values and 'chunk_minutes' not in values
        assert values['upload_id']=='fixture-upload' and values['gpu_uuid']==gpus[0]['uuid']
        assert not errors,errors
        await browser.close()
    print(engine,'PASS')


asyncio.run(main())
