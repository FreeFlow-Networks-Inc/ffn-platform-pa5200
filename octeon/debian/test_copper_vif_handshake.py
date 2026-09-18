#!/usr/bin/env python3
"""Root-only driver RPC/carrier test on an isolated dummy packet trunk.

The production event loop and TAP ioctls run unchanged. Only the physical
trunk prerequisite and inspection engine are replaced for this namespace.
"""
import builtins
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import ffn_network as network
import ffn_vif_runtime as runtime
import ffn_dp_packet_transport as transport
import ffn_inspection as inspection
from ffn_copper_vif import LEASE_SECONDS, CopperVif
from test_copper_vif import profile, observation


class NoInspection:
    def tick(self):pass
    def allow(self,*args):return True
    def close(self):pass


def main():
    network.NS='ffn-cuv-rpc-'+str(os.getpid())
    network.run('ip','netns','add',network.NS)
    worker=None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            network.STATE=Path(tmp)/'network.json'
            runtime.SOCKET=tmp+'/control.sock'
            runtime.open=lambda path,*args:builtins.open(tmp+'/'+Path(path).name,*args)
            transport.validate_trunk=lambda _:None
            inspection.Inspector=NoInspection
            network.ip('link','add','cuvwire','type','dummy')
            network.ip('link','set','cuvwire','up')
            backend=runtime.Linux()
            owner=runtime.Owner(backend,{2},Path(tmp)/'vifs.json',Path(tmp)/'pending',copper=CopperVif(profile()))
            owner.replace({'revision':0,'vifs':{'fv4002':{'port':2,'vlan':None,'enabled':True,
                'network':{'mode':'l3','addresses':[]}}}})
            def child():
                fd=os.open('/run/netns/'+network.NS,os.O_RDONLY)
                os.setns(fd,0);os.close(fd)
                runtime.serve(owner,'cuvwire',LEASE_SECONDS+6)
            worker=multiprocessing.get_context('fork').Process(target=child);worker.start()
            deadline=time.monotonic()+5
            while not Path(runtime.SOCKET).exists():
                if time.monotonic()>deadline:raise AssertionError('driver socket not ready')
                time.sleep(.05)
            def carrier():return 'LOWER_UP' in backend.links()['fv4002']['flags']
            initial=runtime.rpc('status',{})
            assert not initial['forwarding'] and not carrier()
            observed=runtime.rpc('links',{'token':initial['link_token'],'ports':[observation()]})
            assert observed['forwarding'] and carrier()
            try:runtime.rpc('links',{'token':initial['link_token'],'ports':[observation()]})
            except RuntimeError:pass
            else:raise AssertionError('replayed observation accepted')
            down=runtime.rpc('links',{'token':observed['link_token'],'ports':[observation()|{'link':False}]})
            assert not down['forwarding'] and not carrier()
            observed=runtime.rpc('links',{'token':down['link_token'],'ports':[observation()]})
            assert observed['forwarding'] and carrier()
            # No frontend polling can keep the physical lease alive by itself.
            deadline=time.monotonic()+LEASE_SECONDS+1
            while time.monotonic()<deadline:time.sleep(.25)
            stale=runtime.rpc('status',{})
            assert not stale['forwarding'] and not carrier()
            worker.join(7)
            assert worker.exitcode==0,'driver did not stop cleanly'
    finally:
        if worker is not None and worker.is_alive():worker.terminate();worker.join(5)
        network.run('ip','netns','del',network.NS)
    print(json.dumps({'driver_handshake':True,'replay_rejected':True,'link_down_stops_forwarding':True,
                      'unrefreshed_lease_lowers_kernel_carrier':True,'physical_packets_sent':0}))


if __name__=='__main__':main()
