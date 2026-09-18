#!/usr/bin/env python3
"""FE100 IPv4 flow wire encoding and transactional session lifecycle.

Wire layout: local owner pcs/packets/fe100.py flowKey/flowEntry. This is
NOT the layout of pan_fe100_flow_entry_t passed to the C library. No C ABI
cast, register writer or automatic activation is provided by this module.
"""
import ipaddress
import struct


def uint(value, bits, name):
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError('invalid '+name)
    return value


def key4(src, dst, sport, dport, protocol, zone):
    if protocol not in (6,17) or type(protocol) is not int:
        raise ValueError('only established TCP or UDP flows are eligible')
    a, b = ipaddress.IPv4Address(src), ipaddress.IPv4Address(dst)
    if a.is_multicast or b.is_multicast or a.is_unspecified or b.is_unspecified:
        raise ValueError('unicast addresses required')
    return struct.pack('!BBHHH4s4s', 0x40, protocol, uint(zone,16,'zone'),
        uint(sport,16,'source port'), uint(dport,16,'destination port'), a.packed,b.packed)


def entry4(key, flow_id):
    if len(key) != 16 or key[0] != 0x40 or key[1] not in (6,17):
        raise ValueError('invalid IPv4 key')
    # Plain flow identity with all forwarding overrides/NAT/rewrites unset.
    # Owner diagnostic insert uses the key and state.flowid only. Actual
    # forwarding actions need separate, verified programming and packet tests.
    return key + bytes(20) + struct.pack('!I',uint(flow_id,32,'flow ID')) + bytes(24)


def forwarding_entry4(key, flow_id, next_hop=None, *, drop=False, decrement_ttl=False):
    """Audited FE100 state subset; programming still requires qualification.

    VM sysroot libpandp_cp DWARF condor_flow_state_t and pdt/fe100.py
    _update_fe100: CT31, TTL28, DROP27, NH26; override at state+6.
    NAT, QoS, recirculation and unreviewed flags are deliberately unavailable.
    Next-hop provisioning/ownership is the caller's responsibility.
    """
    if type(drop) is not bool or type(decrement_ttl) is not bool:
        raise ValueError('action flags must be boolean')
    if drop:
        if next_hop is not None or decrement_ttl:
            raise ValueError('drop cannot include forwarding actions')
        flags, override = 1 << 27, 0
    else:
        override = uint(next_hop, 16, 'next-hop index')
        flags = (1 << 31) | (1 << 26) | (decrement_ttl << 28)
    wire = bytearray(entry4(key, flow_id))
    struct.pack_into('!II', wire, 16, flags, override)
    return bytes(wire)


def validate_entry4(wire):
    wire = bytes(wire)
    if len(wire) != 64:
        raise ValueError('invalid flow entry length')
    key, flow_id = wire[:16], int.from_bytes(wire[36:40], 'big')
    identity = entry4(key, flow_id)
    if wire == identity:
        return wire
    flags, override = struct.unpack_from('!II', wire, 16)
    if flags == 1 << 27:
        expected = forwarding_entry4(key, flow_id, drop=True)
    elif flags in ((1 << 31) | (1 << 26), (1 << 31) | (1 << 26) | (1 << 28)):
        expected = forwarding_entry4(key, flow_id, override, decrement_ttl=bool(flags & (1 << 28)))
    else:
        raise ValueError('unsupported flow actions')
    if wire != expected:
        raise ValueError('unsupported flow state')
    return wire


def owned_variants(entry):
    # Native FLOWADD precedes FLOWUPDATE. The durable desired entry owns the
    # intermediate identity with the same key AND flow ID after a lost reply.
    validate_entry4(entry)
    return (entry, entry4(entry[:16],int.from_bytes(entry[36:40],'big')))


def readiness(summary, transport_verified):
    reasons = []
    if transport_verified is not True:
        reasons.append('DP physical packet transport is not qualified')
    clocks = summary.get('external_clock_status', {})
    if any(clocks.get(k) != 1 for k in ('dram_ddr_clk','dram_pclk','tcam_2x_clk','tcam_1x_clk')):
        reasons.append('FE100 external lookup clocks are not ready')
    if summary.get('offload_verified') is not True:
        reasons.append('FE100 session forwarding has not passed packet validation')
    if summary.get('mode_faults_or_pauses') or any(f.get('current') is True for f in summary.get('flow_control_flags',[])):
        reasons.append('FE100 lookup pipeline reports a fault, pause or flow control')
    return reasons


