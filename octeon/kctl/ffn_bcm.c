// SPDX-License-Identifier: GPL-2.0
/*
 * ffn_bcm -- FFN OCTEON III control driver for the BCM88375 (Qumran-AX) CMIC.
 *
 * Runs on the PA-5220's dataplane complex: an OCTEON III CN73XX with the
 * Broadcom switch on its own PCIe segment (0001:01:00.0/.1). This driver owns
 * the CPU Management Interface (CMIC) -- the documented CPU-side door into the
 * switch: S-channel for register and table access, and the LED processor.
 *
 * It replaces the /dev/mem path ffn_cpdpd used during bring-up. That path
 * proved the hardware answers; it could not make access exclusive, and
 * S-channel is a stateful shared resource where two interleaved messages
 * silently return each other's replies. See ffn_bcm_abi.h for the full
 * rationale.
 *
 * NOT in scope here, deliberately: DNX device init, PMD firmware load, and
 * anything that presents the switch's front ports as netdevs. Those need
 * ordered exclusive register access, which is what this driver provides; they
 * are the layer above it, not part of it.
 *
 * Everything here is FFN's own code. No Broadcom SDK, BDE or header is used or
 * required: the register offsets and bit positions were recovered from the
 * BCM88375_A0 field database carried in the vendor's own shipped symbol data,
 * and are stated as plain constants below.
 */

#include <linux/delay.h>
#include <linux/debugfs.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pci.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "ffn_bcm_abi.h"

#define PCI_DEVICE_ID_BCM88375	0x8375

/*
 * CMIC register offsets, relative to BAR2.
 *
 * PROVENANCE: recovered from the BCM88375_A0 field database inside the
 * vendor's bcm.user.dbg -- CMIC_COMMON_SCHAN_CTRL {MSG_START:0, MSG_DONE:1,
 * ABORT:2, SER_CHECK_FAIL:20, NACK:21, TIMEOUT:22, SCHAN_ERROR:23} and
 * CMIC_LEDUP0_CTRL {LEDUP_EN:0}. Read out of shipped symbol data, not guessed,
 * and not lifted from a Broadcom header.
 */
#define CMIC_SCHAN_CTRL		0x10000
#define CMIC_SCHAN_ACK_BEAT	0x10004
#define CMIC_SCHAN_ERR		0x10008
#define CMIC_SCHAN_MSG0		0x1000c

#define CMIC_SCHAN_MSG_START	BIT(0)
#define CMIC_SCHAN_MSG_DONE	BIT(1)
#define CMIC_SCHAN_ABORT	BIT(2)
#define CMIC_SCHAN_SER_FAIL	BIT(20)
#define CMIC_SCHAN_NACK		BIT(21)
#define CMIC_SCHAN_TIMEOUT	BIT(22)
#define CMIC_SCHAN_ERROR	BIT(23)
#define CMIC_SCHAN_ERRMASK	(CMIC_SCHAN_SER_FAIL | CMIC_SCHAN_NACK | \
				 CMIC_SCHAN_TIMEOUT | CMIC_SCHAN_ERROR)

#define CMIC_LEDUP0_CTRL	0x20000
#define CMIC_LEDUP0_CLK_PARAMS	0x20050
#define CMIC_LEDUP0_CLK_DIV	0x2005c
#define CMIC_LEDUP0_DATA_RAM	0x20400		/* 256 B, 4-byte stride */
#define CMIC_LEDUP0_PROG_RAM	0x20800		/* 256 B, 4-byte stride */
#define CMIC_LEDUP_STRIDE	0x1000		/* LEDUP1 = +0x1000, ... */
#define CMIC_LEDUP_EN		BIT(0)
#define CMIC_LEDUP_UNITS	4

static unsigned int schan_spins = 20000;
module_param(schan_spins, uint, 0644);
MODULE_PARM_DESC(schan_spins,
		 "S-channel completion poll iterations (default 20000)");

