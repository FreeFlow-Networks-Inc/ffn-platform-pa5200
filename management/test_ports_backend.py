"""The ports resource: the table from the configuration, and the commit's sync against fake planes."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ports_backend as backend

BOOT='29b0a1e4-a04b-4c23-81b9-2c5bfb6f4099'
XML='''<config><devices><entry name="localhost.localdomain"><network><interface><ethernet>
<entry name="ethernet1/1"><layer3><ip><entry name="192.0.2.1/24"/></ip></layer3></entry>
<entry name="ethernet1/2"><comment>placeholder</comment><layer3/></entry>
<entry name="ethernet1/3"><link-state>down</link-state><layer3><ip><entry name="198.51.100.1/24"/></ip></layer3></entry>
<entry name="ethernet1/4"><layer3><interface-management-profile>PING-ONLY</interface-management-profile></layer3></entry>
<entry name="ethernet1/5"><layer3><ip><entry name="203.0.113.1/29"/></ip></layer3></entry>
<entry name="ethernet1/21"><aggregate-group>ae1</aggregate-group></entry>
<entry name="ethernet1/22"><aggregate-group>ae1</aggregate-group></entry>
<entry name="ethernet1/23"><aggregate-group>bond9</aggregate-group></entry>
</ethernet><aggregate-ethernet><entry name="ae1"><layer3/></entry></aggregate-ethernet></interface></network></entry></devices></config>'''


class TableTests(unittest.TestCase):
    def test_every_port_has_a_row_derived_from_the_committed_interface(self):
        rows=backend.desired_table(XML.encode())
        self.assertEqual(sorted(rows),list(range(1,25)))
        self.assertEqual(rows[1],{'mode':'l3'})                      # an address
        self.assertEqual(rows[2],{'mode':'disabled'})                # an empty layer3 placeholder
        self.assertEqual(rows[3],{'mode':'disabled'})                # link-state down
        self.assertEqual(rows[4],{'mode':'l3'})                      # a management profile alone
        self.assertEqual(rows[5],{'mode':'l3'})
        self.assertEqual(rows[21],{'mode':'aggregate','group':'ae1'})
        self.assertEqual(rows[22],{'mode':'aggregate','group':'ae1'})
        self.assertEqual(rows[23],{'mode':'disabled'})               # not a hardware aggregate name
        self.assertEqual(rows[24],{'mode':'disabled'})               # not configured at all
        request=backend.table_request(rows,BOOT,'g'*64)
        self.assertEqual((request['boot_id'],request['generation'],request['ports']['21']),(BOOT,'g'*64,{'mode':'aggregate','group':'ae1'}))
        self.assertEqual(len(request['ports']),24)


class Planes:
    """CP owners and the DP daemon as the sync sees them, recording every call in order."""
    def __init__(self,attach=(1,5),wan_pending=False,cp_enabled=()):
        self.calls=[];self.attach=set(attach);self.wan_pending=wan_pending;self.cp_enabled=set(cp_enabled)
        self.revision=4;self.pushed=None;self.drained=[]
    def table(self):
        rows={}
        for port in range(1,25):
            desired=(self.pushed or {}).get(str(port),{'mode':'disabled'})
            attached=desired['mode']=='l3' and port in self.attach
            rows[str(port)]=dict(desired=desired['mode'],group=desired.get('group'),attached=attached,member_of=None,
                                 reason='running' if attached else ('attach failed: native physical owner: No such device' if desired['mode']=='l3' else desired['mode']),failures=0 if attached else 3)
        return dict(owner='ffn-portd',boot_id=BOOT,revision=self.revision,ports=rows,aggregates={})
    def __call__(self,role,operation,payload):
        self.calls.append((role,operation))
        if (role,operation)==('dp','table-status'):return self.table()
        if (role,operation)==('dp','table-apply'):
            assert payload['boot_id']==BOOT and len(payload['ports'])==24,payload
            self.pushed=payload['ports'];self.revision+=1;return {'ok':True,'revision':self.revision,'rejected':{}}
        if (role,operation)==('cp','port-status'):
            port=payload['port'];enabled=port in self.cp_enabled
            return dict(epoch='epoch-1',ready=enabled,state=dict(enabled=enabled,dp_boot_id=BOOT if enabled else None,pending=None))
        if (role,operation)==('cp','port-start'):
            assert payload=={'port':payload['port'],'epoch':'epoch-1','dp_boot_id':BOOT},payload
            self.cp_enabled.add(payload['port']);return dict(ready=True)
        if (role,operation)==('cp','port-stop'):self.cp_enabled.discard(payload['port']);return dict(ready=False)
        if (role,operation)==('cp','wan-status'):
            enabled=1 in self.cp_enabled
            return dict(revision=7,epoch='bcm-2',ready={'1':enabled},state=dict(enabled=enabled,dp_boot_id=BOOT if enabled else None,pending='start' if self.wan_pending else None,epoch='bcm-2'))
        if (role,operation)==('cp','wan-recover'):self.wan_pending=False;return dict(revision=8,epoch='bcm-2',state=dict(enabled=False,pending=None,epoch='bcm-2'))
        if (role,operation)==('cp','wan-start'):
            assert payload['dp_boot_id']==BOOT,payload
            self.cp_enabled.add(1);return dict(revision=payload['revision']+1,ready={'1':True})
        if (role,operation)==('cp','wan-stop'):self.cp_enabled.discard(1);return dict(revision=payload['revision']+1,ready={'1':False})
        raise AssertionError('unexpected call %s/%s'%(role,operation))
    def drain(self,data):self.calls.append(('mp','drain'));self.drained.append(json.loads(data));return {'drained':True}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.running=Path(self.tmp.name)/'running-config.xml';self.running.write_text(XML)
        for name,value in [('RUNNING',self.running),('ATTACH_WAIT',0.3),('POLL',0.01)]:
            p=patch.object(backend,name,value);p.start();self.addCleanup(p.stop)
        self.payload=dict(operation='sync',running_revision=hashlib.sha256(self.running.read_bytes()).hexdigest(),revision=4)

    def test_sync_starts_cp_owners_pushes_one_table_and_reads_back(self):
        planes=Planes(attach=(1,4,5))
        result=backend.sync(self.payload,planes,planes.drain)
        self.assertEqual(result['errors'],{});self.assertTrue(result['applied'])
        self.assertEqual(result['attached'],[1,4,5]);self.assertEqual(result['revision'],5)
        self.assertEqual(planes.pushed['21'],{'mode':'aggregate','group':'ae1'});self.assertEqual(planes.pushed['2'],{'mode':'disabled'})
        # the CP owners first (port 1 through the WAN forwarding owner, after the policy barrier), one push, one readback
        self.assertEqual(planes.calls,[('dp','table-status'),('cp','wan-status'),('cp','port-status'),('cp','port-status'),('mp','drain'),
                                       ('cp','wan-start'),('cp','port-start'),('cp','port-start'),('dp','table-apply'),('dp','table-status')])
        self.assertEqual(planes.cp_enabled,{1,4,5})

    def test_a_port_that_never_attaches_reports_the_daemon_reason_and_loses_its_cp_redirect(self):
        planes=Planes(attach=(1,4))
        result=backend.sync(self.payload,planes,planes.drain)
        self.assertFalse(result['applied']);self.assertEqual(result['attached'],[1,4])
        self.assertEqual(list(result['errors']),['ethernet1/5']);self.assertIn('No such device',result['errors']['ethernet1/5'])
        self.assertEqual(planes.calls[-1],('cp','port-stop'));self.assertEqual(planes.cp_enabled,{1,4})

    def test_a_port_leaving_layer3_is_withdrawn_on_the_cp_after_the_push(self):
        planes=Planes(attach=(1,4,5),cp_enabled=(1,4,5,9))
        planes.pushed={str(p):({'mode':'l3'} if p in (1,4,5,9) else {'mode':'disabled'}) for p in range(1,25)}
        planes.attach.add(9)
        result=backend.sync(self.payload,planes,planes.drain)
        self.assertEqual(result['errors'],{})
        self.assertIn(('cp','port-stop'),planes.calls);self.assertNotIn(9,planes.cp_enabled)
        self.assertGreater(planes.calls.index(('cp','port-stop')),planes.calls.index(('dp','table-apply')))
        self.assertEqual([c for c in planes.calls if c[1] in ('port-start','wan-start')],[])   # owners already in step: nothing restarted
        self.assertNotIn(('mp','drain'),planes.calls)                                            # port 1 did not change

    def test_wan_redirect_from_another_lifetime_is_recovered_before_the_start(self):
        planes=Planes(attach=(1,4,5),wan_pending=True)
        backend.sync(self.payload,planes,planes.drain)
        order=[c for c in planes.calls if c[0]=='cp' and c[1].startswith('wan')]
        self.assertEqual(order,[('cp','wan-status'),('cp','wan-recover'),('cp','wan-start')])
        self.assertEqual(planes.drained[0]['operation'],'sync')

    def test_stale_revisions_never_reach_the_planes(self):
        planes=Planes()
        with self.assertRaisesRegex(ValueError,'Running configuration changed'):backend.sync(dict(self.payload,running_revision='0'*64),planes,planes.drain)
        self.assertEqual(planes.calls,[])
        with self.assertRaisesRegex(ValueError,'revision changed'):backend.sync(dict(self.payload,revision=3),planes,planes.drain)
        self.assertEqual(planes.calls,[('dp','table-status')])

    def test_execute_routes_operations_and_validates_sync(self):
        planes=Planes(attach=(1,4,5))
        self.assertEqual(backend.execute('validate',self.payload,planes,planes.drain),{'validated':True})
        self.assertEqual(planes.calls,[])
        with self.assertRaisesRegex(ValueError,'Unknown port operation'):backend.execute('apply',{'operation':'teleport'},planes,planes.drain)
        with self.assertRaisesRegex(ValueError,'Status takes no fields'):backend.execute('status',{'x':1},planes)

    def test_status_reads_the_table_and_the_cp_owner_of_every_layer3_row(self):
        planes=Planes(attach=(1,5),cp_enabled=(1,5))
        planes.pushed={str(p):({'mode':'l3'} if p in (1,5) else {'mode':'disabled'}) for p in range(1,25)}
        result=backend.status(planes,aggregates=False)
        self.assertEqual(result['config'],{'revision':4});self.assertEqual(result['boot_id'],BOOT)
        self.assertEqual(sorted(result['cp']),['1','5']);self.assertTrue(result['cp']['5']['ready'])
        self.assertEqual(result['dp']['ports']['5']['attached'],True)
        self.assertEqual(planes.calls,[('dp','table-status'),('cp','wan-status'),('cp','port-status')])

    def test_a_dataplane_without_the_daemon_is_an_error_not_an_empty_table(self):
        def call(role,operation,payload):
            if (role,operation)==('dp','table-status'):return {'daemon':False}
            raise AssertionError(operation)
        with self.assertRaisesRegex(RuntimeError,'not serving'):backend.sync(self.payload,call,lambda data:None)


if __name__=='__main__':unittest.main()
