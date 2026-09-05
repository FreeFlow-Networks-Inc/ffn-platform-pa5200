#!/usr/bin/env python3
"""ffn-bcmd -- the BCM88375 control daemon. Runs ON the CP.

The WebUI and the FFN-CLI both live on the MP, but the BCM88375 is on the CP's
PCIe bus and only the CP can touch it. This daemon is the bridge: it owns the
chip and answers structured requests over the CP<->MP virtual ethernet.

    MP 127.1.1.1  --JSON/TCP 127.1.1.2:8104-->  ffn-bcmd  --pty-->  bcm.user

WHY A DAEMON AT ALL, rather than running bcm.user per request:

  1. A full chip init takes about 150 SECONDS (most of it DDR SHMOO training).
     Nothing interactive can pay that per request, so exactly one bcm.user is
     started here and held open for the life of the daemon.
  2. bcm.user is an interactive REPL and the chip is SINGLE-SESSION. Two
     concurrent users wedge it -- the same trap ffn-dpsh hit on the DP, recorded
     in DP-BRINGUP.md. So every request serialises on one lock, and the socket
     handler never touches the pty directly.
  3. bcm.user is statically linked, so with stdout on a pipe glibc block-buffers
     and a wedge loses everything since the last flush. It MUST be driven on a
     pty. That is not a nicety; it is the difference between a transcript and
     nothing.

WHAT IT DELIBERATELY DOES NOT DO:

  * It does not re-init the chip. If bcm.user dies, the daemon reports DEAD and
    stops answering chip questions rather than silently re-initing -- a re-init
    resets the switch and would drop live traffic. Recovery is an explicit
    operator action.
  * It does not parse `ps` when a cint call can answer instead. Shell text is a
    UI, not an API: `bcm_port_enable_set` either returns 0 or it does not,
    whereas a fixed-width table changes between SDK versions. Text parsing is
    used only where there is no better source (the port table itself).

PROTOCOL -- newline-delimited JSON, one request per line, one reply per line.
Chosen over anything framed or binary because it is debuggable with `nc` from a
busybox CP and readable in a packet capture.

    -> {"op": "status"}
    <- {"ok": true, "state": "ready", "chip": "BCM88375_B0", ...}

    -> {"op": "port.list"}
    <- {"ok": true, "ports": [{"name": "xe13", "port": 13, "enabled": true,
                               "link": false, "speed_mb": 10000, ...}, ...]}

    -> {"op": "port.set", "port": 13, "enable": true}
    <- {"ok": true, "port": 13, "enable": true}

Every reply has "ok". On failure: {"ok": false, "error": "...", "detail": "..."}.
A request that arrives before init finishes gets ok=false with state="init" and
an "eta_s" hint, so a UI can show progress instead of an error.
"""

import argparse
import json
import os
import re
import select
import signal
import socket
import sys
import threading
import time

# pty is imported lazily in Chip.start(), not here. It pulls in termios, which
# only exists on a real Unix -- and --no-chip mode never starts a process, so
# insisting on it up front would make the protocol layer untestable anywhere but
# the CP itself. The daemon proper always has it.

# ---------------------------------------------------------------------------
# Configuration. Defaults match the CP as brought up by ffn-bcm-prep.sh.
# ---------------------------------------------------------------------------

DEF_BIND = "127.1.1.2"          # the CP end of ffnnet0; see octeon/pcnet/
DEF_PORT = 8104
DEF_BCM = "/usr/local/ffn/bcm.user.hwswap"
DEF_CFG = "/tmp/bcmcfg"

# bcm.user's prompt. Unit 0 is the only unit on this board.
PROMPT = b"BCM.0>"

# A warm init is ~50 s (the DRAM tuning is reused once runningConfig.soc
# exists) and a cold one is far longer: octeon/BCM88375-PORTS.md records a
# MEASURED ~700 s to reach the prompt, most of it DDR SHMOO. The old 600 s
# ceiling was below that, so a genuine cold start timed out and latched
# state=dead -- and the daemon deliberately never re-inits, because a re-init
# resets the switch. 900 s still fails rather than hanging forever, and is
# above the number anyone has actually measured.
INIT_TIMEOUT = 900.0
CMD_TIMEOUT = 60.0

