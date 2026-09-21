"""Exercise the real supervisor pipes without touching packet hardware."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid


def agent(role,fail):
    if role=='cp':
        for line in sys.stdin:
            message=json.loads(line);identity=message['payload']
            print(json.dumps(dict(identity,sequence=message['sequence'],phase='active',links=[])),flush=True)
        return
    intent=json.loads(sys.stdin.readline())
    base=dict(group=intent['group'],token=intent['token'],boot_id=intent['boot_id'])
    print(json.dumps(base),flush=True)
    count=0
    for line in sys.stdin:
        message=json.loads(line);count+=1
        if fail and count==3:
            print(json.dumps(dict(base,error='simulated dataplane restart')),flush=True)
            return
        print(json.dumps(dict(base,configuration_revision=message['config']['revision'],
            attachment_ready=True,network_ready=True,distributing=intent['members'],
            subinterfaces=[dict(name=u['name'],tag=u['tag'],applied=True) for u in intent['network']['units']])),flush=True)


def owner(directory,boot,fail):
    import aggregate_activation as activation
    activation.DIRECTORY=Path(directory);activation.RUNNING=Path(directory)/'running.xml'
    def remote(role,operation,payload):
        if operation=='fabric':return dict(ready=True,boot_id=boot,epoch='cp')
        if operation!='status':return {}
        if role=='dp':return dict(boot_id=boot,groups={})
        return dict(epoch='cp',groups={})
    # resume_selection binds its default at import; inject the fake transport
    # explicitly while exercising the real recovery and supervisor logic.
    resume=activation.resume_selection
    with patch.object(activation,'resume_selection',side_effect=lambda name,**kw:resume(name,remote,**kw)),patch.object(activation,'remote',side_effect=remote),patch.object(activation,'command',side_effect=lambda role,op:[sys.executable,__file__,'--agent',role,str(int(fail))]),patch('policy_guard.before_commit'):
        raise SystemExit(0 if activation.supervise('ae1') else 1)


class RecoveryPipeTests(unittest.TestCase):
    def test_failed_owner_is_replaced_and_acknowledged_after_dp_reboot(self):
        import aggregate_activation as activation
        from test_aggregate_config import XML
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);boot=str(uuid.uuid4())
            raw=XML.replace(b'</bond>',b'</bond><units><entry name="ae1.37"><tag>37</tag></entry></units>')
            (root/'running.xml').write_bytes(raw)
            selected=activation.prepare(raw,'ae1',False,boot);selected['epoch']='cp'
            activation.atomic(root/'ae1-intent.json',selected)
            previous_token=selected['intent']['token']
            for fail in (True,False):
                if not fail:boot=str(uuid.uuid4())
                process=subprocess.Popen([sys.executable,__file__,'--owner',directory,boot,str(int(fail))],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    deadline=time.monotonic()+10;applied=None
                    while time.monotonic()<deadline:
                        path=root/'ae1-status.json'
                        if path.exists():
                            row=json.loads(path.read_text())
                            if row.get('applied') and row['token']!=previous_token:
                                applied=row;break
                        if process.poll() is not None:break
                        time.sleep(.02)
                    self.assertIsNotNone(applied,'Replacement never acknowledged configuration')
                    self.assertEqual(applied['dataplane']['boot_id'],boot)
                    self.assertEqual(applied['configuration_revision'],applied['dataplane']['configuration_revision'])
                    self.assertTrue(applied['dataplane']['subinterfaces'][0]['applied'])
                    previous_token=applied['token']
                    if not fail:process.send_signal(signal.SIGTERM)
                    stdout,stderr=process.communicate(timeout=12)
                    self.assertEqual(process.returncode,1 if fail else 0,stdout+stderr)
                    with patch.object(activation,'DIRECTORY',root):state=activation.status()['groups']['ae1']
                    self.assertFalse(state['fresh']);self.assertFalse(state['applied'])
                    self.assertIsNone(state['dataplane']['configuration_revision'])
                    self.assertFalse(state['dataplane']['subinterfaces'][0]['applied'])
                finally:
                    if process.poll() is None:process.kill()
                    process.communicate()


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--agent':agent(sys.argv[2],sys.argv[3]=='1')
    elif len(sys.argv)>1 and sys.argv[1]=='--owner':owner(sys.argv[2],sys.argv[3],sys.argv[4]=='1')
    else:unittest.main()
