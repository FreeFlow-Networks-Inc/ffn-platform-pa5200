// SPDX-License-Identifier: GPL-2.0
/*
 * ffn_nac -- FFN own-code driver for the PA-5200 NAC accelerators.
 *
 * WHAT THESE DEVICES ARE
 * ----------------------
 * A family of hardware Aho-Corasick pattern matchers. Every vendor module in
 * it -- cheetah, ocelot, ce40, ce10, ca1, ca2 -- carries the same modinfo
 * description, "PAN Nir-Aho-Corsik (nac) Driver", and the vendor PDT proves
 * they are ONE design: ce40.py and ca1.py are 1791 lines each and differ by
 * nothing but the name. So one driver covers the family; the variants differ
 * in memory bring-up, not in register layout.
 *
 * WHY CE40 IS THE PRIMARY TARGET
 * ------------------------------
 * This was first written for CA1 (feed:a101), which does enumerate on our own
 * PA-5220 at 0003:06:00.0 on the CONTROL PLANE PCIe domain -- invisible from
 * the x86 MP, visible once the OCTEON CP boots. It binds, it answers, it was
 * mapped. But it is a dead end on this platform, for two measured reasons:
 *
 *   1. PAN-OS 11.2.4-h5 ships NO DRIVER for it. The installed DP module tree
 *      holds nac-ce10, nac-ce40, nac-cheetah and nac-ocelot; there is no
 *      ca1.ko or ca2.ko anywhere in the sysroot. The vendor OS that ran on
 *      this box for its whole service life could not talk to feed:a101.
 *   2. Its memory channels never come up (BAR0 +0x0b8 reads 0x00000002, bit 0
 *      clear) and ca1.ko has no recovery path -- just a KERN_ERR. ce40.ko
 *      logs the same condition at KERN_WARNING because it HAS one:
 *      check_rld_status(), RLD calibration, and link recovery.
 *
 * CE40 (feed:a00d) is the live, vendor-supported member: three DDR layouts,
 * a memory check, and RLD calibration. The CP board agent configures it on
 * every vendor boot ("Setting CE40_ILKPHY_CFG to 0x72", "Resetting CE40
 * (link 0)"), and we already load its bitstream ourselves.
 *
 * NOT YET VERIFIED: whether feed:a00d actually ENUMERATES on our board. The
 * CE40 is an FPGA and may only appear as a PCI endpoint once programmed; the
 * PCI inventory we have was taken on boots where it was not. This driver
 * binds it if it is there and stays silent if it is not. Do not record
 * "CE40 works" until a probe line from real silicon says it bound.
 *
 * WHY MEMORY DECODE MATTERS (the whole reason this driver exists)
 * --------------------------------------------------------------
 * With no driver bound, CA1 PCI COMMAND reads 0x0000 -- memory decode OFF.
 * Every BAR then reads back 0xffffffff, which looks exactly like dead or
 * unprogrammed silicon. It is not: the BARs are assigned and the link is
 * fine, the device simply is not decoding memory cycles. pci_enable_device()
 * sets the MEM bit and the window comes alive. For comparison the FE100 on
 * the same CP reads COMMAND=0x0142 and works.
 *
 * WHAT IS DELIBERATELY NOT HERE
 * -----------------------------
 * We do not know the register semantics. The vendor driver surface
 * (nac_aho_read/write, nac_dfa_read/write, nac_mem_take/release,
 * nac_ddr_layout_small/medium/large) tells us the SHAPE -- a chardev over a
 * large memory window, with separate Aho and DFA table regions and three DDR
 * layouts -- but not the offsets. So this driver does two honest things: it
 * turns the device on, and it provides a safe instrument for mapping the
 * register space empirically. No invented offsets, no DMA, no bus mastering.
 *
 * In particular there is NO RLD calibration here even though ce40.ko has one.
 * We have its existence, not its registers. A calibration sequence written
 * from a guessed offset would be worse than none: it would look like
 * bring-up and be noise.
 *
 * SAFETY
 * ------
 *  - Writes are refused unless loaded with allow_write=1.
 *  - Bus mastering is never enabled; the device cannot reach host memory.
 *  - All MMIO is 32-bit aligned; unaligned access on MIPS64 BE traps.
 */

