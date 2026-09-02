#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_cfgd -- the MP-side config authority, relaying south to the CP and DP.

Where this sits
---------------
The control and NGFW config daemons run on the MP and are the source of truth.
The CP and DP hold no configuration of their own; they receive it. This daemon
publishes a versioned key/value set and the agents pull it, so a node that
reboots re-converges on its own rather than needing the MP to notice and push.

Two facts about this board shape the design.

1. **MP -> CP is IP; MP -> DP is NOT.** The MP reaches the CP over the PCIe
   virtual ethernet (ffnnet0, 127.1.1.1 <-> 127.1.1.2, measured working). There
   is no ffn_dpnet interface on the CP and no IP path to the DP at all, so the
   DP leg is store-and-forward through the CP over the PCIe mailbox
   (`ffn-dpsh`). The agent handles that; this daemon just marks which keys are
   for whom.

2. **The DP has no interpreter.** It has a full busybox and nothing else -- no
   python, no perl. So the wire format is deliberately line-oriented key=value
   that a shell can parse, not JSON, and the protocol is answerable by `nc`.

Namespacing decides delivery, so one config set serves every node:

    mp.<key>    applied on the MP only
    cp.<key>    applied on the CP
    dp.<key>    relayed by the CP to the DP
    all.<key>   applied everywhere

Protocol, chosen so a busybox agent can speak it:

    -> GET\n                 client asks for the current set
    <- VERSION <n>\n         monotonic, bumped whenever the file changes
       key=value\n           ... zero or more, already filtered for the caller
       .\n                   end of set

    -> VERSION\n             cheap poll: just the version, no payload
    <- VERSION <n>\n

A client that already holds VERSION n polls until it changes, then GETs. That
keeps the steady-state cost to one line each way.
"""
import argparse
import os
import socket
import socketserver
import threading

DEFAULT_CONF = "/etc/ffn/config.env"
DEFAULT_BIND = "127.1.1.1"
DEFAULT_PORT = 7420

_lock = threading.Lock()
_state = {"version": 0, "mtime": None, "pairs": []}


def load(path):
    """Re-read the config file if it changed, and bump the version if so.

    Version is derived from content changes rather than a counter held in
    memory, so restarting the daemon does not make agents think the config
    regressed.
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    sig = (st.st_mtime_ns, st.st_size)
    with _lock:
        if sig == _state["mtime"]:
            return
        pairs = []
        try:
            with open(path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    pairs.append(line)
        except OSError:
            return
        _state["mtime"] = sig
        _state["pairs"] = pairs
        _state["version"] += 1


def visible_to(pairs, scope):
    """Keys this caller should receive.

    The CP asks for scope "cp" and gets cp.* and all.*, plus every dp.* key --
    it cannot apply those, but it is the only route to the DP, so it must carry
    them. Delivery and applicability are different questions.
    """
    out = []
    for p in pairs:
        k = p.split("=", 1)[0]
        if k.startswith("all."):
            out.append(p)
        elif scope == "cp" and (k.startswith("cp.") or k.startswith("dp.")):
            out.append(p)
        elif scope == "dp" and k.startswith("dp."):
            out.append(p)
        elif scope == "mp" and k.startswith("mp."):
            out.append(p)
    return out


class Handler(socketserver.StreamRequestHandler):
    timeout = 20

    def handle(self):
        try:
            line = self.rfile.readline(256).decode("ascii", "replace").strip()
        except Exception:
            return
        if not line:
            return
        parts = line.split()
        cmd = parts[0].upper()
        scope = parts[1].lower() if len(parts) > 1 else "cp"

        load(self.server.conf_path)
        with _lock:
            ver = _state["version"]
            pairs = list(_state["pairs"])

        if cmd == "VERSION":
            self.wfile.write(b"VERSION %d\n" % ver)
            return
        if cmd == "GET":
            self.wfile.write(b"VERSION %d\n" % ver)
            for p in visible_to(pairs, scope):
                self.wfile.write(p.encode("utf-8") + b"\n")
            self.wfile.write(b".\n")
            return
        self.wfile.write(b"ERR unknown command\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conf", default=DEFAULT_CONF)
    ap.add_argument("--bind", default=DEFAULT_BIND)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()

    if not os.path.exists(a.conf):
        os.makedirs(os.path.dirname(a.conf), exist_ok=True)
        with open(a.conf, "w") as fh:
            fh.write("# FFN config -- namespaced mp./cp./dp./all.\n")
    load(a.conf)

    srv = Server((a.bind, a.port), Handler)
    srv.conf_path = a.conf
    print("ffn_cfgd on %s:%d serving %s (version %d, %d keys)"
          % (a.bind, a.port, a.conf, _state["version"], len(_state["pairs"])),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
