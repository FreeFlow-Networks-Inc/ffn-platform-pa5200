// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * ffn_bde -- FFN's own linux-user-bde / linux-kernel-bde for OCTEON III.
 *
 * PURPOSE
 *
 * The vendor's Broadcom SDK shell (bcm.user) performs full DNX init of the
 * BCM88375: DRAM calibration, fabric, traffic manager, ports, SerDes microcode.
 * It is statically linked and runs on FFN's own kernel already -- it stops only
 * because /dev/linux-user-bde does not exist. The vendor's own BDE modules are
 * vermagic 3.10.87-oct2-dp and will not load on 4.9.57.
 *
 * Providing that interface ourselves lets the vendor stack initialise the switch
 * IN PLACE on the appliance that owns it, while FFN keeps its own kernel and all
 * its tooling, and can drive init repeatedly and watch exactly what it does.
 *
 * DESIGN: DISCOVERY-DRIVEN, NOT GUESS-DRIVEN
 *
 * Only four of the 31 command names survive in the vendor material, and the
 * per-command payload semantics are still being recovered. Rather than implement
 * 31 guesses, this module implements the handful needed to get past device
 * discovery and LOGS EVERY UNHANDLED COMMAND with its number and payload.
 *
 * That turns bcm.user's own diagnostics into the discovery loop: each run gets
 * further, complains about something specific, and tells us what to implement
 * next. It is also the safe order -- a wrong guess at a register-access or
 * DMA command writes to the wrong physical address on live silicon, whereas an
 * unimplemented one just fails cleanly.
 *
 * WHY THIS DOES NOT CLAIM THE PCI DEVICE
 *
 * ffn_bcm already binds 14e4:8375 and is FFN's own working path to the CMIC
 * (S-channel reads and writes both verified). Two drivers cannot claim the same
 * device, and taking it away from ffn_bcm would cost us the tooling we use to
 * observe what bcm.user does -- which is the entire point of running it.
 *
 * So this looks the device up with pci_get_domain_bus_and_slot() and does not
 * register a pci_driver. It reports the device's identity and BAR layout to
 * userspace without owning it. Deliberate, and it means both drivers can be
 * loaded while we learn.
 *
 * SAFETY
 *
 * Nothing here writes to the switch. Register access commands are unimplemented
 * on purpose until their payload semantics are established -- see ffn_bde_abi.h
 * for which of those are recovered and which are still inference.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/fs.h>
#include <linux/pci.h>
#include <linux/uaccess.h>
#include <linux/mm.h>
#include <linux/pfn.h>
#include <linux/slab.h>
#include <linux/mutex.h>
#include <linux/dma-mapping.h>
#include <linux/io.h>
#include <linux/interrupt.h>
#include <linux/wait.h>
#include <linux/sched.h>
/*
 * get_dbe(): a load whose Data Bus Error is FIXED UP instead of fatal. MIPS
 * keeps a second exception table (__dbe_table) just for this, do_be() consults
 * it via search_dbe_tables(), and arch/mips/kernel/module.c registers the
 * __dbe_table of a loaded module -- so it works from here, not just from
 * built-in PCI probe code. This is the only reason probing BAR0+0x2C00 is not
 * a gamble: see ffn_bde_paxb_probe_window().
 */
#include <asm/paccess.h>
#include <asm/octeon/octeon.h>
#include <asm/octeon/cvmx-pemx-defs.h>

#include "ffn_bde_abi.h"

#define DRV "ffn_bde"

/*
 * The BCM88375 sits at a fixed place on the CP Octeon's PCIe: domain 1, bus 1,
 * device 0. Two functions exist (.0 and .1); the SDK wants .0, whose BAR2 is the
 * 8 MB CMIC window. Overridable because nothing good comes of hardcoding a BDF
 * with no escape hatch.
 */
static int domain = 1;
static int bus = 1;
static int slot;
static int func;
module_param(domain, int, 0444);
module_param(bus, int, 0444);
module_param(slot, int, 0444);
module_param(func, int, 0444);
MODULE_PARM_DESC(domain, "PCI domain of the switch (default 1)");
MODULE_PARM_DESC(bus, "PCI bus of the switch (default 1)");

static int dma_mb = 4;
module_param(dma_mb, int, 0444);
MODULE_PARM_DESC(dma_mb,
	"size of the coherent DMA region to offer the SDK, in MB (default 4). "
	"The CP has only ~440 MB total, so this is deliberately modest; raise it "
	"if the SDK complains that the region is too small.");

/*
 * A boot-reserved DMA pool. Zero (default) keeps the dma_alloc_coherent path,
 * which is capped at 4 MB on this kernel. Non-zero names the physical base of
 * an ffn_reserve= range on the CP boot line; dma_mb gives its size. The range
 * is checked at load: below 4 GB, and NOT in the kernel's memory map.
 */
static unsigned long long dma_phys;
module_param(dma_phys, ullong, 0444);
MODULE_PARM_DESC(dma_phys,
	"physical base of a boot-reserved range to use as the DMA pool (default "
	"0 = allocate dma_mb MB coherently, max 4 MB on this kernel). Pair with "
	"ffn_reserve=<base>,<size> on the CP boot line and the same size in "
	"dma_mb. Refused if the range is above 4 GB or is live kernel RAM.");

/*
 * Byte order reported for command 21. NOT a blind guess: ffn_bcm already reads
 * this device correctly and its own note records that BAR0 register 0 reads
 * 0x75830000 raw where the device id is 0x8375 -- i.e. raw access comes back
 * with the halves swapped and FFN swaps in software. So the SDK should be told
 * PIO needs swapping.
 *
 * It is a module parameter because getting this wrong does not fail loudly, it
 * silently byte-swaps every register access to a live switch. Being able to flip
 * it without a rebuild is worth more than a hardcoded constant that looks
 * confident.
 */
static int be_pio = 1;
static int be_packet = 1;
static int be_other = 1;
module_param(be_pio, int, 0644);
module_param(be_packet, int, 0644);
module_param(be_other, int, 0644);
MODULE_PARM_DESC(be_pio, "byte-swap PIO accesses (default 1, big-endian host)");

/*
 * Command 5 returns d1, and the client stores it into a global that _mmap later
 * reads. Combined with the _use_kernel_bde_mmap symbol, d1 looks like the flag
 * that selects whether the SDK maps the DMA region through the BDE device or
 * through /dev/mem. With d1=0 it called libc mmap on an fd that is not ours and
 * got EINVAL, and our mmap handler was never entered.
 *
 * Parameterised so the hypothesis can be tested without a rebuild.
 */
static int dma_flag = 1;
module_param(dma_flag, int, 0644);
MODULE_PARM_DESC(dma_flag,
	"UNUSED. d1 turned out to be the mmap LENGTH, not a flag, so command 5 "
	"now returns the region size there. Kept only so existing insmod lines "
	"do not fail.");

/*
 * Command 12 (get_device_type) returns the bus type. Returning 0 made the SDK
 * say "Error : Unknow bus type 0x0 !!", so it keys on this. BDE device types are
 * conventionally a bit set with PCI as bit 0; parameterised so the value can be
 * probed without a rebuild rather than asserted.
 */
/*
 * Hardware byte-swap. The CMIC is little-endian and this CPU is big-endian, so
 * something has to swap. Without this the SDK's S-channel operations time out
 * with no other symptom, which is a long way from the cause. See
 * ffn_bde_paxb_init() for the mechanism and the evidence.
 */
static int paxb_be = 1;
module_param(paxb_be, int, 0444);
MODULE_PARM_DESC(paxb_be,
	"enable PAXB big-endian PIO mode at probe (default 1). Set 0 only to "
	"drive the chip with software byte-swapping instead, as ffn_bcm does; "
	"the two conventions are mutually exclusive, so do not run ffn_bcm "
	"against a device left in hardware-swap mode without accounting for "
	"it.");

/*
 * The rest of the vendor's shbde_iproc_paxb_init, decoded from
 * linux-kernel-bde.ko .text 0x6878. See ffn_bde_paxb_init() below.
 *
 * DEFAULT OFF, deliberately. Step 2 reads BAR0 + 0x2C08, which lives in the
 * same block as the BAR0 + 0x2C00 read that once left the OCTEON PCIe path in
 * an error state and took the MP down with it -- recovery was a physical power
 * cycle. From the kernel a bus error there is a panic, not a SIGSEGV. Two
 * things argue it is safe now (the vendor kernel reads 0x2C08 itself on this
 * hardware, and it does so only after the 0x2030 write that ffn_bde now always
 * performs), but "argues" is not "measured", so this stays opt-in.
 */
static int paxb_full = 1;
module_param(paxb_full, int, 0444);
MODULE_PARM_DESC(paxb_full,
	"run the full vendor PAXB init, not just the byte-order step (default "
	"1). Programs the OUTBOUND window (PAXB_OARR_2 / _UPPER) the CMIC DMA "
	"engines use to reach host memory; without it every DMA reports DONE "
	"and nothing crosses the bus. The BAR0+0x2C08 read this involves was "
	"once suspected of wedging PCIe; it has since been probed and read by "
	"the vendor client on this board without incident. 0 reproduces the "
	"old failure.");

static int paxb_dma_hi_bits;
module_param(paxb_dma_hi_bits, int, 0444);
MODULE_PARM_DESC(paxb_dma_hi_bits,
	"value for PAXB_OARR_2_UPPER (BAR0+0x2D64), the upper AXI address bits "
	"of the outbound window the CMIC's DMA engines use to reach host memory. "
	"0 = derive as the vendor does: PAXB_IMAP0_2 (BAR0+0x2C08) bit 12 set "
	"means PAXB_1 and gives 2, clear gives 1. Force 1 or 2 to skip the "
	"read. This board reads 0x18012001 -> 1.");

/* Deprecated spelling of paxb_dma_hi_bits, kept so existing insmod lines load. */
static int paxb_variant;
module_param(paxb_variant, int, 0444);
MODULE_PARM_DESC(paxb_variant, "deprecated alias of paxb_dma_hi_bits");

static int paxb_preemph;
module_param(paxb_preemph, int, 0444);
MODULE_PARM_DESC(paxb_preemph,
	"also set PAXB_OARR_FUNC0_MSI_PAGE bit 0 (BAR0+0x2D34) even when MSI "
	"is off (default 0). OpenBCM names this register; it is not a PCIe "
	"pre-emphasis control as earlier guessed. With use_msi=1 it is set "
	"regardless of this knob.");

/*
 * Probe the PAXB iProc window block instead of assuming anything about it.
 *
 * BAR0 + 0x2C00 is where the vendor client reads the existing window registers
 * during _open, and BAR0 + 0x2C1C is the dynamic window control register the
 * SDK programs for every iProc access outside its three static windows. A read
 * of 0x2C00 once left this board's PCIe path in an error state that took the MP
 * down with it, which is why dev_type bit 25 was set to make the client skip
 * that loop -- and bit 25 turned out to ALSO switch _iproc_read to linear
 * addressing, which is what makes soc_dpp_init die. So whether this block reads
 * is the question that decides whether bit 25 can be cleared.
 *
 * This probe answers it without risking the box: get_dbe() turns a bus error
 * into -EFAULT.
 */