#include <linux/module.h>
#include <linux/pci.h>
#include <linux/cdev.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/io.h>
#include <linux/slab.h>
#include <linux/mutex.h>
#include <linux/idr.h>
#include <linux/string.h>

#include "ffn_nac_abi.h"

/*
 * The family, with the capabilities each vendor module demonstrably has.
 * Sources: the modinfo alias for the id, and the module own strings and
 * symbols for the caps -- ffn_nac_abi.h records which string proved which
 * bit. CE40 first because it is the one we want to bind.
 */
struct ffn_nac_variant {
	u16			device;
	const char	*name;
	u32			caps;
};

static const struct ffn_nac_variant ffn_nac_variants[] = {
	{ FFN_NAC_DEVICE_CE40,    "ce40",    FFN_NAC_CAP_DDR_LAYOUTS |
					     FFN_NAC_CAP_MEM_CHECK   |
					     FFN_NAC_CAP_RLD_CAL },
	{ FFN_NAC_DEVICE_CE10,    "ce10",    FFN_NAC_CAP_MEM_CHECK },
	{ FFN_NAC_DEVICE_CA1,     "ca1",     FFN_NAC_CAP_DDR_LAYOUTS },
	{ FFN_NAC_DEVICE_CA2,     "ca2",     FFN_NAC_CAP_DDR_LAYOUTS },
	{ FFN_NAC_DEVICE_OCELOT,  "ocelot",  FFN_NAC_CAP_RLD_RETRY },
	{ FFN_NAC_DEVICE_CHEETAH, "cheetah", 0 },
};

static const struct ffn_nac_variant *ffn_nac_lookup(u16 device)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(ffn_nac_variants); i++)
		if (ffn_nac_variants[i].device == device)
			return &ffn_nac_variants[i];
	return NULL;
}

#define DRV_NAME	"ffn_nac"
#define FFN_NAC_MAX_DEV	4

/*
 * Per-call MMIO bounce size.  Keep this SMALL.
 *
 * Measured on hardware: against a region that does NOT answer, every access
 * pays a full PCIe completion timeout.  The copy loop is uninterruptible, so a
 * large burst becomes a multi-second kernel-side stall that cannot be killed
 * and holds the device mutex throughout, blocking every other reader behind
 * it.  1 KiB (256 words) bounds the worst case to a few seconds and gives
 * userspace a cancellation point between calls.  Callers that want more loop.
 */
#define FFN_NAC_BOUNCE	1024

static bool allow_write;
module_param(allow_write, bool, 0444);
MODULE_PARM_DESC(allow_write,
	"permit write() to the device window (default 0 = read-only)");

/*
 * Byte order is a property of the WINDOW, not of the device.
 *
 * The vendor driver settles this. In nac_probe it reads the layout selector
 * with a bare `lw` off the BAR0 base -- no byteswap -- while every table
 * accessor (nac_aho_read/write, nac_dfa_read/write) wraps its access in
 * `wsbh`+`ror 0x10`, the MIPS 32-bit byteswap idiom. So:
 *
 *   BAR0 registers   raw, no swap      = big-endian    -> ioread32be
 *   BAR1 AHO table   byteswapped       = little-endian -> ioread32
 *   BAR2 DFA table   byteswapped       = little-endian -> ioread32
 *   BAR3             not touched by those accessors; treated as BAR0 until
 *                    something shows otherwise
 *
 * Our own measurements agree independently for BAR0: the scratch register at
 * +0x0ec reads 0x00112233 big-endian (an incrementing byte pattern) versus
 * 0x33221100 little-endian, and the other live registers become small sensible
 * integers that way -- +0x000 = 63, +0x014 = 24, +0x0b8 = 2.
 *
 * A single device-wide flag was wrong and is gone. be_mask is a bitmask over
 * BAR index so a sibling NAC part can be driven without a rebuild.
 */
#define FFN_NAC_BE_DEFAULT	((1u << 0) | (1u << 3))	/* BAR0 and BAR3 */

static unsigned int be_mask = FFN_NAC_BE_DEFAULT;
module_param(be_mask, uint, 0444);
MODULE_PARM_DESC(be_mask,
	"bitmask of BARs accessed big-endian (default 0x9 = BAR0+BAR3; BAR1/BAR2 are little-endian table windows)");

static inline bool nac_bar_is_be(u32 bar)
{
	return !!(be_mask & (1u << bar));
}

