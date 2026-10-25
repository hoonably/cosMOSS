"""Result file actions and safe, on-demand Markdown dialog; no AI requests."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.summaries import markdown_document
from src.summary_preview import preview_document


async def main():
    base, output = sys.argv[1:3]
    engine = sys.argv[3] if len(sys.argv)>3 else 'webkit'
    stamp=datetime.now(timezone.utc).isoformat()
    note={'id':'txt_'+'b'*32,'source_type':'text_file','title':'제품 개발 회의','text_path':'/qa/source.txt','text_body':'가상 대화',
          'text_hash':'fixture','status':'done','created_at':stamp,'updated_at':stamp,'output_format':'txt','segments':[],'speaker_names':{},'text_readable':True}
    with tempfile.TemporaryDirectory(prefix='cosmoss-summary-preview-') as temporary:
        root=Path(temporary)
        path=root/'제품 개발 회의_요약.md'
        document=markdown_document('[00:01] 민수: 금요일까지 확인하겠습니다.', '# 제품 개발 회의\n\n## 결정 사항\n\n- **금요일**까지 기능 확인\n\n### 기능 점검\n\n| 항목 | 담당 |\n|---|---|\n| 확인 | 민수 |\n\n'+ '\n'.join('- 검증용 가상 기록 '+str(i) for i in range(35)))
        path.write_text(document)
        before=path.read_bytes()
        result={'provider':'codex','text':'# 과거 내용','document_markdown':'# 이전 문서','source_hash':'fixture','completed_at':stamp,'markdown_path':str(path)}
        summary={'phase':'done','result':result}
        previews=[]; mode='normal'; delayed=asyncio.Event()
        async def preview(route):
            kind=parse_qs(urlsplit(route.request.url).query).get('kind',['markdown'])[0]
            previews.append(kind)
            response=preview_document(result,kind)
            if mode=='delay':
                delayed.set(); await asyncio.sleep(.5)
                response['html']='<h1>이전 요청의 응답</h1>'
            elif mode=='error':
                return await route.fulfill(status=409,json={'detail':'미리보기 테스트 오류'})
            await route.fulfill(json=response)
        async with async_playwright() as pw:
            options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}
            browser=await getattr(pw,engine).launch(headless=True,**options)
            page=await browser.new_page(viewport={'width':1440,'height':960},device_scale_factor=2)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            await page.add_init_script('''window.webkit={messageHandlers:{revealTextFile:{postMessage:async({path})=>{window.__revealed=path;return true;}}}};''')
            await page.route('**/api/notes',lambda r:r.fulfill(json=[]))
            await page.route('**/api/text-library**',lambda r:r.fulfill(json=[note]))
            await page.route('**/api/notes/'+note['id'],lambda r:r.fulfill(json=note))
            await page.route('**/api/notes/'+note['id']+'/summary',lambda r:r.fulfill(json=summary))
            await page.route('**/api/notes/*/summary/preview?*',preview)
            await page.route('**/api/summary/providers',lambda r:r.fulfill(json=[{'id':'codex','name':'Codex','installed':True,'ready':True,'message':'로그인됨'}]))
            await page.route('**/api/summary/models**',lambda r:r.fulfill(json={'provider':'codex','models':[{'id':'current','name':'GPT Current','efforts':['medium'],'default_effort':'medium'}],'selection':{'model':'current','effort':'medium'}}))
            await page.route('**/api/devices',lambda r:r.fulfill(json=[]))
            await page.route('**/api/local-model',lambda r:r.fulfill(json={'phase':'idle'}))
            await page.goto(base)
            await page.get_by_role('button',name='제품 개발 회의 선택',exact=True).click()
            await page.locator('#summary-result-wrap').wait_for(state='visible')
            assert not previews,'Preview fetched without clicking'
            assert await page.locator('#copy-summary,#copy-corrected,#summary-result,#corrected-transcript-preview').count()==0
            assert await page.locator('#summary-summary-file button').count()==2
            assert await page.locator('#summary-corrected-file').is_hidden()
            assert await page.locator('#summary-summary-name').inner_text()==path.name
            await page.locator('#reveal-summary').click()
            assert await page.evaluate('window.__revealed')==str(path)
            await page.screenshot(path=output)
            await page.locator('#preview-summary').click()
            await page.locator('#summary-preview-content h1').filter(has_text='제품 개발 회의').wait_for()
            assert await page.locator('#summary-preview-content table').count()==1
            assert not await page.locator('#summary-preview-content details').evaluate('e=>e.open')
            assert await page.locator('#summary-preview-content').evaluate('e=>e.scrollHeight>e.clientHeight')
            assert await page.locator('#summary-preview-dialog').evaluate('e=>e.scrollHeight<=e.clientHeight+2')
            assert await page.locator('#close-summary-preview').evaluate('e=>e.getBoundingClientRect().bottom<innerHeight')
            await page.locator('#summary-preview-dialog').screenshot(path=str(Path(output).with_stem(Path(output).stem+'-dialog')))
            await page.locator('#summary-preview-content details summary').click()
            assert '금요일까지' in await page.locator('#summary-preview-content details').inner_text()
            await page.keyboard.press('Escape')
            await page.locator('#summary-preview-dialog').wait_for(state='hidden')
            assert await page.locator('#summary-preview-content').inner_html()==''
            assert await page.locator('#preview-summary').evaluate('e=>document.activeElement===e')
            assert path.read_bytes()==before

            # Reopen reflects the saved file rather than a stale cached result.
            path.write_text('# 외부에서 편집한 문서\n\n새 내용입니다.')
            await page.locator('#preview-summary').click()
            await page.locator('#summary-preview-content h1').filter(has_text='외부에서 편집한 문서').wait_for()
            await page.locator('#close-summary-preview').click()
            path.unlink()
            await page.locator('#preview-summary').click()
            await page.locator('#summary-preview-content h1').filter(has_text='이전 문서').wait_for()
            assert '보관된 결과' in await page.locator('#summary-preview-status').inner_text()
            await page.mouse.click(2,2)
            await page.locator('#summary-preview-dialog').wait_for(state='hidden')

            # Cancelled old request cannot replace the next preview.
            mode='delay'
            await page.locator('#preview-summary').click()
            await asyncio.wait_for(delayed.wait(),3)
            await page.keyboard.press('Escape')
            mode='normal'
            await page.locator('#preview-summary').click()
            await page.locator('#summary-preview-content h1').filter(has_text='이전 문서').wait_for()
            await page.wait_for_timeout(600)
            assert '이전 요청의 응답' not in await page.locator('#summary-preview-content').inner_text()
            await page.keyboard.press('Escape')
            mode='error'
            await page.locator('#preview-summary').click()
            await page.locator('#summary-preview-status').filter(has_text='미리보기 테스트 오류').wait_for()
            assert await page.locator('#summary-preview-content').inner_html()==''
            await page.locator('#close-summary-preview').click()
            mode='normal'

            # Historical separate correction TXT still has the same two actions.
            corrected=root/'제품 개발 회의_보정.txt';corrected.write_text('<script>그대로 표시</script>')
            result['corrected_path']=str(corrected)
            await page.reload()
            await page.get_by_role('button',name='제품 개발 회의 선택',exact=True).click()
            await page.locator('#summary-corrected-file').wait_for(state='visible')
            assert await page.locator('#summary-corrected-file button').count()==2
            await page.locator('#reveal-corrected').click()
            assert await page.evaluate('window.__revealed')==str(corrected)
            await page.locator('#preview-corrected').click()
            await page.locator('#summary-preview-content pre').filter(has_text='<script>그대로 표시</script>').wait_for()
            assert await page.locator('#summary-preview-content script').count()==0
            await page.keyboard.press('Escape')
            for width in (1100,390):
                await page.set_viewport_size({'width':width,'height':820})
                await page.locator('#preview-summary').click()
                await page.locator('#summary-preview-content h1').wait_for()
                assert await page.locator('#summary-preview-dialog').evaluate('e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}')
                assert await page.locator('#summary-preview-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+2')
                await page.keyboard.press('Escape')
            assert not errors,errors
            await browser.close()
    print('PASS',engine,': two file actions, on-demand rendered Markdown, collapsed transcript, file edits, cached fallback, read-only, close/focus/abort, legacy TXT, errors, responsive')


asyncio.run(main())