static unsigned int schan_poll_us = 5;
module_param(schan_poll_us, uint, 0644);
MODULE_PARM_DESC(schan_poll_us,
		 "microseconds between S-channel completion polls (default 5)");

struct ffn_bcm {
	struct pci_dev *pdev;
	void __iomem *cmic;		/* BAR2, the CMIC window */
	void __iomem *ident;		/* BAR0, small ident/config window */
	u32 cmic_len;
	u32 ident_len;
	u32 ident_reg;			/* BAR0 reg 0, byte-swapped */
	u64 schan_ops;
	u64 schan_errs;
	u64 schan_timeouts;
};

/*
 * ONE device, ONE lock, and the lock covers both the published pointer and
 * every register access made through it.
 *
 * This shape is deliberate. The earlier ffn_pcic driver kept a module-global
 * owner pointer that its probe error path left published after freeing the
 * object, and it called misc_register() once per PCI function. The result was
 * an oops and two unkillable processes. Here: the pointer is published last
 * and retracted under the same mutex every ioctl holds, so an ioctl can never
 * observe a half-built or torn-down device; and the misc device is registered
 * once, in module_init, where there is exactly one of it.
 *
 * Holding one mutex for the whole ioctl also gives S-channel the mutual
 * exclusion that was the main reason to write this driver. Ops complete in
 * microseconds, so there is no reason to want finer granularity.
 */
static DEFINE_MUTEX(ffn_bcm_lock);
static struct ffn_bcm *ffn_bcm_cur;
static struct dentry *ffn_bcm_dbg;

/*
 * The CMIC's registers are little-endian; the OCTEON runs big-endian. A plain
 * 32-bit load therefore returns the four bytes reversed. __raw_readl() is that
 * plain load and le32_to_cpu() puts it right.
 *
 * Done explicitly rather than through readl() on purpose: readl()'s swapping
 * on MIPS depends on CONFIG_SWAP_IO_SPACE, so it is right or wrong depending
 * on a platform config symbol that has nothing to do with this device. Being
 * explicit means the same source is correct either way, and probe() verifies
 * the convention against the device id before anything relies on it.
 */
static inline u32 bcm_rd(void __iomem *base, u32 off)
{
	return le32_to_cpu((__force __le32)__raw_readl(base + off));
}

static inline void bcm_wr(void __iomem *base, u32 off, u32 val)
{
	__raw_writel((__force u32)cpu_to_le32(val), base + off);
}

/* Resolve an ABI bar selector to a mapping plus its length. */
static int bcm_bar(struct ffn_bcm *b, u32 bar, void __iomem **map, u32 *len)
{
	switch (bar) {
	case FFN_BCM_BAR_CMIC:
		*map = b->cmic;
		*len = b->cmic_len;
		return 0;
	case FFN_BCM_BAR_IDENT:
		*map = b->ident;
		*len = b->ident_len;
		return 0;
	default:
		return -EINVAL;
	}
}

/* Offsets come from userspace: bounds-check every one against the real BAR
 * length rather than trusting the caller or a compiled-in size. */
static int bcm_range_ok(u32 off, u32 count, u32 len)
{
	if (off & 3)
		return -EINVAL;
	if (!count || count > FFN_BCM_SCHAN_NMSG)
		return -EINVAL;
	if (off > len || count * 4 > len - off)
		return -ERANGE;
	return 0;
}

/*
 * One complete S-channel transaction. Caller holds ffn_bcm_lock.
 *
 * The sequence is the one proven against this chip during bring-up: clear,
 * load MSG words, MSG_START, poll for MSG_DONE or an error bit, read the
 * reply, release. s->ctrl and s->spins are filled in on every path, including
 * the failures, because which error bit came back is the whole diagnostic.
 */
