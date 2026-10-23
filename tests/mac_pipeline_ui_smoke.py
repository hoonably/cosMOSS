"""Mac per-note model selection, bundle readiness, payload and combined results."""
import asyncio
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright
from ui_helpers import choose_option


async def main():
    base, png, engine = sys.argv[1:4]
    local = {'id':'local','name':'이 Mac','kind':'local','backend':'mlx','model_key':'mlx-8bit',
             'model_selected':True,'options':{'chunk_minutes':30},'setup':{'phase':'installed','model_key':'mlx-8bit'}}
    errors, downloads, changes, estimates, submitted = [], [], [], [], []
    installed = {'mlx-8bit'}
    note = {'id':'e'*32,'title':'Mac 녹음','original_filename':'Mac 녹음.m4a','status':'queued',
            'created_at':datetime.now(timezone.utc).isoformat(),'segments':[],'speaker_names':{}}
    async def readiness(route):
        if route.request.method == 'POST':
            key = route.request.post_data_json['model_key']
            downloads.append(key); installed.add(key)
        else:
            key = parse_qs(urlsplit(route.request.url).query)['model_key'][0]
        ready = key in installed
        if not ready:
            return await route.fulfill(json={'phase':'auth_required','ready':False,
                'error':'모델 이용 권한을 확인해 주세요.',
                'auth_url':'https://huggingface.co/example/model' if key=='mlx-16bit' else 'javascript:alert(1)'})
        await route.fulfill(json={'phase':'installed' if ready else 'idle','ready':ready,'asr_ready':ready,'diarization_ready':True})
    async def estimate(route):
        query = {k:v[0] for k,v in parse_qs(urlsplit(route.request.url).query).items()}
        estimates.append(query)
        await route.fulfill(json={'time_seconds':{'mlx-8bit':700,'mlx-16bit':900,'mlx-4bit':1200}[query['model_key']],
            'memory_expected_gib':None,'memory_note':'MOSS MLX + Nemotron 통합 Peak는 추가 측정이 필요합니다.',
            'calibration_model':'fixture','calibration_status':'provisional','calibration_note':'실측 참고 · Nemotron 1회 측정으로 추정'})
    async def selection(route):
        changes.append(route.request.post_data_json)
        await route.fulfill(json=local)
    async def notes(route):
        if route.request.method == 'POST':
            raw=b'Content-Type: '+route.request.headers['content-type'].encode()+b'\r\n\r\n'+route.request.post_data_buffer
            submitted.append({p.get_param('name',header='content-disposition'):p.get_payload(decode=True).decode()
                              for p in BytesParser(policy=default).parsebytes(raw).iter_parts()})
            return await route.fulfill(status=202,json=note)
        await route.fulfill(json=[note] if submitted else [])
    async with async_playwright() as pw:
        options = {'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.set_default_timeout(12000)
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[local]))
        await page.route('**/api/local-model',lambda r:r.fulfill(json=local['setup']))
        await page.route('**/api/setup',lambda r:r.fulfill(json={'ready':True,'phase':'ready'}))
        await page.route('**/api/devices/local/model-readiness**',readiness)
        await page.route('**/api/devices/local/estimate?*',estimate)
        await page.route('**/api/devices/local/model/selection',selection)
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes',notes)
        await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
        await page.route('**/api/uploads/*',lambda r:r.fulfill(status=204))
        await page.goto(base)
        await page.locator('[data-device-id="local"] .use-device').click()
        await page.wait_for_function('!cosmossSpeech.blocked()')
        assert await page.locator('#upload-local-model').input_value()=='mlx-8bit'
        assert await page.locator('#execution-gpu').is_hidden()
        assert await page.locator('#speech-batch-controls').is_hidden()
        assert 'Nemotron' in await page.locator('[data-device-id="local"] .device-chunk-size').inner_text()
        assert await page.locator('#upload-compute #speech-options').count()==1
        assert not downloads and not changes
        await page.evaluate('''() => {
          state.uploadPreparation={id:'f'.repeat(32),duration:3937,status:'ready',controller:new AbortController(),file:{name:'Mac 녹음.m4a',size:12345}};
          document.querySelector('#audio-file').required=false;renderFilePreparation();
        }''')
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 11:40"')
        assert await page.locator('#upload-estimate-memory').inner_text()=='추가 측정 필요'
        assert await page.locator('#upload-chunk-count').is_enabled()
        assert await page.locator('#upload-form button[type=submit]').is_enabled()
        await page.locator('#upload-chunk-count').fill('4')
        await choose_option(page,'#upload-local-model','mlx-16bit')
        await page.locator('#prepare-upload-model').wait_for(state='visible')
        assert await page.locator('#upload-form button[type=submit]').is_disabled()
        assert await page.locator('#upload-model-status').inner_text()=='모델 이용 권한을 확인해 주세요.'
        assert await page.locator('#prepare-upload-model').inner_text()=='확인 후 다시 시도'
        assert await page.locator('#upload-model-auth-link').get_attribute('href')=='https://huggingface.co/example/model'
        assert await page.locator('#upload-model-auth-link').get_attribute('rel')=='noopener noreferrer'
        assert len(page.context.pages)==1, 'Auth must never navigate without a click'
        assert await page.locator('#upload-chunk-count').input_value()=='4'
        assert await page.locator('[data-device-model="local"]').input_value()=='mlx-8bit'
        assert not downloads and not changes
        await page.locator('#prepare-upload-model').click()
        await page.wait_for_function('!cosmossSpeech.blocked()')
        assert downloads==['mlx-16bit']
        await page.wait_for_function('document.querySelector("#upload-estimate-time").textContent==="약 15:00"')
        await page.locator('#upload-dialog').screenshot(path=png)
        # Low precision remains visibly risky in the same selector, with no install on selection.
        await choose_option(page,'#upload-local-model','mlx-4bit')
        assert await page.locator('#local-model-caution').is_visible()
        await page.wait_for_function('document.querySelector("#upload-model-status").textContent==="모델 이용 권한을 확인해 주세요."')
        assert await page.locator('#upload-model-auth-link').is_hidden()
        assert await page.locator('#upload-model-auth-link').get_attribute('href') is None
        assert await page.locator('#upload-form button[type=submit]').is_disabled()
        assert await page.locator('#upload-local-model').locator('..').locator('.custom-select-badge.is-danger').inner_text()=='위험'
        await choose_option(page,'#upload-local-model','mlx-16bit')
        await page.wait_for_function('!cosmossSpeech.blocked()')
        for width in (1100,390):
            await page.set_viewport_size({'width':width,'height':950})
            assert await page.locator('#upload-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        await page.set_viewport_size({'width':1440,'height':1000})
        await page.locator('#upload-form button[type=submit]').click()
        await page.locator('#upload-dialog').wait_for(state='hidden')
        assert len(submitted)==1
        payload=submitted[0]
        assert payload['local_model_key']=='mlx-16bit' and payload['chunk_count']=='4',payload
        assert not {'speech_pipeline','speech_batch','gpu_uuid'}.intersection(payload),payload
        assert not changes
        assert estimates[-1]['model_key']=='mlx-16bit' and estimates[-1]['count']=='4'
        await page.locator('[data-device-id="local"] .use-device').click()
        assert await page.locator('#upload-local-model').input_value()=='mlx-8bit', 'Per-note selection changed the home default'
        await page.locator('#close-upload').click()
        # The same background card reports the actual diarization stage without a modal.
        note.update(status='processing',progress_phase='transcribing',inference_started_at='2026-10-24T00:00:00Z',
                    inference_elapsed_seconds=30,estimated_inference_seconds=100,model_progress={'pipeline_phase':'diarizing'})
        await page.evaluate('loadNotes()')
        assert '화자 구분 중' in await page.locator('.background-job').inner_text()
        assert await page.locator('#note-dialog').is_hidden()
        # Combined result avoids labelling host RSS as GPU/unified-memory Peak.
        note.update(status='done',duration=3937,chunk_count=4,progress_phase='done',
                    segments=[{'start':0,'end':3,'text':'목소리를 확인합니다.','speaker':'S01'}],
                    pipeline_result={'pipeline':'moss-mlx','model_key':'mlx-16bit','diarizer':'nemotron-3',
                                     'inference_seconds':900,'chunk_count':4,'peak_host_rss_gib':6.5,'peak_process_gib':None})
        await page.evaluate('loadNotes()')
        await page.evaluate('(id)=>openNote(id)',note['id'])
        result=await page.locator('#speech-result').inner_text()
        assert 'MLX 16bit + Nemotron' in result and '4청크 · 순차 실행' in result,result
        assert 'Peak' not in result and 'undefined' not in result,result
        assert not errors,errors
        await browser.close()
    print(engine,'PASS: Mac bundle readiness, per-note models, unchanged home default, chunk/estimate payload, background diarization and combined results')

asyncio.run(main())
