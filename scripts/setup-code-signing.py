#!/usr/bin/env python3
"""Explicitly create once, or verify, the pinned free code-signing identity."""
import argparse
from pathlib import Path
from persistent_signing import initialize, probe_identity, verify_app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--initialize',action='store_true',help='Create only when no identity has ever been pinned')
parser.add_argument('--verify-app',type=Path,help='Verify pinned app identity without opening a private keychain')
args = parser.parse_args()
if args.verify_app:
    verify_app(args.verify_app)
    print('Verified app signature and pinned certificate identity.')
elif args.initialize:
    initialize()
else:
    probe_identity()
    print('Verified actual signing with the pinned persistent identity.')
