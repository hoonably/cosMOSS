"""Multi-file/Voice Memos selection, partial failures and retry without duplicates."""
import asyncio
import sys
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from playwright.async_api import async_playwright


async def main():
    base, engine, png = sys.argv[1:4]
    uploads, notes, deleted, submissions, errors = {}, [], [], [], []
    fail_partial = True
    def decode(request):
        raw = b'Content-Type: '+request.headers['content-type'].encode()+b'\r\n\r\n'+request.post_data_buffer
        return {p.get_param('name',header='content-disposition'):(p.get_filename() or p.get_payload(decode=True).decode()) for p in BytesParser(policy=default).parsebytes(raw).iter_parts()}
    def prepared(name):
        identity = f'{len(uploads)+1:032x}'
        uploads[identity] = {'id':identity,'status':'ready','duration':3900 if '긴' in name else 600,'name':name}
        return uploads[identity]
    def created(identity):
        upload = uploads[identity]
        note = {'id':identity,'title':upload['name'],'status':'queued','progress_phase':'queued','created_at':'2026-10-24T00:00:00Z','segments':[],'speaker_names':{}}
        assert all(n['id'] != identity for n in notes), 'Duplicate note created'
        notes.append(note)
        return note
    async def upload_route(route):
        if route.request.method == 'POST':
            return await route.fulfill(json=prepared(decode(route.request)['file']))
        identity = route.request.url.rsplit('/',1)[-1]
        if route.request.method == 'DELETE':
            deleted.append(identity);return await route.fulfill(status=204)
        await route.fulfill(json=uploads[identity])
    async def create(route):
        nonlocal fail_partial
        if route.request.method == 'GET': return await route.fulfill(json=notes)
        if route.request.url.endswith('/batch'):
            body=route.request.post_data_json;submissions.append(body)
            accepted, failures = [], []
            for index,item in enumerate(body['items']):
                if fail_partial and index == 1:
                    failures.append({'upload_id':item['upload_id'],'error':'테스트: 준비된 파일을 다시 확인해 주세요.'})
                else: accepted.append({'upload_id':item['upload_id'],'note':created(item['upload_id'])})
            fail_partial=False
            return await route.fulfill(json={'notes':accepted,'errors':failures})
        body=decode(route.request);submissions.append(body)
        await route.fulfill(json=created(body['upload_id']))
    memos=[{'id':f'memo{i}','title':title,'available':True,'version':'v1','size':1000,'duration':duration,'created_at':'2026-10-24T00:00:00Z'} for i,title,duration in [(1,'긴 음성 메모',3900),(2,'짧은 음성 메모',600),(3,'제외할 음성 메모',600)]]
    imports=[]
    async def import_memo(route):
        identity=route.request.post_data_json['id'];imports.append(identity)
        await route.fulfill(json=prepared(next(m['title'] for m in memos if m['id']==identity)))
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        page.set_default_timeout(12000)
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[{'id':'local','name':'이 Mac','kind':'local','backend':'mlx','options':{'chunk_minutes':30}}]))
        await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'installed'}))
        await page.route('**/api/devices/local/model-readiness?*',lambda r:r.fulfill(json={'ready':True,'phase':'installed','asr_ready':True,'diarization_ready':True}))
        await page.route('**/api/setup',lambda r:r.fulfill(json={'ready':True,'phase':'ready'}))
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=[]))
        await page.route('**/api/notes',create)
        await page.route('**/api/notes/batch',create)
        await page.route('**/api/uploads',upload_route)
        await page.route('**/api/uploads/*',upload_route)
        await page.route('**/api/voice-memos',lambda r:r.fulfill(json={'recordings':memos}))
        await page.route('**/api/voice-memos/import',import_memo)
        await page.goto(base)
        await page.wait_for_function('state.devices.length > 0')
        await page.evaluate('openUpload()')
        await page.locator('#audio-file').set_input_files([
            {'name':'긴 녹음.wav','mimeType':'audio/wav','buffer':b'audio-long'},
            {'name':'빼둘 녹음.wav','mimeType':'audio/wav','buffer':b'audio-skip'},
            {'name':'짧은 녹음.wav','mimeType':'audio/wav','buffer':b'audio-short'}])
        await page.wait_for_function('state.uploadBatch.length===3 && state.uploadBatch.every(p=>p.status==="ready")')
        assert await page.locator('#upload-batch-list input:checked').count()==3
        await page.locator('#upload-batch-list input').nth(1).uncheck()
        assert await page.locator('#upload-batch-count').inner_text()=='2 / 3개 선택'
        assert await page.locator('input[name=title]').is_disabled()
        assert '01:05:00' in await page.locator('#upload-duration').inner_text()
        await page.locator('#upload-chunk-count').fill('3')
        await page.locator('.submit-upload').click()
        await page.wait_for_function('!state.uploading && state.uploadBatch.length===2')
        assert len(notes)==1 and len(submissions)==1
        payload=submissions[0]
        assert len(payload['items'])==2
        assert [item['chunk_count'] for item in payload['items']]==[3,1]
        assert payload['device_id']=='local' and payload['output_format']=='txt'
        assert uploads[payload['items'][0]['upload_id']]['name']=='긴 녹음.wav'
        assert uploads[payload['items'][1]['upload_id']]['name']=='짧은 녹음.wav'
        assert await page.locator('#upload-dialog').is_visible()
        assert await page.locator('#note-dialog').is_hidden()
        assert await page.evaluate('state.current') is None
        await page.locator('#upload-dialog').screenshot(path=png)
        await page.locator('.submit-upload').click()
        await page.locator('#upload-dialog').wait_for(state='hidden')
        assert len(notes)==2 and len(submissions)==2
        assert submissions[1]['upload_id']==payload['items'][1]['upload_id']
        assert submissions[1]['chunk_count']=='1', 'Partial retry changed the intended chunk duration'
        assert any(uploads[id]['name']=='빼둘 녹음.wav' for id in deleted)
        assert await page.locator('.background-job').count()==2
        assert await page.locator('.note-item').count()==0

        await page.evaluate('openUpload()')
        await page.locator('#choose-voice-memo').click()
        await page.locator('.voice-memo-item').first.wait_for()
        assert await page.locator('.voice-memo-item input[type=checkbox]').count()==3
        await page.locator('.voice-memo-item').nth(0).click()
        await page.locator('.voice-memo-item').nth(1).click()
        await page.locator('#voice-memo-search').fill('제외할')
        assert await page.locator('#import-voice-memo').is_enabled(), 'Selection must survive search filtering'
        assert '2개' in await page.locator('#import-voice-memo').inner_text()
        await page.locator('#import-voice-memo').click()
        await page.wait_for_function('state.uploadBatch.length===2 && state.uploadBatch.every(p=>p.status==="ready")')
        assert imports==['memo1','memo2']
        await page.locator('.submit-upload').click()
        await page.locator('#upload-dialog').wait_for(state='hidden')
        assert len(notes)==4
        assert len(submissions[-1]['items'])==2
        assert await page.locator('.background-job').count()==4
        assert await page.locator('#note-dialog').is_hidden()
        assert not errors,errors
        await browser.close()
    print(engine,'PASS: multi-file/Voice Memos checkboxes, longest-file estimate, per-file chunks, shared settings, partial retry, no duplicate successes, background queue')


asyncio.run(main())
