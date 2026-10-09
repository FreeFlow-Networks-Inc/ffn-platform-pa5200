#!/usr/bin/env python3
"""Transmit verification for the SFP+ cages (ethernet1/5 .. 1/20): where does a port's path break?

The insertion watcher re-links a module when it appears; nothing verified
that the port then actually carries traffic. A faceplate that reads "link up"
can still have its laser held off by the cage control line, a module
reporting a transmitter fault, a link mode that does not match the module,
or a dataplane attachment the switch left pending after an interrupted apply
(ethernet1/5 on 2026-10-09: laser on, link up, no frames). This check walks
the chain the packets take and names the first layer that fails:

  cage         presence and the TX_DISABLE control line (PCA9555)
  module       identity and SFF-8472 diagnostics: laser power, bias, TX fault, RX LOS
  link         switch MAC: enabled, link, interface mode against the module's rate
  attachment   the CP's physical attachment journal for the port (epoch, pending)

Verdicts, first failing layer wins:
  no-module, disabled, transmitter-off, tx-fault, laser-off, no-rx-light,
  link-mode-mismatch, link-down, attachment-pending, attachment-stale,
  attachment-off, ok.

Every result carries the measured values, so the console shows TX/RX power
beside the verdict, and a `remedy`: `enable-transmitter` and `relink` the
watcher applies itself; `reapply` needs the committed configuration replayed
(the convergence page's button, or a commit); `hardware` and `far-end` need
hands. Module diagnostics are an I2C read per present module, so callers
rate-limit them (the watcher: once a minute); presence is cheap.
"""
import json
import os
import sys
import time
from pathlib import Path

PORTS = tuple(range(5, 21))
HEALTH = Path('/run/ffn-sfp-health.json')
JOURNAL = Path('/etc/ffn')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
LASER_MIN_DBM = -20.0      # any launched signal sits well above this; below it the laser is off
RX_MIN_DBM = -30.0         # below the receiver's floor: dark fibre or a dead far end
INTERFACES = {1000: 'BCM_PORT_IF_GMII', 10000: 'BCM_PORT_IF_XFI'}
REMEDY = {'transmitter-off': 'enable-transmitter', 'link-mode-mismatch': 'relink',
          'attachment-pending': 'reapply', 'attachment-stale': 'reapply', 'attachment-off': 'reapply',
          'tx-fault': 'hardware', 'laser-off': 'hardware', 'no-rx-light': 'far-end', 'link-down': 'far-end'}
ORDER = ('no-module', 'disabled', 'transmitter-off', 'tx-fault', 'laser-off', 'no-rx-light', 'link-mode-mismatch',
         'link-down', 'attachment-pending', 'attachment-stale', 'attachment-off', 'ok')


def expected_interface(module, configured_speed):
    """The switch interface mode a module of this kind should run at the configured speed, or None."""
    if not isinstance(module, dict) or not module.get('speeds'):
        return None
    speeds = sorted(int(s) for s in module['speeds'])
    if configured_speed not in (None, 'auto'):
        try:
            wanted = int(configured_speed)
        except ValueError:
            return None
        return INTERFACES.get(wanted) if wanted in speeds else None
    return INTERFACES.get(speeds[-1])


def attachment(port, journal=JOURNAL, current_epoch=None):
    """The CP's physical attachment journal for `port`: state in {none, pending, stale, enabled, disabled}."""
    path = journal / ('physical-%d.json' % port)
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(state='none', enabled=False, pending=None, epoch=None)
    if not isinstance(row, dict):
        return dict(state='none', enabled=False, pending=None, epoch=None)
    pending = row.get('pending')
    epoch = row.get('epoch')
    if pending:
        state = 'pending'
    elif current_epoch is not None and epoch != current_epoch:
        state = 'stale'
    else:
        state = 'enabled' if row.get('enabled') is True else 'disabled'
    return dict(state=state, enabled=row.get('enabled') is True, pending=pending, epoch=epoch)


