#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""PA-5200 adapter for the core Linux network engine (install core first)."""
import sys
import json
sys.path.insert(0, '/usr/local/lib/ffn')
import ffn_linux_network as engine
engine.MAX_PORTS = 24
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
