"""Initial MOSS context recommendation; OOM recovery remains the final fallback.

This is a recommendation, not a physical GPU limit. Hardware-scoped measurements
take precedence over a conservative shared curve for unmeasured GPUs.
"""
import math
from src.duration_estimates import calibrations, interpolate, server_profile
from src.speech_engines import ENGINE_VERSION


def recommend_moss_context(gpu, observations=(), *, duration=None):
    total=(gpu or {}).get('memory_total_mib')
    if not isinstance(total,(int,float)) or not math.isfinite(total) or total<=0:
        return dict(seconds=5400,source='unknown',fits=None,measured=False)
    capacity=total/1024
    free=(gpu or {}).get('memory_free_mib')
    available=min(capacity,free/1024) if isinstance(free,(int,float)) and math.isfinite(free) and free>=0 else capacity
    profile=server_profile('moss-sglang',gpu,ENGINE_VERSION)
    reference=profile or calibrations()['profiles']['moss-sglang']
    engine=calibrations().get('moss_engine_context',{})
    engine=engine if engine.get('runtime')==ENGINE_VERSION else {}
    pool=None
    token_ceiling=5400
    if engine:
        # Match SGLang's static KV budget, subtracting memory already occupied
        # by other jobs. The 24-GiB anchor is observed; other capacities project.
        cache_bytes=(capacity-engine['reference_capacity_gib'])*engine['mem_fraction_static']*1024**3
        cache_bytes-=(capacity-available)*1024**3
        pool=max(0,min(engine['max_kv_tokens'],math.floor(engine['reference_kv_tokens']+cache_bytes/engine['kv_bytes_per_token'])))
        input_limit=max(0,pool-engine['input_limit_headroom_tokens'])
        token_ceiling=max(0,(input_limit*engine['input_budget_fraction']-engine['fixed_input_tokens'])/engine['audio_tokens_per_second'])
    name=' '.join((gpu or {}).get('name','').lower().removeprefix('nvidia ').split())
    rows=[r for r in observations if r.get('pipeline')=='moss-sglang' and r.get('runtime')==ENGINE_VERSION
          and r.get('gpu_name')==name and r.get('batch')==1 and r.get('moss_partition_count',1)==1
          and abs(r.get('capacity_gib',0)-capacity)<4]
    def raw(seconds):
        value=interpolate(seconds,reference['memory_knots'],memory=True)
        if not profile and engine:
            # The shared H200 curve includes a full 131072-token cache. A smaller
            # GPU reserves a smaller cache; its lower base must be reflected.
            value=max(0,value-(engine['max_kv_tokens']-pool)*engine['kv_bytes_per_token']/1024**3)
        return value
    # A successful request at a nearby length can only raise the memory estimate.
    def required(seconds):
        near=[r['peak_process_gib'] for r in rows if abs(r['audio_seconds']-seconds)<=max(1,seconds*.1)]
        return max([raw(seconds),*near])*reference['memory_multiplier']+reference['memory_headroom_gib']
    validations=calibrations().get('moss_context_validation',[])
    matching=next((r for r in validations if r['gpu_name']==name and abs(r['capacity_gib']-capacity)<4
                   and r['runtime']==ENGINE_VERSION),None)
    ceiling=min(token_ceiling,matching['max_verified_seconds'] if matching else 5400)
    # Unknown/partial sweeps do not prove a hard maximum: OOM retries handle
    # runtime differences. Explicit failed-limit validation can cap the default.
    success=max((r['audio_seconds'] for r in rows),default=0)
    ceiling=min(token_ceiling,max(ceiling,min(5400,success)))
    choices=[seconds for seconds in range(5400,299,-300) if seconds<=ceiling and required(seconds)<=available]
    selected=choices[0] if choices else 300
    measured_until=max((point[0] for point in profile['time_knots']),default=0) if profile else 0
    if matching:measured_until=max(measured_until,matching['max_verified_seconds'])
    return dict(seconds=selected,source='hardware-measured' if profile else 'conservative-reference',
                fits=bool(choices),measured=bool(profile),available_gib=available,
                required_gib=round(required(selected),2),capacity_gib=capacity,extrapolated=selected>measured_until,
                engine_input_limit_tokens=input_limit if engine else None,
                engine_context_ceiling_seconds=round(token_ceiling) if engine else None,
                estimated_memory_gib=round(required(min(duration,selected) if duration else selected),1))
