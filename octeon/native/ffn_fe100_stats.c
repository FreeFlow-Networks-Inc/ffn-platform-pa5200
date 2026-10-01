/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_fe100_stats.h"
#include <string.h>
static uint16_t be16(const uint8_t *p) {return (uint16_t)p[0]<<8|p[1];}
static uint32_t be32(const uint8_t *p) {return (uint32_t)be16(p)<<16|be16(p+2);}
int ffn_fe100_counters_decode(const uint8_t *wire, size_t size,
    uint16_t trunk, uint16_t port, struct ffn_fe100_counters *out)
{
    if(out)memset(out,0,sizeof(*out));
    if(!wire || !out || !trunk || !port || trunk==port)return -1;
    if(size<12 || be16(wire)!=trunk || be16(wire+2)!=port || wire[11]!=19)return 0;
    /* OTMH4 + retained ITMH4 + condor_cmh_t16 + condor_flow_stats_t8.
     * Short messages are padded to 64 bytes after OTMH. The count, not the
     * Ethernet padding, determines the number of eight-byte FCR records.
     */
    if(size<32 || wire[4]!=1 || (wire[5]&0xe0) || wire[7] ||
       wire[8] || wire[9] || be32(wire+20) ||
       wire[24] || wire[25] || wire[26] || be32(wire+28))return -1;
    unsigned count=wire[27];
    if(!count || count>FFN_FE100_STATS_MAX)return -1;
    size_t end=32+(size_t)count*8;
    if(size!=end && size!=(end<68?68:end))return -1;
    for(size_t i=end;i<size;i++)if(wire[i])return -1;
    for(unsigned i=0;i<count;i++) {
        const uint8_t *record=wire+32+i*8;
        uint32_t value=be32(record+4);
        out->records[i]=(struct ffn_fe100_counter){
            .flow_id=be32(record), .packets=(uint8_t)((value>>22)&255),
            .octets=value&0x3fffff, .reason=(uint8_t)(value>>30)};
    }
    out->count=count;
    return 1;
}