static inline u32 nac_read32(void __iomem *p, u32 bar)
{
	return nac_bar_is_be(bar) ? ioread32be(p) : ioread32(p);
}

static inline void nac_write32(u32 v, void __iomem *p, u32 bar)
{
	if (nac_bar_is_be(bar))
		iowrite32be(v, p);
	else
		iowrite32(v, p);
}

struct ffn_nac_dev {
	/* Which family member this is; never NULL, because probe refuses an
	 * unlisted id rather than guessing its capabilities. */
	const struct ffn_nac_variant	*var;
	struct pci_dev		*pdev;
	void __iomem		*bar[FFN_NAC_NUM_BARS];
	resource_size_t		len[FFN_NAC_NUM_BARS];
	struct cdev		cdev;
	struct device		*dev;
	dev_t			devt;
	struct mutex		lock;		/* serialises MMIO bursts */
	int			minor;
	u32			regions_mask;	/* BARs successfully requested */
};

static struct class *ffn_nac_class;
static dev_t ffn_nac_devt_base;
static DEFINE_IDA(ffn_nac_ida);

/*
 * Translate a flat chardev offset to (BAR, offset-in-BAR), rejecting the holes
 * between a BAR's real length and the end of its 4 MiB slot.
 */
static int ffn_nac_resolve(struct ffn_nac_dev *d, loff_t off, size_t len,
			   void __iomem **base, u32 *in_bar, u32 *bar_out)
{
	u32 bar, o;

	if (off < 0 || off >= FFN_NAC_WINDOW_SIZE)
		return -ENXIO;

	bar = FFN_NAC_OFF_TO_BAR(off);
	o   = FFN_NAC_OFF_IN_BAR(off);

	if (bar >= FFN_NAC_NUM_BARS || !d->bar[bar])
		return -ENXIO;
	/* must not run past the BAR's real length, nor out of its slot */
	if (o + len > d->len[bar] || o + len > FFN_NAC_SLOT_SIZE)
		return -ENXIO;

	*base    = d->bar[bar];
	*in_bar  = o;
	*bar_out = bar;
	return 0;
}

static int ffn_nac_open(struct inode *ino, struct file *f)
{
	struct ffn_nac_dev *d =
		container_of(ino->i_cdev, struct ffn_nac_dev, cdev);

	f->private_data = d;
	return 0;
}

static loff_t ffn_nac_llseek(struct file *f, loff_t off, int whence)
{
	return fixed_size_llseek(f, off, whence, FFN_NAC_WINDOW_SIZE);
}

static ssize_t ffn_nac_read(struct file *f, char __user *ubuf, size_t count,
			    loff_t *ppos)
{
	struct ffn_nac_dev *d = f->private_data;
	void __iomem *base;
	u32 in_bar, bar;
	void *tmp;
	int ret;

	/* MMIO: keep everything 32-bit aligned. */
	if ((*ppos & 3) || (count & 3))
		return -EINVAL;
	if (!count)
		return 0;
	count = min_t(size_t, count, FFN_NAC_BOUNCE);

	ret = ffn_nac_resolve(d, *ppos, count, &base, &in_bar, &bar);
	if (ret)
		return ret;

	tmp = kmalloc(count, GFP_KERNEL);
	if (!tmp)
		return -ENOMEM;

	/*
	 * Interruptible: a reader queued behind a slow burst against a
	 * non-answering window must stay killable.
	 */
	if (mutex_lock_interruptible(&d->lock)) {
		kfree(tmp);
		return -ERESTARTSYS;
	}
	/*
	 * MUST be an explicit 32-bit loop, NOT memcpy_fromio().
	 *
	 * This device only honours 32-bit accesses.  memcpy_fromio() is free to
	 * copy byte-wise, and when it does every sub-word access returns
	 * 0xff -- so the whole window reads as all-ones and looks dead, while
	 * a plain ioread32() of the very same address returns real data
	 * (0x3f000000 at BAR0+0).  It is also 4x the transactions, each paying
	 * a completion timeout, which is what made the first scan take hours.
	 *
	 * Values land in the buffer in CPU order; userspace unpacks native.
	 */
	{
		u32 *dst = tmp;
		size_t i;

		for (i = 0; i < count / 4; i++)
			dst[i] = nac_read32(base + in_bar + i * 4, bar);
	}
	mutex_unlock(&d->lock);

	if (copy_to_user(ubuf, tmp, count)) {
		kfree(tmp);
		return -EFAULT;
	}
	kfree(tmp);
	*ppos += count;
	return count;
}

