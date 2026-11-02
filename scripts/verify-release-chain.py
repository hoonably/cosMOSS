#!/usr/bin/env python3
"""Verify a release and install a signed synthetic update into a disposable app.

Only the temporary no-op executable may be launched; user apps/data are never
opened. The original production payload, key, feed and framework are verified
before constructing the isolated test. No test artifact is published.
"""
from pathlib import Path
import functools,hashlib,http.server,json,os,plistlib,shutil,subprocess,tempfile,threading,time,uuid
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
root=Path(__file__).resolve().parents[1]
cfg=json.loads((root/'release.json').read_text())
archive=root/'dist'/cfg['update_archive'].format(version=cfg['version'])
key=Path(os.environ.get(cfg['key_env'],str(Path.home()/'Library/Application Support'/cfg['key_directory']/'sparkle-private-key')))
public=(root/cfg['public_key_file']).read_text().strip()
def run(args,**kw):return subprocess.run([str(x) for x in args],check=True,**kw)
def sign(app):run(['codesign','--force','--sign','-',app],capture_output=True)
run(['swift',root/'scripts/setup-update-key.swift',root,'--check-only'])
run(['swift',root/'scripts/verify-update.swift',root/cfg['public_key_file'],root/'appcast.xml',archive])
class Handler(http.server.SimpleHTTPRequestHandler):
 def log_message(self,*_args):pass
 def end_headers(self):self.send_header('Cache-Control','no-store');super().end_headers()
