/* SPDX-License-Identifier: GPL-2.0-or-later */
/* One-block owner adapter, with a native watchdog that also bounds C loops. */
#define fe100_reg_rd ffn_block_reg_rd
#define fe100_reg_wr ffn_block_reg_wr
#include "ffn_fe100_mmio.c"
#undef fe100_reg_rd
#undef fe100_reg_wr
#include <signal.h>
#include <stdarg.h>
static int verbose;
void ffn_flow_verbose(int enabled) { verbose = !!enabled; }
int fe100_dbg_prt(uint32_t level, const char *format, ...)
{
    (void)level;
    if (verbose && format) {
        va_list args;
        va_start(args, format);
        vfprintf(stderr, format, args);
        va_end(args);
        fflush(stderr);
    }
    return 0;
}
static int session_mode;
static int diagnostic_mode;
int ffn_flow_diagnostic_mode(void)
{
    if (regs || (block_base != 0x98000 && block_base != 0xa8000 && block_base != 0xb0000)) return -1;
    diagnostic_mode = 1;
    return 0;
}
int ffn_flow_session_mode(void)
{
    if (regs || block_base != 0x40000) return -1;
    session_mode = 1;
    return 0;
}
static int session_cfp(uint32_t off)
{
    return session_mode && block_base == 0x40000 &&
        (off == 0x48014 || off == 0x48018 || (off >= 0x486c0 && off <= 0x486f0));
}
int fe100_reg_rd(uint32_t dev, uint32_t off, uint32_t *value)
{
    uint32_t saved = block_base;
    if (session_cfp(off)) block_base = 0x48000;
    int rc = ffn_block_reg_rd(dev, off, value);
    block_base = saved;
    return rc;
}
int fe100_reg_wr(uint32_t dev, uint32_t off, uint32_t value)
{
    if (diagnostic_mode) {
        uint32_t command = block_base + (block_base == 0xb0000 ? 0x500 : 0x320);
        /* DPHY read only. IA address selection does not write memory/PHY state.
         * Targets 0x400/0x800 select the two channels (sysroot dphy_reg_rd). */
        int allowed = (off == command+4 && value <= 0xffff) ||
            (off == command && (value == 0x02000801 || value == 0x02001001));
        if (!allowed) {
            faults++;
            if (trace) fprintf(trace,"DENIED DIAGNOSTIC WRITE off=0x%x value=0x%x\n",off,value);
            return 12;
        }
    }
    uint32_t saved = block_base;
    if (session_cfp(off)) block_base = 0x48000;
    int rc = ffn_block_reg_wr(dev, off, value);
    block_base = saved;
    return rc;
}
static void flow_timeout(int sig)
{
    (void)sig;
    static const char msg[] = "FE100 native call timed out; inspect journal before recovery\n";
    ssize_t ignored = write(STDERR_FILENO, msg, sizeof(msg)-1);
    (void)ignored;
    _exit(124);
}
void ffn_flow_watchdog(unsigned seconds)
{
    if (seconds > 120) seconds = 120;
    signal(SIGALRM, flow_timeout);
    alarm(seconds);
}
