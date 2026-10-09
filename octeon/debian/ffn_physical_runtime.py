#!/usr/bin/env python3
"""One physical TAP owner per MP-selected faceplate port; no carrier test."""
import json
import sys
import ffn_wan_runtime as runtime

if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('operation and port required')
    runtime.select_physical(int(sys.argv[2]))
    if sys.argv[1]=='serve':runtime.serve()
    else:print(json.dumps(runtime.execute(sys.argv[1],json.load(sys.stdin))))