# The vendor's own front-panel list, from enable_fp_ports.c in the config dir.
# Kept here so port.list can mark which ports are faceplate ports without
# shelling out, and deliberately NOT re-derived from the port table: two of the
# chip's enabled ports (4, 5, 8, 9) are internal and must not be presented as
# front panel. See octeon/BCM88375-PORTS.md.
FACEPLATE = [28, 13, 14, 15, 16, 1, 18, 19, 6, 21, 22, 23, 7,
             11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35, 12]

# `ps` prints a fixed-width table whose rows look like:
#     xe13( 13)  down   10G  FD   SW  No   Disable  TX RX   None    D    XFI ...
#     xl2(  2)  up     40G  FD   SW  No   Disable  TX RX   None    D  XLAUI ...
# The name and number are reliable; everything after is positional and has
# changed shape between SDK releases, so only the fields we actually need are
# pulled out and the rest is kept as raw text for the caller to ignore.
PS_ROW = re.compile(
    r"^\s*(?P<name>[a-z]+\d+)\(\s*(?P<port>\d+)\)\s+"
    r"(?P<state>!ena|up|down|\S+)\s+"
    r"(?P<speed>[\d.]+[GM]|-)\s*"
    r"(?P<rest>.*)$"
)


def now():
    return time.time()


# ---------------------------------------------------------------------------
# The chip session: one bcm.user on a pty, and a lock around it.
# ---------------------------------------------------------------------------

class ChipBusy(Exception):
    pass


class ChipDead(Exception):
    pass


