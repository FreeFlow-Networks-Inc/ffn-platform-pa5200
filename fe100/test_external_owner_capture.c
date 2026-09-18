/* SPDX-License-Identifier: GPL-2.0-only */
/* Offline ABI oracle. This test library has NO MMIO, I2C or device access.
 * Interpose the audited cfg4 routine's only side effect, fe100_ia_op. */
#include <stdint.h>
#include <stddef.h>
struct ia { uint32_t target,loop,inc,phy,type,size,addr_size;
    uint64_t address; uint32_t count; uint32_t *data; uint32_t cc,hit; };
struct record { uint64_t address; uint32_t words[3]; };
static struct record records[263];
static unsigned count;
_Static_assert(sizeof(struct ia)==64,"owner IA size");
_Static_assert(offsetof(struct ia,data)==48,"owner IA data offset");
int fe100_dbg_prt(uint32_t level, const char *format, ...) { return 0; }
int fe100_ia_op(uint32_t dev,uint32_t block,struct ia *r)
{
    unsigned i;
    if(dev || block!=16 || !r || !r->data || count>=263 || r->target!=256 ||
       r->loop || r->inc || r->phy || r->type!=2 || r->size!=3 ||
       r->addr_size || r->count!=3) return 12;
    records[count].address=r->address;
    for(i=0;i<3;i++) records[count].words[i]=r->data[i];
    count++;
    return 0;
}
unsigned ffn_capture_count(void) { return count; }
const struct record *ffn_capture_record(unsigned i) { return i<count ? &records[i] : 0; }
