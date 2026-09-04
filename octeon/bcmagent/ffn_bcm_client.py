#!/usr/bin/env python3
"""ffn_bcm_client -- talk to ffn-bcmd on the CP. Runs ON THE MP.

One client used by both consumers, so the wire protocol has exactly one
implementation:

    WebUI   --HTTP--> ffn_manager --+
                                    |-- this module --JSON/TCP--> ffn-bcmd (CP)
    ffn-cli --HTTP--> ffn_manager --+

WHY ASYNC: ffn_manager is FastAPI, so its handlers run on one event loop. A
chip operation can take seconds (a `ps` walks 35 ports; a cint call compiles and
runs), and a blocking socket read inside a handler would stall EVERY other
request on the box -- the WebUI would appear hung while one BCM query ran. So
the primary API is asyncio. A blocking wrapper is provided for the CLI-shaped
callers that have no loop.

WHAT THIS DOES NOT DO: it does not retry. A BCM op either mutates hardware or
reads it, and a transparent retry on a mutation is how you enable a port twice
or race a loopback change against its own readback. Failures are returned to the
caller, which is in a position to know whether repeating is safe.
"""

import asyncio
import json
import os
import socket

DEF_HOST = os.environ.get("FFN_BCMD_HOST", "127.1.1.2")
DEF_PORT = int(os.environ.get("FFN_BCMD_PORT", "8104"))

# Chip ops are slow but bounded. This must exceed ffn-bcmd's own CMD_TIMEOUT
# (60 s) or we would give up while the daemon is still working and leave its
# reply queued for whoever connects next.
DEF_TIMEOUT = 75.0

# What the caller sees when the CP is unreachable. Shaped like a daemon reply so
# every consumer has ONE response schema to handle, rather than an exception on
# one path and a dict on the other.
def _unreachable(detail, host, port):
    return {
        "ok": False,
        "error": "bcmd unreachable",
        "detail": detail,
        "endpoint": "%s:%d" % (host, port),
        # The single most common cause, and the check that distinguishes it:
        # 127.1.1.2 is inside 127/8, so with ffnnet0 down the kernel routes it
        # to the MP's own loopback and a connection is REFUSED rather than
        # timing out -- while ping still succeeds. See ffn-cp-health.
        "hint": "check `ip route get 127.1.1.2` shows dev ffnnet0 (not lo), "
                "then that ffn-bcmd is running on the CP "
                "(`ffn-bcmd-ctl.sh status`)",
    }


async def call(req, host=DEF_HOST, port=DEF_PORT, timeout=DEF_TIMEOUT):
    """Send one request, return the reply dict. Never raises for I/O."""
    writer = None
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=10.0)
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return _unreachable("timed out after %.0fs" % timeout, host, port)
    except (OSError, ConnectionError) as exc:
        return _unreachable(str(exc), host, port)
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

    if not line:
        return _unreachable("daemon closed the connection without replying",
                            host, port)
    try:
        return json.loads(line.decode("utf-8", "replace"))
    except ValueError as exc:
        return {"ok": False, "error": "bad reply from bcmd",
                "detail": str(exc), "raw": line[:200].decode("utf-8", "replace")}


def call_sync(req, host=DEF_HOST, port=DEF_PORT, timeout=DEF_TIMEOUT):
    """Blocking version, for callers with no event loop (the CLI, scripts).

    Deliberately a plain socket rather than asyncio.run() around call(): the CLI
    may already be inside a loop, and asyncio.run() would raise there.
    """
    s = None
    try:
        s = socket.create_connection((host, port), timeout=10.0)
        s.settimeout(timeout)
        f = s.makefile("rwb")
        f.write((json.dumps(req) + "\n").encode("utf-8"))
        f.flush()
        line = f.readline()
    except (OSError, socket.timeout) as exc:
        return _unreachable(str(exc), host, port)
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    if not line:
        return _unreachable("daemon closed the connection without replying",
                            host, port)
    try:
        return json.loads(line.decode("utf-8", "replace"))
    except ValueError as exc:
        return {"ok": False, "error": "bad reply from bcmd", "detail": str(exc)}


# -- typed helpers ---------------------------------------------------------
#
# Thin on purpose: they exist so callers do not spell op names as string
# literals in a dozen places, not to add a layer of translation.

async def status(**kw):
    return await call({"op": "status"}, **kw)


async def port_list(**kw):
    return await call({"op": "port.list"}, **kw)


async def port_set(port, enable, **kw):
    return await call({"op": "port.set", "port": int(port),
                       "enable": bool(enable)}, **kw)


async def port_loopback(port, mode, name=None, **kw):
    req = {"op": "port.loopback", "port": int(port), "mode": mode}
    if name:
        req["name"] = name
    return await call(req, **kw)


async def led_status(**kw):
    return await call({"op": "led.status"}, **kw)


if __name__ == "__main__":
    # Debug entry point: ffn_bcm_client.py '{"op":"status"}'
    import sys
    req = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"op": "status"}
    print(json.dumps(call_sync(req), indent=1))
