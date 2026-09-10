#!/usr/bin/env python3
"""DP kernel/control tests in a disposable namespace, without physical ports."""
import json
from pathlib import Path
import tempfile
import fcntl
import ffn_network as net


def main():
    with open('/run/ffn-network.lock', 'w') as lock, tempfile.TemporaryDirectory(prefix='ffn-router-test-') as tmp:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        net.NS = 'ffn-router-test'
        net.STATE = Path(tmp)/'network.json'
        assert not net.exists(), 'test namespace already exists'
        cfg = {'revision':0, 'vrfs':{'vrf-blue':1101,'vrf-red':1102}, 'ports':{
            'p1':{'mode':'l3','vrf':'vrf-blue','addresses':['192.0.2.1/24','2001:db8:1::1/64']},
            'p3':{'mode':'l3','vrf':'vrf-red','addresses':['192.0.2.1/24','2001:db8:1::1/64']},
            'p5':{'mode':'l3','vrf':'vrf-blue','addresses':['198.18.105.1/24','fd52:20:105::1/64']}},
            'routes':[
                {'dst':'0.0.0.0/0','table':1101,'nexthops':[
                    {'dev':'p5','via':'198.18.105.2','weight':1},
                    {'dev':'p5','via':'198.18.105.3','weight':2}]},
                {'dst':'::/0','table':1101,'nexthops':[
                    {'dev':'p5','via':'fd52:20:105::2','weight':1},
                    {'dev':'p5','via':'fd52:20:105::3','weight':2}]},
                {'dst':'203.0.113.0/24','table':1102,'type':'blackhole'},
                {'dst':'2001:db8:ff::/64','table':1102,'type':'blackhole'}]}
        net.validate(cfg)
        try:
            net.start(cfg); net.save(cfg)
            print(json.dumps({'test':'two-vrfs-overlapping-ipv4-ipv6-addresses','passed':True}),flush=True)
            for family in ('-4','-6'):
                routes = json.loads(net.ip(family,'-j','route','show','table','1101'))
                default = next(r for r in routes if r['dst']=='default' and r.get('metric')==100)
                assert len(default['nexthops'])==2
                assert [h['weight'] for h in default['nexthops']] == [1,2]
                print(json.dumps({'test':family+'-weighted-ecmp-kernel-install','passed':True}),flush=True)
            for family, src, dst in (('-4','192.0.2.2','203.0.113.2'),
                                     ('-6','2001:db8:1::2','2001:db8:ff::2')):
                args = (family,'-j','route','get',dst,'from',src,'iif','p1')
                before = json.loads(net.ip(*args))
                assert before[0]['dev']=='p5' and str(before[0].get('table'))=='1101', before
                source = '192.0.2.0/24' if family=='-4' else '2001:db8:1::/64'
                policy = {'from':source,'iif':'p1','table':1102,'priority':101}
                cfg = net.patch(cfg,{'revision':cfg['revision'],'rules':[policy]})['config']
                try:
                    net.ip(*args)
                except RuntimeError as error:
                    assert 'Invalid argument' in str(error), str(error)
                else:
                    raise AssertionError('policy did not select the blackhole table')
                cfg = net.patch(cfg,{'revision':cfg['revision'],'rules':[]})['config']
                assert str(json.loads(net.ip(*args))[0]['table'])=='1101'
                print(json.dumps({'test':family+'-source-policy-selection-and-removal','passed':True}),flush=True)
            # Exercise controller removal, including unreachable sentinel routes.
            disabled = {p:{'mode':'disabled'} for p in cfg['ports']}
            cfg = net.patch(cfg,{'revision':cfg['revision'],'ports':disabled,'routes':[],'vrfs':{}})['config']
            assert not any(i['ifname'].startswith('vrf-') for i in json.loads(net.ip('-j','link')))
            print(json.dumps({'test':'vrf-route-policy-cleanup','passed':True}),flush=True)
        finally:
            if net.exists():
                net.run('ip','netns','delete',net.NS)


if __name__=='__main__':
    main()
