#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the production DHCP hook in disposable Linux namespaces (root)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=25, **kwargs).stdout


def main():
    suffix=uuid.uuid4().hex[:8];client='ffn-dhcpc-'+suffix;server='ffn-dhcps-'+suffix
    namespaces=[];process=None
    with tempfile.TemporaryDirectory(prefix='ffn-dhcp-test-') as directory:
        root=Path(directory);token=str(uuid.uuid4())
        try:
            for name in (client,server):
                run('ip','netns','add',name);namespaces.append(name)
                run('ip','-n',name,'link','set','lo','up')
            run('ip','-n',client,'link','add','ae1','type','veth','peer','name','srv0','netns',server)
            run('ip','-n',client,'link','set','ae1','alias','ffn-aggregate:'+token)
            run('ip','-n',client,'link','set','ae1','up')
            run('ip','-n',server,'address','add','192.0.2.1/24','dev','srv0')
            run('ip','-n',server,'link','set','srv0','up')
            intent=dict(token=token,boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                        control_only=False,network=dict(dhcp=True,dhcp_default_route=True,dhcp_route_metric=321,
                        management=dict(profile='',ping=False,sources=[],tcp=[],udp=[])))
            (root/'ffn-aggregate-ae1-intent.json').write_text(json.dumps(intent))
            module_dir=str(Path(__file__).resolve().parent)
            hook=root/'hook.py'
            hook.write_text('#!/usr/bin/python3\nimport sys,os\nfrom pathlib import Path\nsys.path.insert(0,'+repr(module_dir)+')\n'
                'import ffn_aggregate_runtime as runtime\nimport ffn_aggregate_dhcp as dhcp\n'
                'runtime.NS=dhcp.NS='+repr(client)+'\ndhcp.RUNDIR=Path('+repr(directory)+')\n'
                'dhcp.execute(sys.argv[1],os.environ)\n')
            hook.chmod(0o755)
            config=root/'server.conf'
            config.write_text('start 192.0.2.10\nend 192.0.2.20\ninterface srv0\noption subnet 255.255.255.0\n'
                'option router 192.0.2.1\noption lease 60\nlease_file '+str(root/'leases')+'\npidfile '+str(root/'server.pid')+'\n')
            with (root/'server.log').open('w') as log:
                process=subprocess.Popen(['ip','netns','exec',server,'busybox','udhcpd','-f',str(config)],stdout=log,stderr=log)
                time.sleep(.3)
                assert process.poll() is None,'DHCP server failed to start'
                run('ip','netns','exec',client,'busybox','udhcpc','-f','-n','-q','-t','3','-T','2',
                    '-i','ae1','-s',str(hook),'-p',str(root/'client.pid'))
                lease=json.loads((root/'ffn-aggregate-ae1-lease.json').read_text())
                assert lease['error'] is None and lease['router']=='192.0.2.1',lease
                routes=json.loads(run('ip','-n',client,'-j','route','show','default'))
                assert len(routes)==1 and routes[0]['dev']=='ae1' and routes[0]['metric']==321,routes
                run(str(hook),'deconfig',env=dict(os.environ,interface='ae1'))
                assert json.loads(run('ip','-n',client,'-j','route','show','default'))==[]
                addresses=json.loads(run('ip','-n',client,'-j','address','show','dev','ae1'))[0]['addr_info']
                assert not any(a['family']=='inet' for a in addresses),addresses
                print(json.dumps(dict(acquired=lease['address'],owned_default_route=True,deconfigured=True)))
        finally:
            if process is not None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            for name in reversed(namespaces):run('ip','netns','delete',name)


if __name__=='__main__':main()