identity='io.github.hoonably.release-chain.'+uuid.uuid4().hex
with tempfile.TemporaryDirectory(prefix='release-chain-') as tmp:
 stage=Path(tmp);current=stage/'installed'/f"{cfg['app_name']}.app";current.parent.mkdir()
 if archive.suffix=='.dmg':
  mounted=plistlib.loads(subprocess.check_output(['hdiutil','attach',str(archive),'-readonly','-nobrowse','-plist']))
  volumes=[e['mount-point'] for e in mounted['system-entities'] if 'mount-point' in e]
  try:run(['ditto','--norsrc',Path(volumes[0])/current.name,current])
  finally:
   for v in volumes:run(['hdiutil','detach',v],capture_output=True)
 else:run(['ditto','-x','-k',archive,current.parent])
 info_path=current/'Contents/Info.plist';original=plistlib.loads(info_path.read_bytes())
 assert original['CFBundleShortVersionString']==cfg['version'] and original['CFBundleVersion']==str(cfg['build'])
 assert original['CFBundleIdentifier']==cfg['bundle_identifier'],'Bundle identity changed'
 assert original['SUPublicEDKey']==public
 assert original['SUFeedURL']==f"https://raw.githubusercontent.com/{cfg['repository']}/main/appcast.xml"
 assert original['SURequireSignedFeed'] and original['SUVerifyUpdateBeforeExtraction']
 framework=current/'Contents/Frameworks/Sparkle.framework'
 assert framework.is_dir(),'The distributed app must include Sparkle'
 run(['codesign','--verify','--deep','--strict',current],capture_output=True)
 installer=root/'dist'/cfg['installer'].format(version=cfg['version'])
 assert installer.is_file(),'Missing DMG installer'
 if installer != archive:
  mounted=plistlib.loads(subprocess.check_output(['hdiutil','attach',str(installer),'-readonly','-nobrowse','-plist']))
  volumes=[e['mount-point'] for e in mounted['system-entities'] if 'mount-point' in e]
  try:
   dmg_app=Path(volumes[0])/current.name
   run(['codesign','--verify','--deep','--strict',dmg_app],capture_output=True)
   assert plistlib.loads((dmg_app/'Contents/Info.plist').read_bytes())==original,'DMG and updater metadata differ'
   def manifest(app):
    return {str(p.relative_to(app)):('link:'+str(p.readlink()) if p.is_symlink() else hashlib.sha256(p.read_bytes()).hexdigest()) for p in app.rglob('*') if p.is_file() or p.is_symlink()}
   assert manifest(dmg_app)==manifest(current),'DMG and update ZIP contain different apps'
  finally:
   for v in volumes:run(['hdiutil','detach',v],capture_output=True)
 previous_build=None
 committed_feed=subprocess.run(['git','show','HEAD:appcast.xml'],capture_output=True)
 if archive.suffix=='.dmg' and committed_feed.returncode==0:
  ns='{http://www.andymatuschak.org/xml-namespaces/sparkle}'
  previous_items=ET.fromstring(committed_feed.stdout).findall('channel/item')
  if previous_items:
   previous_item=max(previous_items,key=lambda item:int(item.findtext(ns+'version')))
   previous_build=int(previous_item.findtext(ns+'version'))
   if previous_build<int(cfg['build']):
    enclosure=previous_item.find('enclosure');url=enclosure.get('url')
    expected=f"https://github.com/{cfg['repository']}/releases/download/"
    assert url.startswith(expected),'Unexpected previous release download location'
    legacy=stage/'legacy';legacy.mkdir();old_feed=legacy/'appcast.xml';old_feed.write_bytes(committed_feed.stdout)
    old_payload=legacy/Path(urlsplit(url).path).name
    run(['curl','--fail','--location','--silent','--show-error','--max-time','120',url,'--output',old_payload])
    run(['swift',root/'scripts/verify-update.swift',root/cfg['public_key_file'],old_feed,old_payload])
    shutil.rmtree(current)
    if old_payload.suffix=='.dmg':
     mounted=plistlib.loads(subprocess.check_output(['hdiutil','attach',str(old_payload),'-readonly','-nobrowse','-plist']))
     volumes=[e['mount-point'] for e in mounted['system-entities'] if 'mount-point' in e]
     try:run(['ditto','--norsrc',Path(volumes[0])/current.name,current])
     finally:
      for v in volumes:run(['hdiutil','detach',v],capture_output=True)
    else:run(['ditto','-x','-k',old_payload,current.parent])
    original=plistlib.loads(info_path.read_bytes())
    assert original['SUPublicEDKey']==public and original['CFBundleIdentifier']==cfg['bundle_identifier']
    run(['codesign','--verify','--deep','--strict',current],capture_output=True)
    print('Testing DMG updates with the previous production app, build',previous_build)
 tools=next((p for base in [root/'.build',root/'macos/.build'] if base.exists() for p in base.glob('artifacts/**/bin/generate_appcast')),None)
 assert tools is not None
 server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(stage)))
 threading.Thread(target=server.serve_forever,daemon=True).start()
 try:
  base=f'http://127.0.0.1:{server.server_port}/'
  info=dict(original,CFBundleIdentifier=identity,SUFeedURL=base+'appcast.xml',SUEnableAutomaticChecks=False,SUAutomaticallyUpdate=False,NSAppTransportSecurity={'NSAllowsArbitraryLoads':True})
  info.pop('CosMOSSProjectRoot',None)
  info_path.write_bytes(plistlib.dumps(info));sign(current)
  future=stage/'payload'/current.name;(future/'Contents/MacOS').mkdir(parents=True)
  major,minor,patch=map(int,cfg['version'].split('.'))
  following=f'{major}.{minor}.{patch+1}'
  updated=dict(info,CFBundleVersion=str(cfg['build']+1),CFBundleShortVersionString=following)
  (future/'Contents/Info.plist').write_bytes(plistlib.dumps(updated))
  stub=stage/'NoOp.swift';stub.write_text('import Foundation\n')
  run(['swiftc',stub,'-o',future/'Contents/MacOS'/info['CFBundleExecutable']])
  sign(future)
  next_payload=stage/('signed-next'+archive.suffix)
  if archive.suffix=='.dmg':
   run(['hdiutil','create','-volname',cfg['app_name'],'-srcfolder',future.parent,'-format','UDZO',next_payload],capture_output=True)
  else:
   run(['ditto','-c','-k','--keepParent','--norsrc',future,next_payload])
  shutil.rmtree(future.parent)
  run([tools,'--ed-key-file',key,'--download-url-prefix',base,'--maximum-versions','1','--maximum-deltas','0','-o',stage/'appcast.xml',stage])
  run(['swift',root/'scripts/verify-update.swift',root/cfg['public_key_file'],stage/'appcast.xml',next_payload])
  # Independently reject archive and feed tampering with the same verifier.
  bad_payload=stage/'tampered'/next_payload.name;bad_payload.parent.mkdir();bad_payload.write_bytes(next_payload.read_bytes()+b'tampered')
  assert subprocess.run(['swift',str(root/'scripts/verify-update.swift'),str(root/cfg['public_key_file']),str(stage/'appcast.xml'),str(bad_payload)],capture_output=True).returncode!=0
  bad_feed=stage/'tampered.xml';bad_feed.write_bytes((stage/'appcast.xml').read_bytes().replace(b'<title>',b'<title>tampered ',1))
  assert subprocess.run(['swift',str(root/'scripts/verify-update.swift'),str(root/cfg['public_key_file']),str(bad_feed),str(next_payload)],capture_output=True).returncode!=0
  harness=stage/'Harness.app';binary=harness/'Contents/MacOS/UpdateHarness';binary.parent.mkdir(parents=True)
  shutil.copytree(framework,harness/'Contents/Frameworks/Sparkle.framework',symlinks=True)
  (harness/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':identity+'.harness','CFBundleName':'Release Chain Test','CFBundleExecutable':'UpdateHarness','CFBundlePackageType':'APPL','CFBundleVersion':'1','LSBackgroundOnly':True}))
  run(['swiftc','-parse-as-library','-F',framework.parent,'-framework','Sparkle','-Xlinker','-rpath','-Xlinker','@executable_path/../Frameworks',root/'scripts/ReleaseUpdateHarness.swift','-o',binary])
  sign(harness)
  result=subprocess.run([str(binary),str(current),str(cfg['build']+1),str(stage/'installed-marker')],capture_output=True,text=True,timeout=70)
  if result.returncode:raise AssertionError(result.stdout+'\n'+result.stderr)
  for _ in range(100):
   try:
    if plistlib.loads(info_path.read_bytes()).get('CFBundleVersion')==str(cfg['build']+1):break
   except OSError:pass
   time.sleep(.1)
  assert plistlib.loads(info_path.read_bytes())['CFBundleVersion']==str(cfg['build']+1),'The disposable app was not updated'
  run(['codesign','--verify','--deep','--strict',current],capture_output=True)
  print(result.stdout.strip())
  report={'version':cfg['version'],'build':cfg['build'],'public_key':public,'installer':installer.name,'installer_sha256':hashlib.sha256(installer.read_bytes()).hexdigest(),'archive':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'verified':['identical DMG and updater app, signature and metadata','production signed feed and archive','signed next-version discovery, download, validation and actual disposable app replacement','feed and archive tampering rejected'],'future_build':cfg['build']+1,'future_version':following,'previous_build':previous_build,'update_format':archive.suffix.lstrip('.')}
  (root/'dist/release-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
  print('PASS: verified real installer and the complete isolated next-update chain')
 finally:
  server.shutdown();server.server_close()
  subprocess.run(['defaults','delete',identity],capture_output=True)
  subprocess.run(['defaults','delete',identity+'.harness'],capture_output=True)
