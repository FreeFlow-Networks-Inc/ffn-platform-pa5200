#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""PA-5200 adapter for the core Linux network engine (install core first)."""
import sys
import json
import re
sys.path.insert(0, '/usr/local/lib/ffn')
import ffn_linux_network as engine
engine.MAX_PORTS = 24


def install_guards(target):
    """Keep device-specific ownership checks in the selected platform adapter."""
    if getattr(target, '_pa5200_assignment_guards', False):
        return
    original_prepare, original_run = target.prepare, target.run

    def prepare(cfg, request):
        proposed, changed = original_prepare(cfg, request)
        vif_path = target.STATE.with_name('vifs.json')
        if vif_path.exists():
            from ffn_vif import protect_network
            protect_network(proposed, vif_path)
        links = {p['ifname']: p for p in json.loads(target.ip('-j', 'link'))} if changed else {}
        if any(re.fullmatch(r'lag(?:[1-9]|1[0-2])', str(links.get(p, {}).get('master', ''))) for p in changed):
            raise ValueError('deactivate the LACP group before reconfiguring a member port')
        return proposed, changed

    def run(*args):
        # The core invokes teardown while holding its network configuration lock.
        # Keep the guard here rather than checking before acquiring that lock.
        if args == ('ip', 'netns', 'delete', target.NS):
            vif_path = target.STATE.with_name('vifs.json')
            if vif_path.exists() and json.loads(vif_path.read_text())['vifs']:
                raise RuntimeError('remove VIF assignments before stopping their network namespace')
        return original_run(*args)

    target.prepare, target.run = prepare, run
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