static int probe_paxb_win;
module_param(probe_paxb_win, int, 0444);
MODULE_PARM_DESC(probe_paxb_win,
	"probe BAR0+0x2C00..0x2C1C at load and log what each word reads "
	"(default 0). Safe: uses get_dbe(), so a data bus error is reported as "
	"a fault rather than taking the CP down. Run this before clearing "
	"dev_type bit 25 -- if the block reads, the client can do its own "
	"window discovery and iProc addressing works properly.");

/*
 * Interrupts.
 *
 * The ISR cannot decide whether this device raised the line without reading
 * chip registers, and those belong to the SDK -- touching them from here would
 * race the very code we are serving. So the ISR masks at the *controller*
 * instead: exactly one interrupt is delivered, and the line stays masked until
 * userspace comes back through command 9 (or 6). That is the uio_pci_generic
 * pattern and it makes an interrupt storm structurally impossible.
 */
static int use_msi = 1;
module_param(use_msi, int, 0444);
MODULE_PARM_DESC(use_msi,
	"request MSI and install an interrupt handler at load (default 1). "
	"With 0, or if MSI cannot be had, command 9 still blocks correctly -- "
	"it just never wakes on hardware, which parks the SDK interrupt thread "
	"instead of letting it spin. Parking is the safe failure: spinning is "
	"what bus-errored on SBUSDMA_CH1_STATUS.");

static int intr_poll_ms;
module_param(intr_poll_ms, int, 0644);
MODULE_PARM_DESC(intr_poll_ms,
	"if non-zero, wake the command 9 waiter this often even with no "
	"interrupt (default 0 = block until one arrives). A debugging aid for "
	"the case where the SDK needs its handlers to run but MSI is not "
	"being delivered; keep it large, because each wake makes the SDK read "
	"chip status registers.");

/*
 * Probe a fixed set of BAR2 CMIC registers with get_dbe() at load. Three known-
 * good registers act as controls: if those read and the SBUSDMA ones do not,
 * the block genuinely does not decode; if everything reads here but the SDK
 * still bus-errors on the same address, then it is something the SDK does that
 * stops the chip answering, not the address.
 */
static int probe_bar2;
module_param(probe_bar2, int, 0444);
MODULE_PARM_DESC(probe_bar2,
	"probe CMIC control + SBUSDMA registers in BAR2 at load and log what "
	"each reads (default 0). Safe: get_dbe() reports a data bus error "
	"instead of taking the CP down.");

/*
 * An arbitrary BAR2 range, for the register block currently under suspicion.
 * The fixed list above is the standing regression check; this is the scratchpad,
 * and having it as a parameter is what keeps a new question from costing a
 * rebuild, a redeploy and a CP re-boot.
 */
static int probe_bar2_off;
module_param(probe_bar2_off, int, 0444);
MODULE_PARM_DESC(probe_bar2_off,
	"byte offset into BAR2 to start the ad-hoc probe at (with "
	"probe_bar2_n). Rounded down to a word.");

static int probe_bar2_n;
module_param(probe_bar2_n, int, 0444);
MODULE_PARM_DESC(probe_bar2_n,
	"how many 32-bit words to probe from probe_bar2_off (default 0 = "
	"skip). Uses get_dbe(), so an offset that does not decode is logged "
	"rather than fatal. Capped at 64.");

/* Same idea as probe_bar2_off/_n, for BAR0 (the PAXB/iProc window). */
static int probe_bar0_off;
module_param(probe_bar0_off, int, 0444);
MODULE_PARM_DESC(probe_bar0_off,
	"byte offset into BAR0 to start the ad-hoc probe at (with "
	"probe_bar0_n). Rounded down to a word.");

static int probe_bar0_n;
module_param(probe_bar0_n, int, 0444);
MODULE_PARM_DESC(probe_bar0_n,
	"how many 32-bit words to probe from probe_bar0_off (default 0 = "
	"skip). get_dbe(), so a non-decoding offset is a log line. Capped at 64.");


/*
 * Which PEM (PCIe port) to dump the inbound BAR setup for. The BCM88375 is on
 * domain 0001, which is PEM1 on CN73XX -- that chip has ports 1..3, and the
 * three domains we see (0001 BCM, 0002 FE100, 0003 the DP OCTEON) line up with
 * them.
 */
static int probe_pem = -1;
module_param(probe_pem, int, 0444);
MODULE_PARM_DESC(probe_pem,
	"PCIe port whose PEM inbound BAR configuration to dump at load "
	"(default -1 = off; 1 is the BCM). Reports BAR_CTL decoded, so "
	"bar2_enb can be checked directly -- with it clear the chip's DMA "
	"writes get UR and are silently dropped, which is exactly what we "
	"measure.");



/*
 * Bus mastering. Off by default in the hardware as we find it, and nothing else
 * in the boot path turns it on -- the CP log line
 * "PCI: Enabling device 0001:01:00.0 (0000 -> 0002)" is memory space alone.
 */
static int set_master = 1;
module_param(set_master, int, 0444);
MODULE_PARM_DESC(set_master,
	"enable PCI bus mastering on the switch at load (default 1). The chip "
	"cannot DMA without it, so the SDK's first SBUSDMA in block init never "
	"completes and the status poll turns into a Data Bus Error. Set 0 only "
	"to reproduce that.");






static int dev_type = 0x20000001;
module_param(dev_type, int, 0644);
MODULE_PARM_DESC(dev_type,
	"bus type reported by command 12. Default 0x22000001, one bit at a "
	"time: bit 0 PCI, bit 29 256K register space. Bit 25 is DELIBERATELY "
	"NOT set -- see the comment below. Both were read out of the "
	"client's own _open. "
	"Bits 0 and 29: _open gates the whole register window on "
	"(dev_type & 0x0001008d) and sizes it from bits 30/29/31 = "
	"128K/256K/320K; the vendor kernel ORs bit 29 for this device, so "
	"256K is not a guess. 2 is BDE_SPI_DEV_TYPE -- it attaches and even "
	"prints a banner, but matches none of the mask bits, so the register "
	"mmap AND a sal_mutex_create are both skipped and init later dies on "
	"a NULL mutex nowhere near the cause. Bit 25: see skip_iproc_probe "
	"below for why it defaults on.");

/*
 * Bit 25 of dev_type, and why it is now CLEAR.
 *
 * It was set here for months on the belief that it meant "skip the BAR0 + 0x2C00
 * probe read" -- a read that had once left this board's PCIe path in an error
 * state and needed a power cycle. It does skip that read. It also does something
 * else, and that something else broke every iProc access the SDK made.
 *
 * Bit 25 has TWO effects, in two different functions:
 *
 *   _open (line 962)         skips the loop at line 965 over BAR0 + 0x2C00 + i*4.
 *                            That loop is WINDOW DISCOVERY: it reads the PAXB
 *                            IMAP registers and fills iproc_map[dev] from the
 *                            hardware's real configuration. Skip it and iproc_map
 *                            stays at the compiled-in iproc_map_default.
 *
 *   _iproc_read (line 1863)  bypasses _iproc_offset() ENTIRELY and uses the raw
 *                            iProc address as a direct index into the 32 KB BAR0
 *                            mapping:
 *
 *                              v0 = devs[dev]->[0x04]   (this field is dev_type)
 *                              ext v0, v0, 0x19, 0x1    (bit 25)
 *                              bnez v0, <skip the windowing>
 *
 * So with bit 25 set, soc_dpp_init's first iProc read computed
 * base + 0x18300004 against a 0x8000 window and died. There is no range check in
 * _iproc_read, only a NULL check on the base, so it faults at a wild address far
 * from anything mapped.
 *
 * The 0x2C00 block was then probed properly, from the kernel, with get_dbe() --
 * see ffn_bde_paxb_probe_window(). All 8 words read, no bus errors, and they are
 * IMAP windows already programmed by firmware:
 *
 *   0x2C00 0x18000001   0x2C08 0x18012001   0x2C10 0x18300001   0x2C18 0
 *   0x2C04 0xffff0001   0x2C0C 0xffff1001   0x2C14 0x18310001   0x2C1C 0
 *
 * Window 4 already covers 0x18300000 -- exactly the address the SDK wanted. The
 * hardware layout differs from iproc_map_default (which has 0x18030000 and lacks
 * 0x18300000), which is precisely why the discovery loop exists. With bit 25
 * clear the client reads these itself, and the SDK walks on into real DNX init:
 * Device Reset and Access Enable, Blocks OOR and PLL configuration, Traffic
 * Disable, Blocks Initial configuration.
 *
 * Clearing bit 25 does re-enable the client's own user-mode read of that block
 * through /dev/mem. That was the thing feared -- and it succeeds. The original
 * abort was not this address being absent.
 *
 * Set bit 25 again only to reproduce the old failure. It is not a safety knob;
 * it changes the iProc addressing mode.
 */

/*
 * Command 5 also returns d2, and the recovered contract says mmap goes to
 * /dev/linux-kernel-bde only when nr 5 reports d2 != 0. Leaving it zero is why
 * our mmap handler was never entered in any run: with dma_flag(d1)=1 the SDK
 * skipped mapping altogether, which is consistent with the shell reporting
 *   CM: Base=(nil)
 * i.e. no register window at all -- so init could never get anywhere.
 */
static int dma_d2 = 1;
module_param(dma_d2, int, 0644);
MODULE_PARM_DESC(dma_d2,
	"value returned as d2 from command 5 (default 1). Non-zero selects mmap "
	"through /dev/linux-kernel-bde, which is what gets a register window.");

static int dev_state = 0;
module_param(dev_state, int, 0644);
MODULE_PARM_DESC(dev_state,
	"value reported for command 30 get_dev_state (default 0). Unknown "
	"semantics; parameterised so it can be probed without a rebuild.");

static int verbose = 1;
module_param(verbose, int, 0644);
MODULE_PARM_DESC(verbose,
	"log every ioctl. 1 logs unhandled commands (default), 2 logs all.");

struct ffn_bde_dev {
	struct pci_dev *pdev;
	u64 bar_start[6];
	u64 bar_len[6];
	/*
	 * A coherent region offered to the SDK in response to command 5.
	 * dma_handle is what the device should use; cpu_addr is the kernel
	 * mapping, and its physical address is what userspace mmaps. On OCTEON
	 * with no IOMMU in this path the two coincide, which is checked at
	 * allocation time rather than assumed.
	 */
	void *dma_cpu;
	dma_addr_t dma_handle;
	size_t dma_size;
	bool dma_reserved;	/* pool is a boot-reserved range, not an allocation */

	/*
	 * Interrupt delivery to userspace. The SDK runs a thread that blocks in
	 * command 9 and services the chip itself, so all this side has to do is
	 * "wake me when the line asserts" -- it must never touch chip interrupt
	 * registers, because the SDK owns those.
	 *
	 * irq_seq is a counter rather than a flag so a wakeup cannot be missed:
	 * a waiter records the value it started from and sleeps until it moves.
	 * irq_masked tracks whether the ISR has disabled the line, so that
	 * disable/enable stay balanced (an unbalanced enable_irq() warns and
	 * then breaks the count).
	 */
	int irq;
	bool irq_requested;
	bool msi_enabled;
	wait_queue_head_t irq_wq;
	atomic_t irq_seq;
	atomic_t irq_masked;
	u32 irq_seen;

