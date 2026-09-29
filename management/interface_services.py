#!/usr/bin/env python3
"""MP-owned management service tunnel using the commissioned DP SSH transport."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

RUNTIME = Path('/run/ffn-interface-services')


def transport():
    config = json.loads(Path('/etc/ffn/controld.json').read_text())
    argv = config['agents']['dp']['argv']
    # Reuse the exact commissioned destination, proxy and host-key pins.
    if not argv or Path(argv[0]).name != 'ssh' or not argv[-1].endswith('ffn_dp_agent.py stream'):
        raise ValueError('Commissioned DP SSH channel is required')
    credential = os.environ.get('CREDENTIALS_DIRECTORY', '/run/credentials/ffn-interface-service-tunnel.service') + '/plane-agent-key'
    return [arg.replace('/run/credentials/ffn-controld.service/plane-agent-key', credential)
            for arg in argv[:-1]]


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    provider = tunnel = None
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        provider = subprocess.Popen([sys.executable, '/opt/ffn-ngfw-v2/ffn_interface_services.py',
                                     'provider', '--socket', str(RUNTIME / 'provider.sock')])
        while provider.poll() is None:
            try:
                argv = transport()
                subprocess.run(argv + ['python3 /usr/local/lib/ffn/ffn_interface_services.py prepare-transport'],
                               check=True, timeout=20)
                tunnel = subprocess.Popen(argv[:-1] + ['-N', '-T', '-o', 'ExitOnForwardFailure=yes',
                    '-o', 'StreamLocalBindUnlink=yes', '-R',
                    '/run/ffn-interface-services/upstream.sock:/run/ffn-interface-services/provider.sock', argv[-1]])
                while tunnel.poll() is None and provider.poll() is None:
                    time.sleep(1)
                stop(tunnel)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                print('Interface service channel unavailable: ' + str(error), flush=True)
            time.sleep(3)
        raise RuntimeError('Management service provider exited')
    except KeyboardInterrupt:
        pass
    finally:
        stop(tunnel)
        stop(provider)


if __name__ == '__main__':
    main()
