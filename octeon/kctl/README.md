# FFN OCTEON III kernel control driver for the BCM88375 CMIC

`ffn_bcm.ko` runs on the PA-5220's dataplane OCTEON III (CN73XX) and owns the
CPU Management Interface of the Broadcom BCM88375 Qumran-AX at `0001:01:00.0`.
It is the ordered, exclusive register path that the port bring-up work needs.

## What it provides

| ioctl | purpose |
|---|---|
| `FFN_BCM_IOC_RD` / `_WR` | CSR access to BAR2 (CMIC) and BAR0 (ident) |
| `FFN_BCM_IOC_SCHAN` | one complete S-channel transaction under the driver lock |
| `FFN_BCM_IOC_LED_LOAD` / `_LED_EN` | LED processor program load and enable |
| `FFN_BCM_IOC_INFO` | identity, BAR geometry, S-channel counters |

Plus `/sys/kernel/debug/ffn_bcm`, which reads `SCHAN_CTRL`, `LEDUP0_CTRL` and
`LEDUP0_CLK_DIV` live, so it is a probe and not just a counter dump.

## Why it exists

`ffn_cpdpd` reached these registers through `/dev/mem` during bring-up and that
proved the hardware answers. It could not make access exclusive, and that is
the problem:

* **S-channel is stateful and shared.** A message is *write MSG0..MSGn, set
  MSG_START, poll MSG_DONE, read MSG0..MSGn*. Two writers interleaving in that
  window return each other's replies with no error indication. A mutex in one
  driver fixes this; `/dev/mem` cannot.
* **BARs get discovered, not transcribed.** `ffn_cpdp.h` carried
  `0x11c0100800000` as a literal, and an earlier revision labelled the BCM's
  window `FFN_FE100_BAR2` — wrong chip, so every LEDUP access read as an FE100
  access. `pci_resource_start()` cannot be wrong that way.
* **`pci_enable_device()` sets `PCI_COMMAND_MEMORY` properly.** This chip comes
  out of reset with memory decode off, which is why raw BAR reads returned
  `0xffffffff` until something wrote the sysfs `enable` node.

## Two things that will bite anyone editing this

**Endianness.** The CMIC is little-endian; the OCTEON is big-endian. Every
32-bit access is `le32_to_cpu(__raw_readl())`, done explicitly rather than
through `readl()` — `readl()`'s swapping on MIPS depends on
`CONFIG_SWAP_IO_SPACE`, so it is right or wrong according to a platform symbol
unrelated to this device. `probe()` verifies the convention before anything
relies on it: BAR0 register 0 holds the device id, so the swapped low half must
read `0x8375`.

**MIPS ioctl encoding is not the generic one.** MIPS uses `_IOC_NONE=1`,
`_IOC_READ=2`, `_IOC_WRITE=4` in a 3-bit direction field with 13 size bits;
the generic encoding is `NONE=0`, `WRITE=1`, `READ=2` in 2 bits with 14. So
`_IOR` and `_IOW` come out with *different values* on MIPS:

    FFN_BCM_IOC_WR = 0x80504202   on MIPS
                     0x40504202   with the generic encoding

Never hand-compute these. `ffn_bcmctl` includes `ffn_bcm_abi.h` and is built
against the kernel's own sanitized uapi headers, so both sides expand the same
macros. Hand-written constants get `-ENOTTY`.

## S-channel error bits latch and have no clear

Measured on the BCM88375_A0 in this chassis, not inferred:

* A message with an invalid opcode returns `SCHAN_CTRL = 0x00800002` —
  `MSG_DONE | SCHAN_ERROR` — on the first poll.
* `SCHAN_ERROR` (bit 23) then **stays set**. Writing 0 to `SCHAN_CTRL` clears
  `MSG_DONE` but not bit 23. Writing the bit back does not clear it. Asserting
  `ABORT` does not clear it. `SCHAN_ERR` (0x10008) and `SCHAN_ACK_BEAT`
  (0x10004) read 0 the whole time.

The first version of this driver broke on that. Its poll loop exited on "any
error bit", so the *next* message exited on its first read with a stale bit set
and `MSG_DONE` clear — indistinguishable from a timeout. Every message after
the first failure returned `-ETIMEDOUT` at `spins=0`, having never actually
been given a chance to complete.

The driver now snapshots the latched bits before starting and judges the
message on bits it sets *itself*, reporting the snapshot back as
`schan.pre_err`. A caller decoding `ctrl` on its own must subtract `pre_err`.

**Known limitation, by construction:** once `SCHAN_ERROR` is latched, a second
message failing *the same way* sets no new bit, so it is reported as success.
The bit's behaviour — sticky, no clear, set alongside `MSG_DONE`, with `NACK`
never asserted — reads more like a "S-channel has seen an error since reset"
health latch than a per-message verdict. If that is right, the per-message
signal is `NACK`/`TIMEOUT`/`SER_CHECK_FAIL` or the reply header itself, and
`SCHAN_ERROR` should move out of the pass/fail mask into a reported-only
health bit. Settling that needs a known-good opcode to compare against, which
comes with the DNX message-format work.

## Building

    make                  # ffn_bcm.ko, against /mnt/clones/kbuild
    make strict           # same, with -Werror

    # ffn_bcmctl: freestanding, needs sanitized uapi headers
    make -C $SDK/linux/kernel/linux ARCH=mips \
         INSTALL_HDR_PATH=/tmp/uapi-mips headers_install
    $SDK/tools/bin/mips64-octeon-linux-gnu-gcc -O2 -mabi=64 -march=octeon3 \
        -mno-abicalls -fno-pic -fno-stack-protector -ffreestanding -nostdlib \
        -Wall -Wl,-e_start -I. -I/tmp/uapi-mips/include \
        -o ffn_bcmctl ffn_bcmctl.c

Both ship inside FFN's own embedded initramfs — `sbin/ffn_bcmctl` and
`lib/modules/4.9.57/extra/ffn_bcm.ko`, with `modules.dep` regenerated — so they
travel with the kernel and need no vendor overlay. That matters for licensing:
the dev overlay is vendor content used in place and never packaged, whereas
these two are FFN's own code and can ship.

## On the box

    modprobe ffn_bcm            # or: insmod /lib/modules/4.9.57/extra/ffn_bcm.ko
    cat /sys/kernel/debug/ffn_bcm
    ffn_bcmctl <<'EOF'
    info
    ledstat 0
    rd 2 0x10000
    EOF

Module parameters: `schan_spins` (default 20000) and `schan_poll_us` (default
5) bound one S-channel wait at about 100 ms.

## Deliberately not here

DNX device init, PMD firmware load, and presenting the switch's front ports as
netdevs. Those are the layer above: they need exactly the ordered exclusive
register access this driver provides, which is why it came first.

## Lessons carried over from ffn_pcic

That driver oopsed and left two unkillable processes. Three specific causes,
all avoided here:

1. It matched PCI function `.1` as well as `.0`. This driver binds function 0
   only — `.0` and `.1` are two functions of one chip sharing one CMIC.
2. It called `misc_register()` per device for a module-global misc device, so
   the second function failed with `EBUSY`. This one registers once, in
   `module_init`.
3. Its probe error path freed the device but left the global owner pointer
   published. Here the pointer is published last and retracted under the same
   mutex every ioctl holds, and the BAR maps are `pcim_*`-managed so there is
   no hand-written unwind to get wrong.
