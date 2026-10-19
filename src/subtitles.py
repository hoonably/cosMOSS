"""Read subtitle speakers and change their labels without rewriting cue timing."""
from __future__ import annotations

import html
import re


STAMP = r'(?:\d{2,}:)?[0-5]\d:[0-5]\d[.,]\d{3}'
TIMING = re.compile(rf'(?P<start>{STAMP})[ \t]+-->[ \t]+(?P<end>{STAMP})(?:[ \t]+[^\r\n]*)?')
VOICE = re.compile(r'<v(?:\.[^\s>]+)*[ \t]+(?P<name>[^>\r\n]{1,240})>', re.IGNORECASE)
LABEL = re.compile(r'[ \t]*(?:-[ \t]+)?(?P<name>[^:<>\r\n]{1,60}):[ \t]*')
TAGS = re.compile(r'<[^>]*>')


def seconds(value: str) -> float:
    parts = value.replace(',', '.').split(':')
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def subtitle_records(text: str, format_name: str):
    if format_name == 'vtt' and not re.match(r'^WEBVTT(?:[ \t\r\n]|$)', text):
        return []
    records = []
    offset = 0
    boundaries = list(re.finditer(r'\r?\n[ \t]*\r?\n', text))
    blocks = []
    for boundary in boundaries:
        blocks.append((offset, text[offset:boundary.start()]))
        offset = boundary.end()
    blocks.append((offset, text[offset:]))
    for offset, block in blocks:
        lines = block.splitlines(keepends=True)
        if not lines or re.match(r'^(?:WEBVTT|NOTE|STYLE|REGION)(?:\s|$)', lines[0]):
            continue
        timing = None
        position = offset
        for line in lines[:2]:
            timing = TIMING.fullmatch(line.rstrip('\r\n'))
            position += len(line)
            if timing:
                break
        if not timing or seconds(timing['end']) < seconds(timing['start']):
            continue
        payload = text[position:offset+len(block)]
        voices = list(VOICE.finditer(payload))
        fragments = []
        if voices:
            prefix = payload[:voices[0].start()]
            if html.unescape(TAGS.sub('', prefix)).strip():
                fragments.append(('화자 미지정', prefix, position, position, 'plain'))
            for index, voice in enumerate(voices):
                end = voices[index+1].start() if index+1 < len(voices) else len(payload)
                label = html.unescape(voice['name']).strip()
                if not label or len(label) > 60:
                    continue
                fragments.append((label, payload[voice.end():end], position+voice.start('name'), position+voice.end('name'), 'voice'))
        else:
            match = LABEL.match(payload)
            if match:
                raw = match['name']
                start = match.start('name') + len(raw)-len(raw.lstrip())
                end = match.end('name') - (len(raw)-len(raw.rstrip()))
                fragments.append((html.unescape(raw.strip()), payload[match.end():], position+start, position+end, 'label'))
            elif payload.strip():
                fragments.append(('화자 미지정', payload, position, position, 'plain'))
        for label, body, start, end, kind in fragments:
            records.append({'label':label, 'text':html.unescape(TAGS.sub('', body)).strip(),
                            'start':seconds(timing['start']), 'end':seconds(timing['end']),
                            'label_start':start, 'label_end':end, 'kind':kind})
    return records


def subtitle_fields(text: str, format_name: str):
    labels, segments = {}, []
    for record in subtitle_records(text, format_name):
        key = labels.setdefault(record['label'], f'S{len(labels)+1:02d}')
        segments.append({'id':len(segments), 'start':record['start'], 'end':record['end'],
                         'speaker':key, 'text':record['text']})
    return segments, {key:label for label,key in labels.items()}


def rename_subtitle_speakers(text: str, format_name: str, replacements: dict[str, str]):
    for record in reversed(subtitle_records(text, format_name)):
        label = record['label']
        replacement = replacements.get(label, label)
        if replacement == label:
            continue
        if record['kind'] in {'voice', 'label'}:
            replacement = html.escape(replacement, quote=False)
        elif record['kind'] == 'plain':
            replacement = f'<v {html.escape(replacement, quote=False)}>' if format_name == 'vtt' else f'{html.escape(replacement, quote=False)}: '
        text = text[:record['label_start']] + replacement + text[record['label_end']:]
    return text
