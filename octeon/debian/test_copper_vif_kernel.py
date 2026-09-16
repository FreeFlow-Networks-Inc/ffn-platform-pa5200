#!/usr/bin/env python3
"""Exercise the copper TAP carrier ioctl in an isolated Linux namespace.

No physical socket, PHY write, route in ffn-data, or WAN packet is used.
Run as root on the DP to verify its MIPS ioctl ABI as well as Linux carrier.
"""
import json
import os
import tempfile
import time
from pathlib import Path
import ffn_network as network
from ffn_vif_runtime import Linux, Owner
from ffn_copper_vif import LEASE_SECONDS, CopperVif
from test_copper_vif import profile, observation


def main():
    network.NS='ffn-cuv-test-'+str(os.getpid())
    driver=CopperVif(profile())
    backend=Linux();fd=None
    network.run('ip','netns','add',network.NS)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            network.STATE=Path(tmp)/'network.json'
            owner=Owner(backend,{2},Path(tmp)/'vifs.json',Path(tmp)/'pending',copper=driver)
            owner.replace({'revision':0,'vifs':{'fv4001':{'port':2,'vlan':None,'enabled':True,
                'network':{'mode':'l3','addresses':[]}}}})
            fd=backend.open('fv4001',copper=True)
            def carrier():return 'LOWER_UP' in backend.links()['fv4001']['flags']
            assert not carrier(),'TAP attach invented physical carrier'
            for row,expected in [(observation(),True),(observation()|{'mac_link':False},False),
                                 (observation(),True),(observation()|{'phy_enabled':False},False)]:
                driver.observe({'token':driver.challenge(),'ports':[row]})
                backend.carrier(fd,driver.allowed(2))
                assert carrier()==expected,'kernel carrier differs from physical observation'
            driver.observe({'token':driver.challenge(),'ports':[observation()]})
            backend.carrier(fd,driver.allowed(2));assert carrier()
            driver.clock=lambda:time.monotonic()+LEASE_SECONDS+1
            backend.carrier(fd,driver.allowed(2));assert not carrier(),'stale observation left carrier up'
            os.close(fd);fd=None
            assert not carrier(),'driver close left carrier up'
    finally:
        if fd is not None:os.close(fd)
        network.run('ip','netns','del',network.NS)
    print(json.dumps({'copper_attach_down':True,'phy_mac_carrier_transitions':True,
                      'stale_carrier_down':True,'close_carrier_down':True,'physical_packets_sent':0}))


if __name__=='__main__':main()
