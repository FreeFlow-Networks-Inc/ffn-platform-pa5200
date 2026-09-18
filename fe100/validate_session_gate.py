#!/usr/bin/env python3
"""Confirm an expected live prerequisite blocks session register writes."""
import argparse
import json
from pathlib import Path
from ffn_fe100_live_sessions import LiveSessions, ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-blocker',required=True)
    args=parser.parse_args()
    before=set(ROOT.glob('session-io-*.txt'))
    error=None
    try:
        LiveSessions(writable=True)
    except RuntimeError as e:
        error=str(e)
    traces=set(ROOT.glob('session-io-*.txt'))-before
    writes=[line for f in traces for line in f.read_text().splitlines() if line.startswith('W ')]
    passed=(error is not None and args.expect_blocker in error and
            len(traces)==1 and not writes)
    print(json.dumps({'schema':1,'cp_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'activation_rejected_without_writes':passed,'reason':error,
        'expected_blocker':args.expect_blocker,
        'writes':writes,'traces':[str(f) for f in traces],'session_offload_verified':False},indent=2))
    raise SystemExit(0 if passed else 1)


if __name__=='__main__': main()
