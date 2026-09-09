// SPDX-License-Identifier: GPL-2.0-or-later
/* Interoperate with the owner's phymod callbacks through locked Linux MDIO. */
#include <stdint.h>
#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>

struct request { uint32_t write, phy, devad, reg, value; };
static int mdio = -1, writes;
static FILE *trace;

int ffn_gearbox_open(const char *path, int apply)
{
    if (mdio >= 0) return -1;
    trace = fopen(path, "w");
    if (!trace) return -1;
    setvbuf(trace, NULL, _IOLBF, 0);
    mdio = open("/dev/ffn-mdio1", O_RDWR | O_CLOEXEC);
    writes = apply;
    return mdio < 0 ? -1 : 0;
}

static int transfer(uint32_t phy, uint32_t reg, uint32_t *value, int write)
{
    struct request r = {write, phy, reg >> 16, reg & 65535, *value};
    int result;
    if (mdio < 0 || phy != 0 || r.devad > 31 || (write && (!writes || *value > 65535))) {
        if (trace) fprintf(trace, "DENIED %c %u %08x %08x\n", write ? 'W' : 'R', phy, reg, *value);
        return -1;
    }
    result = ioctl(mdio, _IOWR('M', 1, struct request), &r);
    if (result == 0) *value = r.value;
    fprintf(trace, "%c %u %08x %04x rc=%d\n", write ? 'W' : 'R', phy, reg, *value, result);
    return result;
}

int pan_read_gearbox_register(void *user, uint32_t phy, uint32_t reg, uint32_t *value)
{
    (void)user;
    if (!value) return -1;
    *value = 0;
    return transfer(phy, reg, value, 0);
}

int pan_write_gearbox_register(void *user, uint32_t phy, uint32_t reg, uint32_t value)
{
    (void)user;
    return transfer(phy, reg, &value, 1);
}
