#!/usr/bin/env python3
"""Root-only isolated kernel LACP negotiation test; never uses physical ports."""
import json
import os
import subprocess as s
import time
from unittest.mock import patch
import ffn_lacp as lacp


def main():
    names = ['ffn-lacp-test-%d-%s' % (os.getpid(), side) for side in ('a','b')]
    made = []
    original_ns = lacp.net.NS
    try:
        for ns in names:
            s.run(['ip','netns','add',ns],check=True); made.append(ns)
        for i in (1,3):
            s.run(['ip','link','add','p%d'%i,'netns',names[0],'type','veth',
                   'peer','name','p%d'%i,'netns',names[1]],check=True)
        for index, ns in enumerate(names):
            lacp.net.NS = ns
            links = {p['ifname']:p for p in json.loads(lacp.net.ip('-j','link'))}
            g = {'members':['p1','p3'],'activity':'active','rate':'fast','hash':'layer2+3',
                 'min_links':1,'system_priority':32768,
                 'network':{'mode':'l3','addresses':['192.0.2.%d/24'%(index+1)]}}
            # This test's isolated veth pair deliberately substitutes for the
            # physical preflight. Production rejects virtual member devices.
            with patch.object(lacp,'preflight',return_value=links):
                lacp.activate('lag1',g)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            ready=True
            for ns in names:
                lacp.net.NS=ns
                info=lacp.runtime()['lag1']['bonding']
                ready &= 'Number of ports: 2' in info and 'Partner Mac Address: 00:00:00:00:00:00' not in info
            if ready: break
            time.sleep(1)
        else: raise RuntimeError('LACP peers did not forward within 30 seconds')
        s.run(['ip','netns','exec',names[0],'ping','-c','3','-W','2','192.0.2.2'],check=True,capture_output=True)
        states=[]
        for ns in names:
            lacp.net.NS=ns
            state=lacp.runtime()['lag1']
            if 'Number of ports: 2' not in state['bonding']:
                raise RuntimeError('both members did not aggregate: '+state['bonding'])
            states.append(state)
        s.run(['ip','-n',names[0],'link','set','p1','down'],check=True)
        time.sleep(2)
        s.run(['ip','netns','exec',names[0],'ping','-c','3','-W','2','192.0.2.2'],check=True,capture_output=True)
        print(json.dumps({'negotiated_members':2,'ipv4_forwarding':True,'single_member_failover':True,
                          'physical_ports_used':False,'states':states},indent=2))
    finally:
        lacp.net.NS=original_ns
        for ns in reversed(made):
            s.run(['ip','netns','delete',ns],check=True)


if __name__=='__main__': main()
