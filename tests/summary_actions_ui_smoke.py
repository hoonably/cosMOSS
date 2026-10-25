"""Prompt copying independent of CLI auth; explicit connection and summary actions."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

from playwright.async_api import async_playwright
from ui_helpers import choose_option


async def main():
    base, screenshot = sys.argv[1:3]
    engine = sys.argv[3] if len(sys.argv)>3 else 'webkit'
    stamp=datetime.now(timezone.utc).isoformat()
    note={'id':'txt_'+'c'*32,'source_type':'text_file','title':'버튼 테스트','text_path':'/qa/source.txt','text_body':'전사 내용은 따로 첨부합니다.',
          'text_hash':'fixture','status':'done','created_at':stamp,'updated_at':stamp,'output_format':'txt','segments':[],'speaker_names':{},'text_readable':True}
    defaults={'default_prompt':'사용자가 저장한 기본 프롬프트입니다.','recommended_prompt':'추천 프롬프트','customized':True}
    installed=False; ready=False; provider_checks=0; requests=[]; summary={'phase':'idle'}
    async def providers(route):
        nonlocal provider_checks
        provider_checks+=1
        await route.fulfill(json=[{'id':'codex','name':'Codex','installed':installed,'ready':ready,
                'message':'로그인됨' if ready else 'CLI에서 로그인해 주세요.' if installed else 'CLI를 설치해 주세요.',
                'install':'npm install -g @openai/codex','login':'codex login','url':'https://developers.openai.com/codex/cli/'},
             {'id':'claude','name':'Claude','installed':False,'ready':False,'message':'CLI를 설치해 주세요.',
                'install':'brew install --cask claude-code','login':'claude auth login','url':'https://code.claude.com/docs/en/quickstart'}])
    async def summarize(route):
        if route.request.method=='POST':
            requests.append(route.request.post_data_json)
            summary.update(phase='done',result={'provider':'codex','text':'# 요약 결과','source_hash':'fixture','completed_at':stamp})
        await route.fulfill(json=summary)
    async with async_playwright() as pw:
        options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
        browser=await getattr(pw,engine).launch(headless=True,**options)
        page=await browser.new_page(viewport={'width':1440,'height':960},device_scale_factor=2)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        await page.add_init_script('''Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{if(window.__failCopy)throw Error('denied');window.__copied=text;}}});''')
        await page.route('**/api/notes',lambda r:r.fulfill(json=[]))
        await page.route('**/api/text-library**',lambda r:r.fulfill(json=[note]))
        await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
        await page.route('**/api/notes/'+note['id']+'/summary',summarize)
        await page.route('**/api/summary/default-prompt',lambda r:r.fulfill(json=defaults))
        await page.route('**/api/summary/providers',providers)
        await page.route('**/api/summary/models**',lambda r:r.fulfill(json={'provider':'codex','models':[{'id':'current','name':'GPT Current','efforts':['medium'],'default_effort':'medium'}] if ready else [],'selection':{'model':'current','effort':'medium'} if ready else {}}))
        await page.route('**/api/devices',lambda r:r.fulfill(json=[]))
        await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'idle'}))
        await page.goto(base)
        await page.locator('#run-summary').filter(has_text='CLI 연결하기').wait_for()
        await page.wait_for_function('!state.summaryProviderLoading')
        assert await page.locator('#run-summary').is_enabled(),'Connection incorrectly requires a selected note'
        assert await page.locator('#copy-summary-prompt').is_enabled()
        assert await page.locator('#cli-setup').is_hidden()
        # Copy exactly the configured request; neither CLI nor note selection is required.
        await page.locator('#summary-prompt').fill('추가로 결정 사항을 확인해 주세요.')
        await page.locator('#copy-summary-prompt').click()
        assert await page.evaluate('window.__copied')==defaults['default_prompt']+'\n\n추가로 결정 사항을 확인해 주세요.'
        await page.locator('#use-default-summary-prompt').uncheck()
        await page.locator('#copy-summary-prompt').click()
        assert await page.evaluate('window.__copied')=='추가로 결정 사항을 확인해 주세요.'
        await page.locator('#summary-prompt').fill('   ')
        assert await page.locator('#copy-summary-prompt').is_disabled()
        await page.locator('#summary-prompt').fill('내 요청만')
        await page.evaluate('window.__failCopy=true')
        await page.locator('#copy-summary-prompt').click()
        await page.locator('#toast').filter(has_text='복사하지 못했습니다').wait_for()
        assert await page.locator('#copy-summary-prompt').is_enabled()
        await page.evaluate('window.__failCopy=false')
        # The connection button only opens instructions. It never starts a summary.
        await page.locator('#run-summary').click()
        await page.locator('#summary-cli-dialog').wait_for(state='visible')
        assert await page.locator('#summary-cli-title').inner_text()=='Codex 연결'
        assert await page.locator('#cli-setup-command').inner_text()=='npm install -g @openai/codex\ncodex login'
        await page.locator('#copy-summary-cli-command').click()
        assert await page.evaluate('window.__copied')=='npm install -g @openai/codex\ncodex login'
        installed=True
        await page.locator('#check-summary-cli').click()
        await page.wait_for_function('!state.summaryProviderLoading')
        assert await page.locator('#summary-cli-dialog').is_visible()
        assert await page.locator('#cli-setup-command').inner_text()=='codex login'
        assert '로그인' in await page.locator('#summary-cli-status').inner_text()
        assert not requests
        await page.locator('#summary-cli-dialog').screenshot(path=str(Path(screenshot).with_stem(Path(screenshot).stem+'-connect')))
        ready=True
        await page.locator('#check-summary-cli').click()
        await page.locator('#summary-cli-dialog').wait_for(state='hidden')
        assert await page.locator('#run-summary').inner_text()=='CLI로 요약하기\n→'
        assert await page.locator('#run-summary').is_disabled(),'Summary enabled without a note'
        assert not requests
        await page.get_by_role('button',name='버튼 테스트 선택',exact=True).click()
        await page.locator('#run-summary:not([disabled])').wait_for(state='attached')
        await page.locator('#summary-prompt').fill('추가 요청')
        await page.locator('#use-default-summary-prompt').check()
        await page.locator('#copy-summary-prompt').click()
        assert '전사 내용은 따로' not in await page.evaluate('window.__copied')
        assert await page.evaluate('window.__copied')==defaults['default_prompt']+'\n\n추가 요청'
        assert not requests
        await page.locator('.summary-actions').screenshot(path=screenshot)
        await page.locator('#run-summary').click()
        await page.locator('#summary-result-wrap').wait_for(state='visible')
        assert len(requests)==1 and requests[0]['provider']=='codex' and requests[0]['prompt']=='추가 요청' and requests[0]['use_default_prompt'] is True
        assert 'CLI로 다시 요약하기' in await page.locator('#run-summary').inner_text()
        # Claude gets its own instructions and copies work while it is disconnected.
        await choose_option(page,'#summary-provider','claude')
        await page.wait_for_function('!state.summaryModelLoading.size')
        assert 'CLI 연결하기' in await page.locator('#run-summary').inner_text()
        assert await page.locator('#copy-summary-prompt').is_enabled()
        await page.locator('#run-summary').click()
        assert await page.locator('#summary-cli-title').inner_text()=='Claude 연결'
        assert 'claude auth login' in await page.locator('#cli-setup-command').inner_text()
        assert await page.locator('#cli-docs').get_attribute('href')=='https://code.claude.com/docs/en/quickstart'
        await page.keyboard.press('Escape')
        await page.locator('#summary-cli-dialog').wait_for(state='hidden')
        for width in (1100,390):
            await page.set_viewport_size({'width':width,'height':820})
            for selector in ('.summary-actions','#copy-summary-prompt','#run-summary'):
                assert await page.locator(selector).evaluate('e=>e.scrollWidth<=e.clientWidth+2'),selector
            await page.locator('#run-summary').click()
            assert await page.locator('#summary-cli-dialog').evaluate('e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}')
            await page.locator('#close-summary-cli').click()
        assert not errors,errors
        assert provider_checks==3
        await browser.close()
    print('PASS',engine,': prompt copy/default/custom/error without CLI, install/login instructions, command copy, connection check, no auto-run, selected provider, summary submission, responsive')


asyncio.run(main())
