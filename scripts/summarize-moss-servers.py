"""Export anonymous MOSS timings, provisional hardware curves, and a static plot."""
from pathlib import Path
import argparse, copy, json, statistics, sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
ROOT=Path(__file__).resolve().parents[1]
FAMILIES={'h200':'H200 140 GiB','ada':'RTX 6000 Ada 48 GiB','3090':'RTX 3090 24 GiB'}


def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('reports',type=Path,nargs='+');p.add_argument('--output',type=Path,required=True)
 p.add_argument('--apply',action='store_true');args=p.parse_args()
 chosen={};failures=[];meta={}
 for path in args.reports:
  report=json.loads(path.read_text());meta.update({k:report[k] for k in ['pipeline','runtime','source_revision','source_seconds','repeated_tail_seconds','scope'] if k in report})
  for row in report['cases']:
   if row['status']=='done': chosen[row['family'],row['audio_seconds']]=row
   else:failures.append({'family':row['family'],'audio_seconds':row['audio_seconds'],'status':row['status']})
 args.output.parent.mkdir(parents=True,exist_ok=True)
 fields=['family','gpu_name','capacity_gib','pipeline','runtime','batch','audio_seconds','inference_seconds','load_seconds','total_seconds',
         'peak_process_mib','moss_partition_count','chunk_count','stage_metrics','segment_count','transcript_end_seconds','previous_prediction_seconds']
 rows=[{k:row[k] for k in fields if k in row} for _,row in sorted(chosen.items())]
 public={**meta,'schema':1,'cases':rows,'unsuccessful_attempts':failures,
         'validation':'One pass per duration per GPU family on identical audio prefixes. Provisional: no independent recording/held-out run. Input content/speech density and cold-start/cache variation can change real latency.'}
 args.output.with_suffix('.json').write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n')
 config_path=ROOT/'src/duration_calibrations.json';config=json.loads(config_path.read_text())
 template=config['profiles']['moss-sglang'];profiles=[]
 for family in FAMILIES:
  group=sorted((r for r in rows if r['family']==family),key=lambda r:r['audio_seconds'])
  single=[r for r in group if r.get('moss_partition_count',1)==1 and r['audio_seconds']<=5400]
  if len(single)<2:continue
  profile=copy.deepcopy(template)
  def monotone(values):
   peak=0;points=[]
   for x,y in values:
    peak=max(peak,y);points.append([x,peak])
   return points
  profile.update(sample_count=len(group),time_knots=monotone((r['audio_seconds'],r['inference_seconds']) for r in single),
   memory_knots=monotone((r['audio_seconds'],r['peak_process_mib']/1024) for r in group),
   cold_setup_seconds=statistics.median(r['load_seconds'] for r in group),
   hardware={'name':' '.join(group[0]['gpu_name'].lower().removeprefix('nvidia ').split()),'capacity_gib':group[0]['capacity_gib']},
   validation={'duration_seconds':None,'status':'provisional','time_absolute_error_percent':None,
               'reason':'Same-input duration sweep; no independent held-out run; concurrent cases on two different GPUs.'},
   measurement_report='docs/performance/2026-11-10-moss-servers.json', split_time_knots={})
  for count in {r.get('moss_partition_count',1) for r in group}-{1}:
   profile['split_time_knots'][str(count)]=monotone((r['audio_seconds'],r['inference_seconds']) for r in group if r.get('moss_partition_count',1)==count)
  profiles.append(profile)
 if args.apply and profiles:
  existing=config.setdefault('server_profiles',{}).get('moss-sglang',[])
  names={p['hardware']['name'] for p in profiles}
  config['server_profiles']['moss-sglang']=profiles+[p for p in existing if p['hardware']['name'] not in names]
  config['version']='duration-calibration-20261110-moss-v2'
  config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
 # Plot in English to remain portable without adding a platform font dependency.
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 from src.speech_pipeline import estimate_pipeline
 from src.duration_estimates import calibrations
 calibrations.cache_clear()
 plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.labelcolor':'#34483f','text.color':'#263b36','axes.titleweight':'bold'})
 fig,axes=plt.subplots(1,3,figsize=(13.5,4.3),sharex=True)
 for ax,(family,label) in zip(axes,FAMILIES.items()):
  group=sorted((r for r in rows if r['family']==family),key=lambda r:r['audio_seconds'])
  ax.set_title(label,loc='left',pad=14)
  if group:
   x=[r['audio_seconds']/60 for r in group]
   ax.plot(x,[r['total_seconds']/60 for r in group],'o-',color='#168568',label='Measured incl. setup',linewidth=2)
   ax.plot(x,[r['inference_seconds']/60 for r in group],'s:',color='#4268ad',label='Model work only',linewidth=1.7)
   prior=[r.get('previous_prediction_seconds') for r in group]
   if any(v is not None for v in prior):ax.plot(x,[v/60 if v is not None else float('nan') for v in prior],'--',color='#a6ada8',label='Previous prediction')
   if args.apply:
    gpu={'name':group[0]['gpu_name'],'memory_total_mib':group[0]['capacity_gib']*1024}
    ax.plot(x,[estimate_pipeline('moss-sglang',r['audio_seconds'],gpu)['time_seconds']/60 for r in group],'^--',color='#a26535',label='Adjusted prediction',linewidth=1.3)
  else:
   ax.text(.5,.5,'No completed measurement\nSSH connection / transfer failed',transform=ax.transAxes,ha='center',va='center',color='#81938a')
  ax.set_xticks([10,30,60,90,120,180]);ax.set_xlabel('Recording duration (minutes)');ax.set_ylabel('Processing time (minutes)');ax.set_ylim(bottom=0);ax.grid(axis='y',alpha=.15)
 fig.suptitle('MOSS default · GPU duration sweep',x=.055,ha='left',fontsize=16,fontweight='bold')
 handles,labels=axes[0].get_legend_handles_labels()
 fig.legend(handles,labels,loc='upper left',bbox_to_anchor=(.055,.91),ncol=4,frameon=False,fontsize=9)
 fig.text(.055,.015,'Setup includes model startup; audio transfer excluded. One trial per point; no independent validation. 180-min input repeats ~10 min at the end.',fontsize=8,color='#697b70')
 fig.tight_layout(rect=[.015,.065,1,.81]);fig.savefig(args.output.with_suffix('.png'),dpi=180);fig.savefig(args.output.with_suffix('.svg'));plt.close(fig)
 print(json.dumps({'successful_cases':len(rows),'profiles':[p['hardware']['name'] for p in profiles],'output':str(args.output)},ensure_ascii=False))

if __name__=='__main__':main()
