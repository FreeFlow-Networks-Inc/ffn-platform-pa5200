/* SPDX-License-Identifier: GPL-2.0-only */
/* Offline DDR initialization audit. Never maps hardware or opens I2C.
 * Reads are a register model, not evidence of a trained memory device. */
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
static uint32_t registers[0x100000/4];
static unsigned events, denied;
int fe100_dbg_prt(uint32_t level,const char *format,...) { return 0; }
int fe100_get_dram_info(void *config) {
    /* Explicitly model the audited owner's missing-EEPROM fallback. */
    fprintf(stdout,"EEPROM_FALLBACK_NO_OVERRIDE\n"); return 0;
}
int fe100_reg_rd(uint32_t dev,uint32_t off,uint32_t *value) {
    if(dev || !value || off>=0x100000 || (off&3) || ++events>10000) {
        denied++; return 12;
    }
    *value=registers[off/4]; return 0;
}
int fe100_reg_wr(uint32_t dev,uint32_t off,uint32_t value) {
    if(dev || off>=0x100000 || (off&3) || ++events>10000) {
        denied++; return 12;
    }
    registers[off/4]=value;
    fprintf(stdout,"W %05x %08x\n",off,value); return 0;
}
unsigned ffn_ddr_capture_denied(void) { return denied; }
