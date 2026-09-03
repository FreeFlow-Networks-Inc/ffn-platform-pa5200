#!/usr/bin/env python3
"""Interop: the Python MP client against the real C DP over a shared mmap.

Two implementations passing their own unit tests proves nothing about whether
they agree with each other. This drives the actual C dp_service_commands()
loop and checks the DP's answers.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, '/opt/ffn-ngfw-v2')
import ffn_dpring as D

SERVE = '/opt/ffn-ngfw-v2/octeon-dp/dp_serve'
REGION = os.path.join(tempfile.mkdtemp(), 'region.bin')
SIZE = D.OFF_BANK0 + 65536

fails = []


def chk(c, m):
    print(("  ok   " if c else "  FAIL ") + m)
    if not c:
        fails.append(m)


with open(REGION, 'wb') as f:
    f.write(bytes(SIZE))

# The C side lays down the header (fresh=1), so start it first.
srv = subprocess.Popen([SERVE, REGION, '6'], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
time.sleep(0.4)

r = D.DpRing.open_file(REGION, SIZE)
try:
    h = r.handshake()
    chk(True, 'handshake against the real DP: ABI v%d' % h['abi_version'])
    caps = h['dp_caps']
    print('  dp_caps = 0x%x (PORT_CTL=%s PORT_HW=%s)'
          % (caps, bool(caps & D.CAP_PORT_CTL), bool(caps & D.CAP_PORT_HW)))

    # PING is the simplest proof the C loop is reading our descriptors.
    chk(r.ping(), 'the DP answered PING with a matching token')

    if not caps & D.CAP_PORT_CTL:
        print('  NOTE: DP does not advertise PORT_CTL; sending port commands '
              'directly to exercise the handlers')
        push = r.push
    else:
        push = None

    # Configure the 5220's own complement on one Octeon: 2 RJ-45, 8 SFP+,
    # 2 QSFP+ = 12 ports. The QSFP+ pair is modelled as HSCI (the HA2/HA3
    # high-speed chassis interconnect), which is 40 G Ethernet on a QSFP+
    # cage -- not Interlaken: this chip has no ilk* CSRs and every live
    # GSERn_CFG has the ILA bit clear.
    plan = ([('RJ45', 0, 1000, 'data')] * 2 +
            [('SFP+', 3, 10000, 'data')] * 8 +
            [('QSFP+', 4, 40000, 'HSCI')] * 2)
    for i, (ptype, lmac_type, speed, role) in enumerate(plan):
        cfg = D._pack_cfg(D.PORT_TYPE_ID[ptype.lower()], lmac_type,
                          i & 0xFF, 1, 0xFF, D.F_HAS_LMAC,
                          D.PORT_ROLE_ID[role.lower()])
        r.push(D.CMD_PORT_CONFIG, i, cfg, D._pack_a2(speed, 1500))
    time.sleep(0.3)

    ports = r.ports()
    chk(len(ports) == 12, 'DP created 12 port entries (%d)' % len(ports))
    kinds = {}
    for p in ports:
        kinds[p['port_type']] = kinds.get(p['port_type'], 0) + 1
    chk(kinds.get('RJ45') == 2 and kinds.get('SFP+') == 8
        and kinds.get('QSFP+') == 2,
        'complement is 2 RJ-45 / 8 SFP+ / 2 QSFP+ (%s)' % kinds)
    chk(all(p['state'] == 'powerdown' for p in ports),
        'every port starts in POWERDOWN, none auto-started')
    # role axis survived the MP -> DP -> MP round trip
    hsci = [p for p in ports if p['role'] == 'HSCI']
    chk(len(hsci) == 2, 'two HSCI ports round-tripped (%d)' % len(hsci))
    chk(all(p['port_type'] == 'QSFP+' for p in hsci),
        'HSCI ports report the QSFP+ form factor, role kept separate')
    chk(all(not p['bridgeable'] for p in hsci),
        'HSCI ports are reported as NOT bridgeable')
    chk(all(p['bridgeable'] for p in ports if p['role'] == 'data'),
        'data ports are reported as bridgeable')

    # the safety property, across the real boundary
    r.drain()
    r.push(D.CMD_PORT_ADMIN, 10, 1)      # an HSCI port
    r.push(D.CMD_PORT_ADMIN, 2, 1)       # a data port
    time.sleep(0.3)
    chk(r.port(10)['admin_up'] is False,
        'the DP refuses admin-up on an HSCI port')
    chk(r.port(2)['admin_up'] is True,
        'the DP still allows admin-up on a data port')
    errs = [e for e in r.drain() if e['op'] == D.EVT_ERROR]
    chk(any(e['a1'] == 10 for e in errs),
        'the HSCI refusal came back as an error event')

    sfp = [p for p in ports if p['port_type'] == 'SFP+'][0]
    chk(sfp['lmac_type'] == '10G-R', 'SFP+ carries LMAC_TYPE 10G-R')
    qsfp = [p for p in ports if p['port_type'] == 'QSFP+'][0]
    chk(qsfp['lmac_type'] == '40G-R' and qsfp['speed_mbps'] == 40000,
        'QSFP+ carries LMAC_TYPE 40G-R at 40000 Mbps')

    r.drain()
    r.push(D.CMD_PORT_ADMIN, 0, 1)
    time.sleep(0.3)
    p0 = r.port(0)
    chk(p0['admin_up'] is True, 'admin-up applied by the DP')
    chk(p0['state'] == 'startup',
        'DP stops at STARTUP without CVMX rather than claiming RUN')
    evs = r.drain()
    chk(any(e['op'] == D.EVT_PORT_LINK for e in evs),
        'DP emitted PORT_LINK (%s)' % [e['name'] for e in evs])

    r.push(D.CMD_PORT_ENUM)
    time.sleep(0.3)
    evs = r.drain()
    n = sum(1 for e in evs if e['op'] == D.EVT_PORT_INFO)
    chk(n == 12, 'PORT_ENUM returned 12 PORT_INFO events (%d)' % n)

    # the state word the DP packs must decode with our unpacker
    info = [e for e in evs if e['op'] == D.EVT_PORT_INFO][0]
    st = D._unpack_state(info['a1'])
    chk(st['port_type'] in D.PORT_TYPE.values(),
        'DP state word decodes with the MP unpacker (%s/%s)'
        % (st['port_type'], st['state']))

    print()
    print(D._fmt_ports(r.ports()))
    print()

    r.push(D.CMD_SHUTDOWN)
    time.sleep(0.3)
finally:
    r.close()
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill()
    out = srv.stdout.read() if srv.stdout else ''
    print('  [dp_serve] ' + ' | '.join(out.split('\n')[:3]).strip())

print()
print('==== MP<->DP interop: %d failed ====' % len(fails))
sys.exit(1 if fails else 0)
