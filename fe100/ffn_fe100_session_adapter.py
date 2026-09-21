#!/usr/bin/env python3
"""FFN session adapters for an initialized, hash-pinned FE100 owner service.

Importing this module opens no device and loads no vendor code. The owner
service supplies a bounded `call(operation, native_entry)` endpoint, already
holding the FE100 table lock for the entire SessionManager transaction. The
64-byte FFN wire entry is never passed as the native C union.
"""
import ctypes as C
from ffn_fe100_sessions import entry4, readiness, validate_entry4

OWNER_SHA256 = 'b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9'
NATIVE_SIZE = 144
KEY_OFFSET, STATE_OFFSET, NAT_OFFSET = 16, 32, 64
SUCCESS, NOT_FOUND = 0, 3  # condor status returned by native hash lookup


class NativeEntry(C.Union):
    _fields_ = [('bytes', C.c_ubyte * NATIVE_SIZE), ('align', C.c_uint64 * 18)]


def validate_entry(wire):
    return validate_entry4(wire)


def encode_native(wire):
    wire = validate_entry(wire)
    # DWARF pan_fe100_flow4_entry_t: hlink16, key16, state32, NAT...
    out = bytearray(NATIVE_SIZE)
    out[KEY_OFFSET:KEY_OFFSET+16] = wire[:16]
    out[STATE_OFFSET:STATE_OFFSET+32] = wire[16:48]
    if int.from_bytes(wire[16:20],'big') & (3<<29):
        # Native v4 NAT: sport, dport, saddr, daddr (12 bytes). The union's
        # inactive v6 tail and host-only fields must remain zero on writes.
        out[NAT_OFFSET:NAT_OFFSET+12]=wire[56:60]+wire[48:56]
    return bytes(out)


def decode_native(native, expected_key):
    native = bytes(native)
    if len(native) != NATIVE_SIZE or len(expected_key) != 16:
        raise ValueError('incorrect FE100 native entry size')
    if native[KEY_OFFSET:KEY_OFFSET+16] != expected_key:
        raise RuntimeError('FE100 lookup returned a different key')
    wire = native[KEY_OFFSET:KEY_OFFSET+16] + native[STATE_OFFSET:STATE_OFFSET+32] + bytes(16)
    # Inactive NAT storage is unspecified on readback. Active IPv4 NAT has
    # exactly 12 meaningful bytes; do not adopt the inactive IPv6 union tail.
    mode=(int.from_bytes(native[STATE_OFFSET:STATE_OFFSET+4],'big')>>29)&3
    if mode==3:raise RuntimeError('FE100 IP version translation is unsupported')
    if mode:
        wire=wire[:48]+native[NAT_OFFSET+4:NAT_OFFSET+12]+native[NAT_OFFSET:NAT_OFFSET+4]+bytes(4)
    return validate_entry(wire)


class NativeSessionAdapter:
    """Concrete wire/native adapter implementing SessionManager's backend.

    Transport status must report the owner ABI hash, initialized device0,
    exclusive lock, and a bounded call implementation. Calls return
    `(native_status, 144-byte native entry)`; timeouts/errors propagate as
    ambiguous writes so SessionManager journals/reconciles them by lookup.
    Production qualification is checked afresh, never cached at import time.
    """
    def __init__(self, transport, health):
        self.transport, self.health = transport, health

    def _check_transport(self):
        s = self.transport.status()
        if s.get('owner_sha256') != OWNER_SHA256 or s.get('device') != 0:
            raise RuntimeError('unrecognized FE100 owner ABI/device')
        for key in ('initialized', 'exclusive', 'bounded'):
            if s.get(key) is not True:
                raise RuntimeError('FE100 session transport requires '+key)

    def readiness(self):
        try:
            self._check_transport()
        except RuntimeError as e:
            return [str(e)]
        health = self.health()
        return readiness(health['summary'], health.get('physical_transport_verified'))

    def _call(self, op, wire):
        self._check_transport()
        rc, result = self.transport.call(op, encode_native(wire))
        if type(rc) is not int or len(result) != NATIVE_SIZE:
            raise RuntimeError('malformed FE100 session response')
        if rc != SUCCESS and not (op == 'fetch' and rc == NOT_FOUND):
            raise RuntimeError('FE100 '+op+' failed: '+str(rc))
        return rc, result

    def fetch(self, key):
        rc, result = self._call('fetch', entry4(key, 0))
        return None if rc == NOT_FOUND else decode_native(result, bytes(key))

    def insert(self, wire):
        reasons = self.readiness()
        if reasons:
            raise RuntimeError('; '.join(reasons))
        wire = validate_entry(wire)
        if int.from_bytes(wire[16:20],'big') & (3<<29):
            if self.health().get('nat_offload_verified') is not True:
                raise RuntimeError('FE100 NAT packet forwarding is not qualified')
        identity = entry4(wire[:16], int.from_bytes(wire[36:40], 'big'))
        # Sysroot pan_fe100_insert_flow_entry sends FLOWADD15 with only key
        # and flow ID. FLOWUPDATE16 carries state/NAT. ADD success must never
        # be mistaken for forwarding-action installation.
        self._call('insert', identity)
        if wire != identity:
            self._call('update', wire)

    def delete(self, key):
        # The native delete uses state.flowid as well as the key. Fetch it;
        # constructing an all-zero flow ID from the key deletes incorrectly.
        wire = self.fetch(key)
        if wire is not None:
            self._call('delete', wire)


class CtypesOwnerEndpoint:
    """Bridge inside an already initialized big-endian owner process.

    The embedding service supplies `invoke(fn, entry)` with its watchdog and
    lock lifetime. This class deliberately cannot dlopen a fresh owner and
    assume its process-local device pointers have been initialized.
    """
    def __init__(self, library, state, invoke):
        import sys
        if sys.byteorder != 'big' or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError('native FE100 ABI requires big-endian MIPS64')
        self._state, self._invoke = state, invoke
        self._functions = {}
        for op in ('fetch', 'insert', 'update', 'delete'):
            fn = getattr(library, 'pan_fe100_'+op+'_flow_entry')
            fn.argtypes = [C.c_uint32, C.POINTER(NativeEntry)]
            fn.restype = C.c_int
            self._functions[op] = fn

    def status(self):
        return self._state()

    def call(self, operation, native):
        if operation not in self._functions or len(native) != NATIVE_SIZE:
            raise ValueError('invalid native session operation')
        entry = NativeEntry.from_buffer_copy(native)
        result = self._invoke(self._functions[operation], entry)
        return result, bytes(entry.bytes)
