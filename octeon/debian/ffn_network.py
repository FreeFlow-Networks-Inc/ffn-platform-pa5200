#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""PA-5200 adapter for the core Linux network engine (install core first)."""
import sys
import json
import re
from pathlib import Path
import time
sys.path.insert(0, '/usr/local/lib/ffn')
import ffn_linux_network as engine
engine.MAX_PORTS = 24


def install_guards(target):
    """Keep device-specific ownership checks in the selected platform adapter."""
    if getattr(target, '_pa5200_assignment_guards', False):
        return
    original_prepare = target.prepare
    original_backend = target.backend

    def route_link(dev):
        try:
            value=json.loads(Path('/run/ffn-route-links.json').read_text())
            if value['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip() or not 0<=time.monotonic()-value['observed']<=60:
                return 'hardware-link-observation-stale'
            if re.fullmatch(r'p[1-9][0-9]*',dev):
                return None if value['links'].get(dev) is True else 'hardware-link-down'
            parent=dev.split('.')[0]
            if re.fullmatch(r'ae[1-9][0-9]*',parent):
                owner=json.loads(Path('/run/ffn-aggregate-'+parent+'-status.json').read_text())
                return None if any(value['links'].get('p'+str(p)) is True for p in owner.get('distributing',[])) else 'aggregate-no-live-member'
            return 'hardware-link-unavailable'
        except (OSError,ValueError,KeyError): return 'hardware-link-observation-unavailable'

    target.route_link=route_link

    def backend():
        result=dict(original_backend())
        if target.exists():
            from ffn_platform_policy_bindings import physical_bindings
            links={p['ifname']:p for p in json.loads(target.ip('-d','-j','link'))}
            physical=physical_bindings(links)
            result['ports']=sorted(set(result.get('ports',[]))|{int(p[1:]) for p in physical.values()})
            if physical:result.update(transport='direct OCTEON physical attachments',max_mtu=1500)
        return result

    def prepare(cfg, request):
        proposed, changed = original_prepare(cfg, request)
        links = {p['ifname']: p for p in json.loads(target.ip('-j', 'link'))} if changed else {}
        if any(re.fullmatch(r'lag(?:[1-9]|1[0-2])', str(links.get(p, {}).get('master', ''))) for p in changed):
            raise ValueError('deactivate the LACP group before reconfiguring a member port')
        return proposed, changed

    target.prepare = prepare
    target.backend = backend
    target._pa5200_assignment_guards = True


install_guards(engine)
if __name__ == '__main__':
    engine.REQUIRE_ATTACHMENT = True
    if any(a == '--backend' or a.startswith('--backend=') for a in sys.argv):
        raise SystemExit('PA-5200 adapter uses the platform TAP fabric backend')
    try:
        engine.main()
    except ValueError as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(2)
else:
    # Preserve module-level injection used by platform backend tests.
    sys.modules[__name__] = engine