static int schan_xfer(struct ffn_bcm *b, struct ffn_bcm_schan *s)
{
	unsigned int i, spins;
	u32 ctrl = 0, pre, newerr = 0;

	if (!s->nsend || s->nsend > FFN_BCM_SCHAN_NMSG ||
	    s->nrecv > FFN_BCM_SCHAN_NMSG)
		return -EINVAL;

	/*
	 * Snapshot the error bits that are ALREADY latched, because they do not
	 * go away. Measured on a BCM88375_A0: after a message that fails,
	 * SCHAN_ERROR stays set; writing 0 to SCHAN_CTRL clears MSG_DONE but
	 * not SCHAN_ERROR, writing the bit back does not clear it, asserting
	 * ABORT does not clear it, and SCHAN_ERR (0x10008) reads 0 the whole
	 * time. There is no clear to issue, so the only correct thing is to
	 * ignore what was already standing and judge this message on the bits
	 * it sets itself.
	 *
	 * Getting this wrong is not subtle: a poll loop that breaks on "any
	 * error bit" exits on its FIRST read with a stale bit set and MSG_DONE
	 * clear, which then looks exactly like a timeout. Every message after
	 * the first failure fails, at spins=0, with the wrong errno.
	 */
	pre = bcm_rd(b->cmic, CMIC_SCHAN_CTRL) & CMIC_SCHAN_ERRMASK;

	/* Clears MSG_DONE and MSG_START from any previous message. (Not the
	 * error bits -- see above.) */
	bcm_wr(b->cmic, CMIC_SCHAN_CTRL, 0);
	wmb();

	for (i = 0; i < s->nsend; i++)
		bcm_wr(b->cmic, CMIC_SCHAN_MSG0 + i * 4, s->msg[i]);
	wmb();				/* all words land before MSG_START */

	bcm_wr(b->cmic, CMIC_SCHAN_CTRL, CMIC_SCHAN_MSG_START);
	wmb();

	for (spins = 0; spins < schan_spins; spins++) {
		ctrl = bcm_rd(b->cmic, CMIC_SCHAN_CTRL);
		newerr = (ctrl & CMIC_SCHAN_ERRMASK) & ~pre;
		if ((ctrl & CMIC_SCHAN_MSG_DONE) || newerr)
			break;
		udelay(schan_poll_us);
	}
	s->ctrl = ctrl;
	s->spins = spins;
	s->pre_err = pre;

	/* A new error bit is this message failing, and must be reported as
	 * such -- checked before the completion test so it is never mislabelled
	 * a timeout. */
	if (newerr) {
		for (i = 0; i < s->nrecv; i++)
			s->msg[i] = bcm_rd(b->cmic, CMIC_SCHAN_MSG0 + i * 4);
		bcm_wr(b->cmic, CMIC_SCHAN_CTRL, 0);
		wmb();
		b->schan_ops++;
		b->schan_errs++;
		dev_dbg(&b->pdev->dev,
			"S-channel error, CTRL=0x%08x (new bits 0x%08x)\n",
			ctrl, newerr);
		return -EIO;
	}

	if (!(ctrl & CMIC_SCHAN_MSG_DONE)) {
		/* Genuinely never completed. Abort, so the block is not left
		 * mid-message for whoever runs next, then clear. */
		bcm_wr(b->cmic, CMIC_SCHAN_CTRL, CMIC_SCHAN_ABORT);
		wmb();
		bcm_wr(b->cmic, CMIC_SCHAN_CTRL, 0);
		wmb();
		b->schan_timeouts++;
		dev_warn(&b->pdev->dev,
			 "S-channel: no completion after %u polls, CTRL=0x%08x\n",
			 spins, ctrl);
		return -ETIMEDOUT;
	}

	for (i = 0; i < s->nrecv; i++)
		s->msg[i] = bcm_rd(b->cmic, CMIC_SCHAN_MSG0 + i * 4);

	bcm_wr(b->cmic, CMIC_SCHAN_CTRL, 0);	/* release for the next op */
	wmb();

	b->schan_ops++;
	return 0;
}

/* LEDUP<unit> register base, bounds-checked. */
static int ledup_base(struct ffn_bcm *b, u32 unit, u32 reg, u32 *out)
{
	u32 off;

	if (unit >= CMIC_LEDUP_UNITS)
		return -EINVAL;
	off = reg + unit * CMIC_LEDUP_STRIDE;
	if (off + 4 > b->cmic_len)
		return -ERANGE;
	*out = off;
	return 0;
}

