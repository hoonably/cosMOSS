"""Snapshot one existing job's timings without changing the app or its database.

Run again after completion to record the app's persisted finish time precisely.
No transcript, recording, credentials, or remote commands are collected.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3


def capture(database: Path, note_id: str, output: Path) -> dict:
    fields = ('id', 'status', 'duration', 'chunk_count', 'chunk_seconds', 'bridge_count',
              'processed_chunks', 'processed_bridges', 'progress_phase', 'created_at',
              'updated_at', 'processing_started_at', 'processing_finished_at',
              'processing_seconds', 'preprocessing_seconds', 'device_name',
              'device_snapshot_json')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(f"SELECT {', '.join(fields)} FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise ValueError('The monitored note no longer exists; no measurement was invented.')
    note = dict(row)
    snapshot = json.loads(note.pop('device_snapshot_json') or '{}')
    # Deliberate allowlist: the snapshot can also contain an API credential.
    device = {key: snapshot.get(key) for key in
              ('backend', 'model', 'model_key', 'options', 'gpu_index', 'gpu_uuid', 'hardware')}
    timestamp = datetime.now(timezone.utc)
    elapsed = note['processing_seconds']
    if elapsed is None and note['processing_started_at']:
        elapsed = (timestamp - datetime.fromisoformat(note['processing_started_at'])).total_seconds()
        elapsed += note['preprocessing_seconds'] or 0
    record = json.loads(output.read_text()) if output.exists() else {
        'schema_version': 1,
        'note_id': note_id,
        'monitor_started_at': timestamp.isoformat(),
        'observations': [],
        'measurement_scope': 'App-reported end-to-end processing_seconds, including prepared-upload preprocessing and device preparation/transfer/inference/finalization. Not isolated GPU inference latency. Remote cleanup after the done state is excluded.',
        'limitations': ['Single real recording; not a benchmark average.',
                        'Monitoring began after transcription started; earlier phase durations were not measured.',
                        'GPU contention, GPU peak memory, and cold/warm cache state were not measured.',
                        'Do not infer 30-minute versus 65-minute chunk scaling from this single sample.'],
    }
    if record['note_id'] != note_id:
        raise ValueError('Refusing to overwrite a different measurement.')
    record.update(last_observed_at=timestamp.isoformat(), job=note, device=device)
    observation = {key: note[key] for key in
                   ('status', 'progress_phase', 'processed_chunks', 'processed_bridges', 'updated_at')}
    observation.update(observed_at=timestamp.isoformat(), elapsed_seconds=round(elapsed, 3) if elapsed is not None else None)
    record['observations'].append(observation)
    seconds, duration = note['processing_seconds'], note['duration']
    record['result'] = {
        'complete': note['status'] == 'done',
        'terminal': note['status'] in ('done', 'error', 'cancelled'),
        'total_processing_seconds': seconds,
        'processing_seconds_per_audio_second': round(seconds / duration, 6) if note['status'] == 'done' and duration and seconds else None,
        'audio_speed_multiple': round(duration / seconds, 3) if note['status'] == 'done' and duration and seconds else None,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(output)
    render_report(record, output.with_suffix('.md'))
    return record


def clock_time(value):
    if not value:
        return '진행 중'
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')


def duration_text(value):
    if value is None:
        return '진행 중'
    seconds = round(value)
    return f'{seconds // 60}분 {seconds % 60:02d}초'


def render_report(record, output):
    job, device, result = record['job'], record['device'], record['result']
    result_line = '진행 중: 완료되면 앱에 저장된 정확한 종료 시각과 소요 시간을 기록합니다.'
    if result['complete']:
        result_line = (f"실제 총 소요 **{duration_text(result['total_processing_seconds'])}**, "
                       f"실시간 대비 **{result['audio_speed_multiple']}배** "
                       f"(RTF {result['processing_seconds_per_audio_second']}).")
    elif result['terminal']:
        result_line = f"작업이 {job['status']} 상태로 종료되어 성공한 전사의 보정 자료로 사용하지 않습니다."
    text = f"""# 실제 전사 시간 기록

{result_line}

| 항목 | 값 |
| --- | --- |
| 상태 | {job['status']} · {job['progress_phase']} |
| 녹음 길이 | {duration_text(job['duration'])} ({job['duration']}초) |
| 장치 | {job['device_name']} |
| 모델 | {device.get('model')} |
| 청크 | {job['chunk_count']}개 · 청크당 {duration_text(job['chunk_seconds'])} |
| 브리지 | {job['bridge_count']}개 |
| 처리 시작 | {clock_time(job['processing_started_at'])} |
| 처리 완료 | {clock_time(job['processing_finished_at'])} |
| 총 처리 시간 | {duration_text(job['processing_seconds'])} |
| 업로드 전처리 | {job['preprocessing_seconds']}초 |
| 마지막 확인 | {clock_time(record['last_observed_at'])} |

앱의 `processing_seconds`를 기준으로 합니다. 업로드 전처리, 장치 준비, 파일 전송,
전사와 화자 정리를 포함하며, 완료 상태 이후 원격 파일 정리 시간은 제외합니다.
순수 GPU 추론 시간으로 해석하지 않습니다.

이번 한 건은 예측 보정용 원자료입니다. GPU 동시 사용량·Peak VRAM·캐시의 cold/warm
상태는 측정하지 않았으며, 30분 청크와의 속도 차이는 이 결과 하나로 판단할 수 없습니다.
추정 계수는 자동 변경하지 않았습니다. 상세 설정과 관찰 이력은 옆 JSON에 보관합니다.
녹음 내용과 전사 본문, 인증 정보는 기록하지 않습니다.
"""
    output.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--note-id', required=True)
    parser.add_argument('--database', type=Path, default=Path.home() / 'Library/Application Support/cosMOSS/data/moss-note.sqlite3')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    record = capture(args.database, args.note_id, args.output)
    print(json.dumps({'status': record['job']['status'], 'result': record['result'],
                      'record': str(args.output.resolve())}, ensure_ascii=False))


if __name__ == '__main__':
    main()
