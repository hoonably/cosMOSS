"""Export timing and agreement metrics, never audio or transcription contents.

Needs the app dependencies and rapidfuzz. Read a private directory containing
benchmark-cuda.py outputs. Agreement against the baseline is not ASR accuracy
or diarization error rate: this recording has no human reference transcript.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import tempfile


def aligned_speaker_agreement(reference, candidate):
    left = sorted({s['speaker'] for s in reference})
    right = sorted({s['speaker'] for s in candidate})
    matrix = [[0.0 for _ in right] for _ in left]
    for a in reference:
        for b in candidate:
            overlap = min(a['end'], b['end']) - max(a['start'], b['start'])
            if overlap > 0:
                matrix[left.index(a['speaker'])][right.index(b['speaker'])] += overlap
    total = sum(map(sum, matrix))
    if not total:
        return None
    if max(len(left), len(right)) > 12:
        return None
    @lru_cache(None)
    def solve(index, used):
        if index == len(left):
            return 0.0
        return max([solve(index + 1, used)] + [
            matrix[index][j] + solve(index + 1, used | (1 << j))
            for j in range(len(right)) if not used & (1 << j)])
    return solve(0, 0) / total


def summarize(root):
    from rapidfuzz.distance import Levenshtein
    # Import the real app parser/bridge merger with disposable stores. Never
    # initialize the user's database or run a FastAPI lifespan/model process.
    with tempfile.TemporaryDirectory() as folder:
        os.environ.update(MOSS_DATA_DIR=folder, MOSS_RUNTIME_DIR=folder)
        from src.main import merge_chunk_segments
        from src.transcript import parse_transcript

        records, segments_by_case, text_by_case = [], {}, {}
        for report in sorted(root.glob('*/result.json')):
            result = json.loads(report.read_text())
            if result.get('phase') != 'done':
                records.append({k: result.get(k) for k in ('case', 'phase', 'error_type', 'duration',
                    'chunks', 'kv_bits', 'censored_after_seconds', 'observed_generated_tokens', 'stop_reason')})
                continue
            chunks, bridges = [], []
            for run in result['runs']:
                raw = (report.parent / f"{run['kind']}-{run['index']}.txt").read_text()
                parsed = parse_transcript(raw)
                (chunks if run['kind'] == 'chunk' else bridges).append((run['offset'], parsed))
            segments = merge_chunk_segments(chunks, bridges)
            text = ''.join(segment['text'] for segment in segments)
            normalized = re.sub(r'[\W_]+', '', text.casefold())
            segments_by_case[result['case']] = segments
            text_by_case[result['case']] = normalized
            result['quality'] = dict(segment_count=len(segments), speaker_count=len({s['speaker'] for s in segments}),
                normalized_characters=len(normalized), first_start=min((s['start'] for s in segments), default=None),
                last_end=max((s['end'] for s in segments), default=None),
                invalid_time_segments=sum(s['end'] < s['start'] or s['start'] < 0 or s['end'] > result['duration'] + 2 for s in segments),
                hit_token_limit=any(run['hit_token_limit'] for run in result['runs']))
            records.append(result)
        for result in records:
            if result['phase'] != 'done':
                continue
            reference = next((r for r in records if r['phase'] == 'done' and r.get('duration') == result['duration']
                              and r.get('kv_bits') == 0 and r.get('chunks') == 1
                              and not r['case'].endswith(('repeat', 't64'))), None)
            if reference:
                a, b = text_by_case[reference['case']], text_by_case[result['case']]
                result['quality'].update(reference_case=reference['case'],
                    relative_character_edit_rate=Levenshtein.distance(a, b) / max(1, len(a)),
                    speaker_agreement_on_overlapping_segments=aligned_speaker_agreement(
                        segments_by_case[reference['case']], segments_by_case[result['case']]))
        return dict(schema_version=1, cases=records, quality_scope='Normalized character edit rate and '
                    'permutation-aligned speaker agreement against the original-KV same-duration baseline. '
                    'No ground-truth transcript: neither metric measures recognition accuracy or DER. '
                    'Two-chunk outputs use the app parser and 30-second bridge speaker merger. '
                    'Raw audio, transcript contents, source filenames and credentials are excluded.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = summarize(args.root)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps([{'case':r['case'], 'phase':r['phase'], 'seconds':r.get('inference_seconds'),
                      'quality':r.get('quality')} for r in result['cases']],ensure_ascii=False))
