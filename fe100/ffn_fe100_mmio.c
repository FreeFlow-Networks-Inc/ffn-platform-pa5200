// SPDX-License-Identifier: GPL-2.0-or-later
/* FE100 register ABI adapter for the appliance's owner-supplied library.
 * The NIF diagnostic is confined to known NIF registers; writes default off.
 */
#define _DEFAULT_SOURCE
#include <endian.h>
#include <fcntl.h>
#include <stdint.h>
#include <pthread.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

static volatile uint32_t *regs;
static unsigned char known[0x100000 / 4];
static int writable;
static FILE *trace;
static pthread_mutex_t ia_mutex = PTHREAD_MUTEX_INITIALIZER;
/* The inspected owner ABI returns void; table helpers use these around IA. */
void fe100_lock(uint32_t dev) { if (dev == 0) pthread_mutex_lock(&ia_mutex); }
void fe100_unlock(uint32_t dev) { if (dev == 0) pthread_mutex_unlock(&ia_mutex); }
#ifndef FFN_BLOCK_BASE
#define FFN_BLOCK_BASE 0x10000
#endif
static uint32_t block_base = FFN_BLOCK_BASE;
static int lif_access_table = -1;
/* The owner getter dereferences PAN's process-local device object. A lab
 * caller must explicitly select the table before opening this adapter. */
int ffn_fe100_select_lif_table(unsigned int table)
{
    if (regs || table > 1) return -1;
    lif_access_table = (int)table;
    return 0;
}
unsigned int pan_fe100_get_access_tbl(uint32_t dev)
{
    return dev == 0 && block_base == 0x80000 && lif_access_table >= 0
        ? (unsigned int)lif_access_table : ~0U;
}
int ffn_fe100_select_block(uint32_t base)
{
    if (regs || (base & 0x7fff) || base > 0xb0000) return -1;
    block_base = base;
    return 0;
}

int ffn_fe100_open(uint64_t base, const char *log_path, int enable_writes)
{
    int fd;
    if (regs || (base & 4095)) return -1;
    trace = fopen(log_path, "w");
    if (!trace) return -1;
    setvbuf(trace, NULL, _IOLBF, 0);
    fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) return -1;
    regs = mmap(NULL, 0x100000, PROT_READ | PROT_WRITE, MAP_SHARED, fd, base);
    close(fd);
    if (regs == MAP_FAILED) { regs = NULL; return -1; }
    writable = enable_writes;
    return 0;
}
int ffn_fe100_allow(uint32_t off)
{
    if ((off & 3) || off >= 0x100000) return -1;
    known[off / 4] = 1;
    return 0;
}
static int valid(uint32_t dev, uint32_t off)
{
    return regs && dev == 0 && !(off & 3) && off >= block_base &&
           off < block_base + 0x8000 && known[off / 4];
}
int fe100_reg_rd(uint32_t dev, uint32_t off, uint32_t *value)
{
    if (!value || !valid(dev, off)) {
        if (trace) fprintf(trace, "DENIED READ dev=%u off=0x%x\n", dev, off);
        return 12;
    }
    *value = le32toh(regs[off / 4]);
    fprintf(trace, "R 0x%05x 0x%08x\n", off, *value);
    return 0;
}
int fe100_reg_wr(uint32_t dev, uint32_t off, uint32_t value)
{
    if (!writable || !valid(dev, off)) {
        if (trace) fprintf(trace, "DENIED WRITE dev=%u off=0x%x value=0x%x\n", dev, off, value);
        return 12;
    }
    fprintf(trace, "W 0x%05x 0x%08x\n", off, value);
    regs[off / 4] = htole32(value);
    __sync_synchronize();
    return 0;
}
