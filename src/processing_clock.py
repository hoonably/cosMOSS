"""Separate model preparation from model work without changing total progress."""
from datetime import UTC, datetime


def advance_stage(timing, phase, now=None):
    now = now or datetime.now(UTC)
    result = dict(timing or {})
    previous = result.get('phase')
    if previous in {'loading', 'processing'} and result.get('started_at'):
        elapsed = max(0, (now-datetime.fromisoformat(result['started_at'])).total_seconds())
        field = previous+'_seconds'
        result[field] = result.get(field, 0)+elapsed
    result.update(phase=phase, started_at=now.isoformat())
    return result


def stage_elapsed(timing, now=None):
    if not timing:
        return None
    result = advance_stage(timing, timing.get('phase'), now)
    return {key: result.get(key, 0) for key in ('loading_seconds', 'processing_seconds')}
