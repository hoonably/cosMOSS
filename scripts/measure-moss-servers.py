"""Measure the real default MOSS+Nemotron path; keep only anonymous numeric metrics.

Inputs/temporary audio stay in a private temporary directory. No transcripts are
written, and no saved app/device/model settings are changed. Every case runs in
a fresh worker; the standard worker owns the GPU lock and cleans its processes.
"""
from __future__ import annotations
import argparse, asyncio, csv, json, shutil, subprocess, sys, tempfile, time, wave
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.speech_jobs import SpeechJobs
from src import speech_jobs as speech_jobs_module
from src.ssh_devices import ssh_arguments
from src.speech_pipeline import estimate_pipeline

MINUTES = [10, 30, 60, 90, 120, 180]
SERVERS = {
 'h200': ('juyu-h200', [0, 1]),
 'ada': ('ubi-rtx6000', [4, 5]),
 '3090': ('wonso-rtx3090', [0, 1]),
}

def atomic(path, value):
 path.parent.mkdir(parents=True, exist_ok=True)
 tmp = path.with_suffix('.tmp')
 tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
 tmp.replace(path)

async def inventory(host):
 command = ('nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits; '
            'echo PROCESS_ROWS; nvidia-smi --query-compute-apps=gpu_uuid,process_name --format=csv,noheader,nounits')
 p = await asyncio.create_subprocess_exec(*ssh_arguments(host), '-o', 'ClearAllForwardings=yes', host, command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
 try:
  out, err = await asyncio.wait_for(p.communicate(), 30)
 except BaseException:
  if p.returncode is None: p.kill()
  await p.communicate()
  raise
 if p.returncode: raise RuntimeError(err.decode()[-500:])
 rows, processes = out.decode().split('PROCESS_ROWS', 1)
 active = {}
 for row in csv.reader(processes.strip().splitlines()):
  if len(row) >= 2: active.setdefault(row[0].strip(), []).append(row[1].strip())
 result = {}
 for row in csv.reader(rows.strip().splitlines()):
  index, uuid, name, used, capacity, utilization = [x.strip() for x in row]
  result[int(index)] = dict(uuid=uuid, name=name, memory_used_mib=float(used), memory_total_mib=float(capacity),
                           utilization=float(utilization), processes=active.get(uuid, []))
 return result

def safe_gpu(family, gpu):
 if not gpu['processes'] and gpu['memory_used_mib'] < 100: return True
 return family == 'h200' and gpu['memory_used_mib'] < 2000 and all(p in {'GPU0','GPU1','GPU2','GPU3'} for p in gpu['processes'])

def prefixes(inputs, work):
 normalized = []
 for index, source in enumerate(inputs):
  target = work/f'input-{index}.wav'
  subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-ac','1','-ar','16000','-c:a','pcm_s16le',str(target)],check=True)
  normalized.append(target)
 joined = work/'joined.wav'
 with wave.open(str(joined),'wb') as output:
  output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
  for source in normalized:
   with wave.open(str(source),'rb') as wav:
    while block := wav.readframes(320000): output.writeframesraw(block)
 with wave.open(str(joined),'rb') as wav: source_seconds=wav.getnframes()/wav.getframerate()
 full=work/'full.wav'
 subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-stream_loop','-1','-i',str(joined),'-t','10800','-c:a','pcm_s16le',str(full)],check=True)
 for path in normalized+[joined]: path.unlink()
 cases={}
 for minutes in MINUTES:
  target=work/f'{minutes}.wav'
  with wave.open(str(full),'rb') as source, wave.open(str(target),'wb') as output:
   output.setparams(source.getparams());remaining=minutes*60*source.getframerate()
   while remaining:
    count=min(remaining,320000);output.writeframesraw(source.readframes(count));remaining-=count
  cases[minutes]=target
 full.unlink()
 return cases, source_seconds

async def run(args):
 with tempfile.TemporaryDirectory(prefix='cosmoss-moss-measure-') as temporary:
  work=Path(temporary)
  report={'schema':1,'started_at':datetime.now(timezone.utc).isoformat(),'pipeline':'moss-sglang',
          'minutes':args.minutes,'cases':[],'active':{},'scope':'Default MOSS SGLang + Nemotron, fresh worker per case, no model downloads in timed region. Transfer separately recorded. Same audio prefix across GPUs; final 180-minute input repeats the beginning only beyond available source.',
          'status':'preparing_audio'}
  def save(): atomic(args.report,report)
  save()
  cases, source_seconds=await asyncio.to_thread(prefixes,args.audio,work)
  report.update(source_seconds=source_seconds,repeated_tail_seconds=max(0,10800-source_seconds),status='running');save()
  control = Path(tempfile.mkdtemp(prefix='cm-b-', dir='/tmp'))
  original_ssh_arguments = ssh_arguments
  def pooled_ssh(host):
   arguments = original_ssh_arguments(host)
   arguments = [f'ControlMaster=auto' if a == 'ControlMaster=no' else f'ControlPath={control}/%C' if a == 'ControlPath=none' else a for a in arguments]
   return arguments + ['-o', 'ControlPersist=60']
  globals()['ssh_arguments'] = pooled_ssh
  speech_jobs_module.ssh_arguments = pooled_ssh
  jobs=SpeechJobs(work/'samples.db')
  abort_long={}
  async def lane(family,host,index,durations):
   for minutes in durations:
    case={'family':family,'gpu_index':index,'audio_seconds':minutes*60,'status':'running'}
    identity=f'{family}-{minutes}'
    if family in abort_long and minutes>abort_long[family]:
     case.update(status='skipped',error='Shorter default-model run exceeded GPU memory budget');report['cases'].append(case);save();continue
    try:
     for attempt in range(3):
      try:
       gpu=(await inventory(host))[index]
       break
      except (OSError, RuntimeError, TimeoutError):
       if attempt == 2: raise
       await asyncio.sleep(3)
     if not safe_gpu(family,gpu): raise RuntimeError('GPU became occupied; no existing work was interrupted')
     case['gpu_name']=gpu['name'];case['capacity_gib']=gpu['memory_total_mib']/1024
     case['previous_prediction_seconds']=estimate_pipeline('moss-sglang',minutes*60,gpu)['time_seconds']
     started=time.monotonic();transfer_started=None;transfer_finished=None
     last_phase=None
     def event(e):
      nonlocal last_phase,transfer_started,transfer_finished
      phase=e.get('phase')
      if phase=='transferring' and transfer_started is None:transfer_started=time.monotonic()
      if phase=='loading' and transfer_finished is None:transfer_finished=time.monotonic()
      report['active'][identity]={k:e[k] for k in ['phase','engine','completed','chunk_count','partition_index','partition_count'] if k in e}
      if phase!=last_phase:
       print(json.dumps({'case':identity,**report['active'][identity]},ensure_ascii=False),flush=True);last_phase=phase
      save()
     response=await jobs.execute(host,'moss-sglang','run',event,audio=cases[minutes],batch=1,
                               gpu_uuid=gpu['uuid'],language='ko',allow_moss_split=True)
     result=response['result']
     allowed=['pipeline','runtime','batch','audio_seconds','load_seconds','inference_seconds','total_seconds',
              'peak_process_mib','memory_phases','capacity_gib','gpu_name','moss_partition_count','chunk_count','stage_metrics']
     case.update({k:result[k] for k in allowed if k in result})
     if not result.get('segments'):raise RuntimeError('No completed transcript; timing excluded')
     case.update(status='done',wall_seconds=time.monotonic()-started,segment_count=len(result['segments']),
                 transfer_seconds=(transfer_finished-transfer_started) if transfer_finished and transfer_started else None,
                 transcript_end_seconds=max(s['end'] for s in result['segments']))
    except Exception as error:
     case.update(status='error',error=(str(error) or type(error).__name__)[-1800:])
     if '메모리' in str(error) or 'out of memory' in str(error).lower():abort_long[family]=min(minutes,abort_long.get(family,minutes))
    report['active'].pop(identity,None);report['cases'].append(case);save()
    print(json.dumps({'finished':identity,'status':case['status'],'seconds':case.get('total_seconds'),'error':case.get('error')},ensure_ascii=False),flush=True)
  try:
   queues={family:iter(args.minutes) for family in args.family}
   await asyncio.gather(*(lane(family,host,index,queues[family])
        for family,(host,indices) in SERVERS.items() if family in args.family for index in args.gpus or indices))
  finally:
   for family,(host,_) in SERVERS.items():
    if family in args.family:
     await asyncio.to_thread(subprocess.run, pooled_ssh(host)+['-O','exit',host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
   shutil.rmtree(control)

  report.update(status='completed',completed_at=datetime.now(timezone.utc).isoformat());save()

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--audio',type=Path,nargs='+',required=True)
 parser.add_argument('--report',type=Path,required=True)
 parser.add_argument('--minutes',type=int,nargs='+',choices=MINUTES,default=MINUTES)
 parser.add_argument('--gpus',type=int,nargs='+')
 parser.add_argument('--family', choices=list(SERVERS), nargs='+', default=list(SERVERS))
 asyncio.run(run(parser.parse_args()))
