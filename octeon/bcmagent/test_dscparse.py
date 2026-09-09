#!/usr/bin/env python3
"""Unit-test ffn-bcmd's `phy diag <port> dsc` parser against REAL captured output.

Every fixture below was copied verbatim out of a `port.phy` reply from the
appliance on 2026-09-06, not hand-written -- the same principle as
test_psparse.py. The bug this file exists to pin down was invisible to any
invented fixture, because it depended on a bit the chip sets and then clears.

WHAT WENT WRONG. `_dsc_lanes` read SD and LCK with `int(token, 0)` inside a
`try/except ValueError: continue`. The SDK prints a status bit it has seen
CHANGE since the last read with a trailing '*' -- `1*` -- so int() raised, the
handler dropped the lane, and with every lane dropped the whole table came back
null. The marker is sticky and reading clears it, which is why it looked like a
falcon_tsc/TSCF problem: a port shows `1*` on the FIRST look after it links and
plain `1` on the second, so whichever port you read first came back null. It
was never about the SerDes family, and it hit TSCE ports just as hard -- xl2,
xe8, xe9 and xl24 are all TSCE and all returned null on their first read.

The fixtures cover every shape the two families produce:

  * ce35_first / ce35_again   the same falcon_tsc port, linked, read twice --
                              `1*` then `1`. THE regression: the first must
                              parse, and both must agree on the values.
  * xl24_first                TSCE, linked, `1*`. Proves the marker is not a
                              falcon thing.
  * ce32                      falcon_tsc, down, empty cage -- all zeroes.
  * xl25                      TSCE, down, four lanes.
  * xe8                       TSCE, one lane, and it is numbered 2, not 0 --
                              lane numbers are the core's, not the port's.
  * il20                      the ILKN port, printed by a third dump function
                              (falcon_phy_pmd_info_dump) and scanning ONE lane
                              where its PMD-state block lists four.

The two families disagree about their columns, which is the whole reason the
parser reads names off the header: TSCE has one RST_ST where TSCF has RST and
ST, TSCF has an M1mV column TSCE lacks, and TSCE's DFE carries seven
sub-values plus two SLICER columns against TSCF's six.

Run: python3 test_dscparse.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_bcmd  # noqa: E402

# ---------------------------------------------------------------- fixtures
# Verbatim. Interior spacing is load-bearing -- it is the column alignment the
# parser reads -- so do not reflow these.

CE35_FIRST = """\
 tscf_phy_pmd_info_dump:516 type = 65536 laneMask  = 0xF, Address = 0x8
SerDes type = falcon_tsc
CORE RST ST PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  API_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE   PLL_DIV    PLL_LOCK
 00   0  00    0        1     156.25MHz   D10B_23    A1021F     0xb2       65C      (11) 65C    0x08    25.750GHz     227        (07) 165       1