class Chip(object):
    """Owns exactly one bcm.user process and serialises access to it.

    State machine, which the wire protocol exposes verbatim so a UI can be
    honest about what is happening:

        init   -- bcm.user started, has not reached its prompt yet (~150 s)
        ready  -- at the prompt, accepting commands
        dead   -- the process exited or the pty closed; no recovery attempted
    """

    def __init__(self, bcm_path, cfg_dir, log=None):
        self.bcm_path = bcm_path
        self.cfg_dir = cfg_dir
        self.log = log or (lambda *a: None)

        self.state = "init"
        self.pid = None
        self.fd = None
        self.started = now()
        self.ready_at = None
        self.banner = ""
        self.chip = None
        self.rev = None
        self.init_errors = []

        # Serialises the pty. Non-reentrant on purpose: nothing here should ever
        # need to hold it twice, and a reentrant lock would hide a bug where a
        # handler calls into another handler.
        self._lock = threading.Lock()
        self._buf = b""

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not os.path.exists(self.bcm_path):
            self.state = "dead"
            self.init_errors.append("bcm.user not found: %s" % self.bcm_path)
            return
        if not os.path.isdir(self.cfg_dir):
            self.state = "dead"
            self.init_errors.append("config dir not found: %s" % self.cfg_dir)
            return

        import pty                      # see the note at the imports
        pid, fd = pty.fork()
        if pid == 0:
            # Child. Everything below mirrors ffn-bcm-run.py, which is the
            # version of this that has actually worked on hardware.
            try:
                os.chdir(self.cfg_dir)
                # CWD IS NOT ENOUGH: bcm.user carries absolute
                # /usr/share/broadcom paths, so a config copy is only really
                # read when BCM_CONFIG_FILE names it. jer.soc additionally
                # sources helpers and writes runningConfig.soc by absolute path,
                # which is why /usr/share/broadcom is symlinked at the copy.
                os.environ["FFN_BCM_DIR"] = self.cfg_dir
                os.environ["BCM_CONFIG_FILE"] = os.path.join(self.cfg_dir, "config.bcm")
                os.environ["TERM"] = "dumb"
                os.execv(self.bcm_path, ["bcm.user"])
            except Exception:
                pass
            os._exit(127)

        self.pid = pid
        self.fd = fd
        self.log("started bcm.user pid=%d on a pty" % pid)

        # Reaching the prompt takes minutes, so wait for it on a background
        # thread and let the socket serve "state: init" meanwhile.
        threading.Thread(target=self._await_prompt, name="chip-init",
                         daemon=True).start()

    def _await_prompt(self):
        deadline = self.started + INIT_TIMEOUT
        with self._lock:
            try:
                text = self._read_until_prompt(deadline)
            except ChipDead as exc:
                self.state = "dead"
                self.init_errors.append(str(exc))
                self.log("init failed: %s" % exc)
                return
            except Exception as exc:                       # pragma: no cover
                self.state = "dead"
                self.init_errors.append("init error: %r" % (exc,))
                return

        self.banner = text[-8000:]
        self._scrape_banner(text)
        self.ready_at = now()
        self.state = "ready"
        self.log("ready after %.1fs chip=%s rev=%s"
                 % (self.ready_at - self.started, self.chip, self.rev))

    def _scrape_banner(self, text):
        """Pull the facts worth reporting out of the init transcript.

        These are also a cheap self-check: the BDE line must say (PCI). It said
        (I2C) for a long while because the platform makefile was missing
        -D__DUNE_LINUX_BCM_CPU_PCIE__, and every register read was then a lie.
        """
        # [^,\s]+ not \S+: the banner reads "Chip BCM88375_B0, Driver BCM88375_B0"
        # and \S+ captured the comma, so status reported "BCM88375_B0,".
        m = re.search(r"BDE dev \d+ \((\w+)\).*?Chip ([^,\s]+)", text, re.S)
        if m:
            self.bus = m.group(1)
            self.chip = m.group(2)
        m = re.search(r"Rev (0x[0-9a-fA-F]+)", text)
        if m:
            self.rev = m.group(1)
        # Init failures inside jer.soc do NOT stop us reaching the prompt, so
        # record them: a caller seeing "ready" with a non-empty init_errors
        # knows the chip is only partly configured.
        for line in text.splitlines():
            if ("script terminated" in line
                    or "Operation failed" in line
                    or "could not open file" in line):
                self.init_errors.append(line.strip())

    # -- pty plumbing ------------------------------------------------------

    def _read_until_prompt(self, deadline):
        """Read until the prompt sits at the END of what we have.

        The prompt also appears mid-transcript (every command echoes one), so
        matching it anywhere would truncate replies. Requiring it at the tail is
        what makes one request map to one reply.
        """
        while True:
            if self._buf.rstrip().endswith(PROMPT):
                out = self._buf
                self._buf = b""
                return out.decode("utf-8", "replace")

            remaining = deadline - now()
            if remaining <= 0:
                raise ChipDead("timed out waiting for the prompt")

            try:
                r, _, _ = select.select([self.fd], [], [], min(2.0, remaining))
            except (OSError, ValueError):
                raise ChipDead("pty select failed (process gone)")
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                raise ChipDead("pty closed (bcm.user exited)")
            if not chunk:
                raise ChipDead("pty EOF (bcm.user exited)")
            self._buf += chunk

    def run(self, cmd, timeout=CMD_TIMEOUT):
        """Send one command, return its output with echo and prompt stripped."""
        if self.state == "init":
            raise ChipBusy("still initialising")
        if self.state != "ready":
            raise ChipDead("chip session is %s" % self.state)

        if not self._lock.acquire(timeout=timeout):
            raise ChipBusy("another request holds the chip")
        try:
            payload = (cmd + "\n").encode("utf-8")
            try:
                os.write(self.fd, payload)
            except OSError:
                self.state = "dead"
                raise ChipDead("write to pty failed")
            try:
                text = self._read_until_prompt(now() + timeout)
            except ChipDead:
                self.state = "dead"
                raise
        finally:
            self._lock.release()

        return self._strip(text, cmd)

    @staticmethod
    def _strip(text, cmd):
        """Drop the echoed command and the trailing prompt."""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("BCM.0>"):
            lines = lines[:-1]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        return "\n".join(lines)

    def stop(self):
        """Kill our bcm.user and reap it.

        This is not tidiness. The chip is SINGLE-SESSION: a bcm.user that
        outlives its daemon keeps the chip open, so the next ffn-bcmd starts a
        second session against the same hardware and wedges it. Leaving an
        orphan is worse than not restarting at all, which is why this runs from
        the SIGTERM handler as well as normal exit.
        """
        if self.pid is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except OSError:
                return                      # already gone
            for _ in range(20):             # up to 2 s per signal
                try:
                    pid, _st = os.waitpid(self.pid, os.WNOHANG)
                except OSError:
                    return
                if pid == self.pid:
                    self.log("bcm.user pid=%d reaped" % self.pid)
                    self.pid = None
                    return
                time.sleep(0.1)

    def alive(self):
        if self.pid is None:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            return False
        if pid == self.pid:
            self.state = "dead"
            return False
        return True


