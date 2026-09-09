#!/usr/bin/env python3
"""MP: send one structured BCM request, supplied as JSON on stdin."""
import json
import socket
import sys

request = json.load(sys.stdin)
with socket.create_connection(("127.1.1.2", 8104), timeout=10) as sock:
    sock.settimeout(90)
    with sock.makefile("rwb") as stream:
        stream.write((json.dumps(request) + "\n").encode())
        stream.flush()
        response = json.loads(stream.readline())
print(json.dumps(response, indent=2))
sys.exit(0 if response.get("ok") else 1)