class SessionManager:
    """Single-owner lifecycle for a qualified hardware adapter.

    Backend contract: readiness() -> reasons; fetch(key) -> bytes or None;
    insert(entry), delete(key). Calls must finish only after acknowledged
    completion. Ambiguous writes are resolved by fetch, never assumed absent.
    Owner must serialize calls. Supply a Journal for durable write intent;
    omitting it is intended for isolated tests only. On restart call recover()
    under the hardware adapter's table lock before allowing new installs.
    ffn_fe100_session_adapter implements the native ABI boundary; its endpoint
    must still pass live hardware qualification before production installs.
    """
    def __init__(self, backend, journal=None):
        self.backend = backend
        self.journal = journal
        self.sessions = journal.load() if journal else {}
        # A restart never adopts stale sessions as trusted hardware policy.
        self.recovery_required = bool(self.sessions)

    def _save(self, ident, value):
        # Commit intent before performing hardware I/O. Storage failures stop
        # programming; they must never leave an unjournaled hardware write.
        try:
            if self.journal: self.journal.put(ident, value)
        except BaseException:
            self.recovery_required = True
            raise
        self.sessions[ident] = value

    def _forget(self, ident):
        try:
            if self.journal: self.journal.delete(ident)
        except BaseException:
            self.recovery_required = True
            raise
        self.sessions.pop(ident, None)

    def recover(self):
        """Remove exact journal-owned entries; retain conflicts for inspection.

        The adapter must hold the FE100 table lock. No flush-all command and
        no overwriting someone else's flow. Failed recovery blocks installs.
        """
        self.recovery_required = True
        for ident in list(self.sessions):
            self.remove(ident)
        self.recovery_required = False

    def install(self, session_id, entries, revision):
        uint(session_id,32,'session ID'); uint(revision,64,'policy revision')
        if self.recovery_required:
            raise RuntimeError('offload recovery required')
        reasons = self.backend.readiness()
        if reasons: raise RuntimeError('; '.join(reasons))
        if session_id in self.sessions or len(entries) != 2:
            raise ValueError('a new session requires two directional entries')
        entries = tuple(bytes(e) for e in entries)
        if any(len(e) != 64 for e in entries) or entries[0][:16] == entries[1][:16]:
            raise ValueError('invalid bidirectional entries')
        for entry in entries:
            validate_entry4(entry)
        a, b = entries[0][:16], entries[1][:16]
        if a[:4] != b[:4] or a[4:6] != b[6:8] or a[6:8] != b[4:6] or a[8:12] != b[12:16] or a[12:16] != b[8:12]:
            raise ValueError('directional keys must reverse the same zone and tuple')
        # Preflight BOTH keys before touching either direction.
        for entry in entries:
            if self.backend.fetch(entry[:16]) is not None:
                raise RuntimeError('flow key is already owned')
        self._save(session_id, {'entries':entries, 'revision':revision, 'state':'installing'})
        touched = []
        try:
            for entry in entries:
                touched.append(entry)
                self.backend.insert(entry)
                if self.backend.fetch(entry[:16]) != entry:
                    raise RuntimeError('flow readback mismatch')
        except BaseException:
            for entry in reversed(touched):
                try:
                    actual = self.backend.fetch(entry[:16])
                    if actual is not None:
                        if actual not in owned_variants(entry): raise RuntimeError('flow ownership changed')
                        self.backend.delete(entry[:16])
                    if self.backend.fetch(entry[:16]) is not None:
                        raise RuntimeError('flow removal not confirmed')
                except Exception:
                    self.recovery_required = True
            if self.recovery_required:
                self._save(session_id, {'entries':entries, 'revision':revision, 'state':'unknown'})
            else:
                self._forget(session_id)
            raise
        try:
            self._save(session_id, {'entries':entries, 'revision':revision, 'state':'installed'})
        except BaseException:
            self.recovery_required = True
            raise

    def remove(self, session_id):
        session = self.sessions[session_id]
        try:
            session = {**session, 'state':'removing'}
            self._save(session_id, session)
            for entry in session['entries']:
                actual = self.backend.fetch(entry[:16])
                if actual is not None:
                    if actual not in owned_variants(entry): raise RuntimeError('flow ownership changed')
                    self.backend.delete(entry[:16])
                if self.backend.fetch(entry[:16]) is not None:
                    raise RuntimeError('flow removal not confirmed')
        except BaseException:
            session['state'] = 'unknown'
            self.recovery_required = True
            self._save(session_id, session)
            raise
        try:
            self._forget(session_id)
        except BaseException:
            self.recovery_required = True
            raise

    def invalidate_policy(self, revision):
        uint(revision,64,'policy revision')
        for ident, session in list(self.sessions.items()):
            if session['revision'] != revision:
                self.remove(ident)