# ---------------------------------------------------------------------------
# Operations. Each returns a JSON-able dict WITHOUT "ok" (added by the caller).
# ---------------------------------------------------------------------------

def _speed_mb(tok):
    """'10G' -> 10000, '12.5G' -> 12500, '-' -> None."""
    if not tok or tok == "-":
        return None
    try:
        if tok.endswith("G"):
            return int(float(tok[:-1]) * 1000)
        if tok.endswith("M"):
            return int(float(tok[:-1]))
    except ValueError:
        return None
    return None


def op_status(chip, req):
    out = {
        "state": chip.state,
        "chip": chip.chip,
        "rev": chip.rev,
        "bus": getattr(chip, "bus", None),
        "uptime_s": round(now() - chip.started, 1),
        # A caller seeing state=ready with init_errors non-empty is looking at a
        # chip that reached the prompt but is only partly configured.
        "init_errors": chip.init_errors[:20],
    }
    if chip.state == "init":
        out["eta_s"] = max(0, round(INIT_TIMEOUT - (now() - chip.started)))
        out["hint"] = ("a warm init is ~30-50 s; a cold one is far longer -- "
                       "up to ~700 s measured, almost all of it DDR SHMOO "
                       "training -- and is only cold until runningConfig.soc "
                       "exists")
    if chip.ready_at:
        out["init_took_s"] = round(chip.ready_at - chip.started, 1)
    return out


def op_port_list(chip, req):
    """The port table, structured.

    `ps` is the only source for the whole table in one shot, so it is parsed --
    but the enable state is taken from the state column rather than guessed:
    "!ena" means administratively disabled, which is what the shipped config
    leaves every front-panel port in.
    """
    text = chip.run("ps")
    ports = []
    for line in text.splitlines():
        m = PS_ROW.match(line)
        if not m:
            continue
        state = m.group("state")
        num = int(m.group("port"))
        ports.append({
            "name": m.group("name"),
            "port": num,
            "enabled": state != "!ena",
            "link": state == "up",
            "state": state,
            "speed_mb": _speed_mb(m.group("speed")),
            "faceplate": num in FACEPLATE,
            "raw": line.rstrip(),
        })
    if not ports:
        raise RuntimeError("could not parse any port from `ps` output")
    return {"ports": ports, "count": len(ports)}


def op_port_set(chip, req):
    """Enable or disable one port, through the API rather than shell text.

    cint is used instead of a `port` shell command because bcm_port_enable_set
    returns a status we can actually check, where shell text would have to be
    pattern-matched to tell success from a silently ignored argument.
    """
    port = int(req["port"])
    enable = 1 if req.get("enable", True) else 0
    script = ("int rv; rv = bcm_port_enable_set(0, %d, %d); "
              'printf("FFNRV %%d\\n", rv);' % (port, enable))
    text = chip.run("cint\n" + script + "\nexit;")
    m = re.search(r"FFNRV (-?\d+)", text)
    if not m:
        raise RuntimeError("no status from bcm_port_enable_set; output: %s"
                           % text[-400:])
    rv = int(m.group(1))
    if rv != 0:
        raise RuntimeError("bcm_port_enable_set(%d,%d) returned %d"
                           % (port, enable, rv))
    return {"port": port, "enable": bool(enable)}


def op_port_loopback(chip, req):
    """Set loopback mode: none | mac | phy.

    This is the isolation tool, not a toy: mac/phy linking while none stays down
    proves the MAC, PCS and SerDes are all good and the fault is outside the die
    -- which is exactly how the front-panel loopback result was established.
    """
    port = int(req["port"])
    mode = str(req.get("mode", "none")).lower()
    if mode not in ("none", "mac", "phy"):
        raise ValueError("mode must be none, mac or phy")
    name = req.get("name")
    target = name if name else str(port)
    text = chip.run("port %s lb=%s" % (target, mode))
    return {"port": port, "mode": mode, "output": text[-400:]}


