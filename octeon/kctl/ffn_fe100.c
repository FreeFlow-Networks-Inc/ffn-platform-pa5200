// SPDX-License-Identifier: GPL-2.0-or-later
/* FE100 PCI ownership. The CP userspace owner programs the register BAR.
 * Probe/remove never reset the ASIC, initialize tables, or enable bus mastering.
 * Userspace maps the standard PCI resource0 file; there is no /dev/mem fallback.
 */
#include <linux/module.h>
#include <linux/pci.h>

#define FFN_FE100_BAR_SIZE 0x100000

static int ffn_fe100_probe(struct pci_dev *pdev,
                          const struct pci_device_id *id)
{
    int ret;

    if (!(pci_resource_flags(pdev, 0) & IORESOURCE_MEM) ||
        pci_resource_len(pdev, 0) != FFN_FE100_BAR_SIZE ||
        !pci_resource_start(pdev, 0))
        return dev_err_probe(&pdev->dev, -ENODEV, "Unexpected FE100 BAR0 geometry\n");
    ret = pci_enable_device_mem(pdev);
    if (ret)
        return ret;
    /* Non-exclusive reservation deliberately permits PCI sysfs resource mmap. */
    ret = pci_request_region(pdev, 0, "ffn_fe100");
    if (ret) {
        pci_disable_device(pdev);
        return ret;
    }
    dev_info(&pdev->dev, "FE100 bound; BAR0 %pR, hardware state preserved\n",
             &pdev->resource[0]);
    return 0;
}

static void ffn_fe100_remove(struct pci_dev *pdev)
{
    /* The management owner must quiesce and close userspace maps before unbind. */
    pci_release_region(pdev, 0);
    pci_disable_device(pdev);
}

static const struct pci_device_id ffn_fe100_ids[] = {
    { PCI_DEVICE(0xfeed, 0xfe1c) },
    { }
};
MODULE_DEVICE_TABLE(pci, ffn_fe100_ids);

static struct pci_driver ffn_fe100_driver = {
    .name = "ffn_fe100",
    .id_table = ffn_fe100_ids,
    .probe = ffn_fe100_probe,
    .remove = ffn_fe100_remove,
};
module_pci_driver(ffn_fe100_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("FFN FE100 PCI register-window ownership");