static int led_load(struct ffn_bcm *b, struct ffn_bcm_led *l)
{
	u32 base, i;
	int rc;

	if (!l->len || l->len > FFN_BCM_LED_RAMSZ)
		return -EINVAL;
	rc = ledup_base(b, l->unit, CMIC_LEDUP0_PROG_RAM, &base);
	if (rc)
		return rc;
	if (base + l->len * 4 > b->cmic_len)
		return -ERANGE;

	/* PROGRAM_RAM is one program byte per 32-bit register, 4-byte stride. */
	for (i = 0; i < l->len; i++)
		bcm_wr(b->cmic, base + i * 4, l->prog[i]);
	wmb();

	rc = ledup_base(b, l->unit, CMIC_LEDUP0_CTRL, &base);
	if (rc)
		return rc;
	l->ctrl = bcm_rd(b->cmic, base);
	return 0;
}

static int led_enable(struct ffn_bcm *b, struct ffn_bcm_led *l)
{
	u32 base, ctrl;
	int rc;

	rc = ledup_base(b, l->unit, CMIC_LEDUP0_CTRL, &base);
	if (rc)
		return rc;

	ctrl = bcm_rd(b->cmic, base);
	if (l->enable)
		ctrl |= CMIC_LEDUP_EN;
	else
		ctrl &= ~CMIC_LEDUP_EN;
	bcm_wr(b->cmic, base, ctrl);
	wmb();

	/* Read back: the enable bit can be gated elsewhere, and a write that
	 * does not stick is the interesting case, not an error to hide. */
	l->ctrl = bcm_rd(b->cmic, base);
	if (l->enable && !(l->ctrl & CMIC_LEDUP_EN))
		dev_warn(&b->pdev->dev,
			 "LEDUP%u enable did not stick (CTRL=0x%08x)\n",
			 l->unit, l->ctrl);
	return 0;
}

static long ffn_bcm_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
	void __user *up = (void __user *)arg;
	struct ffn_bcm *b;
	long rc;

	mutex_lock(&ffn_bcm_lock);
	b = ffn_bcm_cur;
	if (!b) {
		mutex_unlock(&ffn_bcm_lock);
		return -ENODEV;
	}

	switch (cmd) {
	case FFN_BCM_IOC_RD: {
		struct ffn_bcm_reg r;
		void __iomem *map;
		u32 len, i, n;

		if (copy_from_user(&r, up, sizeof(r))) {
			rc = -EFAULT;
			break;
		}
		rc = bcm_bar(b, r.bar, &map, &len);
		if (rc)
			break;
		n = r.count ? r.count : 1;
		rc = bcm_range_ok(r.off, n, len);
		if (rc)
			break;
		for (i = 0; i < n; i++)
			r.vals[i] = bcm_rd(map, r.off + i * 4);
		r.val = r.vals[0];
		r.count = n;
		rc = copy_to_user(up, &r, sizeof(r)) ? -EFAULT : 0;
		break;
	}
	case FFN_BCM_IOC_WR: {
		struct ffn_bcm_reg r;
		void __iomem *map;
		u32 len;

		if (copy_from_user(&r, up, sizeof(r))) {
			rc = -EFAULT;
			break;
		}
		rc = bcm_bar(b, r.bar, &map, &len);
		if (rc)
			break;
		rc = bcm_range_ok(r.off, 1, len);
		if (rc)
			break;
		bcm_wr(map, r.off, r.val);
		wmb();
		rc = 0;
		break;
	}
	case FFN_BCM_IOC_SCHAN: {
		struct ffn_bcm_schan s;
		long xrc;

		if (copy_from_user(&s, up, sizeof(s))) {
			rc = -EFAULT;
			break;
		}
		xrc = schan_xfer(b, &s);
		/* Copy back even on failure: ctrl and spins are the diagnostic
		 * the caller needs to tell a NACK from a timeout. */
		if (copy_to_user(up, &s, sizeof(s)))
			rc = -EFAULT;
		else
			rc = xrc;
		break;
	}
	case FFN_BCM_IOC_LED_LOAD:
	case FFN_BCM_IOC_LED_EN: {
		struct ffn_bcm_led l;
		long xrc;

		if (copy_from_user(&l, up, sizeof(l))) {
			rc = -EFAULT;
			break;
		}
		xrc = (cmd == FFN_BCM_IOC_LED_LOAD) ? led_load(b, &l)
						    : led_enable(b, &l);
		if (copy_to_user(up, &l, sizeof(l)))
			rc = -EFAULT;
		else
			rc = xrc;
		break;
	}
	case FFN_BCM_IOC_INFO: {
		struct ffn_bcm_info info;

		memset(&info, 0, sizeof(info));
		info.vendor = b->pdev->vendor;
		info.device = b->pdev->device;
		info.revision = b->pdev->revision;
		info.ident = b->ident_reg;
		info.bar0_phys = pci_resource_start(b->pdev, FFN_BCM_BAR_IDENT);
		info.bar2_phys = pci_resource_start(b->pdev, FFN_BCM_BAR_CMIC);
		info.bar0_len = b->ident_len;
		info.bar2_len = b->cmic_len;
		info.schan_ops = b->schan_ops;
		info.schan_errs = b->schan_errs;
		info.schan_timeouts = b->schan_timeouts;
		strlcpy(info.pci, pci_name(b->pdev), sizeof(info.pci));
		rc = copy_to_user(up, &info, sizeof(info)) ? -EFAULT : 0;
		break;
	}
	default:
		rc = -ENOTTY;
		break;
	}

	mutex_unlock(&ffn_bcm_lock);
	return rc;
}