def op_led_status(chip, req):
    """LEDUP0 control state: are the front-panel LED processors running?

    `getreg` already does the two things worth having, so this does NOT
    re-implement them. Its real output is:

        CMIC_LEDUP0_CTRL.CMIC0[0x20000]=0x20b: <LEDUP_EN=1,FIELD_4_9=0x20,...>

    which is (a) the LOGICAL value -- the SDK has already undone the byte
    reversal that a raw BAR read through ffn_bcmctl shows as 0x0b020000 -- and
    (b) the field decoded by name. An earlier version of this function matched
    /0x[0-9a-f]{8}/ and byte-swapped the result by hand: it returned nulls,
    because the value here is three hex digits, and the swap would have been
    wrong even if it had matched. Trust the SDK's decode; fall back to bit 0 of
    the value only if the named field is absent.
    """
    text = chip.run("getreg CMIC_LEDUP0_CTRL")

    enabled = None
    m = re.search(r"LEDUP_EN=(\d+)", text)
    if m:
        enabled = bool(int(m.group(1)))

    value = None
    m = re.search(r"\[0x[0-9a-fA-F]+\]=(0x[0-9a-fA-F]+)", text)
    if m:
        value = m.group(1)
        if enabled is None:
            enabled = bool(int(value, 16) & 1)

    return {"value": value, "enabled": enabled,
            # The LED processors are programmed and started by jer.soc, not by
            # anything here -- see octeon/BCM88375-PORTS.md. enabled=False on a
            # fully initialised chip means jer.soc did not reach its LED section.
            "output": text.strip()[-300:]}


def op_raw(chip, req):
    """Escape hatch: run one shell command verbatim.

    Off unless the daemon was started with --allow-raw. It is the difference
    between a settings API and a remote root shell on the switch ASIC, so it is
    opt-in rather than something a WebUI bug can reach by accident.
    """
    if not req.get("_allow_raw"):
        raise PermissionError("raw is disabled; start ffn-bcmd with --allow-raw")
    cmd = str(req["cmd"])
    return {"cmd": cmd, "output": chip.run(cmd, timeout=float(req.get("timeout", CMD_TIMEOUT)))}


def _pci_read(dev, name, default=""):
    try:
        with open("/sys/bus/pci/devices/%s/%s" % (dev, name)) as f:
            return f.read().strip()
    except OSError:
        return default


# The silicon on the CP's PCIe domains, by (vendor, device). The MP cannot see
# any of it: these parts hang off the CN73XX's own root complexes, so `lspci` on
# the x86 host lists the CP and stops there. That is why the management plane
# reported "no dataplane" on a box whose dataplane was sitting right here.
#
# kind is what the WebUI groups by. The FE100 is called an ASIC rather than an
# FPGA because that is what it is -- Palo Alto's own front-end part, PCI vendor
# 0xfeed -- and labelling it FPGA in an inventory would be a guess dressed as a
# fact. It is listed alongside the accelerators because that is where an
# operator looks for it.
KNOWN_PCI = {
    ("14e4", "8375"): ("switch",  "Broadcom BCM88375 (Qumran-MX) packet processor"),
    ("feed", "fe1c"): ("asic",    "Palo Alto FE100 front-end ASIC"),
    ("177d", "0095"): ("npu",     "Cavium OCTEON III CN78XX (dataplane, 40 cores)"),
    ("177d", "9700"): ("npu",     "Cavium OCTEON III CN73XX (control plane)"),
    # 177d:9700 appears three more times as PCI class 0604 -- those are the
    # CN73XX's own root-complex bridges, one per domain, not three CPs. The
    # class check below reclassifies them, because reporting the processor this
    # code is running on as four separate NPUs is exactly the kind of inventory
    # error that sends someone looking for hardware that is not there.
    ("10b5", "8606"): ("bridge",  "PLX PEX 8606 PCIe switch"),
    ("13a8", "0354"): ("serial",  "Exar XR17V354 quad UART"),
}