def check_port(port, *, cage, module, diagnostics, link, attached, committed):
    """One port's verdict from its layers; pure, so the matrix is testable.

    cage: {present, tx_disable} or None when the cage controls are unreadable;
    module: the cached identity summary or None; diagnostics: the SFF-8472
    record, None, or {'error': text}; link: the faceplate's port row;
    attached: attachment(); committed: {enabled, speed} from the faceplate journal.
    """
    result = dict(port=port, interface='ethernet1/%d' % port, cage=cage, module=module, diagnostics=diagnostics,
                  link=dict(mac_enabled=link.get('mac_enabled'), link=link.get('link'), interface=link.get('interface'),
                            link_mode=link.get('link_mode'), autoneg=link.get('autoneg'), configured_speed=link.get('configured_speed')),
                  attachment=attached, committed=committed, expected_interface=expected_interface(module, committed.get('speed')))
    verdict, detail = 'ok', ''
    if not cage:
        verdict, detail = 'no-module', 'cage controls unreadable'
    elif not cage.get('present'):
        verdict, detail = 'no-module', 'no transceiver in the cage'
    elif not committed.get('enabled'):
        verdict, detail = 'disabled', 'port disabled in the committed configuration'
    elif cage.get('tx_disable'):
        verdict, detail = 'transmitter-off', 'TX_DISABLE asserted by the cage control line'
    elif isinstance(diagnostics, dict) and diagnostics.get('diagnostics_available'):
        if diagnostics.get('tx_disable'):
            verdict, detail = 'transmitter-off', 'module reports its transmitter disabled'
        elif diagnostics.get('tx_fault'):
            verdict, detail = 'tx-fault', 'module reports a transmitter fault'
        elif diagnostics.get('tx_power_dbm') is None or diagnostics['tx_power_dbm'] < LASER_MIN_DBM:
            verdict, detail = 'laser-off', 'launch power %s dBm' % diagnostics.get('tx_power_dbm')
        elif diagnostics.get('rx_los') or (diagnostics.get('rx_power_dbm') is not None and diagnostics['rx_power_dbm'] < RX_MIN_DBM):
            verdict, detail = 'no-rx-light', 'no light from the far end (RX %s dBm)' % diagnostics.get('rx_power_dbm')
    if verdict == 'ok':
        expected = result['expected_interface']
        actual = link.get('interface')
        if expected and actual and actual != expected:
            verdict, detail = 'link-mode-mismatch', 'switch runs %s, module needs %s' % (actual, expected)
        elif link.get('mac_enabled') is False:
            verdict, detail = 'link-down', 'switch MAC disabled'
        elif link.get('link') is False:
            verdict, detail = 'link-down', 'no link on the switch port'
        elif attached['state'] == 'pending':
            verdict, detail = 'attachment-pending', 'dataplane attachment left pending by an interrupted apply'
        elif attached['state'] == 'stale':
            verdict, detail = 'attachment-stale', 'dataplane attachment is from another switch lifetime'
        elif attached['state'] in ('none', 'disabled') and committed.get('attached', True):
            verdict, detail = 'attachment-off', 'no dataplane attachment for this port'
    if isinstance(diagnostics, dict) and diagnostics.get('error') and verdict == 'ok':
        detail = 'module diagnostics unreadable: ' + str(diagnostics['error'])[:120]
    result.update(verdict=verdict, detail=detail, remedy=REMEDY.get(verdict))
    return result


def collect(ports=PORTS, diagnostics=True, sources=None):
    """Run the check against the live hardware; `sources` injects the readers for tests."""
    s = sources or live_sources()
    inventory = s['inventory']()
    observed = s['faceplate']()
    rows = {p['port']: p for p in observed.get('ports', [])}
    saved = observed.get('saved', {})
    epoch = s['epoch']()
    results = []
    for port in ports:
        cage = inventory.get(port) or inventory.get(str(port))
        cage = dict(present=bool(cage['present']), tx_disable=bool(cage.get('tx_disable'))) if cage else None
        module = s['module'](port, bool(cage and cage['present'])) if cage else None
        record = None
        if diagnostics and cage and cage['present']:
            try:
                record = s['diagnostics'](port)
            except Exception as error:   # an I2C fault must leave the other layers readable
                record = dict(error=str(error)[:160])
        row = rows.get(port, {})
        committed = dict(enabled=saved.get('ports', {}).get(str(port), row.get('enabled', False)) is True,
                         speed=saved.get('speeds', {}).get(str(port), 'auto'),
                         attached=s['committed_attachment'](port))
        results.append(check_port(port, cage=cage, module=module, diagnostics=record, link=row,
                                  attached=attachment(port, s['journal'], epoch), committed=committed))
    return results


def live_sources():
    import ffn_faceplate as faceplate
    import ffn_sfp_control as sfp
    from ffn_copper_forwarding import epoch
    return dict(inventory=sfp.inventory, faceplate=faceplate.observe, module=faceplate.sfp_module, diagnostics=sfp.diagnostics,
                epoch=lambda: _quiet(epoch), journal=JOURNAL, committed_attachment=lambda port: True)


def _quiet(call):
    try:
        return call()
    except Exception:
        return None


def publish(results, path=HEALTH, clock=time.time):
    try:
        boot = BOOT_ID.read_text().strip()
    except OSError:
        boot = None
    payload = dict(schema=1, boot_id=boot, checked_at=clock(), ports=results)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with open(temp, 'w', opener=lambda p, flags: os.open(p, flags, 0o644)) as handle:
        json.dump(payload, handle)
    os.replace(temp, path)
    return payload


def summary(results):
    """The verdict counts, for a one-line log or a convergence detail."""
    counts = {}
    for row in results:
        counts[row['verdict']] = counts.get(row['verdict'], 0) + 1
    return {k: counts[k] for k in ORDER if k in counts}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', nargs='?', default='status', choices=['status'])
    parser.add_argument('--ports', help='comma-separated faceplate ports, default 5-20')
    parser.add_argument('--no-diagnostics', action='store_true', help='skip the per-module I2C diagnostics read')
    args = parser.parse_args(argv)
    sys.path.insert(0, '/usr/local/sbin')
    ports = tuple(int(p) for p in args.ports.split(',')) if args.ports else PORTS
    if any(p not in PORTS for p in ports):
        raise SystemExit('ports must be within 5..20')
    results = collect(ports, diagnostics=not args.no_diagnostics)
    print(json.dumps(publish(results)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