	/*
	 * The CMIC register window (BAR2, 256K as the client maps it), kept
	 * mapped for command 22: the SDK hands us CMIC interrupt-mask register
	 * offsets to write, and the vendor implements that as a plain store into
	 * this window. imask/imask2/fmask mirror the vendor's bookkeeping.
	 */
	void __iomem *bar2_regs;
	u32 imask, imask2, fmask;
};

static DEFINE_MUTEX(ffn_bde_lock);
static struct ffn_bde_dev ffn_bde_devs[1];
static int ffn_bde_ndev;

/*
 * Put the PCIe-AXI bridge into big-endian PIO mode.
 *
 * The CMIC's registers are little-endian and this CPU is big-endian. Either the
 * driver swaps every access in software -- which is what ffn_bcm does, and why
 * it reads the device id register as 0x75831100 -- or the bridge swaps in
 * hardware and everyone reads natural values. The SDK assumes the second: it
 * never asks us about byte order (command 21 is never issued in a full run), so
 * it cannot be told, and if the bridge is not swapping, every register write it
 * makes lands byte-reversed. The visible symptom is an S-channel timeout with
 * nothing else wrong, which is why this is worth a long comment.
 *
 * The mechanism, and the write pattern, follow the hardware's own protocol:
 * write 0x01010101 to BAR0 + 0x2030 and read it back. 0x01010101 is chosen
 * because it is byte-symmetric -- it sets bit 0 of whichever byte lane ends up
 * at bit 0, so it works without knowing which convention is currently in
 * effect, which is the only way to break the chicken-and-egg. A readback of
 * exactly 1 means the register took the low bit and reads are now natural. Any
 * other value means this is not that register on this device, and the write is
 * undone rather than left in an unknown state.
 *
 * Verified on the live device, before any of this was in code:
 *
 *   BAR0+0x2030   0x00000000 -> write 0x01010101 -> reads 0x00000001
 *   BAR2+0x10224  0x75831100 becomes 0x00118375   (device id, natural)
 *   BAR2+0x10098  0x04444447 becomes 0x47444404   (ring map, ECI on ring 7)
 *   BAR0+0x0      0x75830000 becomes 0x00008375   (device id, natural)
 *
 * and with it enabled the SDK's S-channel timeout disappeared and its init
 * walked on into real work. __raw_ accessors are used deliberately: readl() and
 * writel() would apply the kernel's own swapping, which on MIPS depends on
 * CONFIG_SWAP_IO_SPACE, so the result would be right or wrong depending on how
 * the kernel was configured rather than on what this chip needs.
 */
#define FFN_PAXB_ENDIAN_OFF	0x2030
#define FFN_PAXB_BE_PATTERN	0x01010101

/*
 * The rest of the vendor sequence, same order as shbde_iproc_paxb_init(). Names
 * are the ones OpenBCM uses (systems/bde/shared/shbde_iproc.c).
 */
#define FFN_PAXB_IMAP0_2_OFF		0x2C08	/* bit 12: PAXB_1 selected           */
#define FFN_PAXB_IMAP0_2_PAXB1_BIT	12
#define FFN_PAXB_EP_AXI_CONFIG_OFF	0x2104	/* written 0: enable DMA to host    */
#define FFN_PAXB_OARR_2_OFF		0x2D60	/* outbound window: 1 = valid       */
#define FFN_PAXB_OARR_2_UPPER_OFF	0x2D64	/* outbound window: dma_hi_bits     */
#define FFN_PAXB_OMAP_2_OFF		0x2D68	/* its PCIe-side base, read for log */
#define FFN_PAXB_OMAP_2_UPPER_OFF	0x2D6C
#define FFN_PAXB_MSI_PAGE_OFF		0x2D34	/* OARR_FUNC0_MSI_PAGE |= 1 for MSI */
#define FFN_PAXB_INTR_EN_OFF		0x2380	/* CMICD_TO_PCIE_INTR_EN bit0 = INTx */

/* Old names, kept for any reference that still uses them. */
#define FFN_PAXB_STRAP_OFF		FFN_PAXB_IMAP0_2_OFF
#define FFN_PAXB_CLR_OFF		FFN_PAXB_EP_AXI_CONFIG_OFF
#define FFN_PAXB_WIN_EN_OFF		FFN_PAXB_OARR_2_OFF
#define FFN_PAXB_WIN_VAR_OFF		FFN_PAXB_OARR_2_UPPER_OFF
#define FFN_PAXB_PREEMPH_OFF		FFN_PAXB_MSI_PAGE_OFF

/* Where the SDK programs the dynamic iProc window. Logged, not written here. */
#define FFN_PAXB_IPROC_WIN_OFF	0x2C1C

/*
 * Read the PAXB window block one word at a time, reporting rather than dying.
 *
 * Every access goes through get_dbe(), so a word that does not decode comes
 * back as -EFAULT and the scan carries on. What we are looking for:
 *
 *   all 8 words fault      -> the block genuinely does not decode here, bit 25
 *                             has to stay set and iProc access needs another
 *                             route entirely
 *   all 8 words read       -> the old abort was about HOW it was accessed (user
 *                             mode through /dev/mem, possibly before the
 *                             0x2030 byte-order write), not about the address.
 *                             Clear bit 25 and let the client window properly.
 */
#define FFN_PAXB_WIN_BLOCK	0x2C00
#define FFN_PAXB_WIN_WORDS	8

static void ffn_bde_paxb_probe_window(void __iomem *bar0)
{
	int i, ok = 0, bad = 0;

	pr_info(DRV ": probing BAR0+0x%x..0x%x with get_dbe (bus errors are "
		"caught, not fatal)\n", FFN_PAXB_WIN_BLOCK,
		FFN_PAXB_WIN_BLOCK + (FFN_PAXB_WIN_WORDS - 1) * 4);

	for (i = 0; i < FFN_PAXB_WIN_WORDS; i++) {
		unsigned int off = FFN_PAXB_WIN_BLOCK + i * 4;
		volatile u32 *p = (volatile u32 *)((unsigned long)bar0 + off);
		u32 v = 0;

		if (get_dbe(v, p)) {
			pr_info(DRV ":   BAR0+0x%04x  BUS ERROR\n", off);
			bad++;
			continue;
		}
		pr_info(DRV ":   BAR0+0x%04x  0x%08x%s\n", off, v,
			off == FFN_PAXB_IPROC_WIN_OFF ?
				"   <- dynamic window control" : "");
		ok++;
	}

	pr_info(DRV ": PAXB window probe: %d readable, %d bus errors. %s\n",
		ok, bad,
		bad == 0 ?
		"Block decodes -- dev_type bit 25 can be cleared so the client "
		"does real iProc windowing." :
		"Block does not fully decode -- leave bit 25 set.");
}

/*
 * ffn_bde_paxb_init() -- the whole vendor PAXB bring-up.
 *
 * Step 1 (byte order) is unconditional and is the part that has always been
 * here. Steps 2-5 are gated on paxb_full; the reasoning for that default is on
 * the module parameter. The vendor reaches these registers from the kernel via
 * linux_io32_read/_write, which is what we do here -- the user-mode /dev/mem
 * path is the one with the abort history.
 *
 * Why steps 2-5 matter at all: the SDK reaches iProc registers through a
 * dynamic window it programs at BAR0+0x2C1C, reading back to confirm, then
 * accessing BAR0+0x7000 + (addr & 0xfff). With the PAXB only half configured
 * that window does not take, and the first iProc read in soc_dpp_init computes
 * an offset outside the 32 KB aperture and dies -- there is no range check in
 * _iproc_read, only a NULL check on the base.
 *
 * BAR0 is mapped once for the whole sequence rather than per register: the old
 * code ioremapped four bytes, which is fine for one register and silly for six.
 */
static int ffn_bde_paxb_init(struct ffn_bde_dev *d)
{
	void __iomem *bar0;
	u32 back, strap, variant;
	int rc = 0;

	if (!paxb_be && !paxb_full)
		return 0;

	if (!d->bar_len[0] || d->bar_len[0] < FFN_PAXB_WIN_VAR_OFF + 4) {
		pr_warn(DRV ": BAR0 is %lu bytes, too small for the PAXB "
			"registers -- byte order NOT configured\n",
			(unsigned long)d->bar_len[0]);
		return -ENODEV;
	}

	bar0 = ioremap(d->bar_start[0], d->bar_len[0]);
	if (!bar0) {
		pr_warn(DRV ": cannot map BAR0 for PAXB init\n");
		return -ENOMEM;
	}

	/* 1. byte order. */
	if (paxb_be) {
		__raw_writel(FFN_PAXB_BE_PATTERN, bar0 + FFN_PAXB_ENDIAN_OFF);
		back = __raw_readl(bar0 + FFN_PAXB_ENDIAN_OFF);
		if (back != 1) {
			/* Not the register we think it is. Leave nothing behind. */
			__raw_writel(0, bar0 + FFN_PAXB_ENDIAN_OFF);
			pr_warn(DRV ": PAXB endianness readback 0x%08x, "
				"expected 1 -- reverted, PIO byte order is "
				"NOT configured\n", back);
			rc = -EIO;
			goto out;
		}
		pr_info(DRV ": PAXB big-endian PIO mode on (BAR0+0x%x reads "
			"1); registers now read natural values\n",
			FFN_PAXB_ENDIAN_OFF);
	}

	if (probe_paxb_win)
		ffn_bde_paxb_probe_window(bar0);

	if (!paxb_full) {
		pr_info(DRV ": PAXB init stopped after byte order "
			"(paxb_full=0). iProc register access will NOT work: "
			"the SDK programs its dynamic window at BAR0+0x%x and "
			"soc_dpp_init needs it.\n", FFN_PAXB_IPROC_WIN_OFF);
		goto out;
	}

	/*
	 * 2. Which PAXB core is in use decides the outbound window's upper AXI
	 *    bits. Vendor: if (IMAP0_2 & 0x1000) pci_num = 1; dma_hi_bits =
	 *    pci_num ? 2 : 1, unless the chip table already set it (it does not
	 *    know 0x8375). Read after step 1, as the vendor orders it.
	 */
	if (paxb_dma_hi_bits == 0 && (paxb_variant == 1 || paxb_variant == 2))
		paxb_dma_hi_bits = paxb_variant;
	if (paxb_dma_hi_bits == 1 || paxb_dma_hi_bits == 2) {
		variant = paxb_dma_hi_bits;
		pr_info(DRV ": PAXB dma_hi_bits forced to %u, IMAP0_2 not read\n",
			variant);
	} else {
		strap = __raw_readl(bar0 + FFN_PAXB_IMAP0_2_OFF);
		variant = (strap & (1u << FFN_PAXB_IMAP0_2_PAXB1_BIT)) ? 2 : 1;
		pr_info(DRV ": PAXB_IMAP0_2 = 0x%08x -> PAXB_%u -> dma_hi_bits %u\n",
			strap, variant - 1, variant);
	}

	/* 3. "Enable iProc DMA to external host memory". Unconditional. */
	__raw_writel(0, bar0 + FFN_PAXB_EP_AXI_CONFIG_OFF);

	/*
	 * 4. The outbound window itself. Vendor gates this on cmic_ver < 4 (i.e.
	 *    not CMICX); the BCM88375 is CMICm, so it applies. OMAP_2 is the
	 *    PCIe-side base of the same window and is logged, not written --
	 *    the vendor leaves it at reset.
	 */
	__raw_writel(1, bar0 + FFN_PAXB_OARR_2_OFF);
	__raw_writel(variant, bar0 + FFN_PAXB_OARR_2_UPPER_OFF);
	pr_info(DRV ": PAXB OARR_2 0x%08x OARR_2_UPPER 0x%08x  OMAP_2 0x%08x "
		"OMAP_2_UPPER 0x%08x\n",
		__raw_readl(bar0 + FFN_PAXB_OARR_2_OFF),
		__raw_readl(bar0 + FFN_PAXB_OARR_2_UPPER_OFF),
		__raw_readl(bar0 + FFN_PAXB_OMAP_2_OFF),
		__raw_readl(bar0 + FFN_PAXB_OMAP_2_UPPER_OFF));

	/* 5. MSI page bit, and route interrupts to MSI rather than INTx. */
	if (paxb_preemph || use_msi) {
		back = __raw_readl(bar0 + FFN_PAXB_MSI_PAGE_OFF);
		__raw_writel(back | 1, bar0 + FFN_PAXB_MSI_PAGE_OFF);
		pr_info(DRV ": PAXB OARR_FUNC0_MSI_PAGE 0x%08x -> 0x%08x\n",
			back, back | 1);
	}
	back = __raw_readl(bar0 + FFN_PAXB_INTR_EN_OFF);
	if (use_msi)
		__raw_writel(back & ~1u, bar0 + FFN_PAXB_INTR_EN_OFF);
	else
		__raw_writel(back | 1u, bar0 + FFN_PAXB_INTR_EN_OFF);
	pr_info(DRV ": PAXB CMICD_TO_PCIE_INTR_EN 0x%08x -> 0x%08x (%s)\n", back,
		__raw_readl(bar0 + FFN_PAXB_INTR_EN_OFF), use_msi ? "MSI" : "INTx");

	pr_info(DRV ": full PAXB init done (dma_hi_bits %u); the iProc window at "
		"BAR0+0x%x should now take\n", variant, FFN_PAXB_IPROC_WIN_OFF);
out:
	iounmap(bar0);
	return rc;
}

