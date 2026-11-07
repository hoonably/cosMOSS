"""A pinned, self-signed certificate shared by local and CI builds.

Only an authenticated encrypted identity is stored in the repository. Plaintext
PKCS12/keychain files exist in a private temporary directory during signing.
No certificate is installed as a trusted root, and TCC settings are untouched.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import shlex
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def run(arguments, **options):
    result = subprocess.run([str(arg) for arg in arguments], capture_output=True, **options)
    if result.returncode:
        # Never include the command line: Keychain CLI arguments may contain
        # temporary passwords. Tool stderr contains no private key material.
        raise RuntimeError(f'{Path(arguments[0]).name}: {result.stderr.decode(errors="replace").strip()}')
    return result.stdout


def metadata(root=ROOT):
    folder = root/'macos/signing'
    if not (folder/'identity.json').exists():
        return None
    saved = json.loads((folder/'identity.json').read_text())
    config = json.loads((root/'release.json').read_text())
    certificate = (folder/'certificate.der').read_bytes()
    if (saved.get('version') != 1 or saved.get('bundle_identifier') != config['bundle_identifier']
            or hashlib.sha256(certificate).hexdigest() != saved.get('certificate_sha256')
            or hashlib.sha1(certificate).hexdigest().upper() != saved.get('certificate_sha1')):
        raise ValueError('Pinned signing certificate differs; refusing to change app identity.')
    return saved


def verify_app(app, root=ROOT):
    saved = metadata(root)
    if saved is None:
        raise ValueError('Persistent signing identity is not initialized.')
    run(['codesign','--verify','--deep','--strict',app])
    result = subprocess.run(['codesign','-d','-r-',str(app)],capture_output=True,text=True)
    match = re.search(r'(?:# )?designated => (.+)',result.stdout+result.stderr)
    if (result.returncode or not match or 'cdhash' in match[1]
            or not re.search(r'certificate root\s*=\s*H"'+saved['certificate_sha1']+r'"',match[1],re.I)):
        raise ValueError('App does not have the pinned certificate-backed identity.')
    return match[1]


@contextmanager
def identity(root=ROOT):
    saved = metadata(root)
    if saved is None:
        raise ValueError('Persistent signing identity is not initialized.')
    with tempfile.TemporaryDirectory(prefix='cosmoss-signing-identity-') as temporary:
        stage = Path(temporary).resolve()
        decoded = stage/'decoded.json'
        run(['swift',root/'scripts/signing-archive.swift','decrypt',root,
             root/'macos/signing/identity.p12.aesgcm',decoded])
        envelope = json.loads(decoded.read_bytes())
        decoded.unlink()
        if envelope.get('certificate_sha256') != saved['certificate_sha256']:
            raise ValueError('Signing archive and pinned certificate differ.')
        pkcs12 = stage/'identity.p12'
        with os.fdopen(os.open(pkcs12,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
            stream.write(base64.b64decode(envelope['pkcs12'],validate=True))
        chain = stage/'identity.keychain-db'
        password = secrets.token_urlsafe(40)
        previous = shlex.split(run(['security','list-keychains','-d','user']).decode())
        try:
            run(['security','create-keychain','-p',password,chain])
            run(['security','unlock-keychain','-p',password,chain])
            # --keychain alone does not add the certificate to codesign's chain
            # search path. Hosted macOS runners need this explicit registration.
            run(['security','list-keychains','-d','user','-s',chain,*previous])
            run(['security','import',pkcs12,'-k',chain,'-P',envelope['password'],'-T','/usr/bin/codesign'])
            pkcs12.unlink()
            certificate = run(['security','find-certificate','-a','-p',chain])
            der = run(['openssl','x509','-outform','DER'],input=certificate)
            if hashlib.sha256(der).hexdigest() != saved['certificate_sha256']:
                raise ValueError('Imported signing certificate differs; refusing to sign.')
            yield saved['certificate_sha1'], chain
        finally:
            try:
                run(['security','list-keychains','-d','user','-s',*previous])
            finally:
                # Remove only our isolated keychain. Trust settings are untouched.
                subprocess.run(['security','delete-keychain',str(chain)],capture_output=True)


def probe_identity(root=ROOT):
    """Verify actual noninteractive signing, rather than certificate listing."""
    with identity(root) as (certificate, chain):
        probe = chain.parent/'signing-probe'
        shutil.copyfile('/usr/bin/true',probe)
        run(['codesign','--force','--sign',certificate,'--keychain',chain,'--timestamp=none',probe])
        verify_app(probe,root)


def initialize(root=ROOT):
    if metadata(root) is not None:
        with identity(root):
            pass
        print('Verified the existing persistent signing identity; nothing rotated.')
        return
    target = root/'macos/signing'
    if target.exists():
        raise ValueError('Incomplete signing configuration exists. Restore it instead of generating another identity.')
    run(['swift',root/'scripts/setup-update-key.swift',root,'--check-only'])
    config = json.loads((root/'release.json').read_text())
    previous_mask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='cosmoss-signing-setup-') as temporary:
            stage = Path(temporary)
            pem,key,der,p12 = [stage/name for name in ('certificate.pem','private.pem','certificate.der','identity.p12')]
            run(['openssl','req','-x509','-newkey','rsa:3072','-sha256','-nodes','-days','7300',
                 '-subj','/CN=cosMOSS Persistent Code Signing/O=hoonably/',
                 '-addext','basicConstraints=critical,CA:FALSE','-addext','keyUsage=critical,digitalSignature',
                 '-addext','extendedKeyUsage=critical,codeSigning','-keyout',key,'-out',pem])
            run(['openssl','x509','-in',pem,'-outform','DER','-out',der])
            password = secrets.token_urlsafe(40)
            run(['openssl','pkcs12','-export','-legacy','-inkey',key,'-in',pem,'-out',p12,
                 '-passout','env:COSMOSS_P12_PASSWORD'],env={**os.environ,'COSMOSS_P12_PASSWORD':password})
            certificate = der.read_bytes()
            saved = dict(version=1,bundle_identifier=config['bundle_identifier'],
                         certificate_sha256=hashlib.sha256(certificate).hexdigest(),
                         certificate_sha1=hashlib.sha1(certificate).hexdigest().upper(),
                         name='cosMOSS Persistent Code Signing')
            envelope = stage/'envelope.json'
            envelope.write_text(json.dumps(dict(certificate_sha256=saved['certificate_sha256'],
                password=password,pkcs12=base64.b64encode(p12.read_bytes()).decode())))
            encrypted = stage/'identity.p12.aesgcm'
            run(['swift',root/'scripts/signing-archive.swift','encrypt',root,envelope,encrypted])
            # Publish all three non-plaintext files together; no partial identity
            # may silently trigger key regeneration on the next invocation.
            with tempfile.TemporaryDirectory(prefix='.signing-stage-',dir=root/'macos') as publication:
                folder = Path(publication)
                (folder/'identity.json').write_text(json.dumps(saved,indent=2)+'\n')
                (folder/'certificate.der').write_bytes(certificate)
                (folder/'identity.p12.aesgcm').write_bytes(encrypted.read_bytes())
                folder.rename(target)
        with identity(root):
            pass
        print('Initialized and verified one persistent code-signing certificate. Existing update key unchanged.')
    finally:
        os.umask(previous_mask)