def op_sys_inventory(chip, req):
    """What silicon is on the CP's PCIe domains, and whether a driver holds it.

    Deliberately does NOT touch the chip session. It reads sysfs and nothing
    else, so it answers while bcm.user is still initialising, and it answers
    after the session has died -- which is exactly when someone needs to know
    what hardware is actually present. Making it depend on the pty would make
    the inventory unavailable in every case where it matters most.
    """
    devices = []
    try:
        names = sorted(os.listdir("/sys/bus/pci/devices"))
    except OSError as exc:
        # Raise rather than return. handle_line turns a raised error into
        # ok=false; returning a dict would have been wrapped in ok=TRUE with an
        # "error" key inside it, and a caller checking ok would read "no
        # devices on this machine" from a failed enumeration.
        raise RuntimeError("cannot enumerate PCI: %s" % exc)

    for dev in names:
        vend = _pci_read(dev, "vendor").replace("0x", "").lower()
        devid = _pci_read(dev, "device").replace("0x", "").lower()
        kind, desc = KNOWN_PCI.get((vend, devid), (None, None))
        if kind is None:
            continue
        klass = _pci_read(dev, "class")
        if klass.startswith("0x0604"):
            # A bridge is a bridge whatever its vendor id says it is.
            if kind == "npu":
                desc = desc.split(" (")[0] + " root complex"
            kind = "bridge"
        # os.path.realpath does NOT raise for a path that does not exist -- it
        # returns the path unchanged -- so a device with no driver would come
        # back as the literal string "driver". Test the link first; the
        # try/except around realpath was dead code guarding the wrong thing.
        link = "/sys/bus/pci/devices/%s/driver" % dev
        driver = None
        if os.path.islink(link):
            driver = os.path.basename(os.path.realpath(link))
        # Whether the device's PCI memory decode has been switched on. Reported
        # because on this board the dataplane OCTEON is driven WITHOUT a kernel
        # driver -- dpboot writes this file and then mmaps resourceN directly --
        # so its driver link is permanently empty and says nothing at all about
        # whether anything is running on it. Note this is not a boot indicator
        # either: it stays 1 after whatever enabled it has gone away.
        # Only the six real BARs. sysfs `resource` has 13 rows: 0-5 are the
        # BARs, 6 is the expansion ROM, and 7-12 are a bridge's forwarding
        # windows. Reporting those as BARs makes a two-BAR device look like it
        # has five, which is the kind of detail someone later trusts.
        bars = []
        try:
            with open("/sys/bus/pci/devices/%s/resource" % dev) as f:
                for i, ln in enumerate(f.read().splitlines()):
                    if i > 5:
                        break
                    parts = ln.split()
                    if len(parts) >= 2:
                        st, en = int(parts[0], 16), int(parts[1], 16)
                        if en > st:
                            bars.append({"bar": i, "bytes": en - st + 1})
        except (OSError, ValueError):
            pass
        try:
            with open("/sys/bus/pci/devices/%s/enable" % dev) as f:
                pci_enabled = f.read().strip() == "1"
        except OSError:
            pci_enabled = None
        devices.append({
            "pci": dev,
            "pci_enabled": pci_enabled,
            "vendor": vend,
            "device": devid,
            "kind": kind,
            "description": desc,
            "driver": driver,
            "bars": bars,
            "class": klass,
        })

    # The kernel this is running on, so the MP can tell a live CP from a cached
    # answer without a second round trip.
    uts = os.uname()
    return {"devices": devices, "count": len(devices),
            "cp": {"machine": uts.machine, "release": uts.release,
                   "nodename": uts.nodename}}


def _proc_matching(needle):
    """PIDs whose cmdline mentions `needle`, read straight from /proc.

    Not `pgrep -f` or `ps | grep`: both match their own command line, which has
    produced a false "it is running" here more than once. Reading /proc and
    skipping our own pid cannot do that.
    """
    hits = []
    me = str(os.getpid())
    try:
        names = os.listdir("/proc")
    except OSError:
        return hits
    for pid in names:
        if not pid.isdigit() or pid == me:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue          # exited between listdir and open; not an error
        if needle in cmd:
            hits.append({"pid": int(pid), "cmd": cmd.strip()[:120]})
    return hits


