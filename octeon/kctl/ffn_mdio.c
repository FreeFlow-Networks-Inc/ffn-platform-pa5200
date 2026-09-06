// SPDX-License-Identifier: GPL-2.0
/*
 * ffn_mdio.c -- read PHYs on the OCTEON's own MDIO (SMI) buses.
 *
 * WHY THIS EXISTS. The BCM84848 behind the four RJ45 ports was probed for a
 * long time on the BCM88375's external MDIO -- all eight of its buses, all four
 * of the PHY's addresses -- and answered nothing. It was on the wrong
 * controller. The vendor reaches it through the OCTEON's SMI instead:
 * pan_read_84848_register() in libpanbcm_cp.so dispatches to a helper built
 * entirely out of cvmx-mdio.h and cvmx-smix-defs.h -- CVMX_SMIX_CLK,
 * CVMX_SMIX_WR_DAT, CVMX_SMIX_CMD -- calling cvmx_mdio_45_read(). Nothing in
 * that path touches the switch.
 *
 * The device tree has both OCTEON controllers, mdio@1180000003800 and
 * mdio@1180000003880, the mdio_octeon driver binds both, and both register as
 * mii_buses. So the hardware path is already there and driven.
 *
 * WHAT IS MISSING is only enumeration. Those DT nodes have no child PHY nodes,
 * so of_mdiobus_register() falls back to scanning -- and the default scan is
 * CLAUSE 22. A clause-45-only PHY does not answer clause 22, so the scan finds
 * nothing and /sys/bus/mdio_bus/devices stays empty. Which looks exactly like
 * "no PHY there", and is not.
 *
 * This module does the clause-45 scan the kernel did not, using the bus the
 * kernel already registered.
 *
 * READ ONLY, deliberately. mdiobus_c45_read() takes the bus lock, so it cannot
 * interleave with anything else on the bus, and a read cannot change PHY state.
 * There is no write path here at all: a write path belongs behind its own gate,
 * once there is a bring-up sequence worth running.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/phy.h>
#include <linux/mdio.h>

/*
 * Bus ids as the kernel registers them -- "800" plus the DT reg, which is what
 * /sys/class/mdio_bus shows. Empty string means "scan every registered bus".
 */
static char *bus = "";
module_param(bus, charp, 0444);
MODULE_PARM_DESC(bus, "mii_bus id, e.g. 8001180000003800 (default: all)");

static int addr = -1;
module_param(addr, int, 0444);
MODULE_PARM_DESC(addr, "PHY address 0-31 (default: scan all)");

static int devad = 1;
module_param(devad, int, 0444);
MODULE_PARM_DESC(devad, "clause-45 device address (default 1, PMA/PMD)");

static int reg = -1;
module_param(reg, int, 0444);
MODULE_PARM_DESC(reg, "register to read; default reads the ID pair 2,3");

/* The vendor's gryphon_phy_addrs, a 16-byte .data object in libports.so:
 * 0x11 0x10 0x13 0x12. Listed pair-swapped; these are the four RJ45 PHYs.
 */
static const int vendor_addrs[] = { 0x10, 0x11, 0x12, 0x13 };

static const char *bus_names[] = {
	"8001180000003800",
	"8001180000003880",
};

/* A read that fails, or a bus with no device, reads all ones. Zero is equally
 * uninformative and shows up on an idle bus, so treat both as "nothing".
 */
static bool interesting(int v)
{
	return v >= 0 && v != 0xffff && v != 0x0000;
}

static void probe_one(struct mii_bus *b, int a)
{
	int id2, id3;

	if (reg >= 0) {
		int v = mdiobus_c45_read(b, a, devad, reg);

		if (interesting(v) || addr >= 0)
			pr_info("ffn_mdio: %s addr %2d devad %d reg 0x%04x = 0x%04x\n",
				b->id, a, devad, reg, v);
		return;
	}

	/* Clause-45 PMA/PMD identifier: registers 2 and 3 of the device. */
	id2 = mdiobus_c45_read(b, a, devad, MDIO_DEVID1);
	id3 = mdiobus_c45_read(b, a, devad, MDIO_DEVID2);

	if (interesting(id2) || interesting(id3) || addr >= 0)
		pr_info("ffn_mdio: %s addr %2d devad %d ID = %04x.%04x%s\n",
			b->id, a, devad, id2, id3,
			(a == vendor_addrs[0] || a == vendor_addrs[1] ||
			 a == vendor_addrs[2] || a == vendor_addrs[3])
			 ? "   <- a vendor RJ45 PHY address" : "");
}

static void scan_bus(const char *name)
{
	struct mii_bus *b = mdio_find_bus(name);
	int a;

	if (!b) {
		pr_info("ffn_mdio: no bus '%s'\n", name);
		return;
	}
	pr_info("ffn_mdio: scanning %s (%s), devad %d\n",
		b->id, b->name ? b->name : "?", devad);

	if (addr >= 0) {
		probe_one(b, addr);
	} else {
		for (a = 0; a < PHY_MAX_ADDR && a < 32; a++)
			probe_one(b, a);
	}
	put_device(&b->dev);
}

static int __init ffn_mdio_init(void)
{
	int i;

	if (addr >= 32 || devad < 0 || devad > 31 || reg > 0xffff) {
		pr_err("ffn_mdio: addr 0-31, devad 0-31, reg 0-65535\n");
		return -EINVAL;
	}

	if (bus && bus[0]) {
		scan_bus(bus);
	} else {
		for (i = 0; i < ARRAY_SIZE(bus_names); i++)
			scan_bus(bus_names[i]);
	}

	pr_info("ffn_mdio: done (nothing printed above means nothing answered)\n");

	/*
	 * Fail the load on purpose. All the work happens here, there is no
	 * state to keep, and staying resident would just mean an rmmod before
	 * every rerun. insmod reports the error; the results are in dmesg.
	 */
	return -ECANCELED;
}

module_init(ffn_mdio_init);

MODULE_DESCRIPTION("FFN: clause-45 scan of the OCTEON SMI buses");
MODULE_AUTHOR("FreeFlow Networks, Inc.");
MODULE_LICENSE("GPL");