static const struct file_operations ffn_bcm_fops = {
	.owner		= THIS_MODULE,
	.unlocked_ioctl	= ffn_bcm_ioctl,
	/* Every struct in the ABI is fixed-width with explicit padding, so a
	 * 32-bit caller sees the same layout and needs no translation. */
	.compat_ioctl	= ffn_bcm_ioctl,
	.llseek		= no_llseek,
};

static struct miscdevice ffn_bcm_misc = {
	.minor	= MISC_DYNAMIC_MINOR,
	.name	= FFN_BCM_DEVNAME,
	.fops	= &ffn_bcm_fops,
	.mode	= 0600,
};

static int ffn_bcm_dbg_show(struct seq_file *s, void *unused)
{
	struct ffn_bcm *b;

	mutex_lock(&ffn_bcm_lock);
	b = ffn_bcm_cur;
	if (!b) {
		seq_puts(s, "state        no device bound\n");
		mutex_unlock(&ffn_bcm_lock);
		return 0;
	}
	seq_printf(s, "pci          %s\n", pci_name(b->pdev));
	seq_printf(s, "device       %04x:%04x rev %02x\n",
		   b->pdev->vendor, b->pdev->device, b->pdev->revision);
	seq_printf(s, "ident        0x%08x (low half should be 0x%04x)\n",
		   b->ident_reg, b->pdev->device);
	seq_printf(s, "bar0         0x%016llx  %u bytes\n",
		   (unsigned long long)pci_resource_start(b->pdev,
							 FFN_BCM_BAR_IDENT),
		   b->ident_len);
	seq_printf(s, "bar2 cmic    0x%016llx  %u bytes\n",
		   (unsigned long long)pci_resource_start(b->pdev,
							 FFN_BCM_BAR_CMIC),
		   b->cmic_len);
	seq_printf(s, "schan        %llu ops, %llu errs, %llu timeouts\n",
		   b->schan_ops, b->schan_errs, b->schan_timeouts);
	/* Live reads, so this file is a probe and not just a counter dump. */
	{
		u32 ctrl = bcm_rd(b->cmic, CMIC_SCHAN_CTRL);

		seq_printf(s, "schan_ctrl   0x%08x\n", ctrl);
		/* These bits latch and have no clear, so a standing error here
		 * is a record of some earlier message, not a live fault. Say so
		 * rather than leaving a reader to assume S-channel is broken. */
		/* BIT() is unsigned long on 64-bit, so the mask promotes the
		 * expression -- cast back to u32 to match %08x. */
		if (ctrl & CMIC_SCHAN_ERRMASK)
			seq_printf(s,
				   "             latched error bits 0x%08x from an earlier message (no clear exists)\n",
				   (u32)(ctrl & CMIC_SCHAN_ERRMASK));
	}
	seq_printf(s, "ledup0_ctrl  0x%08x  LEDUP_EN=%s\n",
		   bcm_rd(b->cmic, CMIC_LEDUP0_CTRL),
		   (bcm_rd(b->cmic, CMIC_LEDUP0_CTRL) & CMIC_LEDUP_EN) ?
			   "on" : "off");
	seq_printf(s, "ledup0_clkdiv 0x%08x\n",
		   bcm_rd(b->cmic, CMIC_LEDUP0_CLK_DIV));
	mutex_unlock(&ffn_bcm_lock);
	return 0;
}

