/* BCM8375 LED registers only. Native BE CMIC mapping; single MMIO accesses. */
#include <stdint.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

static volatile uint32_t *regs;
static int allowed(unsigned offset)
{
    unsigned bases[] = {0x20000, 0x21000, 0x29000};
    unsigned i;
    if (offset & 3) return 0;
    for (i = 0; i < 3; i++) {
        unsigned d = offset - bases[i];
        if (d == 0 || (d >= 0x400 && d < 0xc00)) return 1;
    }
    return 0;
}
int led_open(void)
{
    int fd = open("/sys/bus/pci/devices/0001:01:00.0/resource2", O_RDWR | O_SYNC);
    void *p;
    if (fd < 0) return -errno;
    p = mmap(0, 0x30000, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) return -errno;
    regs = p;
    return 0;
}
int led_read(unsigned offset, uint32_t *value)
{
    if (!regs || !allowed(offset)) return -EINVAL;
    *value = regs[offset / 4];
    return 0;
}
int led_write(unsigned offset, uint32_t value)
{
    if (!regs || !allowed(offset)) return -EINVAL;
    regs[offset / 4] = value;
    __sync_synchronize();
    return 0;
}
void led_close(void)
{
    if (regs) munmap((void *)regs, 0x30000);
    regs = 0;
}