def _iface_state(name):
    """operstate/carrier for a CP netdev, or None if it does not exist."""
    base = "/sys/class/net/%s" % name
    if not os.path.isdir(base):
        return None
    def rd(f):
        try:
            with open(os.path.join(base, f)) as fh:
                return fh.read().strip()
        except OSError:
            return None
    return {"operstate": rd("operstate"), "carrier": rd("carrier"),
            "mtu": rd("mtu")}


def op_sys_dpstatus(chip, req):
    """What the CONTROL PLANE can see about the dataplane.

    This exists because the management plane cannot answer the question. The
    CN78XX hangs off the CP's PCIe, and it is brought up by writing its PCI
    `enable` file and mmap'ing its BARs -- no kernel driver ever binds to it --
    so from the MP there is no driver, no netdev and no signal of any kind. The
    CP is the only place with evidence, so the CP reports it and the MP asks.

    EVERY FIELD IS POSITIVE EVIDENCE, and none of it is inferred from another.
    `pci_enabled` says memory decode is on -- which is NOT a boot indicator,
    because it stays set after whatever set it has gone away. `net` and `agent`
    are the two things that are only true while something is actually running
    on the far side. A caller wanting "is the dataplane up" should read
    `agent`/`net`, not `pci_enabled`, and `summary` says which of those spoke.

    WHAT THIS DELIBERATELY DOES NOT DO: it does not talk to the DP. ffn-dpsh is
    a single shared shell on the far side and concurrent use wedges it, so a
    status endpoint -- something polled every few seconds by definition -- must
    never touch it. Everything here is sysfs and /proc on the CP.
    """
    dev = str(req.get("pci") or "0003:03:00.0")
    base = "/sys/bus/pci/devices/" + dev

    present = os.path.isdir(base)
    out = {"pci": dev, "present": present}
    if present:
        def rd(f):
            try:
                with open(os.path.join(base, f)) as fh:
                    return fh.read().strip()
            except OSError:
                return None
        out["vendor"] = (rd("vendor") or "").replace("0x", "")
        out["device"] = (rd("device") or "").replace("0x", "")
        out["pci_enabled"] = (rd("enable") == "1")
        link = os.path.join(base, "driver")
        out["driver"] = (os.path.basename(os.path.realpath(link))
                         if os.path.islink(link) else None)
        out["runtime_status"] = rd("power/runtime_status")

    # The CP<->DP virtual ethernet. Its presence means something on the CP has
    # brought the link up; carrier means the far side is answering.
    net = {}
    for ifname in ("dpnet0", "ffndp0", "dp0"):
        st = _iface_state(ifname)
        if st:
            net[ifname] = st
    out["net"] = net

    agents = []
    for needle in ("ffn_dpnetd", "ffn_dpagent", "dpagent"):
        for p in _proc_matching(needle):
            p["match"] = needle
            agents.append(p)
    out["agent"] = agents

    # One line an operator or a health check can act on, derived from the
    # evidence above rather than from a guess. Ordered by how much each thing
    # actually proves.
    if not present:
        out["summary"] = "no dataplane processor on the control plane's bus"
    elif agents:
        out["summary"] = "running: %s on the control plane is driving it" % (
            agents[0]["match"],)
    elif any(v.get("carrier") == "1" for v in net.values()):
        out["summary"] = "link up to the dataplane, but no agent on this side"
    elif out.get("pci_enabled"):
        out["summary"] = ("present and PCI-enabled, but nothing here is driving "
                          "it -- enable stays set after a previous bring-up, so "
                          "this is not evidence that it is running")
    else:
        out["summary"] = "present, PCI decode off: never brought up this boot"

    uts = os.uname()
    out["reported_by"] = {"nodename": uts.nodename, "release": uts.release}
    return out


OPS = {
    "status": op_status,
    "port.list": op_port_list,
    "port.set": op_port_set,
    "port.loopback": op_port_loopback,
    "led.status": op_led_status,
    "sys.inventory": op_sys_inventory,
    "sys.dpstatus": op_sys_dpstatus,
    "raw": op_raw,
}


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------