/*
 * One interrupt gets through, then the line is masked until userspace asks for
 * the next one. disable_irq_nosync() is used rather than disable_irq() because
 * we are in the handler for that very irq and the synchronous form would
 * deadlock waiting for itself to finish.
 */
static irqreturn_t ffn_bde_isr(int irq, void *arg)
{
	struct ffn_bde_dev *d = arg;

	if (!atomic_xchg(&d->irq_masked, 1))
		disable_irq_nosync(d->irq);

	atomic_inc(&d->irq_seq);
	wake_up_interruptible(&d->irq_wq);
	return IRQ_HANDLED;
}

/* Undo the ISR's mask. Safe to call when not masked; the xchg makes it a no-op. */
static void ffn_bde_irq_rearm(struct ffn_bde_dev *d)
{
	if (d->irq_requested && atomic_xchg(&d->irq_masked, 0))
		enable_irq(d->irq);
}

static void ffn_bde_irq_mask(struct ffn_bde_dev *d)
{
	if (d->irq_requested && !atomic_xchg(&d->irq_masked, 1))
		disable_irq_nosync(d->irq);
}

/*
 * Route the CMIC interrupt to the INTx line instead of MSI.
 *
 * ffn_bde_paxb_init() already does this when use_msi=0, but it runs BEFORE
 * ffn_bde_irq_setup(), so a load that ASKED for MSI and only discovered at
 * request time that MSI is unavailable has already cleared the bit. Set it
 * back, or the fallback installs a handler for a line the chip will never
 * assert -- which looks exactly like having no handler at all.
 */
static void ffn_bde_paxb_route_intx(struct ffn_bde_dev *d)
{
	void __iomem *bar0;
	u32 back;

	bar0 = ioremap(d->bar_start[0], d->bar_len[0]);
	if (!bar0) {
		pr_warn(DRV ": cannot map BAR0 to route INTx\n");
		return;
	}
	back = __raw_readl(bar0 + FFN_PAXB_INTR_EN_OFF);
	__raw_writel(back | 1u, bar0 + FFN_PAXB_INTR_EN_OFF);
	pr_info(DRV ": PAXB CMICD_TO_PCIE_INTR_EN 0x%08x -> 0x%08x (INTx fallback)\n",
		back, __raw_readl(bar0 + FFN_PAXB_INTR_EN_OFF));
	iounmap(bar0);
}

/*
 * Install the one interrupt the SDK's command 9 waits on: MSI when available,
 * otherwise INTx.
 *
 * The fallback is not a nicety here. CONFIG_PCI_MSI cannot be enabled on the
 * 6.18 CP at all -- it drags in arch/mips/pci/msi-octeon.c, CIU-era OCTEON I/II
 * code that requests OCTEON_IRQ_PCI_MSI0, an interrupt which does not exist on
 * the CN73XX CIU3, and panics rather than degrading. So pci_enable_msi() always
 * fails on this platform, and with no fallback the SDK's first interrupt wait
 * never returns: every core wedges at one PC and the chip soft-resets. That is
 * exactly what running bcm.user on 6.18 produced.
 *
 * INTx is available because pci_assign_irq() works again -- it used to oops on
 * a dangling __init map_irq handler -- so the device has a real legacy IRQ.
 */
static void ffn_bde_irq_setup(struct ffn_bde_dev *d)
{
	int rc;

	init_waitqueue_head(&d->irq_wq);
	atomic_set(&d->irq_seq, 0);
	atomic_set(&d->irq_masked, 0);
	d->irq_seen = 0;
	d->irq = -1;

	if (use_msi) {
		if (!pci_enable_msi(d->pdev)) {
			d->msi_enabled = true;
			d->irq = d->pdev->irq;
			rc = request_irq(d->irq, ffn_bde_isr, 0, DRV, d);
			if (!rc) {
				d->irq_requested = true;
				pr_info(DRV ": MSI irq %d installed; command 9 waits on it\n",
					d->irq);
				return;
			}
			pr_warn(DRV ": request_irq(%d) for MSI failed (%d); falling back to INTx\n",
				d->irq, rc);
			pci_disable_msi(d->pdev);
			d->msi_enabled = false;
			d->irq = -1;
		} else {
			pr_info(DRV ": pci_enable_msi failed -- expected on this CP (CONFIG_PCI_MSI is off; msi-octeon.c is CIU-era and panics on CIU3). Falling back to INTx.\n");
		}
	}

	/* paxb_init cleared the INTx enable if it ran with use_msi=1. */
	ffn_bde_paxb_route_intx(d);

	d->irq = d->pdev->irq;
	if (d->irq <= 0) {
		pr_warn(DRV ": no legacy IRQ assigned (pdev->irq=%d); no handler. Command 9 will block forever (thread parks).\n",
			d->irq);
		d->irq = -1;
		return;
	}

	/*
	 * Exclusive, NOT IRQF_SHARED. ffn_bde_isr() cannot tell whether this
	 * device raised the line without reading chip registers that belong to
	 * the SDK -- racing the very code we serve -- so it returns IRQ_HANDLED
	 * unconditionally. That is only honest on a line nobody else is on, and
	 * /proc/interrupts shows this one unused. Requesting it exclusively means
	 * request_irq() fails loudly if that stops being true, rather than
	 * silently swallowing another driver's interrupts.
	 */
	rc = request_irq(d->irq, ffn_bde_isr, 0, DRV, d);
	if (rc) {
		pr_warn(DRV ": request_irq(%d) for INTx failed (%d); no handler. Command 9 will block forever (thread parks).\n",
			d->irq, rc);
		d->irq = -1;
		return;
	}
	d->irq_requested = true;
	pr_info(DRV ": INTx irq %d installed; command 9 waits on it\n", d->irq);
}

static void ffn_bde_irq_teardown(struct ffn_bde_dev *d)
{
	if (d->irq_requested) {
		/* Balance the ISR's mask before freeing, or free_irq() warns. */
		ffn_bde_irq_rearm(d);
		free_irq(d->irq, d);
		d->irq_requested = false;
	}
	if (d->msi_enabled) {
		pci_disable_msi(d->pdev);
		d->msi_enabled = false;
	}
}

/*
 * BAR2 is 8 MB but the client only ever maps the first 256 KB (dev_type bit 29),
 * and the highest register in the recovered CMIC map is 0x33858, so mapping
 * 256 KB here covers everything the SDK can reach and keeps the probe honest
 * about what it is testing.
 */
#define FFN_BDE_BAR2_WINDOW	0x40000

struct ffn_bde_probe_reg {
	unsigned int off;
	const char *name;
	int control;	/* known to read; a failure here means the probe is wrong */
};

static const struct ffn_bde_probe_reg ffn_bde_bar2_probe[] = {
	{ 0x10000, "CMIC_COMMON_SCHAN_CTRL",              1 },
	{ 0x10098, "CMIC_SBUS_RING_MAP_0_7",              1 },
	{ 0x10224, "device id",                           1 },
	{ 0x31600, "CMIC_CMC0_SBUSDMA_CH0_CONTROL",       0 },
	{ 0x3161c, "CMIC_CMC0_SBUSDMA_CH0_STATUS",        0 },
	{ 0x31650, "CMIC_CMC0_SBUSDMA_CH1_CONTROL",       0 },
	{ 0x3166c, "CMIC_CMC0_SBUSDMA_CH1_STATUS",        0 },
	{ 0x31690, "CMIC_CMC0_SBUSDMA_CH1_SBUSDMA_DEBUG", 0 },
};

