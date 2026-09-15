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
static unsigned faults;
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
/* Some DDR helpers inspect the other controller's capability register.
 * Permit individually named reads outside the selected writable block. */
int ffn_fe100_allow_readonly(uint32_t off)
{
    if ((off & 3) || off >= 0x100000) return -1;
    if (!known[off / 4]) known[off / 4] = 2;
    return 0;
}
unsigned ffn_fe100_faults(void) { return faults; }
/* TDI external-memory diagnostics use the TLU IA bridge (owner block 16).
 * Opt in separately; no other TLU registers or arbitrary targets are exposed. */
int ffn_fe100_allow_external_ia(void)
{
    if (block_base != 0xa0000) return -1;
    for (unsigned off = 0x80500; off <= 0x80518; off += 4)
        known[off / 4] = 3;
    return 0;
}
static int external_ia(uint32_t dev, uint32_t off)
{
    return regs && dev == 0 && !(off & 3) && off >= 0x80500 &&
           off <= 0x80518 && known[off / 4] == 3;
}
static int external_command(uint32_t value)
{
    uint32_t target = (value >> 1) & 0xfffff;
    uint32_t op = (value >> 25) & 7;
    uint32_t addr = le32toh(regs[0x80504 / 4]);
    if ((value & ~0x0e1fffffU) || !(value & 1) || (op != 1 && op != 2))
        return 0;
    if (target == 0x2200) return addr == 0;
    return target == 0x100 && (addr == 1 ||
        (addr >= 0x1000 && addr <= 0x2fe0 && !(addr & 31)) ||
        (addr >= 0x40a00 && addr <= 0x40a03) ||
        addr == 0x40a5a || addr == 0x40a5c);
}
static int valid(uint32_t dev, uint32_t off)
{
    return regs && dev == 0 && !(off & 3) && off >= block_base &&
           off < block_base + 0x8000 && known[off / 4];
}
int fe100_reg_rd(uint32_t dev, uint32_t off, uint32_t *value)
{
    if (!value || !(valid(dev, off) || external_ia(dev, off) || (regs && dev == 0 && !(off & 3) &&
                                      off < 0x100000 && known[off / 4] == 2))) {
        faults++;
        if (trace) fprintf(trace, "DENIED READ dev=%u off=0x%x\n", dev, off);
        return 12;
    }
    *value = le32toh(regs[off / 4]);
    fprintf(trace, "R 0x%05x 0x%08x\n", off, *value);
    return 0;
}
int fe100_reg_wr(uint32_t dev, uint32_t off, uint32_t value)
{
    if (!writable || !((valid(dev, off) && known[off / 4] == 1) ||
        (external_ia(dev, off) && off != 0x80508 &&
         (off != 0x80500 || external_command(value))))) {
        faults++;
        if (trace) fprintf(trace, "DENIED WRITE dev=%u off=0x%x value=0x%x\n", dev, off, value);
        return 12;
    }
    fprintf(trace, "W 0x%05x 0x%08x\n", off, value);
    regs[off / 4] = htole32(value);
    __sync_synchronize();
    return 0;
}
