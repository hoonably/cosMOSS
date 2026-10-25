"""Render saved AI documents without scripts, raw HTML or external image loads."""
from __future__ import annotations

import html
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from src.summaries import markdown_document

MAX_PREVIEW_BYTES = 4 * 1024**2


def render_markdown(text: str) -> str:
    parser = MarkdownIt('js-default')  # Raw HTML is disabled.

    def details(state, start_line, end_line, silent):
        if state.sCount[start_line] - state.blkIndent >= 4:
            return False
        line = state.src[state.bMarks[start_line] + state.tShift[start_line]:state.eMarks[start_line]].strip()
        summary = re.fullmatch(r'<summary>(.*?)</summary>', line)
        if line not in {'<details>', '</details>'} and not summary:
            return False
        if silent:
            return True
        token = state.push('html_block', '', 0)
        token.content = f'<summary>{html.escape(summary[1])}</summary>\n' if summary else line + '\n'
        token.map = [start_line, start_line + 1]
        state.line = start_line + 1
        return True

    # Enable only the three exact, attribute-free tags used by our documents.
    parser.block.ruler.before('paragraph', 'summary_details', details, {'alt':['paragraph', 'reference', 'blockquote', 'list']})
    tokens = parser.parse(text)

    def clean(items):
        for token in items:
            if token.type == 'image':
                token.type = 'text'
                token.content = '[이미지: ' + token.content + ']'
                token.children = None
            elif token.type == 'link_open':
                href = token.attrGet('href') or ''
                if href.startswith('#'):
                    pass
                elif urlsplit(href).scheme.lower() in {'https', 'http'}:
                    token.attrSet('target', '_blank')
                    token.attrSet('rel', 'noopener noreferrer')
                else:
                    token.attrs.pop('href', None)
            if token.children:
                clean(token.children)

    clean(tokens)
    return parser.renderer.render(tokens, parser.options, {})


def preview_document(result: dict, kind: str = 'markdown') -> dict:
    key = 'markdown_path' if kind == 'markdown' else 'corrected_path'
    path = result.get(key)
    source = 'saved'
    text = None
    if path:
        file = Path(path)
        if not file.is_absolute():
            raise ValueError('결과 파일의 저장 경로를 확인해 주세요.')
        try:
            descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('일반 결과 파일만 미리 볼 수 있습니다.')
                if info.st_size > MAX_PREVIEW_BYTES:
                    raise ValueError('미리보기는 최대 4MB까지 지원합니다. Finder에서 파일을 열어 주세요.')
                data = stream.read(MAX_PREVIEW_BYTES + 1)
                if len(data) > MAX_PREVIEW_BYTES:
                    raise ValueError('미리보기는 최대 4MB까지 지원합니다. Finder에서 파일을 열어 주세요.')
                text = data.decode('utf-8-sig')
                source = 'file'
    if text is None:
        if kind == 'corrected':
            text = result.get('corrected_text')
        else:
            text = result.get('document_markdown')
            if not text and result.get('text'):
                text = markdown_document(result['corrected_text'], result['text']) if result.get('corrected_text') else result['text']
    if not isinstance(text, str) or not text.strip():
        raise ValueError('미리 볼 결과가 없습니다.')
    if len(text.encode('utf-8')) > MAX_PREVIEW_BYTES:
        raise ValueError('미리보기는 최대 4MB까지 지원합니다. Finder에서 파일을 열어 주세요.')
    return {'filename':Path(path).name if path else '보정·요약.md' if kind == 'markdown' else '보정 전사문.txt',
            'source':source, 'html':render_markdown(text) if kind == 'markdown' else '<pre>' + html.escape(text) + '</pre>'}
