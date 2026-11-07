"""Exercise real Sparkle DMG replacement with the pinned free app certificate.

Uses an isolated bundle ID, a temporary localhost feed and no-op executables.
Never modifies/runs the installed user app or publishes a release.
"""
from pathlib import Path
import argparse
import functools
import hashlib
import http.server
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from persistent_signing import metadata, verify_app


def run(arguments,**options):
    return subprocess.run([str(x) for x in arguments],check=True,**options)


def sign(app):
    run([sys.executable,'-B',ROOT/'scripts/sign-mac-app.py',app],capture_output=True)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app',nargs='?',default='/Applications/cosMOSS.app')
    parser.add_argument('--legacy-adhoc-current',action='store_true')
    args=parser.parse_args()
    cfg=json.loads((ROOT/'release.json').read_text())
    public=(ROOT/cfg['public_key_file']).read_text().strip()
    key=Path(os.getenv(cfg['key_env'],str(Path.home()/'Library/Application Support'/cfg['key_directory']/'sparkle-private-key')))
    framework=Path(args.app)/'Contents/Frameworks/Sparkle.framework'
    identity='io.github.hoonably.release-chain.persistent.'+uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='release-chain-persistent-') as temporary:
        stage=Path(temporary)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(stage)))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}/'
        try:
            stub=stage/'Stub.swift';stub.write_text('import Foundation\n')
            binary=stage/'Stub';run(['swiftc',stub,'-o',binary],capture_output=True)
            info=dict(CFBundleIdentifier=identity,CFBundleName='cosMOSS Signing Check',CFBundleExecutable='Stub',
                CFBundlePackageType='APPL',CFBundleVersion='1',CFBundleShortVersionString='1.0.0',LSUIElement=True,
                SUPublicEDKey=public,SUFeedURL=base+'appcast.xml',SURequireSignedFeed=True,
                SUVerifyUpdateBeforeExtraction=True,SUEnableAutomaticChecks=False,SUAutomaticallyUpdate=False,
                NSAppTransportSecurity={'NSAllowsArbitraryLoads':True})
            current=stage/'installed/cosMOSS.app';future=stage/'payload/cosMOSS.app'
            for app,version in ((current,'1'),(future,'2')):
                (app/'Contents/MacOS').mkdir(parents=True)
                shutil.copyfile(binary,app/'Contents/MacOS/Stub');(app/'Contents/MacOS/Stub').chmod(0o755)
                (app/'Contents/Info.plist').write_bytes(plistlib.dumps({**info,'CFBundleVersion':version}))
                sign(app)
            new_requirement=verify_app(future)
            if args.legacy_adhoc_current:
                run(['codesign','--force','--sign','-',current],capture_output=True)
                old=subprocess.run(['codesign','-d','-r-',str(current)],capture_output=True,text=True)
                assert 'cdhash' in old.stdout+old.stderr
            else:
                assert verify_app(current)==new_requirement
            archive=stage/'next.dmg'
            run(['hdiutil','create','-volname','cosMOSS Signing Check','-srcfolder',future.parent,'-format','UDZO',archive],capture_output=True)
            shutil.rmtree(future.parent)
            signer=stage/'Sign.swift';signer.write_text('''import CryptoKit
import Foundation
let raw = Data(base64Encoded: try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines))!
let key = try Curve25519.Signing.PrivateKey(rawRepresentation: raw)
print(try key.signature(for: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))).base64EncodedString())
''')
            def signature(file):
                return subprocess.check_output(['swift',str(signer),str(key),str(file)],text=True).strip()
            content=(f'<?xml version="1.0"?><rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel><title>Signing Check</title><item><title>1.0.1</title><sparkle:version>2</sparkle:version><sparkle:shortVersionString>1.0.1</sparkle:shortVersionString><enclosure url="{base}next.dmg" length="{archive.stat().st_size}" type="application/octet-stream" sparkle:edSignature="{signature(archive)}"/></item></channel></rss>').encode()
            unsigned=stage/'unsigned.xml';unsigned.write_bytes(content)
            feed=stage/'appcast.xml';feed.write_bytes(content+f'<!-- sparkle-signatures:\nedSignature: {signature(unsigned)}\nlength: {len(content)}\n-->'.encode())
            run(['swift',ROOT/'scripts/verify-update.swift',ROOT/cfg['public_key_file'],feed,archive],capture_output=True)
            damaged=stage/'damaged/next.dmg';damaged.parent.mkdir();damaged.write_bytes(archive.read_bytes()+b'changed')
            assert subprocess.run(['swift',str(ROOT/'scripts/verify-update.swift'),str(ROOT/cfg['public_key_file']),str(feed),str(damaged)],capture_output=True).returncode!=0
            harness=stage/'Harness.app';executable=harness/'Contents/MacOS/UpdateHarness';executable.parent.mkdir(parents=True)
            shutil.copytree(framework,harness/'Contents/Frameworks/Sparkle.framework',symlinks=True)
            (harness/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier=identity+'.harness',CFBundleName='Signing Check',CFBundleExecutable='UpdateHarness',CFBundlePackageType='APPL',CFBundleVersion='1',LSBackgroundOnly=True)))
            run(['swiftc','-parse-as-library','-F',framework.parent,'-framework','Sparkle','-Xlinker','-rpath','-Xlinker','@executable_path/../Frameworks',ROOT/'scripts/ReleaseUpdateHarness.swift','-o',executable],capture_output=True)
            sign(harness)
            completed=subprocess.run([str(executable),str(current),'2',str(stage/'installed-marker')],capture_output=True,text=True,timeout=70)
            if completed.returncode:raise RuntimeError(completed.stdout+'\n'+completed.stderr)
            assert (stage/'installed-marker').is_file()
            assert plistlib.loads((current/'Contents/Info.plist').read_bytes())['CFBundleVersion']=='2'
            assert verify_app(current)==new_requirement
            print(completed.stdout.strip())
            print('PASS: legacy ad-hoc app migrated to persistent certificate with the same Sparkle key.' if args.legacy_adhoc_current else 'PASS: updated app retains its pinned certificate requirement; tampered DMG rejected.')
            path=ROOT/'macos/signing/verification.json'
            receipt=json.loads(path.read_text()) if path.exists() else {}
            if receipt.get('certificate_sha256') != metadata()['certificate_sha256']:receipt={}
            verified=receipt.get('verified',[])
            checks=['real Sparkle discovery/download/DMG extraction/app replacement',
                    'updated app has the pinned certificate-backed requirement','tampered archive rejected',
                    'legacy ad-hoc app migrated using unchanged Sparkle key' if args.legacy_adhoc_current
                    else 'updated app retains the original certificate-backed requirement']
            receipt.update(certificate_sha256=metadata()['certificate_sha256'],
                         verified=list(dict.fromkeys([*verified,*checks])),
                         tcc_database_modified=False,system_trusted_root_installed=False)
            path.write_text(json.dumps(receipt,indent=2)+'\n')
        finally:
            server.shutdown();server.server_close()
            for name in (identity,identity+'.harness'):
                subprocess.run(['defaults','delete',name],capture_output=True)


if __name__=='__main__':main()