LN (CDRxN      , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV M1mV  DFE(1,2,3,4,5,6)        TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 0 (OSx1       , 0x040c,   0, 0)  1*  1*    0    33     1   11,4    24  -9  172  127  51,  9,  0,  3, -1,  1     0   12,102, 0, 0, 0   12,0  328,343,103, 99    48.4
 1 (OSx1       , 0x040c,   0, 0)  1*  1*    0    35    -1   12,4    23 -12  172  120  50,  9,  0,  2, -2, -1     0   12,102, 0, 0, 0   12,0  328,359,101, 94    48.9
 2 (OSx1       , 0x040c,   0, 0)  1*  1*    0    34     3   13,4    25 -17  174  118  45,  9, -2,  1, -1, -2     0   12,102, 0, 0, 0   12,0  312,373,101,101    65.0
 3 (OSx1       , 0x040c,   0, 0)  1*  1*    0    31     1   13,3    24  -1  177  122  48, 10,  0,  1,  1, -2     0   12,102, 0, 0, 0   12,0  312,328, 99, 99    60.0
------------------------------------------------------------------------
Falcon PMD State
"""

# The same port, read again a moment later. The chip has cleared the marker.
CE35_AGAIN = """\
 tscf_phy_pmd_info_dump:516 type = 65536 laneMask  = 0xF, Address = 0x8
SerDes type = falcon_tsc
CORE RST ST PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  API_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE   PLL_DIV    PLL_LOCK
 00   0  00    0        1     156.25MHz   D10B_23    A1021F     0xb2       65C      (11) 65C    0x08    25.750GHz     227        (07) 165       1
LN (CDRxN      , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV M1mV  DFE(1,2,3,4,5,6)        TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 0 (OSx1       , 0x040c,   0, 0)  1   1     0    33     1   11,4    24  -9  172  127  51,  9,  0,  3, -1,  1     0   12,102, 0, 0, 0   12,0  328,343,103, 99    48.4
 1 (OSx1       , 0x040c,   0, 0)  1   1     0    35    -1   12,4    23 -12  172  120  50,  9,  0,  2, -2, -1     0   12,102, 0, 0, 0   12,0  328,359,101, 94    48.9
 2 (OSx1       , 0x040c,   0, 0)  1   1     0    34     3   13,4    25 -17  174  118  45,  9, -2,  1, -1, -2     0   12,102, 0, 0, 0   12,0  312,373,101,101    65.0
 3 (OSx1       , 0x040c,   0, 0)  1   1     0    31     1   13,3    24  -1  177  122  48, 10,  0,  1,  1, -2     0   12,102, 0, 0, 0   12,0  312,328, 99, 99    60.0
------------------------------------------------------------------------
Falcon PMD State
"""

XL24_FIRST = """\
 tsce_phy_pmd_info_dump:470 type = 65536 laneMask  = 0xF
CORE RST_ST  PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE  PLL_DIV    PLL_LOCK
00    0,00      0        1     156.25MHz   D10F_13     0x00       65C      (13) 65C    0x08    10.250GHz     165       (10) 66        1*
LN (CDRxN  , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV  DFE(1,2,3,4,5,dcd1,dcd2)   SLICER(ze,zo,pe,po,me,mo) TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 0 (OSx1   , 0x0200,   0, 0)  1*  1*  -11    31    -2   10,2    26   4  250   0,  0,  0,  0,  0,  0,  0   2, -5,  0, -7,  0, -2      0    0, 37, 0, 0, 0   12,0  404,392,220,220    26.8
 1 (OSx1   , 0x0200,   0, 0)  1*  1*  -12    31    -2   11,2    23  -5  250   0,  0,  0,  0,  0,  0,  0  -5, -1,  1, -2,  5, -1      0    0, 37, 0, 0, 0   12,0  388,388,209,220    28.0
 2 (OSx1   , 0x0200,   0, 0)  1*  1*  -11    30    -2   11,2    26   7  250   0,  0,  0,  0,  0,  0,  0  -3,  1,  9,  8,  4,  1      0    0, 37, 0, 0, 0   12,0  404,390,211,222    27.7
 3 (OSx1   , 0x0200,   0, 0)  1*  1*  -11    31    -2   12,1    26  11  250   0,  0,  0,  0,  0,  0,  0   2, -1,  1,  5,  4,  8      0    0, 37, 0, 0, 0   12,0  388,376,225,233    27.2
********************************************
**** SERDES UC TRACE MEMORY DUMP ***********
"""

CE32_DOWN = """\
 tscf_phy_pmd_info_dump:516 type = 65536 laneMask  = 0xF, Address = 0xC
SerDes type = falcon_tsc
CORE RST ST PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  API_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE   PLL_DIV    PLL_LOCK
 00   0  00    0        1     156.25MHz   D10B_23    A1021F     0xb2       66C      (11) 66C    0x08    25.750GHz     228        (07) 165       1
LN (CDRxN      , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV M1mV  DFE(1,2,3,4,5,6)        TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 0 (OSx1       , 0x040c,   0, 0)  0   0     0    32     0   10,0    39   0    0    0   0,  0,  0,  0,  0,  0     0   12,102, 0, 0, 0   12,0    0,  0,  0,  0     0.0
 1 (OSx1       , 0x040c,   0, 0)  0   0     0    32     0   10,0    39   0    0    0   0,  0,  0,  0,  0,  0     0   12,102, 0, 0, 0   12,0    0,  0,  0,  0     0.0
 2 (OSx1       , 0x040c,   0, 0)  0   0     0    32     0   10,0    39   0    0    0   0,  0,  0,  0,  0,  0     0   12,102, 0, 0, 0   12,0    0,  0,  0,  0     0.0
 3 (OSx1       , 0x040c,   0, 0)  0   0     0    32     0   10,0    39   0    0    0   0,  0,  0,  0,  0,  0     0   12,102, 0, 0, 0   12,0    0,  0,  0,  0     0.0
------------------------------------------------------------------------
Falcon PMD State
"""

XL25_DOWN = """\
 tsce_phy_pmd_info_dump:470 type = 65536 laneMask  = 0xF
CORE RST_ST  PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE  PLL_DIV    PLL_LOCK
00    0,00      0        1     156.25MHz   D10F_13     0x00       65C      (13) 65C    0x08    10.250GHz     164       (10) 66        1*
LN (CDRxN  , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV  DFE(1,2,3,4,5,dcd1,dcd2)   SLICER(ze,zo,pe,po,me,mo) TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 0 (OSx1   , 0x0200,   0, 0)  0   0    20    32     0    0,0     0   0    0   0,  0,  0,  0,  0,  0,  0   0,  0,  0,  0,  0,  0      0    0, 37, 0, 0, 0   12,0    0,  0,  0,  0     0.0
 1 (OSx1   , 0x0200,   0, 0)  0   0    20    32     0    0,0     0   0    0   0,  0,  0,  0,  0,  0,  0   0,  0,  0,  0,  0,  0      0    0, 37, 0, 0, 0   12,0    0,  0,  0,  0     0.0
 2 (OSx1   , 0x0200,   0, 0)  0   0    20    32     0    0,0     0   0    0   0,  0,  0,  0,  0,  0,  0   0,  0,  0,  0,  0,  0      0    0, 37, 0, 0, 0   12,0    0,  0,  0,  0     0.0
 3 (OSx1   , 0x0200,   0, 0)  0   0    20    32     0    0,0     0   0    0   0,  0,  0,  0,  0,  0,  0   0,  0,  0,  0,  0,  0      0    0, 37, 0, 0, 0   12,0    0,  0,  0,  0     0.0
********************************************
**** SERDES UC TRACE MEMORY DUMP ***********
"""

XE8_ONE_LANE = """\
 tsce_phy_pmd_info_dump:470 type = 65536 laneMask  = 0x4
CORE RST_ST  PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE  PLL_DIV    PLL_LOCK
00    0,00      0        1     156.25MHz   D10F_13     0x00       66C      (13) 66C    0x08    10.250GHz     169       (10) 66        1
LN (CDRxN  , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV  DFE(1,2,3,4,5,dcd1,dcd2)   SLICER(ze,zo,pe,po,me,mo) TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 2 (OSx1   , 0x0007,   0, 0)  1*  1*    0    36    -2    2,1    33   9  250  10,  1,  1,  1,  0,  0,  0   4,  1,  4,  0,  4,  1      0    0, 37, 0, 0, 0   12,0  312,312,141,150    12.4
********************************************
**** SERDES UC TRACE MEMORY DUMP ***********
"""

IL20_ILKN = """\
 falcon_phy_pmd_info_dump:428 type = 65536 laneMask  = 0xF
SerDes type = falcon_tsc
CORE RST ST PLL_PWDN  UC_ATV   COM_CLK   UCODE_VER  API_VER  AFE_VER   LIVE_TEMP   AVG_TMON   RESCAL   VCO_RATE  ANA_VCO_RANGE   PLL_DIV    PLL_LOCK
 00   0  00    0        1     156.25MHz   D10B_23    A1021F     0xb2      410C      (12) 59C    0x08    25.000GHz     204        (06) 160       1
LN (CDRxN      , UC_CFG,RST,STP)  SD LCK RXPPM CLK90 CLKP1 PF(M,L)  VGA DCO P1mV M1mV  DFE(1,2,3,4,5,6)        TXPPM TXEQ(n1,m,p1,2,3) TXAMP   EYE(L,R,U,D)  LINK_TIME
 0 (OSx2       , 0x0404,   0, 0)  0   0     0    32     0   10,0    39   0    0    0   0,  0,  0,  0,  0,  0     0   12,102, 0, 0, 0   12,0    0,  0,  0,  0     0.0
------------------------------------------------------------------------
Falcon PMD State
PARAMETER       \t         LN0         LN1         LN2         LN3
"""


class FakeChip(object):
    """Stands in for the pty session: op_port_phy only calls chip.run()."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def run(self, cmd, timeout=None):
        self.calls.append(cmd)
        return self.text


def phy(text, port="ce35"):
    return ffn_bcmd.op_port_phy(FakeChip(text), {"port": port})


def main():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # ---- THE REGRESSION -------------------------------------------------
    # A linked port on its first read, SD and LCK both flagged `1*`. This is
    # the case that returned null.
    first = phy(CE35_FIRST)
    check(first["lanes"] is not None, "ce35 first read: lanes is null (the bug)")
    check(len(first["lanes"] or []) == 4,
          "ce35 first read: expected 4 lanes, got %r"
          % (first["lanes"] and len(first["lanes"])))
    for ln in first["lanes"] or []:
        check(ln["signal_detect"] == 1 and ln["lock"] == 1,
              "ce35 lane %d: SD/LCK should be 1/1, got %r/%r"
              % (ln["lane"], ln["signal_detect"], ln["lock"]))
        check(ln["signal_detect_changed"] and ln["lock_changed"],
              "ce35 lane %d: the change marker was dropped" % ln["lane"])

    # The same port a moment later, marker cleared. Same values, flags off --
    # the marker must change ONLY the *_changed fields.
    again = phy(CE35_AGAIN)
    check(again["lanes"] is not None, "ce35 second read: lanes is null")
    for a, b in zip(first["lanes"] or [], again["lanes"] or []):
        check(a["fields"] == dict(b["fields"], SD=a["fields"]["SD"],
                                  LCK=a["fields"]["LCK"]),
              "ce35 lane %d: the two reads disagree beyond SD/LCK" % a["lane"])
    for ln in again["lanes"] or []:
        check(not ln["signal_detect_changed"] and not ln["lock_changed"],
              "ce35 lane %d: change marker reported when none was printed"
              % ln["lane"])

    # ---- not a falcon problem: TSCE flags its bits the same way ---------
    tsce = phy(XL24_FIRST, "xl24")
    check(tsce["lanes"] is not None, "xl24 (TSCE, linked, 1*): lanes is null")
    check([l["signal_detect"] for l in tsce["lanes"] or []] == [1, 1, 1, 1],
          "xl24: SD should be 1 on all four lanes, got %r"
          % [l["signal_detect"] for l in tsce["lanes"] or []])
    check(all(l["lock_changed"] for l in tsce["lanes"] or []),
          "xl24: the change marker was dropped")

    # ---- both families, both link states, by NAME -----------------------
    # The two headers disagree; every one of these must come out right anyway.
    fal = (first["lanes"] or [{}])[0].get("fields", {})
    tsc = (tsce["lanes"] or [{}])[0].get("fields", {})

    check(fal.get("DFE(1,2,3,4,5,6)") == "51,9,0,3,-1,1",
          "falcon DFE mis-parsed: %r" % fal.get("DFE(1,2,3,4,5,6)"))
    check(fal.get("TXPPM") == "0",
          "falcon TXPPM stole a DFE value: %r" % fal.get("TXPPM"))
    check(fal.get("M1mV") == "127",
          "falcon M1mV mis-parsed: %r" % fal.get("M1mV"))
    check(fal.get("EYE(L,R,U,D)") == "328,343,103,99",
          "falcon EYE mis-parsed: %r" % fal.get("EYE(L,R,U,D)"))
    check(fal.get("LINK_TIME") == "48.4",
          "falcon LINK_TIME mis-parsed: %r" % fal.get("LINK_TIME"))
    check("SLICER(ze,zo,pe,po,me,mo)" not in fal,
          "falcon row grew a TSCE-only SLICER column")

    check(tsc.get("DFE(1,2,3,4,5,dcd1,dcd2)") == "0,0,0,0,0,0,0",
          "TSCE DFE mis-parsed: %r" % tsc.get("DFE(1,2,3,4,5,dcd1,dcd2)"))
    check(tsc.get("SLICER(ze,zo,pe,po,me,mo)") == "2,-5,0,-7,0,-2",
          "TSCE SLICER mis-parsed: %r" % tsc.get("SLICER(ze,zo,pe,po,me,mo)"))
    check(tsc.get("RXPPM") == "-11",
          "TSCE negative RXPPM mis-parsed: %r" % tsc.get("RXPPM"))
    check(tsc.get("TXAMP") == "12,0",
          "TSCE TXAMP mis-parsed: %r" % tsc.get("TXAMP"))
    check(tsc.get("EYE(L,R,U,D)") == "404,392,220,220",
          "TSCE EYE mis-parsed: %r" % tsc.get("EYE(L,R,U,D)"))
    check("M1mV" not in tsc, "TSCE row grew a falcon-only M1mV column")

    # A down port must still read as a down port.
    down = phy(CE32_DOWN, "ce32")
    check(down["lanes"] is not None, "ce32 (down): lanes is null")
    check(all(l["signal_detect"] == 0 and l["lock"] == 0
              for l in down["lanes"] or []),
          "ce32: a dark port must read SD=0 LCK=0")
    check(not any(l["lock_changed"] for l in down["lanes"] or []),
          "ce32: change marker reported when none was printed")
    tdown = phy(XL25_DOWN, "xl25")
    check(tdown["lanes"] is not None, "xl25 (TSCE, down): lanes is null")
    check(len(tdown["lanes"] or []) == 4, "xl25: expected 4 lanes")

    # ---- lane numbering is the CORE's, not the port's -------------------
    one = phy(XE8_ONE_LANE, "xe8")
    check(one["lanes"] is not None, "xe8: lanes is null")
    check([l["lane"] for l in one["lanes"] or []] == [2],
          "xe8 is lane 2 of its core, not lane 0: got %r"
          % [l["lane"] for l in one["lanes"] or []])
    check((one["lanes"] or [{}])[0]["signal_detect"] == 1,
          "xe8: linked lane read as no signal")

    # ---- a third dump function, and a table shorter than its own PMD block
    ilk = phy(IL20_ILKN, "il20")
    check(ilk["lanes"] is not None, "il20: lanes is null")
    check(len(ilk["lanes"] or []) == 1,
          "il20 scans one lane; the parser must not run on into the PMD "
          "State block below it: got %r" % (ilk["lanes"] and len(ilk["lanes"])))

    # ---- the CORE row ---------------------------------------------------
    # Multi-word values must survive whole. AVG_TMON is the temperature index
    # AND the temperature; PLL_DIV is the register value AND the divider.
    fcore = first["core"]
    check(fcore.get("AVG_TMON") == "(11) 65C",
          "falcon AVG_TMON lost its second word: %r" % fcore.get("AVG_TMON"))
    check(fcore.get("PLL_DIV") == "(07) 165",
          "falcon PLL_DIV lost its second word: %r" % fcore.get("PLL_DIV"))
    check(fcore.get("RST") == "0" and fcore.get("ST") == "00",
          "falcon splits RST and ST into two columns: %r" % fcore)
    check(fcore.get("API_VER") == "A1021F",
          "falcon API_VER mis-parsed: %r" % fcore.get("API_VER"))
    check(fcore.get("VCO_RATE") == "25.750GHz" and
          fcore.get("ANA_VCO_RANGE") == "227",
          "falcon core row is off by a column: %r" % fcore)

    tcore = tsce["core"]
    check(tcore.get("RST_ST") == "0,00",
          "TSCE joins RST and ST in one column: %r" % tcore.get("RST_ST"))
    check(tcore.get("PLL_DIV") == "(10) 66",
          "TSCE PLL_DIV lost its second word: %r" % tcore.get("PLL_DIV"))
    check(tcore.get("PLL_LOCK") == "1*",
          "the core row's change marker must be preserved, not stripped: %r"
          % tcore.get("PLL_LOCK"))
    check("API_VER" not in tcore, "TSCE core row grew a falcon-only API_VER")

    # il20's LIVE_TEMP really does read 410C. It is a nonsense value from the
    # chip, and the parser's job is to report it, not to launder it.
    check(ilk["core"].get("LIVE_TEMP") == "410C",
          "il20 LIVE_TEMP should be reported verbatim: %r"
          % ilk["core"].get("LIVE_TEMP"))

    # ---- uc_running must survive a flagged UC_ATV -----------------------
    check(tsce["uc_running"] is True,
          "xl24: uc_running should be True, got %r" % tsce["uc_running"])
    flagged = phy(XL24_FIRST.replace("     1     156.25MHz",
                                     "     1*    156.25MHz"), "xl24")
    check(flagged["core"].get("UC_ATV") == "1*",
          "the UC_ATV fixture edit did not take: %r" % flagged["core"])
    check(flagged["uc_running"] is True,
          "a running uC printed as `1*` must not be reported dead: %r"
          % flagged["uc_running"])

    # ---- the op is still read-only and asks for what it was told to -----
    chip = FakeChip(CE35_FIRST)
    ffn_bcmd.op_port_phy(chip, {"port": "ce35"})
    check(chip.calls == ["phy diag ce35 dsc"],
          "op_port_phy sent %r" % chip.calls)

    if fails:
        print("FAIL (%d)" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("ok: 7 captures parsed across TSCE/TSCF/falcon, both link states, "
          "with and without the change marker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
