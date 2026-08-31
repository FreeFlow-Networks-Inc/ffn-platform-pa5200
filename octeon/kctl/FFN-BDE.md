# ffn_bde: the SDK attaches. Verified contract, and what is left.

FFN's own `linux-user-bde` replacement. The vendor SDK shell now attaches the
BCM88375 through it and identifies the chip correctly:

    SPI unit 0: Dev 0x8375, Rev 0x11, Chip BCM88375_B0, Driver BCM88375_B0
    rc: unit 0 device BCM88375_B0

That is up from `open /dev/linux-user-bde: No such file or directory`, and it
independently confirms B0 — the same revision the driver struct's PCI field gave.

## Method: let the SDK tell you the ABI

The module implements the minimum and **logs every unhandled command with its
number and payload**. Each run gets further and names the next thing it wants, so
bcm.user's own diagnostics drive development.

That is also the safe order. An unimplemented command fails cleanly; a
wrongly-guessed register or DMA command writes to the wrong physical address on
live silicon.

The sequence it walked, each step discovered by running it:

    open succeeds                 -> asks command 5   (get_dma_info)
    DMA region reported           -> asks command 30  (get_dev_state)
    30 and 29 answered            -> mmap fails, EINVAL
    d1 flag corrected             -> "device 0x14e4 revision 0x75 not supported"
    identity fields corrected     -> "Unknow bus type 0x0 !!"
    bus type corrected            -> ATTACHED, then rc-script failure

## Verified contract

**Command 2, get_device.** Field order established empirically. Reporting vendor
in d0 produced `device 0x14e4 revision 0x75` — 0x14e4 is the vendor and 0x75 the
low byte of device 0x8375. So:

    d0 = PCI device id      (0x8375)
    d1 = PCI revision       (0x11)
    d2 = PCI vendor id      (available, but not what it keys on)

**Command 5, get_dma_info.** Contract read out of the client's own
`_get_dma_info`, which assembles a 64-bit address with `dsll32` then `or`:

    *a0 = (dx.dw[1] << 32) | dx.dw[0]     64-bit physical base
    *a1 = d0                              size
    *a2 = d1                              flag, see below

So dx.dw[0..1] carry the region's physical address low-then-high. A 4 MB coherent
allocation works, and `dma_handle == virt_to_phys()` on this platform — checked at
allocation rather than assumed, because userspace mmaps the physical address while
the device uses the handle, and a divergence would corrupt silently.

**d1 selects the mmap path.** With d1=0 the SDK called libc mmap on an fd that is
not ours, got EINVAL, and asserted at `linux-user-bde.c:1071` with our mmap
handler never entered. With **d1=1** the mmap succeeds and it reports
`DMA pool size: 1`. The client stores d1 into a global that `_mmap` later reads,
which is what pointed at it, together with the `_use_kernel_bde_mmap` symbol.

**Command 12, get_device_type, must return 2.** This is the bus type. 0 gives
`Error : Unknow bus type 0x0 !!`; 1 and 4 both fail; **2 attaches**. The SDK then
labels the unit "SPI", which is its own category name rather than a statement
about the physical bus — the device is plainly PCI.

**Command 29, instance_attach** sends d0 and d1 and reads nothing back.
**Command 30, get_dev_state** returns a state in d0; 0 is accepted.
**Command 21, bus_features** fills be_pio / be_packet / be_other.

## Deliberate uncertainties, exposed as module parameters

Values that are not established are parameters, not constants, so they can be
probed without a rebuild:

    dev_type=2      verified correct
    dma_flag=1      verified correct
    dma_mb=4        works; 2 also works
    dev_state=0     accepted, semantics unknown
    be_pio / be_packet / be_other = 1      NOT verified, see below

`be_*` deserves the caution. ffn_bcm already reads this device correctly and its
own note records BAR0 register 0 reading `0x75830000` where the device id is
`0x8375` — raw access returns the halves swapped and FFN swaps in software. So
telling the SDK that PIO needs swapping is evidence-based rather than a guess. But
a wrong value here does not fail loudly; it silently byte-swaps every register
access to a live switch. That is exactly why it is a runtime knob.

## Why it does not claim the PCI device

`ffn_bcm` already binds 14e4:8375 and is FFN's verified path to the CMIC. Two
drivers cannot claim one device, and taking it from ffn_bcm would cost the tooling
used to observe what bcm.user does — which is the point of running it. So ffn_bde
looks the device up with `pci_get_domain_bus_and_slot()` and never registers a
`pci_driver`. Both modules load together.

## Where it stops now

    ERROR: Assertion failed: (rm) at src/sal/core/unix/sync.c:554
    ERROR loading rc script on unit 0

The SAL sync layer fails to create a mutex. **Not memory**: 230 MB free before,
227 MB during, no OOM, no allocation failures in dmesg, `/dev/shm` present — and
memory barely moves, so it fails early rather than exhausting anything. A separate
investigation, and the next thing to chase.

Loading the rc script is where DNX init actually begins, so this is the last gate
before the vendor stack starts programming the switch.
