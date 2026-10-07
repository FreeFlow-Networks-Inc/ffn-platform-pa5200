"""tools/ffn-fpga-step.sh, tools/ffn_fpga_program.py, and the boot scripts that call them.

The CE40 FPGA has exactly one programming window -- after the reset into
u-boot, before the kernel is staged -- and one rule that was learned the hard
way: any Octeon reset after programming reverts the socket to the a101 image
with DONE set, so the load must be forced on every boot and the kernel must be
booted from that same u-boot session. These tests pin the helper's contract,
the command it sends, and the call sites' ordering so none of it drifts back.
"""
import os
import py_compile
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STEP = os.path.join(HERE, "ffn-fpga-step.sh")
PROG = os.path.join(HERE, "ffn_fpga_program.py")
UP = os.path.join(HERE, "ffn-octeon-up.sh")
PCNET = os.path.join(HERE, "pcnet-up.sh")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def code_only(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def command_line():
    """The literal the programmer sends, with a representative size."""
    src = read(PROG)
    m = re.search(r'return "([^"]*fpga_program[^"]*)"', src)
    assert m, "command() must build the fpga_program line from one literal"
    return m.group(1) % (0x400000, 0x2e04280)


class FpgaStepHelper(unittest.TestCase):
    def setUp(self):
        self.src = read(STEP)
        self.code = code_only(self.src)

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_it_parses(self):
        subprocess.run(["bash", "-n", STEP], check=True)

    def test_it_delegates_to_the_programmer_and_is_bounded_in_time(self):
        self.assertRegex(self.code, r"timeout \d+ python3 tools/ffn_fpga_program\.py")

    def test_it_is_never_fatal(self):
        """A firewall that boots without the CE40 beats one that will not boot."""
        exits = re.findall(r"\bexit (\d+)", self.code)
        self.assertTrue(exits, "the helper should exit explicitly")
        self.assertEqual(set(exits), {"0"}, "every exit must be 0")
        for fatal in ("set -e", "ABORT"):
            self.assertNotIn(fatal, self.code)

    def test_it_has_no_backslashes_at_all(self):
        """The tooling that edits these scripts strips one backslash level."""
        self.assertNotIn(chr(92), self.src)

    def test_it_records_the_personality_for_the_status_layer(self):
        self.assertIn("/run/ffn-ce40-personality", self.code)
        self.assertIn("a00d-programmed", self.code)

    def test_there_is_an_opt_out(self):
        self.assertIn("FFN_CP_FPGA", self.code)


class FpgaProgrammer(unittest.TestCase):
    def setUp(self):
        self.src = read(PROG)

    def test_it_compiles(self):
        py_compile.compile(PROG, doraise=True)

    def test_the_command_carries_the_selector(self):
        """ce40= is the FPGA selector. Without it u-boot prints 'programming
        unknown' and hangs -- measured twice, 2026-09-20 and 2026-10-06."""
        self.assertIn("ce40=", command_line())

    def test_the_command_forces_a_reprogram(self):
        """DONE set after a reset means the a101 image is loaded, not that the
        CE40 personality is. u-boot would skip without force."""
        self.assertRegex(command_line(), r"\bforce\b")

    def test_the_command_programs_from_dram_with_a_size(self):
        cmd = command_line()
        self.assertIn("load=none", cmd)
        self.assertRegex(cmd, r"\bsize=[0-9a-f]+\b")
        self.assertRegex(cmd, r"\baddr=[0-9a-f]+\b")

    def test_the_verdict_is_read_from_the_console(self):
        """Only the bootloader's own console line says what happened."""
        self.assertIn("Full fpga programming SUCCESS", self.src)
        self.assertIn("ob.fifo(", self.src)
        self.assertNotIn("oct_send_bootcmd", self.src)

    def test_it_proves_the_stage_before_programming(self):
        self.assertIn("sha256", self.src)
        self.assertIn("w.read(ADDR", self.src)

    def test_it_depends_only_on_the_stable_primitives(self):
        """ffn_octctl/ffn_oct differ between the MP and main; the programmer
        must work on both, so it may import neither."""
        imports = re.findall(r"^\s*(?:import|from)\s+(\w+)", self.src, re.M)
        self.assertNotIn("ffn_octctl", imports)
        self.assertNotIn("ffn_oct", imports)
        self.assertIn("ffn_octdram", imports)
        self.assertIn("ffn_octboot", imports)


class OcteonUpWiring(unittest.TestCase):
    def setUp(self):
        self.code = code_only(read(UP))

    def test_the_step_sits_between_the_reset_and_the_kernel(self):
        """fpga_program exists only in u-boot: before the reset there is no
        bootloader to talk to, after ffn_octboot.py the kernel owns the CP."""
        reset = self.code.index("python3 tools/ffn_octctl.py boot --dev 0 --force")
        step = self.code.index("ffn-fpga-step.sh")
        kernel = self.code.index("python3 tools/ffn_octboot.py")
        self.assertLess(reset, step, "the FPGA step must follow the reset")
        self.assertLess(step, kernel, "the FPGA step must precede kernel staging")

    def test_the_step_is_called_exactly_once(self):
        self.assertEqual(self.code.count("ffn-fpga-step.sh"), 1)

    def test_nothing_resets_the_octeon_between_the_step_and_the_kernel(self):
        """The whole point: a second reset would revert the socket to a101."""
        step = self.code.index("ffn-fpga-step.sh")
        kernel = self.code.index("python3 tools/ffn_octboot.py")
        between = self.code[step:kernel]
        self.assertNotIn("ffn_octctl.py boot", between)
        self.assertNotIn("oct-remote-reset", between)


class PcnetUpUnit(unittest.TestCase):
    def setUp(self):
        self.src = read(PCNET)
        self.code = code_only(self.src)

    def test_it_prefers_the_persistent_unit(self):
        """systemd-run refuses a transient unit whose name is already loaded,
        which is exactly what a stopped persistent ffn-pcnetd.service is."""
        self.assertIn("systemctl cat ffn-pcnetd", self.code)
        self.assertIn("systemctl start ffn-pcnetd", self.code)

    def test_the_transient_form_remains_as_the_fallback(self):
        self.assertIn("systemd-run --unit=ffn-pcnetd", self.code)
        self.assertLess(self.code.index("systemctl start ffn-pcnetd"),
                        self.code.index("systemd-run --unit=ffn-pcnetd"))

    def test_no_line_continuations(self):
        offenders = [l for l in self.src.splitlines() if l.rstrip().endswith(chr(92))]
        self.assertEqual(offenders, [])

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_it_parses(self):
        subprocess.run(["bash", "-n", PCNET], check=True)


if __name__ == "__main__":
    unittest.main()