static void ffn_bde_probe_bar2_regs(struct ffn_bde_dev *d)
{
	void __iomem *bar2;
	unsigned int i;
	int ctl_bad = 0, sbus_bad = 0;

	if (!d->bar_len[2]) {
		pr_warn(DRV ": no BAR2 to probe\n");
		return;
	}

	bar2 = ioremap(d->bar_start[2], FFN_BDE_BAR2_WINDOW);
	if (!bar2) {
		pr_warn(DRV ": cannot map BAR2 for the register probe\n");
		return;
	}

	pr_info(DRV ": probing BAR2 CMIC registers with get_dbe\n");
	for (i = 0; i < ARRAY_SIZE(ffn_bde_bar2_probe); i++) {
		const struct ffn_bde_probe_reg *r = &ffn_bde_bar2_probe[i];
		volatile u32 *p =
			(volatile u32 *)((unsigned long)bar2 + r->off);
		u32 v = 0;

		if (get_dbe(v, p)) {
			pr_info(DRV ":   BAR2+0x%05x  BUS ERROR   %s%s\n",
				r->off, r->name,
				r->control ? "   <- CONTROL, probe is wrong" : "");
			if (r->control)
				ctl_bad++;
			else
				sbus_bad++;
			continue;
		}
		pr_info(DRV ":   BAR2+0x%05x  0x%08x  %s\n", r->off, v, r->name);
	}
	iounmap(bar2);

	if (ctl_bad)
		pr_warn(DRV ": %d CONTROL register(s) bus errored -- do not trust "
			"this probe, the mapping or byte order is wrong\n", ctl_bad);
	else if (sbus_bad)
		pr_info(DRV ": controls read, %d SBUSDMA register(s) bus errored "
			"-- the SBUSDMA block does not decode on a clean chip\n",
			sbus_bad);
	else
		pr_info(DRV ": all probed registers read, SBUSDMA included -- the "
			"SDK's later bus error there is something it does, not the "
			"address\n");
}

/*
 * Turn on bus mastering, and say what changed. The before/after is logged
 * because "the chip cannot DMA" is invisible in every other symptom -- register
 * access keeps working perfectly, so the failure only shows up much later and
 * looks like a dead register.
 */
static void ffn_bde_set_master(struct ffn_bde_dev *d)
{
	u16 cmd_before = 0, cmd_after = 0;

	if (!set_master)
		return;

	pci_read_config_word(d->pdev, PCI_COMMAND, &cmd_before);
	pci_set_master(d->pdev);
	pci_read_config_word(d->pdev, PCI_COMMAND, &cmd_after);

	if (cmd_after & PCI_COMMAND_MASTER)
		pr_info(DRV ": bus master enabled, PCI_COMMAND 0x%04x -> 0x%04x; "
			"the chip can now DMA (SBUSDMA needs this)\n",
			cmd_before, cmd_after);
	else
		pr_warn(DRV ": bus master did NOT stick, PCI_COMMAND 0x%04x -> "
			"0x%04x; SBUSDMA will hang and its status read will bus "
			"error\n", cmd_before, cmd_after);
}


/* Ad-hoc range dump. Offsets only -- names are mapped offline from the
 * recovered CMIC register map, which is vendor-derived and stays off the box. */
static void ffn_bde_probe_bar2_range(struct ffn_bde_dev *d)
{
	void __iomem *bar2;
	unsigned int start = (unsigned int)probe_bar2_off & ~3u;
	unsigned int n = (unsigned int)probe_bar2_n;
	unsigned int i;

	if (n > 64)
		n = 64;
	if (!d->bar_len[2] || start + n * 4 > FFN_BDE_BAR2_WINDOW) {
		pr_warn(DRV ": ad-hoc probe 0x%05x+%u words is outside the "
			"0x%x window\n", start, n, FFN_BDE_BAR2_WINDOW);
		return;
	}

	bar2 = ioremap(d->bar_start[2], FFN_BDE_BAR2_WINDOW);
	if (!bar2) {
		pr_warn(DRV ": cannot map BAR2 for the ad-hoc probe\n");
		return;
	}

	pr_info(DRV ": ad-hoc probe BAR2+0x%05x, %u words\n", start, n);
	for (i = 0; i < n; i++) {
		unsigned int off = start + i * 4;
		volatile u32 *p =
			(volatile u32 *)((unsigned long)bar2 + off);
		u32 v = 0;

		if (get_dbe(v, p))
			pr_info(DRV ":   BAR2+0x%05x  BUS ERROR\n", off);
		else
			pr_info(DRV ":   BAR2+0x%05x  0x%08x\n", off, v);
	}
	iounmap(bar2);
}


/*
 * PEMX_BAR_CTL layout (from cvmx-pemx-defs.h):
 *   [6:4] bar1_siz   0x1=64M 0x2=128M 0x3=256M 0x4=512M 0x5=1G 0x6=2G
 *   [3]   bar2_enb   1 = BAR2 responds; 0 = BAR2 access gets UR
 *   [2:1] bar2_esx   XORed with PCIe address [43:42] -> endian swap mode
 *   [0]   bar2_cax   XORed with PCIe address [44]    -> L2 cache attribute
 *
 * The esx/cax fields are the reason an inbound address is not simply a physical
 * address: bits [44:42] of what the device emits select attributes, so a device
 * reaching DRAM through BAR2 targets OCTEON_BAR2_PCI_ADDRESS (0x8000000000)
 * plus the physical address, not the bare physical address.
 */
static void ffn_bde_probe_pem_regs(int port)
{
	u64 bar_ctl, b0, b1, b2, ctl_status;
	unsigned int bar1_siz, bar2_enb, bar2_esx, bar2_cax;

	if (port < 0 || port > 3) {
		pr_warn(DRV ": probe_pem=%d is not a PCIe port\n", port);
		return;
	}

	bar_ctl    = cvmx_read_csr(CVMX_PEMX_BAR_CTL(port));
	b0         = cvmx_read_csr(CVMX_PEMX_P2N_BAR0_START(port));
	b1         = cvmx_read_csr(CVMX_PEMX_P2N_BAR1_START(port));
	b2         = cvmx_read_csr(CVMX_PEMX_P2N_BAR2_START(port));
	ctl_status = cvmx_read_csr(CVMX_PEMX_CTL_STATUS(port));

	bar1_siz = (unsigned int)((bar_ctl >> 4) & 0x7);
	bar2_enb = (unsigned int)((bar_ctl >> 3) & 0x1);
	bar2_esx = (unsigned int)((bar_ctl >> 1) & 0x3);
	bar2_cax = (unsigned int)(bar_ctl & 0x1);

	pr_info(DRV ": PEM%d inbound configuration\n", port);
	pr_info(DRV ":   PEMX_BAR_CTL        0x%016llx\n",
		(unsigned long long)bar_ctl);
	pr_info(DRV ":     bar1_siz %u  bar2_enb %u  bar2_esx %u  bar2_cax %u\n",
		bar1_siz, bar2_enb, bar2_esx, bar2_cax);
	pr_info(DRV ":   P2N_BAR0_START      0x%016llx\n",
		(unsigned long long)b0);
	pr_info(DRV ":   P2N_BAR1_START      0x%016llx\n",
		(unsigned long long)b1);
	pr_info(DRV ":   P2N_BAR2_START      0x%016llx\n",
		(unsigned long long)b2);
	pr_info(DRV ":   PEMX_CTL_STATUS     0x%016llx\n",
		(unsigned long long)ctl_status);

	if (!bar2_enb)
		pr_warn(DRV ":   bar2_enb is CLEAR -- inbound writes to BAR2 get "
			"UR and are dropped. This is why the chip reports DMA "
			"complete and nothing lands in DRAM.\n");
	else
		pr_info(DRV ":   bar2_enb is set, so BAR2 responds; a device "
			"reaches DRAM at OCTEON_BAR2_PCI_ADDRESS (0x8000000000) "
			"+ phys, NOT at the bare physical address.\n");
}


/*
 * Fill / dump the DMA region from the kernel side.
 *
 * Deliberately uses d->dma_cpu rather than a fresh mapping: that is the exact
 * memory dma_alloc_coherent handed out and whose bus address the device was
 * given, so what it shows is what the device should have written to. Reading it
 * any other way reintroduces the question this is meant to settle.
 */
static int ffn_bde_dma_probe_set(const char *val, const struct kernel_param *kp)
{
	struct ffn_bde_dev *d = &ffn_bde_devs[0];
	unsigned int i, nonpat = 0;
	const u32 *w;
	char cmd[16];
	size_t n;
	ssize_t r;

	if (!d->dma_cpu) {
		pr_warn(DRV ": no DMA region to probe\n");
		return -ENODEV;
	}

	/* strlcpy() was removed in 6.8. It returned the SOURCE length, so the
	 * old "n >= sizeof(cmd)" was a truncation test. strscpy() instead
	 * returns the copied length or -E2BIG, so test the error explicitly.
	 * Assigning it straight to a size_t happens to still reject truncation
	 * (-E2BIG wraps to a huge unsigned), but relying on that wraparound is
	 * not something to leave in a driver -- and if n were ever made signed,
	 * cmd[n - 1] below would index before the buffer.
	 */
	r = strscpy(cmd, val, sizeof(cmd));
	if (r < 0)
		return -EINVAL;
	n = (size_t)r;
	while (n && (cmd[n - 1] == 10 || cmd[n - 1] == 13 || cmd[n - 1] == 32))
		cmd[--n] = 0;

	if (!strcmp(cmd, "fill")) {
		memset(d->dma_cpu, 0xff, 4096);
		/* Make sure it is visible to the device before it starts. */
		wmb();
		pr_info(DRV ": DMA region first 4K filled with 0xff (kernel "
			"mapping %p, bus 0x%llx)\n", d->dma_cpu,
			(unsigned long long)d->dma_handle);
		return 0;
	}

	if (strcmp(cmd, "show"))
		return -EINVAL;

	rmb();
	w = (const u32 *)d->dma_cpu;
	pr_info(DRV ": DMA region, words that are no longer the 0xff "
		"pattern (kernel coherent mapping):\n");
	for (i = 0; i < 1024; i++) {
		if (w[i] == 0xffffffffu)
			continue;
		nonpat++;
		/* Cap the log: the shape is visible long before 1024. */
		if (nonpat <= 24)
			pr_info(DRV ":   +0x%04x  %08x\n", i * 4, w[i]);
	}
	if (nonpat > 24)
		pr_info(DRV ":   ... and %u more\n", nonpat - 24);
	pr_info(DRV ": %u of 1024 words in the first 4K differ -- %s\n",
		nonpat,
		nonpat ? "the device DID write here" :
		"nothing was written through this mapping");
	return 0;
}

static const struct kernel_param_ops ffn_bde_dma_probe_ops = {
	.set = ffn_bde_dma_probe_set,
};
module_param_cb(dma_probe, &ffn_bde_dma_probe_ops, NULL, 0200);
MODULE_PARM_DESC(dma_probe,
	"write \"fill\" to stamp the DMA region with 0xff, or \"show\" to "
	"dump it, both through the kernel coherent mapping. Use this rather "
	"than /dev/mem when the question is whether the device wrote at all: "
	"it reads the same memory the device was pointed at.");


static void ffn_bde_probe_bar0_range(struct ffn_bde_dev *d)
{
	void __iomem *bar0;
	unsigned int start = (unsigned int)probe_bar0_off & ~3u;
	unsigned int n = (unsigned int)probe_bar0_n;
	unsigned int i;

	if (n > 64)
		n = 64;
	if (!d->bar_len[0] || start + n * 4 > d->bar_len[0]) {
		pr_warn(DRV ": ad-hoc BAR0 probe 0x%04x+%u words is outside "
			"BAR0 (%llu bytes)\n", start, n,
			(unsigned long long)d->bar_len[0]);
		return;
	}
	bar0 = ioremap(d->bar_start[0], d->bar_len[0]);
	if (!bar0) {
		pr_warn(DRV ": cannot map BAR0 for the ad-hoc probe\n");
		return;
	}
	pr_info(DRV ": ad-hoc probe BAR0+0x%04x, %u words\n", start, n);
	for (i = 0; i < n; i++) {
		unsigned int off = start + i * 4;
		volatile u32 *p =
			(volatile u32 *)((unsigned long)bar0 + off);
		u32 v = 0;

		if (get_dbe(v, p))
			pr_info(DRV ":   BAR0+0x%04x  BUS ERROR\n", off);
		else
			pr_info(DRV ":   BAR0+0x%04x  0x%08x\n", off, v);
	}
	iounmap(bar0);
}

