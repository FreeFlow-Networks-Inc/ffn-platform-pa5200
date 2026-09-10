#!/usr/bin/env python3
"""Commissioning ingress packet inspection. Literal UDP/TCP payload matching.

Unsupported protocols/fragments pass with a separate counter. No stream
reassembly, TLS decryption or complete IPS enforcement is claimed.
"""
import collections
import ctypes
import fcntl
import json
import os
from pathlib import Path
import sys
import time

POLICY = Path('/etc/ffn/inspection.json')
STATUS = Path('/run/ffn-inspection.json')
LIBRARY = '/usr/local/lib/libffn-inline.so'
DEFAULT = {'revision': 0, 'mode': 'off', 'ports': [], 'literal': ''}


def validate(cfg):
    if not isinstance(cfg, dict) or set(cfg) != set(DEFAULT):
        raise ValueError('expected revision, mode, ports, literal')
    if type(cfg['revision']) is not int or cfg['revision'] < 0:
        raise ValueError('invalid revision')
    if cfg['mode'] not in ('off', 'alert', 'block'):
        raise ValueError('mode must be off, alert or block')
    if not isinstance(cfg['ports'], list) or any(type(p) is not int or p not in (1, 3, 5, 13) for p in cfg['ports']):
        raise ValueError('ports must be a list drawn from 1, 3, 5, 13')
    if len(set(cfg['ports'])) != len(cfg['ports']):
        raise ValueError('duplicate port')
    literal = cfg['literal']
    if not isinstance(literal, str) or len(literal) > 63 or any(not 32 <= ord(c) <= 126 for c in literal):
        raise ValueError('literal must contain at most 63 printable ASCII characters')
    if cfg['mode'] != 'off' and (not literal or not cfg['ports']):
        raise ValueError('enabled policy requires a literal and ports')
    return cfg


def load():
    return validate(json.loads(POLICY.read_text())) if POLICY.exists() else dict(DEFAULT)


def atomic(path, data):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(data, f)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def library(path=LIBRARY):
    lib = ctypes.CDLL(path)
    lib.ffn_inline_create.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_int]
    lib.ffn_inline_create.restype = ctypes.c_void_p
    lib.ffn_inline_destroy.argtypes = [ctypes.c_void_p]
    lib.ffn_inline_destroy.restype = None
    lib.ffn_inline_scan.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
    lib.ffn_inline_scan.restype = ctypes.c_int
    return lib


class Inspector:
    def __init__(self):
        self.lib = None
        self.handle = None
        self.cfg = dict(DEFAULT)
        self.counts = collections.Counter()
        self.next_poll = 0
        self.error = None
        self.tick()

    def tick(self):
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + 1
        try:
            cfg = load()
            if cfg != self.cfg:
                handle = None
                if cfg['mode'] != 'off':
                    if self.lib is None:
                        self.lib = library()
                    literal = cfg['literal'].encode('ascii')
                    handle = self.lib.ffn_inline_create(literal, len(literal), 1 if cfg['mode'] == 'alert' else 2)
                    if not handle:
                        raise RuntimeError('engine allocation failed')
                if self.handle:
                    self.lib.ffn_inline_destroy(self.handle)
                self.handle, self.cfg = handle, cfg
            self.error = None
        except Exception as error:
            # Retain the last successfully loaded policy; expose failure to MP.
            self.error = str(error)
        atomic(STATUS, {'pid': os.getpid(), 'updated_at': time.time(),
                        'revision': self.cfg['revision'], 'mode': self.cfg['mode'],
                        'ports': self.cfg['ports'], 'counters': dict(self.counts),
                        'reload_error': self.error})

    def allow(self, port, frame):
        if not self.handle or port not in self.cfg['ports']:
            self.counts['bypassed'] += 1
            return True
        verdict = self.lib.ffn_inline_scan(self.handle, frame, len(frame))
        name = {-2: 'unsupported_pass', -1: 'malformed_pass', 0: 'no_match', 1: 'alert', 2: 'block'}.get(verdict)
        if name is None:
            raise RuntimeError('unexpected analysis verdict')
        self.counts[name] += 1
        self.counts['port_%d_%s' % (port, name)] += 1
        return verdict != 2

    def close(self):
        if self.handle:
            self.lib.ffn_inline_destroy(self.handle)
            self.handle = None
        STATUS.unlink(missing_ok=True)


def main():
    action = sys.argv[1] if len(sys.argv) == 2 else 'status'
    with open('/run/ffn-inspection.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = load()
        if action == 'set':
            requested = validate(json.load(sys.stdin))
            if requested['revision'] != cfg['revision']:
                raise ValueError('revision conflict; fetch status first')
            if requested['mode'] != 'off':
                # Reject unavailable engine before persisting an enabled policy.
                lib = library()
                literal = requested['literal'].encode('ascii')
                h = lib.ffn_inline_create(literal, len(literal), 1 if requested['mode'] == 'alert' else 2)
                if not h:
                    raise RuntimeError('engine allocation failed')
                lib.ffn_inline_destroy(h)
            requested['revision'] += 1
            POLICY.parent.mkdir(parents=True, exist_ok=True)
            atomic(POLICY, requested)
            print(json.dumps({'accepted': requested, 'activation': 'check runtime revision in status'}))
        elif action == 'status':
            runtime = json.loads(STATUS.read_text()) if STATUS.exists() else None
            active = bool(runtime and Path('/proc', str(runtime['pid'])).exists() and
                          time.time() - runtime['updated_at'] < 5)
            print(json.dumps({'config': cfg, 'runtime': runtime, 'running': active}))
        else:
            raise ValueError('usage: ffn_inspection.py [status|set]')


if __name__ == '__main__':
    main()
