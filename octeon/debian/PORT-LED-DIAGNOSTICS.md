# SFP+/QSFP front-panel LED investigation, 2026-09-10

Update: the operator confirmed ports 5, 13, 23 and 24 illuminate when CP CPLD
CSR8 bit 0 is set. Activity blinking remains unqualified. Earlier processor-only
probes left these lamps dark. Do not treat enabled LED processors,
working links, or packed output RAM as proof that front-panel lamps work.

## Evidence

Live BCM port inventory reports front 5/13 (BCM16/7) linked at 10G and front
23/24 (BCM34/35) linked at 100G. The software fabric remained active throughout.
No port mode, PHY setting, routing policy or forwarding entry was changed.

All three program memories match the sequences in the VM owner's
`opt/dpfs/usr/share/broadcom/jer.soc`. Control values are 0x20b/0x28b/0x28b.
Clock divider registers are 72; refresh period registers are 21600000. Status
normally reads 0x24: the program counter after SEND. A cleared executing bit
between refreshes is normal, not a stalled processor diagnosis.

Assembly start is 0x80 on all three processors; upper scan-count and test-mode
registers are zero, matching BCM88375 SDK reset values. No mismatch was found
in these settings. The DAC links are up independently of lamp operation.

All 64 input-remap slots on each processor now read back exactly as configured
by the BCM88375 branch of `jer_drv.c`. The diagnostic reader exposes both raw
words and decoded six-bit slots. Remap offsets +0x10 through +0x4c are read-only
in the helper. A native C policy check verified read access, rejection of writes
and unaligned addresses, and preservation of the existing program-write range.

The one-minute probe tested 8, 24 and 64 packed bits, all ones for 15 seconds
and all zeros for 5 seconds at each length. All three processors produced the
correct 1-, 3- and 8-byte 0xff/0x00 patterns in output RAM. Status PCs were
26/74/194, matching the end of each probe. Original controls and all 256 program
words per processor were restored and verified. This bypasses linkscan input
as a cause of an incorrect test pattern; it does not prove electrical output.

On a repeated test, the operator reported no blinking on SFP+/QSFP, with
RJ45 lights the exception. Whether the RJ45 blink was synchronized to the
probe or ordinary PHY activity is not yet established. The repeated test also
restored every original program and control with no readback errors.
The remaining boundary includes serial output routing, latch/enable/polarity,
and the board LED chain. These are investigation candidates, not diagnosed
faults. A repeated identical pattern test cannot resolve that boundary without
new board reference information or electrical observation. Do not change
unknown CPLD or pin-mux bits speculatively.

## Tools and reference limits

`ffn_led_mmio.c` now allows read-only access to status (+4), assembly start (+8),
refresh (+0x50), upper scan count (+0x54), test mode (+0x58), and clock divider
(+0x5c), using offsets from the VM OpenBCM
`include/soc/mcm/cmicm.h`. Existing writes remain limited to LED control and
program/data RAM. It compiles with `-Wall -Wextra -Werror` for MIPS64-BE.
The diagnostic library is installed on CP as
`/usr/local/lib/libffn-led-diagnostic.so`.

`ffn_port_led_status.py` validates PCI identity, takes a shared LED lock and
returns hashes, controls, clock state, output bytes and nonzero software link
slots. It does not infer faceplate mapping from link slot indices.
It also reports static reference mismatches; an empty list does not qualify
physical lamp operation.
`PORT-LED-STATUS-20260910.json` records the restored state.

`ffn_port_led_probe.py` takes an exclusive lock, checks known controls, backs up
programs under `/run`, runs the bounded test and restores in a finally block.
SIGTERM/SIGINT trigger restoration; SIGKILL or CP failure cannot execute that
cleanup. Use only while an operator can observe the lamps. Two probe construction
tests passed. Vendor code and SDK binaries remain on the VM.

The CP owner's `cpldlib_led_reg_rd` is a stub returning 0xff. It is not an
alternate working interface to the SFP/QSFP lamps on this board.

The owner's `soc_jer_led_tune` disassembly has a chip-type guard before its
six LED output-delay field writes, consistent with the SDK's Jericho Plus A0
branch. This is not evidence for applying those writes to BCM88375. Board
configuration comments about LEDs being flipped describe PHY lane ordering;
they do not identify a separate LED-enable register.

## Separate board-initialization discrepancy

The owner's `brdagent/cp/libports.so` `init` at 0x10014a74..0x10014a90
reads CP CPLD CSR8 and writes back its value OR 1, after BCM initialization
and before `gryphon_port_initialize`. GOT entries were resolved with
`readelf -AW` to distinguish the actual CPLD calls from indirect calls.
Live CSR8 was 0x40: this vendor initialization bit was clear. Its electrical
meaning has not been established; do not label it an LED-enable bit yet.

A bounded 90-second test changed only that bit (0x40 to 0x41), using the
original LED programs. The test restores only bit 0, preserving other bits,
in a finally block and handles SIGINT/SIGTERM. No persistent initializer was
changed. During the test the software fabric service stayed active and the
connected SFP+/QSFP links remained up. Physical lamp results require the
operator's observation; service/link checks alone do not qualify forwarding
traffic or LED illumination.
The test completed with `restored_bit0: true` and CSR8 back at 0x40.

The operator was away during the first CSR8 test, so it was repeated for 90
seconds. During the repeat, the operator confirmed all four connected cage
ports (5/13/23/24) were lit. The repeat also restored bit 0 successfully.
This establishes that the vendor initialization step is needed for visible
front-port output on this unit; it does not establish activity blink behavior.

`ffn_port_led_enable.py` now applies this single-bit update with switch identity,
boot-bus mapping, CPLD version, and readback checks. It shares the chassis LED
lock and preserves unrelated CSR8 bits. All 256 initial byte states passed
preservation/idempotency checks; unknown CPLD versions reject before writing.
Install it at `/usr/local/sbin/ffn_port_led_enable.py` on the CP, install
`ffn-port-led-enable.service` in `/etc/systemd/system`, then run
`systemctl daemon-reload` and `systemctl enable --now ffn-port-led-enable.service`.
The service belongs only to the PA-5220 platform. No core FFN platform hook or
BCM restart is needed. Startup ordering is after `ffn-bcmd.service`; full reboot
qualification remains outstanding.