static ssize_t ffn_nac_write(struct file *f, const char __user *ubuf,
			     size_t count, loff_t *ppos)
{
	struct ffn_nac_dev *d = f->private_data;
	void __iomem *base;
	u32 in_bar, bar;
	void *tmp;
	int ret;

	/*
	 * Default-deny.  We do not yet know which registers in this window are
	 * destructive, and this sits on the control plane of a live box.
	 */
	if (!allow_write)
		return -EPERM;
	if ((*ppos & 3) || (count & 3))
		return -EINVAL;
	if (!count)
		return 0;
	count = min_t(size_t, count, FFN_NAC_BOUNCE);

	ret = ffn_nac_resolve(d, *ppos, count, &base, &in_bar, &bar);
	if (ret)
		return ret;

	tmp = memdup_user(ubuf, count);
	if (IS_ERR(tmp))
		return PTR_ERR(tmp);

	if (mutex_lock_interruptible(&d->lock)) {
		kfree(tmp);
		return -ERESTARTSYS;
	}
	/* 32-bit only, for the same reason as the read path. */
	{
		const u32 *src = tmp;
		size_t i;

		for (i = 0; i < count / 4; i++)
			nac_write32(src[i], base + in_bar + i * 4, bar);
	}
	mutex_unlock(&d->lock);

	kfree(tmp);
	*ppos += count;
	return count;
}

static long ffn_nac_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
	struct ffn_nac_dev *d = f->private_data;
	struct ffn_nac_info info;
	u16 pcicmd = 0;
	int i;

	if (cmd != FFN_NAC_IOC_INFO)
		return -ENOTTY;

	memset(&info, 0, sizeof(info));
	info.abi_version   = FFN_NAC_ABI_VERSION;
	info.vendor        = d->pdev->vendor;
	info.device        = d->pdev->device;
	info.revision      = d->pdev->revision;
	info.num_bars      = FFN_NAC_NUM_BARS;
	info.write_enabled = allow_write ? 1 : 0;
	info.caps          = d->var->caps;
	strscpy(info.variant, d->var->name, sizeof(info.variant));

	pci_read_config_word(d->pdev, PCI_COMMAND, &pcicmd);
	info.pci_command = pcicmd;

	for (i = 0; i < FFN_NAC_NUM_BARS; i++) {
		info.bar[i].phys     = pci_resource_start(d->pdev, i);
		info.bar[i].len      = d->bar[i] ? d->len[i] : 0;
		info.bar[i].slot_off = FFN_NAC_BAR_OFF(i);
		info.bar[i].flags    = (u32)pci_resource_flags(d->pdev, i);
	}

	if (copy_to_user((void __user *)arg, &info, sizeof(info)))
		return -EFAULT;
	return 0;
}

/*
 * mmap is DISABLED on purpose.
 *
 * It was implemented and it does not work on this platform. A userspace
 * mapping of BAR2 read 0xffffffff at the same physical address where the
 * driver's own ioread32be() reads 0x00000000 -- verified with a genuine 32-bit
 * ctypes load, so this is not the byte-access trap. Userspace MMIO mappings on
 * this big-endian MIPS64 CP evidently need handling that plain
 * io_remap_pfn_range() + pgprot_noncached() does not provide.
 *
 * A mapping that silently returns wrong data is worse than no mapping: it
 * invalidates any experiment run through it, which is exactly what happened
 * when it was used as a control for the BAR2 write test. Shipping it disabled
 * until it is understood.
 *
 * read()/write() are the supported interface; they use explicit 32-bit
 * accessors and are known good.
 */
static int ffn_nac_mmap(struct file *f, struct vm_area_struct *vma)
{
	return -EOPNOTSUPP;
}

static const struct file_operations ffn_nac_fops = {
	.owner		= THIS_MODULE,
	.open		= ffn_nac_open,
	.llseek		= ffn_nac_llseek,
	.read		= ffn_nac_read,
	.write		= ffn_nac_write,
	.mmap		= ffn_nac_mmap,
	.unlocked_ioctl	= ffn_nac_ioctl,
	.compat_ioctl	= compat_ptr_ioctl,
};

