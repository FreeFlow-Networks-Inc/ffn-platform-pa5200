#!/usr/bin/env python3
"""Append the missing native-userland stages without changing existing stages."""
from pathlib import Path
import shutil

script = Path('/mnt/clones/debian-mips64/sid-host/root/rebootstrap/bootstrap.sh')
text = script.read_text()
marker = '. /root/rebootstrap/complete-port.sh'
anchor = 'echo "checking installability of build-essential with dose"'
if marker not in text:
    assert text.count(anchor) == 1
    backup = script.with_name('bootstrap.sh.before-complete-port')
    if not backup.exists():
        shutil.copy2(script, backup)
    script.write_text(text.replace(anchor, marker + '\n\n' + anchor))
