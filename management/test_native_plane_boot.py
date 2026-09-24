import io
import json
import struct
import unittest
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


if __name__=='__main__':unittest.main()