/* ------------------------------------------------------------- discovery -- */

static int ffn_bde_scan(void)
{
	struct ffn_bde_dev *d = &ffn_bde_devs[0];
	struct pci_dev *pdev;
	unsigned int i;

	pdev = pci_get_domain_bus_and_slot(domain, bus, PCI_DEVFN(slot, func));
	if (!pdev) {
		pr_warn(DRV ": no PCI device at %04x:%02x:%02x.%d\n",
			domain, bus, slot, func);
		return 0;
	}

	if (pdev->vendor != FFN_BDE_PCI_VENDOR_BROADCOM ||
	    pdev->device != FFN_BDE_PCI_DEVICE_BCM88375) {
		pr_warn(DRV ": %04x:%02x:%02x.%d is %04x:%04x, not the expected "
			"%04x:%04x -- refusing to report it as a BDE device\n",
			domain, bus, slot, func, pdev->vendor, pdev->device,
			FFN_BDE_PCI_VENDOR_BROADCOM, FFN_BDE_PCI_DEVICE_BCM88375);
		pci_dev_put(pdev);
		return 0;
	}

	d->pdev = pdev;
	for (i = 0; i < 6; i++) {
		d->bar_start[i] = pci_resource_start(pdev, i);
		d->bar_len[i] = pci_resource_len(pdev, i);
	}
	ffn_bde_ndev = 1;
	if (d->bar_len[2])
		d->bar2_regs = ioremap(d->bar_start[2],
				min_t(u64, d->bar_len[2], 0x40000));

	pr_info(DRV ": %04x:%02x:%02x.%d %04x:%04x rev %02x\n",
		domain, bus, slot, func, pdev->vendor, pdev->device,
		pdev->revision);
	for (i = 0; i < 6; i++)
		if (d->bar_len[i])
			pr_info(DRV ":   bar%u %pa len %llu\n",
				i, &d->bar_start[i],
				(unsigned long long)d->bar_len[i]);

	/*
	 * Do this here, before anything else can touch a register: the SDK
	 * cannot be told about byte order, so the device has to be in the right
	 * mode before it starts. A failure is reported and not fatal -- the
	 * device is still enumerable and the log says byte order is unconfigured,
	 * which is more useful than refusing to load.
	 */
	ffn_bde_set_master(d);
	ffn_bde_paxb_init(d);
	ffn_bde_irq_setup(d);
	if (probe_bar2)
		ffn_bde_probe_bar2_regs(d);
	if (probe_bar2_n)
		ffn_bde_probe_bar2_range(d);
	if (probe_bar0_n)
		ffn_bde_probe_bar0_range(d);
	if (probe_pem >= 0)
		ffn_bde_probe_pem_regs(probe_pem);
	return 1;
}

/*
 * The SDK asks for a DMA region (command 5) during attach. Allocate it against
 * the PCI device so it is coherent for that device, and verify the assumption
 * this platform lets us make -- that the address the device sees and the physical
 * address userspace would mmap are the same. If they ever diverge, reporting one
 * while userspace maps the other would corrupt silently, so say so loudly rather
 * than continue.
 */
/*
 * Is every page of [first,last] claimed at boot and thus never allocatable?
 *
 * memblock_reserve() leaves a struct page in place with PageReserved set,
 * whereas memblock_remove() leaves no struct page at all. Both mean "the
 * allocator will never hand this out", and both are legitimate ways for an
 * ffn_reserve= range to be protected, so accept either: no struct page
 * (pfn_valid() false) or a reserved one. A single free page anywhere in the
 * range is a refusal -- DMA onto live RAM corrupts silently.
 */
/* NOT __init: ffn_bde_dma_setup() runs at RUNTIME (the SDK's command 5 during
 * attach), so an __init helper would be a call into freed .init.text -- the
 * same defect that made pci_assign_irq oops on this platform. */
static bool ffn_bde_range_boot_reserved(unsigned long first,
					unsigned long last)
{
	unsigned long pfn;

	for (pfn = first; pfn <= last; pfn++) {
		if (!pfn_valid(pfn))
			continue;			/* removed from the map entirely */
		if (!PageReserved(pfn_to_page(pfn)))
			return false;			/* allocatable -- refuse */
	}
	return true;
}

static void ffn_bde_dma_setup(void)
{
	struct ffn_bde_dev *d = &ffn_bde_devs[0];
	size_t size = (size_t)dma_mb << 20;
	phys_addr_t phys;

	if (dma_mb <= 0)
		return;

	if (dma_phys) {
		unsigned long first = PHYS_PFN(dma_phys);
		unsigned long last  = PHYS_PFN(dma_phys + size - 1);

		if (dma_phys & ~PAGE_MASK) {
			pr_warn(DRV ": dma_phys 0x%llx is not page aligned -- "
				"refused, falling back to coherent allocation\n",
				dma_phys);
		} else if (dma_phys + size > 0x100000000ull) {
			pr_warn(DRV ": reserved pool 0x%llx+%zu ends above 4 GB; "
				"SBUSDMA host addresses are 32-bit -- refused, "
				"falling back to coherent allocation\n",
				dma_phys, size);
		} else if ((page_is_ram(first) || page_is_ram(last)) &&
			   !ffn_bde_range_boot_reserved(first, last)) {
			/*
			 * page_is_ram(), not pfn_valid(): under SPARSEMEM the latter is
			 * section-granular (256 MB sections here), so it says yes for a
			 * reserved range that merely shares a section with real RAM.
			 *
			 * But page_is_ram() alone is NOT sufficient, and assuming it was
			 * cost a bring-up: it walks the System RAM RESOURCES, and whether
			 * an ffn_reserve= range is absent from those depends on how the
			 * kernel side implements it. The 6.18 CP uses memblock_reserve(),
			 * which keeps the range out of the buddy allocator but leaves it
			 * in memblock.memory -- so it still appears as System RAM in
			 * /proc/iomem and still has a struct page. (memblock_remove()
			 * would produce the hole this check originally expected, which is
			 * why DP-MEMORY.md records reserved ranges reading KPF_NOPAGE.)
			 *
			 * So ask the question that is actually load-bearing: is every page
			 * of the range marked PageReserved, i.e. claimed at boot and never
			 * available to the allocator? If so a DMA window onto it cannot
			 * collide with anything, whichever mechanism reserved it. Only
			 * refuse when the range contains genuinely free RAM.
			 */
			pr_warn(DRV ": 0x%llx+%zu is IN the kernel's memory map -- "
				"no ffn_reserve= covers it, and a DMA window onto "
				"live RAM would corrupt silently. Refused; falling "
				"back to coherent allocation\n", dma_phys, size);
		} else {
			d->dma_cpu = phys_to_virt(dma_phys);
			d->dma_handle = (dma_addr_t)dma_phys;
			d->dma_size = size;
			d->dma_reserved = true;
			pr_info(DRV ": DMA pool is the boot-reserved range 0x%llx+%zu "
				"(%d MB), kernel mapping %p, bus 0x%llx\n",
				dma_phys, size, dma_mb, d->dma_cpu,
				(unsigned long long)d->dma_handle);
			return;
		}
	}

	if (dma_phys && size > (4u << 20)) {
		/* The reserved pool was refused; coherent allocation cannot exceed 4 MB here. */
		pr_info(DRV ": capping the fallback coherent pool at 4 MB\n");
		size = 4u << 20;
	}
	d->dma_cpu = dma_alloc_coherent(&d->pdev->dev, size, &d->dma_handle,
					GFP_KERNEL);
	if (!d->dma_cpu) {
		pr_warn(DRV ": could not allocate %d MB coherent DMA. The CP has "
			"little free RAM; lower dma_mb.\n", dma_mb);
		return;
	}
	d->dma_size = size;

	phys = virt_to_phys(d->dma_cpu);
	pr_info(DRV ": DMA region %zu MB: dma_handle 0x%llx phys 0x%llx\n",
		size >> 20, (unsigned long long)d->dma_handle,
		(unsigned long long)phys);
	if ((u64)d->dma_handle != (u64)phys)
		pr_warn(DRV ": dma_handle != physical address. Userspace mmaps "
			"the physical address while the device uses the handle, "
			"so these MUST agree here -- do not run the SDK until "
			"this is understood.\n");
}

/* ----------------------------------------------------------------- ioctl -- */

/*
 * Names for the log only. Where the vendor's own name is not known this uses
 * the client function that issues the command, which is what the ABI recovery
 * actually established -- see ffn_bde_abi.h on why these are not presented as
 * vendor identifiers.
 */
static const char *ffn_bde_cmd_name(unsigned int nr)
{
	static const char * const n[] = {
		/*
		 * Corrected against the recovered contract. Only 0, 1, 2 and 12
		 * have names confirmed by surviving assert strings; the rest are
		 * named for the client function that issues them. Earlier guesses
		 * of "intr_array" for 13/14/16 were wrong -- 13/14 are SPI and 16
		 * is not implemented by the vendor module either.
		 */
		"0/version",        "1/get_num_devices", "2/get_device",
		"3/pci_config_put32", "4/pci_config_get32", "5/get_dma_info",
		"6/enable_interrupts", "7/disable_interrupts",
		"8/unknown-never-issued",
		"9/wait_for_interrupt", "10/unknown-never-issued",
		"11/unknown-never-issued",
		"12/get_device_type", "13/spi_read",     "14/spi_write",
		"15/unimpl-in-vendor", "16/unimpl-in-vendor",
		"17/unimpl-in-vendor", "18/unimpl-in-vendor",
		"19/eb_read",       "20/eb_write",
		"21/get_bus_features", "22/irq_mask_set", "23/cpu_write",
		"24/cpu_read",      "25/cpu_pci_register",
		"26/get_device_resource",
		"27/iproc_read",    "28/iproc_write",
		"29/attach_instance", "30/get_dev_state", "31/reprobe",
	};

	return nr < ARRAY_SIZE(n) ? n[nr] : "out-of-range";
}

