/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <errno.h>
#include <fcntl.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <unistd.h>

unsigned ffn_hwio_abi(void) { return 1; }
static int transfer(unsigned bus, struct i2c_msg *messages, unsigned count)
{
    char path[64]; int fd, rc, error;
    struct i2c_rdwr_ioctl_data request = { .msgs = messages, .nmsgs = count };
    if (bus > 65535) { errno = EINVAL; return -1; }
    snprintf(path, sizeof(path), "/dev/i2c-%u", bus);
    fd = open(path, O_RDWR | O_CLOEXEC); if (fd < 0) return -1;
    rc = ioctl(fd, I2C_RDWR, &request); error = errno;
    close(fd);
    if (rc < 0) { errno = error; return -1; }
    if ((unsigned)rc != count) { errno = EIO; return -1; }
    return 0;
}
int ffn_i2c_read(unsigned bus, unsigned address, unsigned offset, unsigned width,
                 uint8_t *out, unsigned count)
{
    uint8_t index[2] = {(uint8_t)(offset >> 8), (uint8_t)offset};
    struct i2c_msg messages[2];
    if (address < 3 || address > 0x77 || width > 2 || !out || !count || count > 4096 ||
        (width == 1 && offset > 255) || offset > 65535 || (!width && offset)) {
        errno = EINVAL; return -1;
    }
    messages[0] = (struct i2c_msg){.addr = address, .len = width, .buf = index + (width == 1)};
    messages[1] = (struct i2c_msg){.addr = address, .flags = I2C_M_RD, .len = count, .buf = out};
    return transfer(bus, messages + !width, width ? 2 : 1);
}
int ffn_i2c_write8(unsigned bus, unsigned address, unsigned offset, unsigned value)
{
    uint8_t bytes[2] = {(uint8_t)offset, (uint8_t)value};
    struct i2c_msg message = {.addr = address, .len = 2, .buf = bytes};
    if (address < 3 || address > 0x77 || offset > 255 || value > 255) {
        errno = EINVAL; return -1;
    }
    return transfer(bus, &message, 1);
}