static int ffn_bcm_dbg_open(struct inode *i, struct file *f)
{
	return single_open(f, ffn_bcm_dbg_show, NULL);
}

static const struct file_operations ffn_bcm_dbg_fops = {
	.owner	 = THIS_MODULE,
	.open	 = ffn_bcm_dbg_open,
	.read	 = seq_read,
	.llseek	 = seq_lseek,
	.release = single_release,
};

static int ffn_bcm_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	struct ffn_bcm *b;
	int rc;

	/*
	 * Function 0 only. The BCM88375 presents .0 and .1, but they are two
	 * functions of ONE chip sharing one CMIC. Binding both would give two
	 * driver instances racing a single register block -- and binding the
	 * second function is exactly what broke ffn_pcic.
	 */
	if (PCI_FUNC(pdev->devfn) != 0) {
		dev_dbg(&pdev->dev, "skipping function %u\n",
			PCI_FUNC(pdev->devfn));
		return -ENODEV;
	}

	b = devm_kzalloc(&pdev->dev, sizeof(*b), GFP_KERNEL);
	if (!b)
		return -ENOMEM;
	b->pdev = pdev;

	/*
	 * Managed enable + BAR maps: released automatically on remove and on
	 * every error path below, so there is no hand-written unwind to get
	 * wrong. pci_enable_device() is also what sets PCI_COMMAND_MEMORY --
	 * this chip comes out of reset with memory decode OFF, which is why
	 * raw BAR reads returned 0xffffffff throughout bring-up until
	 * something wrote the sysfs enable node.
	 */
	rc = pcim_enable_device(pdev);
	if (rc) {
		dev_err(&pdev->dev, "cannot enable device: %d\n", rc);
		return rc;
	}

	rc = pcim_iomap_regions(pdev,
				BIT(FFN_BCM_BAR_IDENT) | BIT(FFN_BCM_BAR_CMIC),
				KBUILD_MODNAME);
	if (rc) {
		dev_err(&pdev->dev, "cannot map BAR0+BAR2: %d\n", rc);
		return rc;
	}
	b->ident = pcim_iomap_table(pdev)[FFN_BCM_BAR_IDENT];
	b->cmic = pcim_iomap_table(pdev)[FFN_BCM_BAR_CMIC];
	b->ident_len = pci_resource_len(pdev, FFN_BCM_BAR_IDENT);
	b->cmic_len = pci_resource_len(pdev, FFN_BCM_BAR_CMIC);
	if (!b->ident || !b->cmic || b->cmic_len < CMIC_LEDUP0_PROG_RAM) {
		dev_err(&pdev->dev, "BAR layout not usable (cmic %u bytes)\n",
			b->cmic_len);
		return -ENODEV;
	}

	/*
	 * Cheap proof that the byte-swap convention above is right, before
	 * anything depends on it: BAR0 register 0 carries the device id. A raw
	 * big-endian load reads 0x75830000; swapped, the low half must be
	 * 0x8375. If this does not match, every access in this driver is
	 * byte-reversed -- worth saying loudly rather than debugging later
	 * through wrong register values.
	 */
	b->ident_reg = bcm_rd(b->ident, 0);
	if ((b->ident_reg & 0xffff) != pdev->device)
		dev_warn(&pdev->dev,
			 "ident 0x%08x: low half is not device id 0x%04x -- check byte order\n",
			 b->ident_reg, pdev->device);

	/* Published LAST, under the lock every ioctl takes. */
	mutex_lock(&ffn_bcm_lock);
	if (ffn_bcm_cur) {
		mutex_unlock(&ffn_bcm_lock);
		dev_err(&pdev->dev, "a BCM control device is already bound\n");
		return -EBUSY;
	}
	ffn_bcm_cur = b;
	mutex_unlock(&ffn_bcm_lock);
	pci_set_drvdata(pdev, b);

	dev_info(&pdev->dev,
		 "BCM88375 CMIC bound: bar2 %u bytes, ident 0x%08x, /dev/%s\n",
		 b->cmic_len, b->ident_reg, FFN_BCM_DEVNAME);
	return 0;
}

