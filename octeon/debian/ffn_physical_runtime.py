#!/usr/bin/env python3
"""One physical port's packet owner is ffn-portd; this keeps the MP's command line.

`status|start|stop <port>` with the DP boot identity on stdin, `serve <port>`
for the ffn-physical@ unit: each execs `ffn-portd physical <verb> --port N`.
Port 1 keeps its WAN owner (ffn_wan_runtime.py); copper ports 2..4 are
faceplate ports like the optical ones, their carrier following the CP's
observation through the owner's link lease.
"""
import sys
from ffn_wan_runtime import main

if __name__=='__main__':
    if len(sys.argv)!=3 or not sys.argv[2].isdigit() or not 2<=int(sys.argv[2])<=24:raise SystemExit('operation and port 2..24 required')
    main(sys.argv[1],int(sys.argv[2]))
