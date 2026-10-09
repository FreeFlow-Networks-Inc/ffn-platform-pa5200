#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Boot-time FE100 datapath initialisation for the PA-5200 CP.

Runs the commissioned initialisation sequence (packet-processing blocks,
parser, TCAM clocks, TDI DDR training and audit, external lookup tables,
FHM/FDT flow memory, FCM counter memory, FLU and SEM) through the existing
audited tools, one step at a time, journaled per CP boot, so the session
readiness gates in ffn_fe100_live_sessions clear on every boot without lab
intervention. Nothing here writes a register itself; every step is one of the
tools already qualified on the appliance, with the same environment the
control owner uses.

Idempotent within a boot: a step recorded completed is not repeated (the tools
refuse replays of training and initialisation), a boot whose hardware already
reads initialised is left alone, and a failed step is reported rather than
retried, because recovery from partial training has its own documented path.
No FE100 reset, no production admission, no policy change.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/var/lib/ffn/fe100')
TOOLS = Path('/usr/local/sbin')
STEP_TIMEOUT = 150
LOCK_RETRIES = 4
ENV = dict(LD_LIBRARY_PATH='/usr/local/lib64:/usr/local/lib64/3p:/usr/local/lib/ffn/owner-deps',
           LD_PRELOAD='/usr/lib/mips64-linux-gnuabi64/libsqlite3.so.0')
BLOCKS = ('tlu', 'prw', 'lag', 'qmm', 'lef', 'fwd', 'dfp', 'acl', 'lif', 'par', 'cfp', 'egr', 'ipq', 'nif', 'tmi')


def plan():
    """The commissioned order; the same sequence the clean rebuild ran."""
    steps = [('block-' + b, ['ffn_fe100_block_init.py', '--block', b, '--apply'] + (['--parser-json'] if b == 'par' else []))
             for b in BLOCKS]
    steps += [('parser', ['ffn_fe100_parser_apply.py', 'apply']),
              ('tcam-clocks', ['ffn_fe100_clocks.py', '--apply-tcam']),
              ('archive-generation', None),
              ('ddr-clocks', ['ffn_fe100_ddr.py', '--prepare-clocks']),
              ('ddr-train', ['ffn_fe100_ddr.py', '--train']),
              ('ddr-audit', ['ffn_fe100_ddr_audit.py', '/usr/local/lib/ffn/libffn-fe100-ddr-capture.so',
                             '--output', str(ROOT / 'ddr-config-audit.json')]),
              ('ddr-verify', ['ffn_fe100_ddr.py', '--verify-memory']),
              ('external-tables', ['ffn_fe100_external_runtime.py', '--activate'])]
    for block, channels in (('fhm', (3, 4)), ('fdt', (5, 6))):
        steps.append((block + '-clocks', ['ffn_fe100_flow_memory.py', '--block', block, '--prepare-clocks']))
        steps += [(block + '-train-' + str(c), ['ffn_fe100_flow_memory.py', '--block', block, '--train', str(c)]) for c in channels]
    steps += [('fcm-clocks', ['ffn_fe100_fcm.py', '--clocks']),
              ('fcm-train-0', ['ffn_fe100_fcm.py', '--train', '0']),
              ('fcm-train-1', ['ffn_fe100_fcm.py', '--train', '1']),
              ('flu', ['ffn_fe100_flu.py', '--apply']),
              ('sem', ['ffn_fe100_sem.py', '--apply'])]
    return steps


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def durable(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as out:
        json.dump(value, out, indent=2); out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)


class Runner:
    """Runs one tool; retries only the initial table-lock contention."""
    def __init__(self, env=None, timeout=STEP_TIMEOUT, sleep=time.sleep):
        self.env = dict(os.environ, **(env or ENV)); self.timeout = timeout; self.sleep = sleep

    def __call__(self, argv):
        for attempt in range(LOCK_RETRIES + 1):
            p = subprocess.run([sys.executable, str(TOOLS / argv[0])] + argv[1:], env=self.env,
                               capture_output=True, text=True, timeout=self.timeout)
            contended = p.returncode and not p.stdout and 'BlockingIOError' in p.stderr and 'flock' in p.stderr
            if not contended or attempt == LOCK_RETRIES:
                return p.returncode, p.stdout, p.stderr
            self.sleep(0.5)


def readiness(run):
    """Read-only readiness report from the session owner's evaluator."""
    rc, out, err = run(['ffn_fe100_live_sessions.py'])
    if rc:
        raise RuntimeError('readiness evaluation failed: ' + (err or out)[-400:])
    report = json.loads(out)
    if not isinstance(report, dict) or 'blockers' not in report:
        raise RuntimeError('unexpected readiness report')
    return report


EXTERNAL_TABLES = 'external-tables.json'


def tdi_in_reset(run):
    """True when the TDI DDR reads the audited reset state (read-only report)."""
    from ffn_fe100_ddr import DDR, DDR_RESET, RST
    rc, out, err = run(['ffn_fe100_ddr.py'])
    if rc:
        raise RuntimeError('DDR read-only report failed: ' + (err or out)[-300:])
    before = json.loads(out)['before']
    return tuple(before[hex(r)] for r in DDR) == tuple(DDR_RESET) and before[hex(RST)] == 0x3464


