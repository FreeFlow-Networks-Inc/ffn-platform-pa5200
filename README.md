# ffn-platform-pa5200

Platform support for **Palo Alto Networks PA-5200-series** appliances, consumed
by [FFN-NGFW](https://github.com/FreeFlow-Networks-Inc/FFN-NGFW) as a submodule at
`platform/pa5200/`.

## Why this is a separate repository

FFN-NGFW is the firewall. This is the code that makes one specific family of
boards do what the firewall asks — and which board you have determines which of
these you need. Platform support is **selected per hardware**, so it is
versioned per hardware rather than bundled into the product:

    platform/pa5200/    this repository  -- PA-5200-series appliances
    platform/vu9p/      FFN-NGFW-FPGA    -- the in-house VU9P card

A clone only needs the platform matching the board in front of it:

    git clone https://github.com/FreeFlow-Networks-Inc/FFN-NGFW
    cd FFN-NGFW
    git submodule update --init platform/pa5200

Nothing in FFN-NGFW's own build depends on this repository being present. If it
is absent, the platform-specific tooling is simply unavailable, and anything
that needs a register offset says which platform submodule it came from.

## What is in here

    ffn_oct.py          OCTEON CP/DP boot orchestration from the host
    ffn_gryphon.py      chassis model: PCI-to-role map, per-slot topology
    ffn_ifroles.py      interface role assignment for this chassis
    ffn_mdprobe.py      management-domain probe

    octeon/
      dpnet2/           CP <-> DP virtual Ethernet over PCIe (FFN's own)
      pcnet/            MP <-> CP virtual Ethernet over PCIe (FFN's own)
      transport/        shared transport definitions
      dpagent/          DP-side control and shell agent
      dpboot/           DP boot and DRAM staging tooling
      dproot/           DP root filesystem staging
      cpsh/  kctl/      CP shell and kernel control helpers
      initramfs/        DP initramfs construction
      patches/          kernel patches against the OCTEON SDK tree
      tools/            on-target helpers

    tools/              host-side operator tooling for this platform

The two PCIe transports are worth singling out: they are FFN's **own** protocol,
not a reimplementation of the vendor's. Rings live in the co-processor's DRAM and
the host reaches them through the BAR window, so the only cross-PCIe access is
host-initiated — the direction that provably works on this hardware without the
co-processor mastering the bus. `octeon/dpnet2/DPNET.md` and
`octeon/pcnet/PCNET.md` explain the reasoning, including where the first attempt
was wrong.

## What is deliberately not in here

No vendor firmware, kernel image, kernel module, bootloader, root filesystem,
FPGA bitstream, configuration file, or source code. Where vendor firmware is
needed to bring a board up, it is used **in place on the appliance the operator
already owns** and is never packaged or redistributed.

The recovered-by-analysis record — what was learned by reading the vendor's
binaries, as opposed to code FFN executes — lives in a separate repository and
is **not** wired in as a submodule, so that a public clone of this repository
never depends on a private one.

## Licence

**GPL-2.0-or-later** ([COPYING](COPYING)), matching FFN-NGFW.

Portions of the OCTEON support are ports of, or written against, the
BSD-3-Clause executive layer of the OCTEON SDK. That notice travels with this
repository — see [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES). Kernel patches
apply to GPL-2.0 upstream code and carry per-patch headers naming the upstream
file.
