"""Saved-file previews are read-only, safe Markdown, with legacy result fallback."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx

from src.summaries import markdown_document
from src.summary_preview import MAX_PREVIEW_BYTES, preview_document, render_markdown


class DocumentPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_file_wins_over_cache_and_external_edits_are_reflected_without_writes(self):
        path = self.root / '회의_요약.md'
        path.write_text('# 실제 파일\n\n**결정 사항**\n')
        before = path.stat()
        result = {'markdown_path':str(path),'document_markdown':'# 이전 내용'}
        response = preview_document(result)
        self.assertEqual(response['filename'],path.name)
        self.assertEqual(response['source'],'file')
        self.assertIn('<strong>결정 사항</strong>',response['html'])
        self.assertEqual(before.st_mtime_ns,path.stat().st_mtime_ns)
        self.assertEqual(path.read_text(),'# 실제 파일\n\n**결정 사항**\n')
        path.write_text('# 다른 앱에서 변경한 내용')
        self.assertIn('다른 앱에서 변경한 내용',preview_document(result)['html'])

    def test_document_tables_and_collapsed_correction_are_rendered(self):
        text = markdown_document('[00:01] S01: 확인하겠습니다.', '# 회의\n\n## 결정 사항\n\n| 항목 | 담당 |\n|---|---|\n| 검토 | 민수 |')
        rendered = preview_document({'document_markdown':text})
        self.assertEqual(rendered['source'],'saved')
        self.assertIn('<table>',rendered['html'])
        self.assertIn('<details>\n<summary>보정 전사문 보기</summary>',rendered['html'])
        self.assertNotIn('<details open',rendered['html'])
        self.assertLess(rendered['html'].index('<h1>회의'),rendered['html'].index('<details>'))
        self.assertIn('[00:01] S01:',rendered['html'])

    def test_missing_file_falls_back_to_saved_modern_or_legacy_result(self):
        path = str(self.root / '없어진 결과.md')
        self.assertIn('최신 문서',preview_document({'markdown_path':path,'document_markdown':'# 최신 문서','text':'이전 문서'})['html'])
        legacy = preview_document({'markdown_path':path,'text':'# 요약','corrected_text':'원본 보정'})
        self.assertEqual(legacy['source'],'saved')
        self.assertIn('<details>',legacy['html'])
        self.assertIn('원본 보정',legacy['html'])
        self.assertIn('<h1>요약</h1>',preview_document({'text':'# 요약'})['html'])
        with self.assertRaisesRegex(ValueError,'미리 볼 결과'):
            preview_document({'markdown_path':path})

    def test_raw_html_scripts_attributes_and_external_images_are_not_active(self):
        content = '''<script>alert(1)</script>
<img src="https://example.test/track" onerror="alert(2)">
<details open onclick="alert(3)">
<summary><img src=x onerror=alert(4)></summary>

![외부 그림](https://example.test/another)

[실행](javascript:alert%281%29) [로컬](file:///etc/passwd) [상대](./some-file)

[웹](https://example.test/docs) [문서 내](#section)
'''
        output = render_markdown(content)
        self.assertNotIn('<script',output)
        self.assertNotIn('<img',output)
        self.assertNotIn('<details open',output)
        self.assertNotIn('href="javascript:',output)
        self.assertNotIn('href="file:',output)
        self.assertNotIn('href="./some-file',output)
        self.assertIn('<summary>&lt;img',output)
        self.assertIn('[이미지: 외부 그림]',output)
        self.assertIn('rel="noopener noreferrer"',output)
        self.assertIn('href="#section"',output)

    def test_fenced_tags_are_literal_and_corrected_txt_is_plain_text(self):
        output = render_markdown('```html\n<details>\n<summary>예시</summary>\n</details>\n```')
        self.assertNotIn('<details>',output)
        self.assertIn('&lt;details&gt;',output)
        path = self.root / '보정.txt'; path.write_bytes(b'\xef\xbb\xbf<script>literal</script>')
        response = preview_document({'corrected_path':str(path)},'corrected')
        self.assertEqual(response['html'],'<pre>&lt;script&gt;literal&lt;/script&gt;</pre>')

    def test_limits_regular_files_and_symlinks(self):
        path = self.root / 'large.md'
        with path.open('wb') as stream:
            stream.truncate(MAX_PREVIEW_BYTES+1)
        for result in ({'markdown_path':str(path)},{'text':'x'*(MAX_PREVIEW_BYTES+1)}):
            with self.assertRaisesRegex(ValueError,'4MB'):
                preview_document(result)
        with self.assertRaisesRegex(ValueError,'저장 경로'):
            preview_document({'markdown_path':'relative.md'})
        path = self.root / 'target.md'; path.write_text('정상')
        link = self.root / 'link.md'; link.symlink_to(path)
        with self.assertRaises(OSError):
            preview_document({'markdown_path':str(link)})
        fifo = self.root / 'pipe'; os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError,'일반 결과 파일'):
            preview_document({'markdown_path':str(fifo)})


class PreviewApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_result_only_kind_validation_and_errors(self):
        from src import main
        with patch.object(main,'note_cleanup',return_value=SimpleNamespace(hidden_note_ids=lambda:set())), patch.object(main,'review_note',return_value={'id':'a'*32}) as note, patch.object(main.SUMMARIES,'status',return_value={'result':{'document_markdown':'# 미리보기'}}) as status:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://localhost') as client:
                url='/api/notes/'+'a'*32+'/summary/preview'
                response=await client.get(url)
                self.assertEqual(response.status_code,200)
                self.assertIn('<h1>미리보기</h1>',response.json()['html'])
                self.assertEqual((await client.get(url+'?kind=arbitrary')).status_code,422)
                status.return_value={}
                self.assertEqual((await client.get(url)).status_code,404)
                status.return_value={'result':{}}
                self.assertEqual((await client.get(url)).status_code,404)
                status.return_value={'result':{'markdown_path':'relative.md'}}
                self.assertEqual((await client.get(url)).status_code,409)
                note.side_effect=KeyError()
                self.assertEqual((await client.get(url)).status_code,404)