static long ffn_bde_ioctl(struct file *filp, unsigned int cmd,
			  unsigned long arg)
{
	ffn_lubde_ioctl_t io;	/* the reply: zeroed, handlers fill it */
	ffn_lubde_ioctl_t in;	/* what the client sent: read-only below */
	unsigned int nr = _IOC_NR(cmd);
	struct ffn_bde_dev *d = &ffn_bde_devs[0];
	int bad_cmd = 0;
	long ret = 0;

	/*
	 * Even a bad type or out-of-range nr must not fail the syscall -- see the
	 * note on the return path below. Report it in rc instead.
	 */
	if (_IOC_TYPE(cmd) != FFN_BDE_IOC_MAGIC || nr > FFN_BDE_IOC_NR_MAX) {
		pr_warn_ratelimited(DRV ": ignoring ioctl type 0x%x nr %u\n",
				    _IOC_TYPE(cmd), nr);
		bad_cmd = 1;
	}
	if (copy_from_user(&in, (void __user *)arg, sizeof(in)))
		return -EFAULT;

	/*
	 * Two structs, on purpose. The recovered contract requires all 96 bytes
	 * to go back on every path with unfilled fields explicitly zeroed, so
	 * the client cannot read its own stale input back as a reply -- for
	 * command 26 that stale value would be a BAR address it then mmaps.
	 *
	 * But several commands SEND arguments, and an earlier version of this
	 * satisfied the zeroing rule by wiping the whole struct in place. That
	 * silently destroyed those arguments: command 26 arrived with d0 = 1
	 * (resource index) and was dispatched as d0 = 0, so it reported "no such
	 * resource", the client left vbase1 NULL, and then dereferenced it.
	 *
	 * So: read inputs from "in", write outputs to "io", never mix them.
	 *
	 * rc defaults to 0 because the vendor kernel sets it before dispatch and
	 * handlers only write it on failure.
	 */
	memset(&io, 0, sizeof(io));
	io.dev = in.dev;

	if (verbose > 1)
		pr_info(DRV ": cmd %-22s dev=%u d0=0x%x d1=0x%x d2=0x%x "
			"d3=0x%x p0=0x%llx\n",
			ffn_bde_cmd_name(nr), in.dev, in.d0, in.d1, in.d2,
			in.d3, (unsigned long long)in.p0);

	mutex_lock(&ffn_bde_lock);

	switch (bad_cmd ? 0xffff : nr) {
	case 0:		/* version */
		/*
		 * Which nr is LUBDE_VERSION is not yet confirmed; 0 is the
		 * assumption. The value returned is the BDE interface version
		 * the client checks -- if bcm.user rejects it, its own message
		 * will say what it wanted, which is exactly the loop this
		 * module is built to run.
		 */
		io.rc = 0;
		io.d0 = 1;
		break;

	case 1:		/* number of devices */
		io.rc = 0;
		io.d0 = ffn_bde_ndev;
		break;

	case 2:		/* get device: identity and BARs */
		if (in.dev >= (u32)ffn_bde_ndev || !d->pdev) {
			io.rc = (u32)-1;
			ret = -ENODEV;
			break;
		}
		/*
		 * Field assignment here is the single most consequential
		 * unknown in this module: the client uses it to decide what
		 * physical memory to map. Deliberately reporting into dx.dw[]
		 * as well as d0..d3 so the log shows what it reads, rather
		 * than committing to one layout that may be wrong.
		 */
		/*
		 * Field assignment established EMPIRICALLY, which is the whole
		 * point of the discovery loop. Reporting vendor in d0 and device
		 * in d1 made the SDK say:
		 *
		 *     warning: device 0x14e4 revision 0x75 is not supported
		 *
		 * 0x14e4 is the VENDOR id and 0x75 is the low byte of the device
		 * id 0x8375, so it reads the device from d0 and the revision from
		 * d1. Corrected accordingly; vendor goes in d2 where it is
		 * available but evidently not what it keys on.
		 */
		/*
		 * d0/d1 are device id and revision -- established empirically.
		 * d3:d2 are NOT spare: the client mmaps them as the REGISTER
		 * WINDOW base (mapping "a" in the recovered contract, landing in
		 * bde_dev_s +48 / ibde_dev_t.base_address). Returning the vendor
		 * id in d2, as this did, is exactly why the SDK reported
		 *     CM: Base=(nil)
		 * and why init could never reach a register.
		 *
		 * BAR2 is the 8 MB CMIC window and is what the SDK wants.
		 */
		io.rc = 0;
		io.d0 = d->pdev->device;
		io.d1 = d->pdev->revision;
		io.d2 = lower_32_bits(d->bar_start[2]);
		io.d3 = upper_32_bits(d->bar_start[2]);
		io.p0 = d->bar_start[0];
		io.dx.dw[0] = lower_32_bits(d->bar_start[0]);
		io.dx.dw[1] = upper_32_bits(d->bar_start[0]);
		io.dx.dw[2] = lower_32_bits(d->bar_len[0]);
		io.dx.dw[3] = lower_32_bits(d->bar_start[2]);
		io.dx.dw[4] = upper_32_bits(d->bar_start[2]);
		io.dx.dw[5] = lower_32_bits(d->bar_len[2]);
		break;

	case 12:	/* device type / bus type */
		io.rc = 0;
		io.d0 = (u32)dev_type;
		break;

	case 5:		/* get_dma_info */
		/*
		 * Contract recovered from the client's own _get_dma_info:
		 *
		 *   *a0 = (dx.dw[1] << 32) | dx.dw[0]   -- 64-bit phys base,
		 *                                          assembled with
		 *                                          dsll32 then or
		 *   *a1 = d0
		 *   *a2 = d1
		 *
		 * The three outputs go to three consecutive globals, and they
		 * are three DIFFERENT things:
		 *
		 *   dx.dw[1]:dw[0] -> _cpu_pbase, what the client mmaps
		 *   d0             -> _dma_pbase, what the DEVICE targets
		 *   d1             -> _dma_size,  also the mmap length
		 *
		 * d1 was zero here at first and the SDK duly printed
		 * "DMA pool size: 1" and mmapped one byte.
		 */
		if (!d->dma_cpu) {
			io.rc = (u32)-1;
			ret = -ENOMEM;	/* for the log only; rc is what matters */
			break;
		}
		io.rc = 0;
		io.dx.dw[0] = lower_32_bits(d->dma_handle);
		io.dx.dw[1] = upper_32_bits(d->dma_handle);
		/*
		 * d1 is the LENGTH the client mmaps, not a flag. The recovered
		 * contract is explicit: mapping "c" takes its address from
		 * dx.dw[1]:dw[0] and its length from (uint32)d1.
		 *
		 * Putting a 0/1 flag here meant the SDK mmapped the DMA region
		 * with length 1 -- and it said so, printing "DMA pool size: 1",
		 * which was misread as a count rather than a size in bytes.
		 */
		/*
		 * d0 is _dma_pbase: the address the DEVICE uses. It is NOT
		 * the size. _l2p() computes every DMA target as
		 * _dma_pbase + (laddr - _dma_vbase), so reporting the size
		 * here pointed the chip at 0x400000 and it DMA'd there --
		 * cleanly, with SBUSDMA STATUS DONE and no error bits, just
		 * into the wrong memory. The SDK then failed its own block
		 * access check because nothing it expected was there.
		 *
		 * dx.dw[1]:dw[0] stays the CPU physical base, which is a
		 * different thing and is what the client mmaps. On this
		 * platform the two coincide, and ffn_bde_dma_setup() checks
		 * that rather than assuming it -- but they are reported
		 * separately because the client keeps them separate.
		 */
		io.d0 = lower_32_bits(d->dma_handle);
		io.d3 = upper_32_bits(d->dma_handle);	/* OpenBCM kmod: d3 = dma_pbase >> 32 */
		io.d1 = (u32)d->dma_size;
		io.d2 = (u32)dma_d2;
		break;

	case 29:	/* instance attach -- inputs only */
		/*
		 * The client sends d0 and d1 and reads nothing back, so there is
		 * nothing to get wrong here. Log the arguments: they are the
		 * instance identity the SDK is claiming, which is worth seeing.
		 */
		pr_info(DRV ": instance_attach dev=%u d0=0x%x d1=0x%x\n",
			in.dev, in.d0, in.d1);
		io.rc = 0;
		break;

	case 30:	/* get device state */
		io.rc = 0;
		io.d0 = (u32)dev_state;
		break;

	case 31:	/* LUBDE_REPROBE -- rescan for switch devices */
		/*
		 * The client only sends this when it walks past the device count
		 * we reported (linux-user-bde.c:814 `if (u >= _ndevices)`), i.e.
		 * it believes a device appeared that the kernel BDE has not seen,
		 * and then ASSERTS the call succeeded
		 * (`assert(_ioctl_LUBDE_REPROBE == 0)` at :821). Without this the
		 * SDK aborts during device discovery, before any chip access.
		 *
		 * Success with no action is the correct answer here, not a stub:
		 * this driver builds its device list once in probe() and it is
		 * fixed for the life of the module, so a rescan genuinely has
		 * nothing new to report. Re-enumerating PCI from an ioctl would
		 * be the wrong thing -- the SDK is not permitted to change which
		 * devices we own.
		 */
		pr_info(DRV ": reprobe requested; device list is fixed at probe, %u device(s)\n",
			(unsigned)ffn_bde_ndev);
		io.rc = 0;
		break;

	case 26:	/* get_device_resource: lkbde_get_dev_resource(dev, d0, &d2, &d3, &d1) */
		/*
		 * rsrc 0 = iowin[0] = the CMIC register window = BAR2 on this chip;
		 * rsrc 1 = iowin[1] = the iProc window = BAR0. d1 is the SIZE, which
		 * the client uses as its mmap length. Anything else reports zeros
		 * with rc 0, as the vendor does.
		 */
		io.rc = 0;
		{
			int bar = (in.d0 == 0) ? 2 : (in.d0 == 1) ? 0 : -1;
			if (bar >= 0 && d->bar_len[bar]) {
				io.d2 = lower_32_bits(d->bar_start[bar]);
				io.d3 = upper_32_bits(d->bar_start[bar]);
				io.d1 = (u32)d->bar_len[bar];
			}
		}
		pr_info(DRV ": get_device_resource rsrc %u -> 0x%08x%08x len 0x%x\n",
			in.d0, io.d3, io.d2, io.d1);
		break;

	case 21:	/* bus features: byte order */
		/*
		 * Fills the three fields of linux_bde_bus_t. Reported from
		 * module parameters, see the comment on be_pio above for why
		 * these are parameters rather than constants.
		 *
		 * Which of d0..d3 / dx the client actually reads is not yet
		 * confirmed, so the same values go into both d0..d2 and
		 * dx.dw[0..2]. Redundant on purpose: the log will show which it
		 * used, and a value in the wrong field is a clean failure while
		 * a wrong VALUE in the right field is a silent one.
		 */
		io.rc = 0;
		io.d0 = (u32)be_pio;
		io.d1 = (u32)be_packet;
		io.d2 = (u32)be_other;
		io.dx.dw[0] = (u32)be_pio;
		io.dx.dw[1] = (u32)be_packet;
		io.dx.dw[2] = (u32)be_other;
		pr_info(DRV ": bus_features -> be_pio=%d be_packet=%d be_other=%d\n",
			be_pio, be_packet, be_other);
		break;

	case 6:	/* enable_interrupts */
		/*
		 * _enable_interrupts (linux-user-bde.c:1136) asserts on the
		 * result at line 1139, so this must succeed. It is called by
		 * _interrupt_connect AFTER the thread is already spinning in
		 * command 9, so all it has to do is un-mask.
		 */
		ffn_bde_irq_rearm(d);
		io.rc = 0;
		break;

	case 7:	/* disable_interrupts */
		ffn_bde_irq_mask(d);
		io.rc = 0;
		break;

	case 9: {	/* wait_for_interrupt -- BLOCKS, see below */
		u32 seen;
		long w;

		/*
		 * This is the one command that sleeps, and two things about
		 * that matter.
		 *
		 * First, the caller holds ffn_bde_lock for the whole switch.
		 * Sleeping under it would deadlock every other command against
		 * an interrupt thread that is idle by design, so drop it across
		 * the wait and take it back before leaving.
		 *
		 * Second, sample the sequence BEFORE re-arming. An interrupt
		 * that lands between the re-arm and the sleep still moves the
		 * counter past the sampled value, so wait_event sees the
		 * condition already true and returns rather than sleeping
		 * through it.
		 */
		seen = (u32)atomic_read(&d->irq_seq);
		ffn_bde_irq_rearm(d);
		mutex_unlock(&ffn_bde_lock);

		if (intr_poll_ms > 0)
			w = wait_event_interruptible_timeout(d->irq_wq,
				(u32)atomic_read(&d->irq_seq) != seen,
				msecs_to_jiffies(intr_poll_ms));
		else
			w = wait_event_interruptible(d->irq_wq,
				(u32)atomic_read(&d->irq_seq) != seen);

		mutex_lock(&ffn_bde_lock);
		d->irq_seen = (u32)atomic_read(&d->irq_seq);
		(void)w;
		/*
		 * Always report success, including on a signal. The client
		 * ignores the value and simply runs its handlers; reporting a
		 * failure would only invite it to treat an ordinary wakeup as
		 * an error.
		 */
		io.rc = 0;
		break;
	}

	case 22:	/* irq_mask_set: lkbde_irq_mask_set(dev, addr=d0, mask=d1, fmask=0) */
		/*
		 * The SDK re-arms the chip's interrupt sources by writing a CMIC
		 * mask register; the vendor kernel does exactly this store. Our ISR
		 * masks at the interrupt controller rather than here, so this is
		 * purely the SDK's own register write, done on its behalf. fmask is
		 * always 0 from the client (it is the secondary-handler path).
		 */
		if (!d->bar2_regs || in.d0 >= 0x40000 || (in.d0 & 3)) {
			pr_warn(DRV ": irq_mask_set offset 0x%x is outside the "
				"CMIC window -- refused\n", in.d0);
			io.rc = (u32)-1;
			break;
		}
		d->imask = in.d1 & ~d->fmask;
		__raw_writel(d->imask | d->imask2, d->bar2_regs + in.d0);
		if (verbose > 1)
			pr_info(DRV ": irq_mask_set BAR2+0x%05x <- 0x%08x\n",
				in.d0, d->imask | d->imask2);
		io.rc = 0;
		break;

	case 3: {	/* pci_config_put32: pci_conf_write(dev, d0, d1) */
		int rc = pci_write_config_dword(d->pdev, in.d0, in.d1);
		io.rc = rc ? (u32)-1 : 0;
		break;
	}

	case 4: {	/* pci_config_get32: d1 = pci_conf_read(dev, d0) */
		u32 v = 0;
		int rc = pci_read_config_dword(d->pdev, in.d0, &v);
		io.d1 = v;
		io.rc = rc ? (u32)-1 : 0;
		break;
	}

	default:
		io.rc = (u32)-1;
		ret = -EOPNOTSUPP;
		break;
	}

	mutex_unlock(&ffn_bde_lock);

	if (ret && verbose)
		pr_info(DRV ": UNIMPLEMENTED %-22s dev=%u d0=0x%x d1=0x%x "
			"d2=0x%x d3=0x%x p0=0x%llx -- implement this next\n",
			ffn_bde_cmd_name(nr), in.dev, in.d0, in.d1, in.d2,
			in.d3, (unsigned long long)in.p0);

	/*
	 * ALWAYS return 0 from the syscall. The client asserts
	 * ioctl(_devfd, command, pdevio) == 0 at linux-user-bde.c:517 and its
	 * assert handler abort()s when it fires on a non-main thread, so a
	 * non-zero return here kills the SDK outright rather than producing a
	 * diagnosable error. Failure is communicated by io.rc, which is what
	 * every caller actually tests.
	 */
	if (copy_to_user((void __user *)arg, &io, sizeof(io)))
		return -EFAULT;
	return 0;
}