static void ffn_bcm_remove(struct pci_dev *pdev)
{
	struct ffn_bcm *b = pci_get_drvdata(pdev);

	/*
	 * Retract under the same lock every ioctl holds while touching the
	 * mapping. After this returns, no ioctl can be mid-access, so the
	 * managed unmap that follows cannot pull memory out from under one.
	 */
	mutex_lock(&ffn_bcm_lock);
	if (ffn_bcm_cur == b)
		ffn_bcm_cur = NULL;
	mutex_unlock(&ffn_bcm_lock);

	if (b)
		dev_info(&pdev->dev,
			 "released: %llu schan ops, %llu errs, %llu timeouts\n",
			 b->schan_ops, b->schan_errs, b->schan_timeouts);
}

static const struct pci_device_id ffn_bcm_ids[] = {
	{ PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, PCI_DEVICE_ID_BCM88375) },
	{ 0 }
};
MODULE_DEVICE_TABLE(pci, ffn_bcm_ids);

static struct pci_driver ffn_bcm_driver = {
	.name		= KBUILD_MODNAME,
	.id_table	= ffn_bcm_ids,
	.probe		= ffn_bcm_probe,
	.remove		= ffn_bcm_remove,
};

static int __init ffn_bcm_init(void)
{
	int rc;

	/*
	 * Registered ONCE, here. Registering a module-global misc device from
	 * probe() is what made ffn_pcic return EBUSY on the second PCI
	 * function and then unwind over a still-published global.
	 */
	rc = misc_register(&ffn_bcm_misc);
	if (rc) {
		pr_err("ffn_bcm: misc_register failed: %d\n", rc);
		return rc;
	}

	/* Best-effort: no debugfs is not a reason to fail the load. */
	ffn_bcm_dbg = debugfs_create_file(KBUILD_MODNAME, 0400, NULL, NULL,
					  &ffn_bcm_dbg_fops);

	rc = pci_register_driver(&ffn_bcm_driver);
	if (rc) {
		debugfs_remove(ffn_bcm_dbg);
		misc_deregister(&ffn_bcm_misc);
		return rc;
	}
	return 0;
}

static void __exit ffn_bcm_exit(void)
{
	pci_unregister_driver(&ffn_bcm_driver);
	debugfs_remove(ffn_bcm_dbg);
	misc_deregister(&ffn_bcm_misc);
}

module_init(ffn_bcm_init);
module_exit(ffn_bcm_exit);

MODULE_DESCRIPTION("FFN OCTEON III control driver for the BCM88375 CMIC");
MODULE_AUTHOR("FFN NGFW");
MODULE_LICENSE("GPL v2");
MODULE_VERSION("0.1");
