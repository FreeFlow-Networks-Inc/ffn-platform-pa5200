import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import Mock,patch
import native_plane_boot as boot
import plane_restart_node as node
import plane_restart_owner as owner
import dp_restart_agent as agent


class NativeBootTests(unittest.TestCase):
    def test_kernel_notes_come_from_elf_section(self):
        data=bytearray(512);data[:6]=b'\x7fELF\x02\x02';data[18:20]=b'\0\x08'
        struct.pack_into('>Q',data,40,64);struct.pack_into('>HHH',data,58,64,2,0)
        struct.pack_into('>IIQQQQIIQQ',data,64,0,0,0,0,256,20,0,0,0,0)
        struct.pack_into('>IIQQQQIIQQ',data,128,1,0,0,0,300,4,0,0,0,0)
        data[256:263]=b'\0.notes';data[300:304]=b'note'
        self.assertEqual(boot.kernel_notes(data),boot.sha(b'note'))
        data[5]=1
        with self.assertRaises(ValueError):boot.kernel_notes(data)

    def test_profile_boot_command_preserves_selected_options(self):
        cfg={'cores':40,'fdt':'0x80000','extra':'ffn_reserve=0x400000,4M'}
        self.assertEqual(boot.command(cfg),'bootoctlinux 21000000 numcores=40 console=ttyS0,115200n8 ffn_fdt=0x80000 rw ffn_reserve=0x400000,4M')

    def staged(self,image_size=3*boot.MIB+5,provision_size=None):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=tmp.name
        image=Path(root)/'rootfs.squashfs';image.write_bytes(b'hsqs'+b'\x5a'*(image_size-4))
        cfg={'role':'dp','cores':40,'fdt':'0x80000','extra':'mem=30G ffn_reserve=0x400000,4M',
             'rootfs':{'path':str(image),'sha256':boot.sha(image.read_bytes())}}
        if provision_size is not None:
            archive=Path(root)/'provision.tar'
            with tarfile.open(archive,'w',format=tarfile.USTAR_FORMAT) as tar:
                info=tarfile.TarInfo('etc/machine-id');info.size=provision_size
                tar.addfile(info,io.BytesIO(b'm'*provision_size))
            cfg['provision']={'path':str(archive),'sha256':boot.sha(archive.read_bytes())}
        return cfg

    def test_rootfs_plan_lays_regions_out_on_mebibyte_boundaries(self):
        cfg=self.staged()
        plan=boot.rootfs_plan(cfg)
        self.assertEqual(plan['image_offset'],boot.MIB)
        self.assertEqual(plan['provision_offset'],5*boot.MIB)       # 1 MiB header + 4 MiB rounded image
        self.assertEqual(plan['reserve_mib'],5)
        self.assertEqual(plan['arguments'],'ffn_reserve=0x28000000,5M phram.phram=dproot,0x28000000,5Mi ffn.rootfs=dproot')
        self.assertEqual(len(plan['header']),4096)
        self.assertEqual(plan['header'].rstrip(b'\0'),('FFN-ROOTFS 1 %d %s\n'%(3*boot.MIB+5,cfg['rootfs']['sha256'])).encode())
        self.assertTrue(boot.command(cfg).endswith(' rw mem=30G ffn_reserve=0x400000,4M '+plan['arguments']))
        self.assertLessEqual(len(boot.command(cfg).encode()),247)
        cfg=self.staged(provision_size=100)
        plan=boot.rootfs_plan(cfg)
        self.assertEqual(plan['reserve_mib'],6)
        self.assertEqual(plan['header'].rstrip(b'\0').split(b' ')[4:],[b'10240',cfg['provision']['sha256'].encode()+b'\n'])
        self.assertIsNone(boot.rootfs_plan({'role':'dp'}))
        self.assertNotIn('ffn.rootfs',boot.command({'role':'cp','cores':8,'fdt':'','extra':''}))

    def test_rootfs_plan_refuses_wrong_formats(self):
        cfg=self.staged()
        Path(cfg['rootfs']['path']).write_bytes(b'not a squashfs image')
        with self.assertRaisesRegex(ValueError,'squashfs'):boot.rootfs_plan(cfg)
        cfg=self.staged(provision_size=1)
        Path(cfg['provision']['path']).write_bytes(b'\0'*600)
        with self.assertRaisesRegex(ValueError,'plain tar'):boot.rootfs_plan(cfg)
        cfg=self.staged()
        cfg['rootfs']['name']='Bad Name'
        with self.assertRaisesRegex(ValueError,'device name'):boot.rootfs_plan(cfg)

    def test_dp_boot_stages_root_and_provisioning_before_the_kernel(self):
        cfg=self.staged(provision_size=100)
        cfg.update(pci='0003:03:00.0',devnum=3,kernel={'path':'/images/vmlinux','sha256':'k'*64,'notes_sha256':'n'*64},
                   bootloader={'path':'/images/u-boot.bin','sha256':'b'*64},transport={'unit':'transport.service'},
                   tools={name:{'path':'/native/'+name} for name in ('reset','boot','csr','load','stage')})
        before={'boot_id':'old','notes_sha256':'n'*64};after={'boot_id':'new','notes_sha256':'n'*64}
        with tempfile.TemporaryDirectory() as run_dir, \
                patch.object(boot,'preflight'),patch.object(boot,'exclusive',return_value=nullcontext()), \
                patch.object(boot,'stop_transport'),patch.object(boot,'start_root_server'),patch.object(boot,'run') as run, \
                patch.object(boot,'Path',side_effect=lambda p:Path(run_dir)/str(p).lstrip('/') if str(p).startswith(('/sys/','/run/')) else Path(p)), \
                patch.object(boot.time,'sleep'),patch.object(node,'observe',side_effect=[before,after]):
            (Path(run_dir)/'sys/bus/pci/devices/0003:03:00.0').mkdir(parents=True)
            (Path(run_dir)/'run').mkdir()
            boot.boot(cfg)
            header=(Path(run_dir)/'run/ffn-dp-rootfs-header').read_bytes()
        calls=[c.args[0] for c in run.call_args_list]
        loads=[c for c in calls if c[0]=='/native/load']
        self.assertEqual(loads,[
            ['/native/load','--devnum=3','0x28000000',str(Path(run_dir)/'run/ffn-dp-rootfs-header')],
            ['/native/load','--devnum=3','0x28100000',cfg['rootfs']['path']],
            ['/native/load','--devnum=3','0x28500000',cfg['provision']['path']],
            ['/native/load','--devnum=3','0x21000000','/images/vmlinux']])
        self.assertEqual(len(header),4096)
        self.assertIn(b'FFN-ROOTFS 1 ',header)
        dispatch=[c for c in calls if c[0]=='/usr/bin/python3' and c[1]=='/native/stage'][0]
        self.assertIn('ffn.rootfs=dproot',dispatch[-1]);self.assertIn('ffn_reserve=0x28000000,6M',dispatch[-1])
        self.assertIn('phram.phram=dproot,0x28000000,6Mi',dispatch[-1])

    def report(self,pid1='/init',osline=''):
        return '\nBEGIN-test\n11111111-1111-1111-1111-111111111111\n6.18.49-ffn\n'+'a'*64+'  /sys/kernel/notes\n'+pid1+'\n'+osline+'\nEND-test\n'

    def test_recovery_is_observable_but_not_forwarding_ready(self):
        value=node.parse_observation(self.report(),'test')
        self.assertTrue(value['control_ready']);self.assertFalse(value['ready']);self.assertEqual(value['runtime'],'recovery')
        self.assertFalse(value['forwarding_verified'])

    def test_systemd_debian_required_for_ready(self):
        self.assertTrue(node.parse_observation(self.report('/usr/lib/systemd/systemd','ID=debian'),'test')['ready'])
        self.assertFalse(node.parse_observation(self.report('/usr/lib/systemd/systemd','ID=openwrt'),'test')['ready'])

    def test_echo_or_wrong_challenge_cannot_forge_observation(self):
        for text in ["printf 'BEGIN-%s' test",self.report().replace('BEGIN-test','BEGIN-other'),self.report()*2]:
            with self.assertRaises(ValueError):node.parse_observation(text,'test')

    def test_preflight_refuses_switching_images(self):
        with patch.object(owner,'preflight',return_value={'notes_sha256':'selected'}),patch.object(owner,'profile'), \
                patch.object(owner,'remote',return_value={'notes_sha256':'different'}):
            with self.assertRaisesRegex(ValueError,'differs'):owner.inspect('cp')

    def test_relay_never_reports_cp_resource_counters(self):
        nonce='11111111-1111-1111-1111-111111111111'
        source=io.BytesIO((json.dumps({'v':1,'nonce':nonce,'op':'observe'})+'\n').encode());out=io.BytesIO()
        with patch.object(agent,'observe',return_value={'boot_id':nonce,'ready':False,'runtime':'recovery'}):agent.serve(source,out)
        value=json.loads(out.getvalue());self.assertEqual(value['role'],'dp');self.assertEqual(value['nonce'],nonce)
        self.assertFalse(value['report']['host_resources']['available']);self.assertFalse(value['report']['ready'])
        self.assertTrue(value['report']['restart_acknowledged'])

    def test_remote_has_no_ui_selected_executable(self):
        with self.assertRaises(ValueError):owner.remote('arbitrary.py','restart')

    def test_truncated_mailbox_report_is_not_acknowledged(self):
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            node.parse_observation('\nBEGIN-test\n11111111-1111-1111-1111-111111111111\nEND-test\n','test')

    def test_new_boot_does_not_hide_failed_transport_restore(self):
        result=Mock(stdout='ActiveState=failed\nResult=exit-code\nExecMainStatus=1\n')
        with patch.object(node.subprocess,'run',return_value=result):
            with self.assertRaisesRegex(RuntimeError,'did not complete'):node.wait_dp_owner()

    def test_restore_waits_for_boot_owner_completion(self):
        results=[Mock(stdout='ActiveState=activating\nResult=success\nExecMainStatus=0\n'),
                 Mock(stdout='ActiveState=inactive\nResult=success\nExecMainStatus=0\n')]
        with patch.object(node.subprocess,'run',side_effect=results),patch.object(node.time,'sleep') as sleep:
            node.wait_dp_owner();sleep.assert_called_once_with(1)

    def test_runtime_services_must_be_ready_before_configuration_replay(self):
        starting={'ready':True,'boot_id':'one','runtime_services_ready':False}
        ready=dict(starting,runtime_services_ready=True)
        with patch.object(node,'observe',side_effect=[starting,ready]),patch.object(node.time,'sleep') as sleep:
            self.assertTrue(node.wait_dp_services()['runtime_services_ready']);sleep.assert_called_once_with(2)
        with patch.object(node,'observe',side_effect=[starting,dict(ready,boot_id='two')]),patch.object(node.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'boot changed'):node.wait_dp_services()
        text=self.report('/usr/lib/systemd/systemd','ID=debian\nFFN-SERVICES:ready')
        self.assertTrue(node.parse_observation(text,'test')['runtime_services_ready'])
        self.assertFalse(node.parse_observation(self.report('/usr/lib/systemd/systemd','ID=debian'),'test')['runtime_services_ready'])

    def test_transient_transport_cannot_be_used_for_restart(self):
        state=Mock(stdout='LoadState=loaded\nTransient=yes\nFragmentPath=/run/systemd/transient/transport.service\n')
        with patch.object(boot.subprocess,'run',return_value=state):
            with self.assertRaisesRegex(ValueError,'Persistent transport'):
                boot.transport_service({'transport':{'unit':'transport.service'}})

    def test_missing_transport_after_stop_prevents_reset(self):
        with patch.object(boot,'run') as run,patch.object(boot,'transport_service',side_effect=ValueError('missing')):
            with self.assertRaisesRegex(ValueError,'missing'):
                boot.stop_transport({'transport':{'unit':'transport.service'}})
        self.assertEqual(run.call_args_list[0].args[0],['systemctl','stop','transport.service'])
        self.assertEqual(run.call_args_list[1].args[0],['systemctl','daemon-reload'])

    def test_cp_reconnect_restores_dp_window_without_resetting_processor(self):
        cfg={'pci':'0003:03:00.0','devnum':3,'tools':{'csr':{'path':'/native/csr'}},
             'transport':{'unit':'transport.service'}}
        before={'boot_id':'unchanged','notes_sha256':'same'}
        with patch.object(node,'profile',return_value=cfg),patch.object(boot,'preflight'), \
                patch.object(boot,'exclusive',return_value=nullcontext()),patch.object(boot,'stop_transport') as stop, \
                patch.object(boot,'run') as run,patch.object(node,'Path') as path, \
                patch.object(node,'observe',return_value=before) as observe:
            self.assertEqual(node.reconnect_dp(before),before)
            stop.assert_called_once_with(cfg);observe.assert_called_once_with('dp',owned=True)
            self.assertEqual([x.args[0] for x in run.call_args_list],[
                ['/native/csr','--devnum=3','PEM0_BAR1_INDEX1','0x11'],
                ['systemctl','start','transport.service']])
            run.reset_mock();observe.return_value=dict(before,boot_id='unexpected')
            with self.assertRaisesRegex(RuntimeError,'DP boot changed'):node.reconnect_dp(before)
            self.assertEqual(run.call_count,2)
            self.assertEqual(stop.call_count,3)  # entry twice, failed identity once

    def test_nfs_root_is_restored_before_dp_identity_probe(self):
        cfg={'pci':'0003:03:00.0','devnum':3,'tools':{'csr':{'path':'/native/csr'}},
             'transport':{'unit':'transport.service'},'root_server_unit':'nfs.service'}
        before={'boot_id':'unchanged','notes_sha256':'same'}
        events=[]
        def observe(*args,**kwargs):
            self.assertIn('root-ready',events)
            return before
        with patch.object(node,'profile',return_value=cfg),patch.object(boot,'preflight'), \
                patch.object(boot,'exclusive',return_value=nullcontext()),patch.object(boot,'stop_transport'), \
                patch.object(boot,'run'),patch.object(node,'Path'),patch.object(node,'observe',side_effect=observe), \
                patch.object(boot,'start_root_server',side_effect=lambda _:events.append('root-ready')):
            self.assertEqual(node.reconnect_dp(before),before)

    def test_root_server_is_started_only_when_provisioned(self):
        with patch.object(boot,'run') as run:
            boot.start_root_server({});run.assert_not_called()
            boot.start_root_server({'root_server_unit':'ffn-cp-nfs.service'})
            self.assertEqual([c.args[0] for c in run.call_args_list],[
                ['systemctl','start','ffn-cp-nfs.service'],
                ['systemctl','is-active','--quiet','ffn-cp-nfs.service']])


if __name__=='__main__':unittest.main()