static void ffn_nac_teardown(struct ffn_nac_dev *d)
{
	int i;

	for (i = 0; i < FFN_NAC_NUM_BARS; i++) {
		if (d->bar[i]) {
			pci_iounmap(d->pdev, d->bar[i]);
			d->bar[i] = NULL;
		}
	}
	if (d->regions_mask) {
		pci_release_selected_regions(d->pdev, d->regions_mask);
		d->regions_mask = 0;
	}
}

static int ffn_nac_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	const struct ffn_nac_variant *var;
	struct ffn_nac_dev *d;
	u16 before = 0, after = 0;
	int i, ret, mapped = 0;

	/* The PCI id table and the variant table are two lists that must
	 * agree. Refuse rather than bind with caps = 0, which would claim a
	 * part has no RLD calibration when it has one. */
	var = ffn_nac_lookup(pdev->device);
	if (!var) {
		dev_err(&pdev->dev,
			"feed:%04x is in the PCI id table but not the variant table\n",
			pdev->device);
		return -ENODEV;
	}

	d = devm_kzalloc(&pdev->dev, sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->pdev = pdev;
	mutex_init(&d->lock);

	pci_read_config_word(pdev, PCI_COMMAND, &before);

	/* THE unlock: this is what sets the memory-decode bit. */
	ret = pci_enable_device(pdev);
	if (ret) {
		dev_err(&pdev->dev, "pci_enable_device failed: %d\n", ret);
		return ret;
	}
	pci_read_config_word(pdev, PCI_COMMAND, &after);
	dev_info(&pdev->dev, "PCI COMMAND 0x%04x -> 0x%04x (MEM decode %s)\n",
		 before, after,
		 (after & PCI_COMMAND_MEMORY) ? "ON" : "still OFF");

	/* Deliberately NOT pci_set_master(): no DMA until we understand it. */

	for (i = 0; i < FFN_NAC_NUM_BARS; i++) {
		resource_size_t len = pci_resource_len(pdev, i);

		if (!len || !(pci_resource_flags(pdev, i) & IORESOURCE_MEM))
			continue;
		if (pci_request_region(pdev, i, DRV_NAME)) {
			dev_warn(&pdev->dev, "BAR%d busy, skipping\n", i);
			continue;
		}
		d->regions_mask |= 1 << i;
		d->bar[i] = pci_iomap(pdev, i, 0);
		if (!d->bar[i]) {
			dev_warn(&pdev->dev, "BAR%d ioremap failed\n", i);
			continue;
		}
		d->len[i] = len;
		mapped++;
		dev_info(&pdev->dev,
			 "BAR%d %pR -> chardev offset 0x%llx\n",
			 i, &pdev->resource[i],
			 (unsigned long long)FFN_NAC_BAR_OFF(i));
	}
	if (!mapped) {
		dev_err(&pdev->dev, "no BARs mapped\n");
		ret = -ENODEV;
		goto err_disable;
	}

	/*
	 * Liveness probe.  Memory decode being on does not mean the device is
	 * answering: if the logic behind the window is not brought up, every
	 * access master-aborts and reads back all-ones -- at roughly 21 ms per
	 * word, which makes a naive scan of a 4 MiB BAR take hours.  Say so
	 * once, here, rather than letting someone discover it the slow way.
	 */
	for (i = 0; i < FFN_NAC_NUM_BARS; i++) {
		u32 probe;

		if (!d->bar[i])
			continue;
		probe = nac_read32(d->bar[i], i);
		if (probe == 0xffffffff)
			dev_warn(&pdev->dev,
				 "BAR%d reads all-ones: window is enabled but the device is not answering (needs bring-up beyond PCI enable?). Reads will be very slow.\n",
				 i);
		else
			dev_info(&pdev->dev, "BAR%d first word 0x%08x\n",
				 i, probe);
	}

	d->minor = ida_alloc_max(&ffn_nac_ida, FFN_NAC_MAX_DEV - 1, GFP_KERNEL);
	if (d->minor < 0) {
		ret = d->minor;
		goto err_unmap;
	}
	d->devt = MKDEV(MAJOR(ffn_nac_devt_base), d->minor);

	cdev_init(&d->cdev, &ffn_nac_fops);
	d->cdev.owner = THIS_MODULE;
	ret = cdev_add(&d->cdev, d->devt, 1);
	if (ret)
		goto err_ida;

	d->dev = device_create(ffn_nac_class, &pdev->dev, d->devt, NULL,
			       "ffn_nac%d", d->minor);
	if (IS_ERR(d->dev)) {
		ret = PTR_ERR(d->dev);
		goto err_cdev;
	}

	d->var = var;
	pci_set_drvdata(pdev, d);
	dev_info(&pdev->dev,
		 "NAC variant %s (feed:%04x) ready as /dev/ffn_nac%d (%s)%s%s%s\n",
		 var->name, pdev->device, d->minor,
		 allow_write ? "read-write" : "READ-ONLY",
		 (var->caps & FFN_NAC_CAP_DDR_LAYOUTS) ? " ddr-layouts" : "",
		 (var->caps & FFN_NAC_CAP_MEM_CHECK)   ? " mem-check"   : "",
		 (var->caps & (FFN_NAC_CAP_RLD_CAL |
			       FFN_NAC_CAP_RLD_RETRY))  ? " rld"         : "");
	return 0;

err_cdev:
	cdev_del(&d->cdev);
err_ida:
	ida_free(&ffn_nac_ida, d->minor);
err_unmap:
	ffn_nac_teardown(d);
err_disable:
	pci_disable_device(pdev);
	return ret;
}

