"""Model/effort UI, refresh and persisted per-provider choices; no AI requests."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
from playwright.async_api import async_playwright
from ui_helpers import choose_option


async def main():
    base, screenshot = sys.argv[1:3]
    choices = {}; removed = False; newer = False; requests = []; refreshes = []
    def entry(identity, name, efforts=None):
        return {'id':identity,'name':name,'efforts':['low','medium','high'] if efforts is None else efforts,'default_effort':'medium'}
    codex = [entry('current','GPT Current'),entry('compact','GPT Compact')]
    claude = [entry('claude-sonnet-future','Sonnet Future'),entry('claude-opus-future','Opus Future'),entry('claude-haiku-future','Haiku Future',[])]
    stamp = datetime.now(timezone.utc).isoformat()
    note = {'id':'txt_'+'a'*32,'source_type':'text_file','title':'모델 테스트','text_path':'/qa/source.txt','text_body':'검증용 가상 대화입니다.',
            'text_hash':'fixture','status':'done','created_at':stamp,'updated_at':stamp,'output_format':'txt','segments':[],'speaker_names':{},'text_readable':True}
    summary = {'phase':'idle'}
    async def catalogs(route):
        nonlocal newer
        if route.request.method == 'PUT':
            body=route.request.post_data_json;provider=body['provider']
            choices[provider]={'model':body['model'],'effort':body['effort']}
        else:
            query=parse_qs(urlsplit(route.request.url).query);provider=query['provider'][0]
            if query.get('refresh')==['true']:
                newer=True;refreshes.append(provider)
        models = list(claude if provider=='claude' else ([entry('latest','GPT Latest')]+codex if newer else codex))
        if provider=='codex' and removed:models=[m for m in models if m['id']!='compact']
        selection=choices.get(provider,{'model':models[0]['id'],'effort':'medium'})
        notice=''
        if selection['model'] not in [m['id'] for m in models]:
            selection={'model':models[0]['id'],'effort':'medium'};choices[provider]=selection
            notice='선택했던 모델이 목록에서 사라져 기본 모델로 변경했습니다.'
        await route.fulfill(json={'provider':provider,'models':models,'selection':selection,'notice':notice,'error':None})
    async def summarize(route):
        nonlocal summary
        if route.request.method=='POST':
            body=route.request.post_data_json;requests.append(body)
            summary={'phase':'done','provider':body['provider'],'result':{'provider':body['provider'],'model':body['model'],
                'model_name':body['model'],'effort':body['effort'],'text':'# 검증 요약','corrected_text':'가상 대화',
                'source_hash':'fixture','completed_at':stamp}}
        await route.fulfill(json=summary)
    async with async_playwright() as pw:
        browser=await pw.webkit.launch(headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=2)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        await page.route('**/api/notes',lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=[note]))
        await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
        await page.route('**/api/notes/'+note['id']+'/summary',summarize)
        await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[{'id':p,'name':p.title(),'installed':True,'ready':True,'message':'로그인됨'} for p in ('codex','claude')]))
        await page.route('**/api/summary/models**',catalogs)
        await page.route('**/api/devices',lambda r:r.fulfill(json=[]))
        await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'idle'}))
        await page.goto(base)
        await page.locator('#summary-model:not([disabled])').wait_for(state='attached')
        assert await page.locator('#summary-model').input_value()=='current'
        assert await page.locator('#summary-effort').input_value()=='medium'
        assert '자동 추천' not in await page.locator('#summary-model').inner_text()
        assert await page.locator('#summary-model-status').is_hidden()
        await choose_option(page,'#summary-model','compact')
        await page.wait_for_function('!state.summaryModelSaving')
        await choose_option(page,'#summary-effort','high')
        await page.wait_for_function('!state.summaryModelSaving')
        await page.reload()
        await page.wait_for_function("document.querySelector('#summary-model').value==='compact' && !state.summaryModelLoading.size")
        assert await page.locator('#summary-effort').input_value()=='high'
        await page.locator('#refresh-summary-providers').click()
        await page.wait_for_function('!state.summaryModelLoading.size && !document.querySelector("#refresh-summary-providers").disabled')
        assert await page.locator('#summary-model').input_value()=='compact'
        assert await page.locator('#summary-effort').input_value()=='high'
        assert await page.locator('#summary-model option[value=latest]').count()==1
        await choose_option(page,'#summary-provider','claude')
        await page.wait_for_function("document.querySelector('#summary-model').value==='claude-sonnet-future' && !state.summaryModelLoading.size")
        await choose_option(page,'#summary-model','claude-haiku-future')
        await page.wait_for_function('!state.summaryModelSaving')
        assert await page.locator('#summary-effort').is_disabled()
        await choose_option(page,'#summary-provider','codex')
        await page.wait_for_function("document.querySelector('#summary-model').value==='compact' && !state.summaryModelLoading.size")
        assert await page.locator('#summary-effort').input_value()=='high'
        await page.get_by_role('button',name='모델 테스트 선택',exact=True).click()
        await page.locator('#run-summary:not([disabled])').wait_for(state='attached')
        await page.locator('#run-summary').click()
        await page.locator('#summary-result-wrap').wait_for(state='visible')
        assert requests[-1]['model']=='compact' and requests[-1]['effort']=='high'
        assert 'High' in await page.locator('#summary-result-label').inner_text()
        await page.screenshot(path=screenshot)
        removed=True
        await page.locator('#refresh-summary-providers').click()
        await page.wait_for_function("document.querySelector('#summary-model').value==='latest' && !state.summaryModelLoading.size")
        assert '사라져' in await page.locator('#summary-model-status').inner_text()
        assert await page.locator('#summary-effort').input_value()=='medium'
        assert refreshes==['codex','codex']
        assert not errors,errors
        await browser.close()
    print('PASS WebKit: real names, Medium default, persistence/reload/refresh, separate providers, model capabilities, submitted model/effort, removed-model fallback')


asyncio.run(main())
