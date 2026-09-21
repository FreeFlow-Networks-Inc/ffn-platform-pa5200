#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Guards on boot618-pcie.sh, because the ways it breaks are all silent.

This script is not run by hand. `ffn-octeon.service` runs `ffn-boot-debian-planes`,
which runs `test-cp-kernel.sh`, which **rewrites this file with a regex** and
executes the copy. So the harness upstream has a contract with this file's TEXT,
and nothing checked that contract at edit time -- a change that breaks it shows
up as a control plane that will not boot, with the dataplane following it down.

Four failure modes, all of which produce a dead CP rather than an error:

  * `test-cp-kernel.sh` does `re.subn(r'^K=.*$', ..., count=1)` and asserts the
    count is 1. Add a second line starting with `K=` and that assert fires. It
    also asserts the `--extra` string appears exactly once, because it splices
    the DMA reserve into it.
  * The INNER block is a QUOTED heredoc fed to `bash -s`, so it is a separate
    program that `bash -n` on this file never checks. A syntax error there is
    found at boot, after the OCTEON has already been reset into u-boot.
  * That child shell runs under `set -u` and does NOT inherit unexported
    variables. The file's own comment explains this for `K`; the same trap
    applies to anything else the block reads.
  * A backslash-newline that loses its newline still passes `bash -n`. An edit
    of the FPGA block did exactly that: two halves of a pipeline joined into one
    line, the surviving backslash escaping a tab into an argument to `tail`.
    Valid shell, wrong program, no error anywhere.
"""
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'boot618-pcie.sh')

# Verbatim from test-cp-kernel.sh. Duplicated rather than described, so that a
# change there fails here instead of at boot.
EXTRA_ANCHOR = '--extra "ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M"'

# Shell specials and environment a child shell already has.
NOT_OURS = {'?', '!', '$', '0', '1', '2', '_', 'PATH', 'HOME', 'PWD', 'IFS',
            'SHELL', 'USER', 'LANG', 'PS1', 'RANDOM', 'LINENO'}


def source():
    with open(SCRIPT, encoding='utf-8') as fh:
        return fh.read()


def inner_block(src):
    """The quoted heredoc that actually runs under flock."""
    return src.split("<<'INNER'\n", 1)[1].split("\nINNER\n", 1)[0]


def fpga_block(inner):
    """Just the FPGA step, from its `if` to the else-branch message."""
    return inner[inner.index('if [ "$FFN_CP_FPGA" = 1 ]'):
                 inner.index('CE40 FPGA programming SKIPPED')]


def code_only(text):
    """Drop comment lines.

    A rule about what the script DOES must not be satisfied, or broken, by
    prose explaining the rule -- the first version of the --reprogram test
    failed on the comment saying it deliberately does not pass --reprogram.
    """
    return '\n'.join(l for l in text.splitlines()
                     if not l.lstrip().startswith('#'))


def check_shell(case, text, label):
    """bash -n, skipped rather than failed where bash is absent."""
    bash = shutil.which('bash')
    if not bash:
        case.skipTest('no bash available to syntax-check %s' % label)
    fd, path = tempfile.mkstemp(suffix='.sh')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        done = subprocess.run([bash, '-n', path], capture_output=True, text=True)
        case.assertEqual(done.returncode, 0,
                         '%s is not valid shell:\n%s' % (label, done.stderr))
    finally:
        os.unlink(path)


class HarnessContract(unittest.TestCase):
    """What test-cp-kernel.sh assumes about this file's text."""

    def test_exactly_one_kernel_assignment(self):
        """Stricter than the harness, deliberately.

        `test-cp-kernel.sh` uses `count=1`, which means AT MOST one
        substitution -- so its `assert n == 1` only proves a `K=` line exists,
        not that there is just one. A second one would sail through and then
        win at runtime, because the last assignment is the one that takes
        effect: the harness would rewrite the first line and the boot would use
        the stale second. A mutation adding a second `K=` line passed the
        faithful version of this test, which is why it now counts lines.
        """
        lines = re.findall(r'^K=.*$', source(), flags=re.M)
        self.assertEqual(len(lines), 1,
                         'expected exactly one K= line, found %r' % lines)
        _, n = re.subn(r'^K=.*$', 'K=' + shlex.quote('/probe'), source(),
                       count=1, flags=re.M)
        self.assertEqual(n, 1, 'test-cp-kernel.sh requires a ^K= line to exist')

    def test_exactly_one_extra_anchor(self):
        self.assertEqual(
            source().count(EXTRA_ANCHOR), 1,
            'test-cp-kernel.sh splices the DMA reserve into this exact string '
            'and asserts it appears exactly once')

    def test_the_rewritten_copy_is_what_actually_runs(self):
        """Check the file as the harness rewrites it, not as it is written."""
        src, _ = re.subn(r'^K=.*$', 'K=' + shlex.quote('/probe'), source(),
                         count=1, flags=re.M)
        src = src.replace(EXTRA_ANCHOR,
                          EXTRA_ANCHOR[:-1] + ' ffn_reserve=0x30000000,64M"')
        self.assertIn('ffn_reserve=0x30000000,64M', src)
        check_shell(self, src, 'rewritten script')