static void ffn_nac_remove(struct pci_dev *pdev)
{
	struct ffn_nac_dev *d = pci_get_drvdata(pdev);

	if (!d)
		return;
	device_destroy(ffn_nac_class, d->devt);
	cdev_del(&d->cdev);
	ida_free(&ffn_nac_ida, d->minor);
	ffn_nac_teardown(d);
	pci_disable_device(pdev);
}

static const struct pci_device_id ffn_nac_ids[] = {
	{ PCI_DEVICE(FFN_NAC_VENDOR_ID, FFN_NAC_DEVICE_CE40)    },
	{ PCI_DEVICE(FFN_NAC_VENDOR_ID, FFN_NAC_DEVICE_CE10)    },
	{ PCI_DEVICE(FFN_NAC_VENDOR_ID, FFN_NAC_DEVICE_CA1)     },
	{ PCI_DEVICE(FFN_NAC_VENDOR_ID, FFN_NAC_DEVICE_CA2)     },
	{ PCI_DEVICE(FFN_NAC_VENDOR_ID, FFN_NAC_DEVICE_OCELOT)  },
	{ PCI_DEVICE(FFN_NAC_VENDOR_ID, FFN_NAC_DEVICE_CHEETAH) },
	{ 0, }
};
MODULE_DEVICE_TABLE(pci, ffn_nac_ids);

static struct pci_driver ffn_nac_driver = {
	.name     = DRV_NAME,
	.id_table = ffn_nac_ids,
	.probe    = ffn_nac_probe,
	.remove   = ffn_nac_remove,
};

static int __init ffn_nac_init(void)
{
	int ret;

	ret = alloc_chrdev_region(&ffn_nac_devt_base, 0, FFN_NAC_MAX_DEV,
				  DRV_NAME);
	if (ret)
		return ret;

	ffn_nac_class = class_create(DRV_NAME);
	if (IS_ERR(ffn_nac_class)) {
		ret = PTR_ERR(ffn_nac_class);
		goto err_chrdev;
	}

	ret = pci_register_driver(&ffn_nac_driver);
	if (ret)
		goto err_class;
	return 0;

err_class:
	class_destroy(ffn_nac_class);
err_chrdev:
	unregister_chrdev_region(ffn_nac_devt_base, FFN_NAC_MAX_DEV);
	return ret;
}

static void __exit ffn_nac_exit(void)
{
	pci_unregister_driver(&ffn_nac_driver);
	class_destroy(ffn_nac_class);
	unregister_chrdev_region(ffn_nac_devt_base, FFN_NAC_MAX_DEV);
	ida_destroy(&ffn_nac_ida);
}

module_init(ffn_nac_init);
module_exit(ffn_nac_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("FreeFlow Networks");
MODULE_DESCRIPTION("FFN driver for the PA-5200 NAC (Nir-Aho-Corasick) accelerators");
