#!/usr/bin/env python3
"""MP: physical OVS then single-chassis OVN tests on the prepared four-port fabric.
Private Unix-socket databases; no remote listener. Restores original ports.
"""
import importlib.util
import json
import shlex
import subprocess as S
import sys
import time
import uuid

spec = importlib.util.spec_from_file_location('matrix', '/usr/local/sbin/test-fabric-matrix.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def dc(*args, check=True):
    p = S.run(m.SSH + [shlex.join(['ip', 'netns', 'exec', 'ffn-data', *args])], text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p


def main():
    state = m.control('status')
    saved = {p: state['config']['ports'][p] for p in ('p1', 'p3', 'p5', 'p13')}
    assert state['backend']['ports'] == [1, 3, 5, 13]
    revision = state['config']['revision']
    base = '/run/ffn-sdn-'+uuid.uuid4().hex[:8]
    ctl = []
    bridge = None

    def vs(*args):
        return dc('/usr/local/bin/ovs-vsctl', '--timeout=20', '--db=unix:'+base+'/ovs.sock', *args).stdout

    def nb(*args):
        return dc('/usr/local/bin/ovn-nbctl', '--timeout=20', '--db=unix:'+base+'/nb.sock', *args).stdout

    def db(name, schema):
        dc('/usr/local/bin/ovsdb-tool', 'create', base+'/'+name+'.db', schema)
        dc('/usr/local/sbin/ovsdb-server', base+'/'+name+'.db', '--remote=punix:'+base+'/'+name+'.sock',
           '--pidfile='+base+'/'+name+'.pid', '--unixctl='+base+'/'+name+'.ctl', '--detach', '--no-chdir')
        ctl.append(base+'/'+name+'.ctl')

    def frames(marker, i):
        f = b'\xff'*6+bytes.fromhex('025220abcd81')+b'\x88\xb5'+marker+bytes([i % 251])*128
        return f, f

    try:
        m.control('patch', {'revision': revision, 'ports': {
            'p1': {'mode': 'l3', 'addresses': []}, 'p5': {'mode': 'l3', 'addresses': []},
            'p3': {'mode': 'disabled'}, 'p13': {'mode': 'disabled'}}})
        revision += 1
        dc('mkdir', '-p', base, '/var/run/openvswitch')
        db('ovs', '/usr/local/share/openvswitch/vswitch.ovsschema')
        vs('--no-wait', 'init')
        dc('/usr/local/sbin/ovs-vswitchd', 'unix:'+base+'/ovs.sock', '--pidfile='+base+'/vswitchd.pid',
           '--unixctl='+base+'/vswitchd.ctl', '--log-file='+base+'/vswitchd.log', '--detach', '--no-chdir')
        ctl.append(base+'/vswitchd.ctl')
        bridge = 'br-hwtest'
        vs('add-br', bridge, '--', 'set-fail-mode', bridge, 'secure', '--',
           'add-port', bridge, 'p1', '--', 'set', 'Interface', 'p1', 'ofport_request=1', '--',
           'add-port', bridge, 'p5', '--', 'set', 'Interface', 'p5', 'ofport_request=5')
        dc('/usr/local/bin/ovs-ofctl', 'add-flow', bridge, 'priority=100,in_port=1,dl_type=0x88b5,actions=output:5')
        dc('/usr/local/bin/ovs-ofctl', 'add-flow', bridge, 'priority=0,actions=drop')
        m.probe('ovs-kernel-physical-forwarding', 1, 13, 3, frames)
        flows = dc('/usr/local/bin/ovs-ofctl', 'dump-flows', bridge).stdout
        assert 'n_packets=300' in flows, flows
        print(json.dumps({'test': 'ovs-flow-counters', 'packets': 300, 'passed': True}), flush=True)
        # skip_sw refuses a software fallback: failure is evidence, not offload.
        dc('tc', 'qdisc', 'add', 'dev', 'p1', 'clsact')
        try:
            offload = dc('tc', 'filter', 'add', 'dev', 'p1', 'ingress', 'protocol', '0x88b5',
                         'pref', '5220', 'flower', 'skip_sw', 'action', 'drop', check=False)
            print(json.dumps({'test': 'tc-hardware-only', 'accepted': offload.returncode == 0,
                              'detail': offload.stderr.strip(),
                              'hardware_offload_verified': False}), flush=True)
        finally:
            dc('tc', 'qdisc', 'del', 'dev', 'p1', 'clsact')
        vs('del-br', bridge)
        bridge = None

        db('nb', '/usr/local/share/ovn/ovn-nb.ovsschema')
        db('sb', '/usr/local/share/ovn/ovn-sb.ovsschema')
        dc('/usr/local/bin/ovn-northd', '--ovnnb-db=unix:'+base+'/nb.sock', '--ovnsb-db=unix:'+base+'/sb.sock',
           '--pidfile='+base+'/northd.pid', '--unixctl='+base+'/northd.ctl', '--detach', '--no-chdir')
        ctl.append(base+'/northd.ctl')
        vs('set', 'Open_vSwitch', '.', 'external_ids:system-id=ffn-pa5220-lab',
           'external_ids:ovn-remote=unix:'+base+'/sb.sock', 'external_ids:ovn-encap-type=geneve',
           'external_ids:ovn-encap-ip=198.18.250.1')
        dc('ip', 'address', 'add', '198.18.250.1/32', 'dev', 'lo')
        bridge = 'br-int'
        vs('add-br', bridge, '--', 'set', 'Bridge', bridge, 'fail_mode=secure', '--',
           'add-port', bridge, 'p1', '--', 'set', 'Interface', 'p1', 'external_ids:iface-id=lab1', '--',
           'add-port', bridge, 'p5', '--', 'set', 'Interface', 'p5', 'external_ids:iface-id=lab5')
        nb('ls-add', 'lab-switch')
        for p in ('lab1', 'lab5'):
            nb('lsp-add', 'lab-switch', p, '--', 'lsp-set-addresses', p, 'unknown')
        dc('/usr/local/bin/ovn-controller', 'unix:'+base+'/ovs.sock', '--pidfile='+base+'/controller.pid',
           '--unixctl='+base+'/controller.ctl', '--detach', '--no-chdir')
        ctl.append(base+'/controller.ctl')
        for _ in range(20):
            up = nb('get', 'Logical_Switch_Port', 'lab1', 'up').strip()
            if up == 'true' and nb('get', 'Logical_Switch_Port', 'lab5', 'up').strip() == 'true':
                break
            time.sleep(1)
        else:
            raise RuntimeError('OVN ports did not bind')
        m.probe('ovn-logical-switch-physical-forwarding', 1, 13, 3, frames)
        print(json.dumps({'test': 'ovn-single-chassis', 'ports_bound': True, 'passed': True}), flush=True)
    finally:
        # Stop controller before removing its bridge, then the other private daemons.
        if base+'/controller.ctl' in ctl:
            dc('/usr/local/bin/ovs-appctl', '-t', base+'/controller.ctl', 'exit', check=False)
            ctl.remove(base+'/controller.ctl')
        if bridge:
            vs('--if-exists', 'del-br', bridge)
        dc('ip', 'address', 'del', '198.18.250.1/32', 'dev', 'lo', check=False)
        for path in reversed(ctl):
            dc('/usr/local/bin/ovs-appctl', '-t', path, 'exit', check=False)
        m.control('patch', {'revision': revision, 'ports': saved})
        print(json.dumps({'test': 'sdn-original-config-restored', 'passed': True}), flush=True)


if __name__ == '__main__':
    main()