class InnerBlock(unittest.TestCase):
    """The heredoc is a separate program; bash -n on the file misses it."""

    def setUp(self):
        self.src = source()
        self.inner = inner_block(self.src)

    def test_inner_is_valid_shell_on_its_own(self):
        check_shell(self, self.inner, 'INNER heredoc')

    def test_everything_it_reads_is_assigned_there_or_exported(self):
        """set -u plus a non-inheriting child shell equals a dead control plane.

        `bash -s` does not inherit unexported variables, and under `set -u` the
        first unset one aborts the block -- after the OCTEON has been reset into
        u-boot and before the kernel is staged, which is the worst possible
        moment. So every name the block reads must either be assigned inside it
        or exported by the parent.
        """
        assigned = set(re.findall(r'^\s*(\w+)=', self.inner, re.M))
        assigned |= set(re.findall(r'for\s+(\w+)\s+in', self.inner))
        read = set(re.findall(r'\$\{?(\w+)\}?', self.inner))
        exported = set(re.findall(r'^export (\w+)\s*$', self.src, re.M))
        missing = sorted(read - assigned - exported - NOT_OURS)
        self.assertEqual(
            missing, [],
            'the INNER block reads %s, which bash -s will not inherit unless '
            'the parent exports it' % missing)

    def test_the_export_guard_would_catch_a_regression(self):
        """The guard above is worthless if it cannot fail; prove it can."""
        src = self.src.replace('export K\n', '', 1)
        assigned = set(re.findall(r'^\s*(\w+)=', self.inner, re.M))
        assigned |= set(re.findall(r'for\s+(\w+)\s+in', self.inner))
        read = set(re.findall(r'\$\{?(\w+)\}?', self.inner))
        exported = set(re.findall(r'^export (\w+)\s*$', src, re.M))
        self.assertIn('K', read - assigned - exported - NOT_OURS)


class FpgaStep(unittest.TestCase):
    """The CE40 load has exactly one window, and must never cost the boot."""

    def setUp(self):
        self.inner = inner_block(source())
        self.block = fpga_block(self.inner)

    def test_it_runs_while_the_cp_is_still_in_uboot(self):
        """fpga_program exists only in the CP bootloader.

        Before the readiness check there is no bootloader to talk to; after the
        kernel boots the command is gone. The window is exactly here.
        """
        ready = self.inner.index('ABORT: reset/u-boot stage failed')
        fpga = self.inner.index('program the CE40 FPGA')
        kernel = self.inner.index('stage kernel over the BAR window')
        self.assertLess(ready, fpga, 'FPGA step precedes the u-boot check')
        self.assertLess(fpga, kernel, 'FPGA step follows the kernel boot')

    def test_it_is_serialised_with_every_other_octeon_user(self):
        """Two concurrent oct users wedge the serial in uninterruptible-D."""
        src = source()
        self.assertIn('flock -w 60 /run/ffn-octeon-ctl.lock', src)
        self.assertIn('program the CE40 FPGA', inner_block(src),
                      'the FPGA step must be inside the flocked block')

    def test_a_failure_never_stops_the_boot(self):
        """A firewall that boots without offload beats one that will not boot."""
        code = code_only(self.block)
        for fatal in ('exit', 'ABORT', 'set -e'):
            self.assertNotIn(fatal, code,
                             'the FPGA step must fall through to the kernel')

    def test_it_is_bounded_in_time(self):
        """The whole plane boot has a 1200 s budget; this cannot hang it."""
        self.assertRegex(code_only(self.block),
                         r'timeout \d+ python3 tools/ffn_octctl\.py fpga')

    def test_it_does_not_force_a_reprogram(self):
        """u-boot skips an already-programmed FPGA, and that is what we want.

        Only a cold boot clears DONE. Forcing on every boot would reprogram a
        working FPGA for no reason and widen the window where it is unusable.
        """
        self.assertNotIn('--reprogram', code_only(self.block))

    def test_the_outcome_is_read_from_the_console(self):
        """The tool returns when the mailbox ACCEPTED the command, not when the
        bootloader finished. Only the console says what actually happened, and
        waiting for it also stops kernel staging racing a load in flight."""
        code = code_only(self.block)
        self.assertIn('Full fpga programming', code)
        self.assertIn('/var/log/ffn-octeon-console.log', code)

    def test_it_uses_no_line_continuations(self):
        """A backslash-newline that loses its newline still passes bash -n.

        That happened to this block once already: the halves of a pipeline
        joined, and the surviving backslash escaped a tab into an argument to
        tail. One command per line cannot fail that way.
        """
        offenders = [l for l in self.block.splitlines()
                     if l.rstrip().endswith(chr(92))]
        self.assertEqual(offenders, [],
                         'FPGA block must not use line continuations')

    def test_there_is_an_opt_out(self):
        self.assertIn('FFN_CP_FPGA', source())
        self.assertIn('FFN_CP_FPGA=0', self.inner)


if __name__ == '__main__':
    unittest.main(verbosity=2)
