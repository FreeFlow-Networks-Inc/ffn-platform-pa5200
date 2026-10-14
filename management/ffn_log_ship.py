#!/usr/bin/env python3
"""Ship the plane journals (CP, DP) to the MP's RAID.

The plane roots are NFS exports of the MP's system disk, so their journald
keeps the journal in RAM (journald-ffn-volatile.conf). This timer job pulls
what is new since the last run with `journalctl -o export --after-cursor`
over the existing control-channel ssh paths and appends it under
/opt/ffn-logs/journal/<plane>/<day>.export; the cursor of the last entry is
kept beside it. Reading back: `journalctl --file` cannot read export files;
use `systemd-journal-remote -o out.journal <file>.export` or grep the text.
Each pull is bounded in time and size so a backlog never holds the MP.
"""
import datetime
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path('/opt/ffn-logs/journal')
PLANES = {
    'cp': ['/usr/local/sbin/ffn-cp'],
    'dp': ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
           '-o', 'UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
           '-o', 'ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W 127.1.2.2:22 ffn-cp', 'root@127.1.2.2'],
}
FIRST_PULL = '--since=-2h'      # a plane whose cursor we do not know yet
TIMEOUT = 90                    # seconds per plane
MAX_BYTES = 64 * 1024 * 1024    # per pull; the rest comes next time


def last_cursor(export):
    """The __CURSOR of the last complete entry in an export stream, or None."""
    cursor = None
    for line in export.split(b'\n'):
        if line.startswith(b'__CURSOR='):
            cursor = line[9:].decode('ascii', 'replace')
    return cursor


def day_file(root, plane, when=None):
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return root / plane / (when.strftime('%Y-%m-%d') + '.export')


def command(plane, cursor):
    select = '--after-cursor=' + cursor if cursor else FIRST_PULL
    return PLANES[plane] + ["journalctl -o export --no-pager -q '" + select + "'"]


def pull(plane, root=ROOT, run=subprocess.run, clock=None):
    directory = root / plane
    directory.mkdir(parents=True, exist_ok=True)
    cursor_file = directory / 'cursor'
    cursor = cursor_file.read_text().strip() if cursor_file.exists() else None
    done = run(command(plane, cursor), capture_output=True, timeout=TIMEOUT)
    if done.returncode:
        return dict(plane=plane, ok=False, error=done.stderr.decode('utf-8', 'replace')[-300:].strip())
    truncated = len(done.stdout) > MAX_BYTES
    data = done.stdout[:MAX_BYTES]
    if truncated:
        # Never cut an entry in half: entries are separated by a blank line.
        data = data[:data.rfind(b'\n\n') + 2]
    new = last_cursor(data)
    if new:
        target = day_file(root, plane, clock() if clock else None)
        with open(target, 'ab') as handle:
            handle.write(data)
        tmp = cursor_file.with_suffix('.tmp')
        tmp.write_text(new + '\n')
        os.replace(tmp, cursor_file)
    return dict(plane=plane, ok=True, bytes=len(data), truncated=truncated, cursor=new)


def main():
    results = [pull(plane) for plane in PLANES]
    for row in results:
        print(row, flush=True)
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