def handle_line(chip, line, allow_raw):
    try:
        req = json.loads(line)
    except ValueError as exc:
        return {"ok": False, "error": "bad json", "detail": str(exc)}
    if not isinstance(req, dict):
        return {"ok": False, "error": "request must be a JSON object"}

    op = req.get("op")
    fn = OPS.get(op)
    if fn is None:
        return {"ok": False, "error": "unknown op",
                "detail": "known ops: %s" % ", ".join(sorted(OPS))}
    if op == "raw":
        req["_allow_raw"] = allow_raw

    try:
        out = fn(chip, req)
    except ChipBusy as exc:
        # Not an error the caller did anything wrong -- surface the state so a UI
        # can wait instead of showing a failure.
        return {"ok": False, "error": "busy", "state": chip.state,
                "detail": str(exc), **({} if chip.state != "init"
                                       else {"eta_s": op_status(chip, {})["eta_s"]})}
    except ChipDead as exc:
        return {"ok": False, "error": "chip session dead", "detail": str(exc),
                "hint": "ffn-bcmd does not re-init on its own: a re-init resets "
                        "the switch and would drop live traffic. Restart it "
                        "deliberately."}
    except (KeyError, ValueError, TypeError) as exc:
        return {"ok": False, "error": "bad request", "detail": str(exc)}
    except PermissionError as exc:
        return {"ok": False, "error": "forbidden", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": "op failed", "detail": "%r" % (exc,)}

    out["ok"] = True
    return out


def serve_client(chip, conn, addr, allow_raw, log):
    conn.settimeout(300.0)
    f = conn.makefile("rwb")
    try:
        for raw in f:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            t0 = now()
            reply = handle_line(chip, line, allow_raw)
            log("%s %s -> ok=%s (%.2fs)"
                % (addr[0], line[:80], reply.get("ok"), now() - t0))
            f.write((json.dumps(reply) + "\n").encode("utf-8"))
            f.flush()
    except (socket.timeout, OSError):
        pass
    finally:
        try:
            f.close()
        except Exception:
            pass
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="BCM88375 control daemon (runs on the CP)")
    ap.add_argument("--bind", default=DEF_BIND)
    ap.add_argument("--port", type=int, default=DEF_PORT)
    ap.add_argument("--bcm", default=DEF_BCM, help="path to bcm.user")
    ap.add_argument("--cfg", default=DEF_CFG, help="config dir (holds config.bcm)")
    ap.add_argument("--allow-raw", action="store_true",
                    help="enable the raw op -- effectively a shell on the ASIC")
    ap.add_argument("--no-chip", action="store_true",
                    help="serve the protocol without starting bcm.user (protocol tests)")
    args = ap.parse_args()

    def log(*a):
        # stderr only. /tmp on the CP is tmpfs and /var is a symlink to it, so a
        # file here would not survive the reboot you would want it for; the
        # supervisor captures stderr instead.
        sys.stderr.write("ffn-bcmd: " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()

    chip = Chip(args.bcm, args.cfg, log=log)
    if args.no_chip:
        chip.state = "dead"
        chip.init_errors.append("--no-chip: bcm.user was not started")
    else:
        chip.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.bind, args.port))
    except OSError as exc:
        # EADDRINUSE is 98 on x86 but 125 on MIPS, so match on the name rather
        # than a number. Say plainly what it means: another ffn-bcmd already
        # owns the chip, and starting a second bcm.user against a single-session
        # chip would wedge it. Kill our own child before exiting so we do not
        # leave the orphan described in Chip.stop().
        chip.stop()
        log("cannot bind %s:%d (%s). Another ffn-bcmd is already running -- "
            "find it with `ps w | grep -a ffn_bcmd` and kill it by PID "
            "(there is no pkill on the busybox CP)."
            % (args.bind, args.port, exc.strerror or exc))
        sys.exit(1)
    srv.listen(8)
    log("listening on %s:%d (chip state %s)" % (args.bind, args.port, chip.state))

    def _bye(*_):
        chip.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    try:
        while True:
            conn, addr = srv.accept()
            # A thread per client, but they all serialise on the chip lock. The
            # threads exist so a slow reader cannot block another client's
            # status query, not to get parallelism at the chip.
            threading.Thread(target=serve_client,
                             args=(chip, conn, addr, args.allow_raw, log),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        chip.stop()


if __name__ == "__main__":
    main()
