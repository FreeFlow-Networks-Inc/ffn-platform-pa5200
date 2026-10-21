#!/usr/bin/env python3
"""The WAN port's packet owner is ffn-portd (octeon/native/ffn_portd.c).

This entry point keeps the MP's command line: `status`, `start` and `stop`
with the DP boot identity on stdin. Each execs `ffn-portd physical <verb>
--port 1`, a client of the daemon that owns every port from boot (`ffn-portd
serve`, management/PORT-TABLE.md); nothing runs in Python here.
"""
import json
import os
import sys

DAEMON=os.environ.get('FFN_PORTD','/usr/local/sbin/ffn-portd')


def main(verb,port=1):
    if verb not in ('status','start','stop'):raise SystemExit('operation status|start|stop required')
    if not os.access(DAEMON,os.X_OK):
        print(json.dumps({'error':'ffn-portd is not installed on this dataplane'}),flush=True);raise SystemExit(1)
    os.execv(DAEMON,[DAEMON,'physical',verb,'--port',str(port)])


if __name__=='__main__':main(sys.argv[1] if len(sys.argv)>1 else '')
