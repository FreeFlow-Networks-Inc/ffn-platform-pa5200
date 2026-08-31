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
#include <linux/slab.h>
#include <linux/mutex.h>
#include <linux/dma-mapping.h>

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
	"value returned as d1 from command 5 (default 1). Believed to select "
	"kernel-BDE mmap rather than /dev/mem.");

/*
 * Command 12 (get_device_type) returns the bus type. Returning 0 made the SDK
 * say "Error : Unknow bus type 0x0 !!", so it keys on this. BDE device types are
 * conventionally a bit set with PCI as bit 0; parameterised so the value can be
 * probed without a rebuild rather than asserted.
 */
static int dev_type = 2;
module_param(dev_type, int, 0644);
MODULE_PARM_DESC(dev_type,
	"bus type reported by command 12. 2 is CORRECT and verified: it "
	"makes the SDK attach the device and report it as SPI unit 0, "
	"Dev 0x8375, Rev 0x11, Chip BCM88375_B0. 1 and 4 both fail.");

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
};

static DEFINE_MUTEX(ffn_bde_lock);
static struct ffn_bde_dev ffn_bde_devs[1];
static int ffn_bde_ndev;

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

	pr_info(DRV ": %04x:%02x:%02x.%d %04x:%04x rev %02x\n",
		domain, bus, slot, func, pdev->vendor, pdev->device,
		pdev->revision);
	for (i = 0; i < 6; i++)
		if (d->bar_len[i])
			pr_info(DRV ":   bar%u %pa len %llu\n",
				i, &d->bar_start[i],
				(unsigned long long)d->bar_len[i]);
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
static void ffn_bde_dma_setup(void)
{
	struct ffn_bde_dev *d = &ffn_bde_devs[0];
	size_t size = (size_t)dma_mb << 20;
	phys_addr_t phys;

	if (dma_mb <= 0)
		return;

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
		"29/attach_instance", "30/get_dev_state",
	};

	return nr < ARRAY_SIZE(n) ? n[nr] : "out-of-range";
}

static long ffn_bde_ioctl(struct file *filp, unsigned int cmd,
			  unsigned long arg)
{
	ffn_lubde_ioctl_t io;
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
	if (copy_from_user(&io, (void __user *)arg, sizeof(io)))
		return -EFAULT;

	/*
	 * Keep dev, discard everything else. The recovered contract requires all
	 * 96 bytes to go back on every path with unfilled fields explicitly
	 * zeroed -- otherwise the client reads its own stale input back, and for
	 * command 26 that stale value is a BAR address it then mmaps.
	 *
	 * rc defaults to 0 because the vendor kernel sets it before dispatch and
	 * handlers only write it on failure.
	 */
	{
		u32 keep_dev = io.dev;

		memset(&io, 0, sizeof(io));
		io.dev = keep_dev;
	}

	if (verbose > 1)
		pr_info(DRV ": cmd %-22s dev=%u d0=0x%x d1=0x%x d2=0x%x "
			"d3=0x%x p0=0x%llx\n",
			ffn_bde_cmd_name(nr), io.dev, io.d0, io.d1, io.d2,
			io.d3, (unsigned long long)io.p0);

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
		if (io.dev >= (u32)ffn_bde_ndev || !d->pdev) {
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
		io.rc = 0;
		io.d0 = d->pdev->device;
		io.d1 = d->pdev->revision;
		io.d2 = d->pdev->vendor;
		io.d3 = 0;
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
		 * So dx.dw[0..1] carry the region's physical address split low
		 * then high, and d0/d1 are two further 32-bit values. d0 is
		 * reported as the size; d1 is left zero because its meaning is
		 * not established, and a wrong non-zero guess is worse than a
		 * zero the client can complain about.
		 */
		if (!d->dma_cpu) {
			io.rc = (u32)-1;
			ret = -ENOMEM;	/* for the log only; rc is what matters */
			break;
		}
		io.rc = 0;
		io.dx.dw[0] = lower_32_bits(d->dma_handle);
		io.dx.dw[1] = upper_32_bits(d->dma_handle);
		io.d0 = (u32)d->dma_size;
		io.d1 = (u32)dma_flag;
		break;

	case 29:	/* instance attach -- inputs only */
		/*
		 * The client sends d0 and d1 and reads nothing back, so there is
		 * nothing to get wrong here. Log the arguments: they are the
		 * instance identity the SDK is claiming, which is worth seeing.
		 */
		pr_info(DRV ": instance_attach dev=%u d0=0x%x d1=0x%x\n",
			io.dev, io.d0, io.d1);
		io.rc = 0;
		break;

	case 30:	/* get device state */
		io.rc = 0;
		io.d0 = (u32)dev_state;
		break;

	case 26:	/* get_device_resource */
		/*
		 * The vendor kernel routes this to lkbde_get_dev_resource. Its
		 * exact field layout is not established here, and the recovered
		 * contract warns specifically that a stale value in this reply
		 * becomes a BAR address the client mmaps. The entry path now
		 * zeroes everything, so failing cleanly is safe: report failure
		 * in rc rather than inventing a window.
		 */
		io.rc = (u32)-1;
		ret = -EOPNOTSUPP;
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

	default:
		io.rc = (u32)-1;
		ret = -EOPNOTSUPP;
		break;
	}

	mutex_unlock(&ffn_bde_lock);

	if (ret && verbose)
		pr_info(DRV ": UNIMPLEMENTED %-22s dev=%u d0=0x%x d1=0x%x "
			"d2=0x%x d3=0x%x p0=0x%llx -- implement this next\n",
			ffn_bde_cmd_name(nr), io.dev, io.d0, io.d1, io.d2,
			io.d3, (unsigned long long)io.p0);

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
		pr_warn(DRV ": mmap of 0x%llx len %lu is not inside any "
			"reported BAR -- refused\n", phys, len);
		return -EINVAL;
	}

	vma->vm_page_prot = pgprot_noncached(vma->vm_page_prot);
	if (remap_pfn_range(vma, vma->vm_start, vma->vm_pgoff, len,
			    vma->vm_page_prot))
		return -EAGAIN;

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
	if (ffn_bde_devs[0].dma_cpu)
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
