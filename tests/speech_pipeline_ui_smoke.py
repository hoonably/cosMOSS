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
    remote={'id':'fixture','name':'GPU 서버','host':'fixture-host','kind':'ssh','backend':'cuda','model_key':'cuda-original','model_selected':True,'options':{'chunk_minutes':90,'speech_pipeline':'moss-sglang'}}
    gpus=[{'index':i,'uuid':f'GPU-{i}-fixture','name':'NVIDIA H200' if i==0 else 'NVIDIA RTX 6000 Ada Generation',
           'memory_total_mib':143771 if i==0 else 24576,'memory_free_mib':143000 if i==0 else 24000,
           'memory_used_mib':0,'utilization_percent':0,'processes':[],'process_info_known':True,'idle_candidate':True} for i in range(2)]
    errors=[];submitted=[];home_changes=[];downloads=[]
    prepared={'moss-sglang','moss-qwen-sequential'}
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
        home_changes.append(route.request.post_data_json)
        remote['options']['speech_pipeline']=route.request.post_data_json['pipeline']
        await route.fulfill(json={**remote,'setup':{'phase':'installed'}})
    async def setup(route):
        if route.request.method=='POST':
            key=route.request.post_data_json['pipeline'];downloads.append(key);prepared.add(key)
            return await route.fulfill(status=202,json={'phase':'installed'})
        query=parse_qs(urlsplit(route.request.url).query)
        await route.fulfill(json={'phase':'installed' if query['pipeline'][0] in prepared else 'idle'})
    async def switch_pipeline(page,key):
        await choose_option(page,'#speech-pipeline',key)
        assert await page.locator('#speech-pipeline').input_value()==key
        await page.evaluate('renderFilePreparation()')
        assert await page.locator('#speech-pipeline').input_value()==key
        assert await page.locator('[data-device-model="fixture"]').input_value()=='speech:moss-sglang'
        assert not home_changes
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
        await page.route('**/api/devices/fixture/speech-setup?*',setup)
        await page.route('**/api/devices/fixture/speech-setup',setup)
        await page.route('**/api/devices/fixture/speech-estimate?*',estimate)
        await page.route('**/api/setup',lambda r:r.fulfill(json={'ready':True,'phase':'ready'}))
        await page.route('**/api/notes',notes)
        await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
        await page.goto(base)
        await page.locator('[data-device-id="fixture"] .use-device').click()
        await page.locator('input[name="selected-gpu"]').first.wait_for()
        assert await page.locator('#speech-pipeline').input_value()=='moss-sglang'
        assert await page.locator('#speech-options .custom-select-trigger:visible').count()==1
        assert await page.locator('#speech-batch').is_hidden()
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
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 03:18"')
        assert '전체 1청크' in await page.locator('#speech-batch-plan').inner_text()
        await page.locator('#upload-dialog').screenshot(path=png.replace('.png','-moss.png'))
        await switch_pipeline(page,'qwen-transformers')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 01:24"')
        await page.locator('#prepare-upload-model').wait_for(state='visible')
        assert await page.locator('#upload-form button[type=submit]').is_disabled()
        assert not downloads
        await page.locator('#prepare-upload-model').click()
        await page.wait_for_function('!cosmossSpeech.blocked()')
        assert downloads==['qwen-transformers']
        await page.locator('input[name="selected-gpu"]').nth(1).check()
        await choose_option(page,'#speech-batch','8')
        await page.wait_for_function('!document.querySelector("#speech-memory-consent").classList.contains("hidden")')
        assert await page.locator('#allow-speech-memory-risk').get_attribute('required') is not None
        assert '70%' in await page.locator('#upload-memory-warning').inner_text()
        assert await page.locator('#upload-estimate-time').inner_text()=='추가 측정 필요'
        await choose_option(page,'#speech-batch','0')
        await page.wait_for_function('document.querySelector("#speech-memory-consent").classList.contains("hidden")')
        assert not await page.locator('#allow-speech-memory-risk').is_checked()
        for width in (1440,1100,390):
            await page.set_viewport_size({'width':width,'height':1000})
            await page.screenshot(path=png.replace('.png',f'-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.locator('#upload-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        await page.set_viewport_size({'width':1440,'height':1000})
        await switch_pipeline(page,'moss-qwen-sequential')
        assert await page.locator('.upload-chunk-row').is_hidden()
        assert await page.locator('#speech-batch').is_hidden()
        assert '비교용' in await page.locator('#speech-description').inner_text()
        await page.locator('input[name="selected-gpu"]').first.check()
        await page.wait_for_function('!cosmossSpeech.blocked()')
        await page.locator('#upload-dialog').screenshot(path=png.replace('.png','-combined.png'))
        await page.locator('#upload-form button[type="submit"]').click()
        await page.wait_for_function('!document.querySelector("#upload-dialog").open')
        assert len(submitted)==1,submitted
        values=submitted[0]
        assert values['speech_pipeline']=='moss-qwen-sequential'
        assert values['speech_batch']=='0'
        assert 'chunk_count' not in values and 'chunk_minutes' not in values
        assert values['upload_id']=='fixture-upload' and values['gpu_uuid']==gpus[0]['uuid']
        assert not home_changes
        await page.wait_for_function('document.querySelector("#note-dialog").open')
        await page.locator('#close-note').click()
        await page.locator('[data-device-id="fixture"] .use-device').click()
        assert await page.locator('#speech-pipeline').input_value()=='moss-sglang'
        assert not errors,errors
        await browser.close()
    print(engine,'PASS')


asyncio.run(main())
