"""Applied PA5200 profile intent, including already-running aggregate workers."""
import json
from pathlib import Path
import time


def records(config=Path('/etc/ffn/network.json'), runtime=Path('/run'), boot=None, now=None):
    boot = boot or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    now = time.monotonic() if now is None else now
    result = []
    def add(name, settings):
        if 'management' in settings:
            result.append(dict(interface=name, settings=settings, boot_id=boot))
    network = json.loads(config.read_text())
    for name, settings in network['ports'].items():
        if settings.get('mode') == 'l3': add(name, settings)
    for path in runtime.glob('ffn-aggregate-*-status.json'):
        try:
            state = json.loads(path.read_text())
            # Older workers acknowledge network_ready and the exact revision,
            # but do not yet emit the separate configuration_ready field.
            ready = state.get('configuration_ready', state.get('network_ready')) is True
            if (state.get('boot_id') != boot or not ready or state.get('control_only')
                    or state.get('network_update_pending') or not 0 <= now-state['updated_monotonic'] <= 10):
                continue
            group = state['group']
            intent = json.loads((runtime / ('ffn-aggregate-'+group+'-intent.json')).read_text())
            if (state['token'] != intent['token'] or
                    state['configuration_revision'] != intent['network_generation']):
                continue
            settings = state['network']
            if settings.get('enabled', True):
                if settings.get('dhcp'):
                    lease = state.get('lease', {})
                    if lease.get('token') != state['token'] or lease.get('error'): continue
                    settings = dict(settings, addresses=[lease['address']] if lease.get('address') else [])
                add(group, settings)
            for unit in settings.get('units', []): add(unit['name'], unit)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result