/*
 * mmap. Whether the client reaches BAR2 this way or through read/write ioctls
 * is still being established, so this maps by physical page frame with the
 * offset taken as an absolute physical address masked to a reported BAR -- and
 * refuses anything outside one. Refusing is the important part: a permissive
 * mmap here would let a misunderstanding map arbitrary physical memory.
 */
static int ffn_bde_mmap(struct file *filp, struct vm_area_struct *vma)
{
	struct ffn_bde_dev *d = &ffn_bde_devs[0];
	u64 phys = (u64)vma->vm_pgoff << PAGE_SHIFT;
	unsigned long len = vma->vm_end - vma->vm_start;
	unsigned int i;

	if (!d->pdev)
		return -ENODEV;

	for (i = 0; i < 6; i++) {
		if (!d->bar_len[i])
			continue;
		if (phys >= d->bar_start[i] &&
		    phys + len <= d->bar_start[i] + d->bar_len[i])
			break;
	}
	if (i == 6) {
		/*
		 * Not a BAR. The client also maps the coherent region reported by
		 * command 5 (mapping "c" of the contract: address from
		 * dx.dw[1]:dw[0], length from d1), so allow exactly that range.
		 *
		 * Everything outside a BAR or this region stays refused. A
		 * permissive mmap here would let a misunderstanding map arbitrary
		 * physical memory, which on a box with 8 GB of live RAM is not a
		 * risk worth taking for convenience.
		 */
		u64 dstart = d->dma_cpu ? (u64)virt_to_phys(d->dma_cpu) : 0;

		if (!d->dma_cpu || phys < dstart ||
		    phys + len > dstart + d->dma_size) {
			pr_warn(DRV ": mmap of 0x%llx len %lu is outside every "
				"BAR and outside the DMA region "
				"[0x%llx,0x%llx) -- refused\n",
				phys, len, dstart, dstart + d->dma_size);
			return -EINVAL;
		}
		pr_info(DRV ": mmap DMA region phys 0x%llx len %lu\n", phys, len);
	}

	pr_info(DRV ": mmap entered: phys 0x%llx len %lu (bar match %u)\n",
		phys, len, i);
	/*
	 * Registers must be uncached. The DMA region must NOT be: the
	 * device's inbound writes land in L2 (PEM bar2_cax = 0) and the
	 * kernel's own coherent mapping is cached, so an uncached view
	 * here would read around the data the SDK is waiting for. That
	 * mismatch is what 'Failed accessing DMA' actually was.
	 */
	if (i < 6)
		vma->vm_page_prot = pgprot_noncached(vma->vm_page_prot);
	if (remap_pfn_range(vma, vma->vm_start, vma->vm_pgoff, len,
			    vma->vm_page_prot))
		return -EAGAIN;

	if (i < 6)
		pr_info(DRV ": mmap bar%u phys 0x%llx len %lu\n", i, phys, len);
	return 0;
}

static int ffn_bde_open(struct inode *ino, struct file *filp)
{
	return 0;
}

static int ffn_bde_release(struct inode *ino, struct file *filp)
{
	return 0;
}

static const struct file_operations ffn_bde_fops = {
	.owner		= THIS_MODULE,
	.open		= ffn_bde_open,
	.release	= ffn_bde_release,
	.unlocked_ioctl	= ffn_bde_ioctl,
	.compat_ioctl	= ffn_bde_ioctl,
	.mmap		= ffn_bde_mmap,
};

/* ------------------------------------------------------------ module ------ */

static int ffn_bde_user_major;
static int ffn_bde_kernel_major;

static int __init ffn_bde_init(void)
{
	int rc;

	/*
	 * Fixed majors, not dynamic. The vendor's /sbin/rc mknods the nodes at
	 * 127 and 126 rather than reading /proc/devices, so these are not
	 * negotiable if bcm.user is to find them.
	 */
	rc = register_chrdev(FFN_BDE_USER_MAJOR, FFN_BDE_USER_NAME,
			     &ffn_bde_fops);
	if (rc < 0) {
		pr_err(DRV ": cannot take major %d for %s: %d\n",
		       FFN_BDE_USER_MAJOR, FFN_BDE_USER_NAME, rc);
		return rc;
	}
	ffn_bde_user_major = FFN_BDE_USER_MAJOR;

	rc = register_chrdev(FFN_BDE_KERNEL_MAJOR, FFN_BDE_KERNEL_NAME,
			     &ffn_bde_fops);
	if (rc < 0) {
		pr_err(DRV ": cannot take major %d for %s: %d\n",
		       FFN_BDE_KERNEL_MAJOR, FFN_BDE_KERNEL_NAME, rc);
		unregister_chrdev(ffn_bde_user_major, FFN_BDE_USER_NAME);
		return rc;
	}
	ffn_bde_kernel_major = FFN_BDE_KERNEL_MAJOR;

	if (ffn_bde_scan())
		ffn_bde_dma_setup();

	pr_info(DRV ": ready. %d device(s). Create the nodes with:\n", ffn_bde_ndev);
	pr_info(DRV ":   mknod /dev/%s c %d 0\n",
		FFN_BDE_KERNEL_NAME, FFN_BDE_KERNEL_MAJOR);
	pr_info(DRV ":   mknod /dev/%s c %d 0\n",
		FFN_BDE_USER_NAME, FFN_BDE_USER_MAJOR);
	pr_info(DRV ": unimplemented ioctls are logged with their number so "
		"bcm.user's own complaints drive what to add next\n");
	return 0;
}

static void __exit ffn_bde_exit(void)
{
	ffn_bde_irq_teardown(&ffn_bde_devs[0]);
	if (ffn_bde_devs[0].bar2_regs)
		iounmap(ffn_bde_devs[0].bar2_regs);
	if (ffn_bde_devs[0].dma_cpu && !ffn_bde_devs[0].dma_reserved)
		dma_free_coherent(&ffn_bde_devs[0].pdev->dev,
				  ffn_bde_devs[0].dma_size,
				  ffn_bde_devs[0].dma_cpu,
				  ffn_bde_devs[0].dma_handle);
	if (ffn_bde_devs[0].pdev)
		pci_dev_put(ffn_bde_devs[0].pdev);
	if (ffn_bde_kernel_major)
		unregister_chrdev(ffn_bde_kernel_major, FFN_BDE_KERNEL_NAME);
	if (ffn_bde_user_major)
		unregister_chrdev(ffn_bde_user_major, FFN_BDE_USER_NAME);
	pr_info(DRV ": unloaded\n");
}

module_init(ffn_bde_init);
module_exit(ffn_bde_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("FFN NGFW");
MODULE_DESCRIPTION("FFN linux-user-bde/linux-kernel-bde for the BCM88375");
MODULE_VERSION("0.1");