def archive_generation(run, root, boot, reset=tdi_in_reset, clock=time.time_ns):
    """Retire a previous boot's external-table journal once its memory is gone.

    The DDR tool refuses to re-initialise while that journal exists, because a
    configured table lives behind the TDI DDR and retraining would silently
    invalidate it. After a reset the memory is uninitialised and the journal
    describes nothing, so it is archived (never deleted) and this boot starts a
    new generation. Retained, trained memory is left for the recovery path.
    """
    path = root / EXTERNAL_TABLES
    if not path.exists():
        return {'archived': None, 'reason': 'no external-table journal'}
    try:
        record = json.loads(path.read_text())
    except ValueError:
        record = {}
    if record.get('cp_boot_id') == boot:
        return {'archived': None, 'reason': 'journal belongs to this boot'}
    if not reset(run):
        raise RuntimeError('external tables of a previous boot are retained in trained TDI memory; recovery review required')
    archive = root / ('external-tables-archived-%s-%d.json' % (str(record.get('cp_boot_id', 'unknown'))[:8], clock()))
    os.replace(path, archive)
    return {'archived': str(archive), 'previous_boot': record.get('cp_boot_id'), 'previous_stage': record.get('stage')}


def links_active(check=None):
    check = check or (lambda: subprocess.run(['systemctl', 'is-active', '--quiet', 'ffn-fe100-links.service']).returncode == 0)
    return check()


def initialise(run, root=ROOT, boot=None, links=links_active, now=time.monotonic, log=print, archive=archive_generation):
    """Bring this boot's FE100 to session readiness; returns the journal."""
    boot = boot or boot_id()
    root.mkdir(parents=True, exist_ok=True)
    journal_path = root / ('init-' + boot + '.json')
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else \
        {'schema': 1, 'cp_boot_id': boot, 'stage': 'started', 'steps': [], 'started_monotonic': now()}
    if journal.get('cp_boot_id') != boot:
        raise RuntimeError('journal belongs to another boot')
    done = {s['name'] for s in journal['steps'] if s.get('rc') == 0}
    failed = [s for s in journal['steps'] if s.get('rc') != 0]
    try:
        before = readiness(run)
        journal['readiness_before'] = {'initialized': before['initialized'], 'blockers': before['blockers']}
        if before['initialized']:
            journal.update(stage='already-initialized', readiness=journal['readiness_before'])
            durable(journal_path, journal); log('FE100 already initialised in this boot'); return journal
        if failed:
            raise RuntimeError('step %s failed earlier in this boot; recovery is a documented manual path' % failed[-1]['name'])
        if not links(): raise RuntimeError('ffn-fe100-links.service is not active')
        for index, (name, argv) in enumerate(plan()):
            if name in done: continue
            started = now()
            if argv is None:
                result = archive(run, root, boot)
                journal['steps'].append({'name': name, 'command': None, 'rc': 0, 'seconds': round(now() - started, 1), 'result': result})
                durable(journal_path, journal); log('%-18s %s' % (name, result.get('archived') or result.get('reason'))); continue
            rc, out, err = run(argv)
            logfile = root / ('init-%s-%02d-%s.log' % (boot[:8], index, name))
            logfile.write_text(out + err)
            journal['steps'].append({'name': name, 'command': argv, 'rc': rc, 'seconds': round(now() - started, 1), 'log': str(logfile)})
            durable(journal_path, journal)
            log('%-18s rc=%d %.1fs' % (name, rc, now() - started))
            if rc:
                raise RuntimeError('%s failed (rc %d): %s' % (name, rc, (err or out).strip()[-300:]))
        after = readiness(run)
        journal['readiness'] = {'initialized': after['initialized'], 'blockers': after['blockers'], 'action_blockers': after.get('action_blockers')}
        if not after['initialized']:
            raise RuntimeError('sequence completed but readiness still blocked: ' + '; '.join(after['blockers']))
        journal['stage'] = 'completed'
    except BaseException as error:
        journal.update(stage='failed', error=str(error)); durable(journal_path, journal); raise
    durable(journal_path, journal)
    log('FE100 initialised: %d steps, %.0fs' % (len(journal['steps']), now() - journal['started_monotonic']))
    return journal


def status(root=ROOT, boot=None):
    boot = boot or boot_id()
    path = root / ('init-' + boot + '.json')
    return json.loads(path.read_text()) if path.exists() else {'cp_boot_id': boot, 'stage': 'absent', 'steps': []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('run', 'status', 'plan'), default='run')
    args = parser.parse_args()
    if args.command == 'plan':
        print(json.dumps([{'name': n, 'command': c or 'archive the previous generation external-table journal'} for n, c in plan()], indent=2)); return
    if args.command == 'status':
        print(json.dumps(status(), indent=2)); return
    try:
        journal = initialise(Runner())
    except RuntimeError as error:
        print('FE100 initialisation failed: ' + str(error), file=sys.stderr); sys.exit(1)
    print(json.dumps({'stage': journal['stage'], 'steps': len(journal['steps']), 'readiness': journal.get('readiness')}))


if __name__ == '__main__':
    main()
