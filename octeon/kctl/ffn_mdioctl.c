// SPDX-License-Identifier: GPL-2.0-or-later
/* Locked access to the CP's copper PHYs through Linux's OCTEON MDIO driver. */
#include <linux/capability.h>
#include <linux/fs.h>
#include <linux/mdio.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/phy.h>
#include <linux/uaccess.h>

struct ffn_mdio_request { __u32 write, phy, devad, reg, value; };
#define FFN_MDIO_XFER _IOWR('M', 1, struct ffn_mdio_request)
static struct mii_bus *ffn_bus, *gearbox_bus;
static struct miscdevice gearbox_dev;
static bool allow_writes;
static bool allow_gearbox_writes;
module_param(allow_gearbox_writes, bool, 0600);
MODULE_PARM_DESC(allow_gearbox_writes, "Permit writes to gearbox bus 1 PHY 0");
module_param(allow_writes, bool, 0600);
MODULE_PARM_DESC(allow_writes, "Permit writes to copper PHY addresses 16..19");

static long ffn_mdio_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct ffn_mdio_request r;
    bool gearbox = file->private_data == &gearbox_dev;
    struct mii_bus *bus = gearbox ? gearbox_bus : ffn_bus;
    int ret;
    if (!capable(CAP_SYS_RAWIO)) return -EPERM;
    if (cmd != FFN_MDIO_XFER) return -ENOTTY;
    if (copy_from_user(&r, (void __user *)arg, sizeof(r))) return -EFAULT;
    if (r.write > 1 || r.phy > 31 || r.devad > 31 || r.reg > 65535 || r.value > 65535)
        return -EINVAL;
    if (r.write) {
        if (gearbox ? (!allow_gearbox_writes || r.phy != 0) :
                      (!allow_writes || r.phy < 16 || r.phy > 19)) return -EPERM;
        ret = mdiobus_c45_write(bus, r.phy, r.devad, r.reg, r.value);
    } else {
        ret = mdiobus_c45_read(bus, r.phy, r.devad, r.reg);
        if (ret >= 0) r.value = ret;
    }
    if (ret < 0) return ret;
    if (copy_to_user((void __user *)arg, &r, sizeof(r))) return -EFAULT;
    return 0;
}

static const struct file_operations ffn_mdio_fops = {
    .owner = THIS_MODULE,
    .unlocked_ioctl = ffn_mdio_ioctl,
};
static struct miscdevice ffn_mdio_dev = {
    .minor = MISC_DYNAMIC_MINOR, .name = "ffn-mdio",
    .fops = &ffn_mdio_fops, .mode = 0600,
};
static struct miscdevice gearbox_dev = {
    .minor = MISC_DYNAMIC_MINOR, .name = "ffn-mdio1",
    .fops = &ffn_mdio_fops, .mode = 0600,
};
static int __init ffn_mdioctl_init(void)
{
    int ret;
    ffn_bus = mdio_find_bus("8001180000003800");
    if (!ffn_bus) return -ENODEV;
    gearbox_bus = mdio_find_bus("8001180000003880");
    if (!gearbox_bus) { put_device(&ffn_bus->dev); return -ENODEV; }
    ret = misc_register(&ffn_mdio_dev);
    if (!ret) {
        ret = misc_register(&gearbox_dev);
        if (ret) misc_deregister(&ffn_mdio_dev);
    }
    if (ret) { put_device(&ffn_bus->dev); put_device(&gearbox_bus->dev); }
    return ret;
}
static void __exit ffn_mdioctl_exit(void)
{
    misc_deregister(&ffn_mdio_dev);
    misc_deregister(&gearbox_dev);
    put_device(&ffn_bus->dev);
    put_device(&gearbox_bus->dev);
}
module_init(ffn_mdioctl_init);
module_exit(ffn_mdioctl_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("FFN CP copper PHY MDIO access");
