/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_fe100_stats.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static const char sample[]=
"0018001401000800000000138008013800000000000000000000000100000000"
"000003e95000204000000000000000000000000000000000000000000000000000000000";
static const char paired_tcp_sample[]=
"0018001401000800000000138008013800000000000000000000000200000000"
"000003e9480011a0000003eb480011a00000000000000000000000000000000000000000";
int main(void) {
    uint8_t wire[1032]={0},copy[1032];struct ffn_fe100_counters out;
    size_t n=strlen(sample)/2;
    for(size_t i=0;i<n;i++){unsigned v;assert(sscanf(sample+i*2,"%2x",&v)==1);wire[i]=(uint8_t)v;}
    assert(ffn_fe100_counters_decode(wire,n,24,20,&out)==1);
    assert(out.count==1 && out.records[0].flow_id==1001 && out.records[0].reason==1);
    assert(out.records[0].packets==64 && out.records[0].octets==64*129);
    assert(ffn_fe100_counters_decode(wire,40,24,20,&out)==1);
    for(size_t i=0;i<n;i++) {
        if(i==40)continue;
        assert(ffn_fe100_counters_decode(wire,i,24,20,&out)!=1 && !out.count);
    }
    assert(ffn_fe100_counters_decode(wire,n+1,24,20,&out)==-1 && !out.count);
    for(unsigned i=0;i<256;i++) {
        if(i==19)continue;
        wire[11]=(uint8_t)i;assert(!ffn_fe100_counters_decode(wire,n,24,20,&out));
    }
    wire[11]=19;
    const unsigned invalid[]={4,7,8,9,20,24,25,26,28,40,67};
    for(unsigned i=0;i<sizeof(invalid)/sizeof(invalid[0]);i++) {
        memcpy(copy,wire,n);copy[invalid[i]]^=128;
        assert(ffn_fe100_counters_decode(copy,n,24,20,&out)==-1 && !out.count);
    }
    assert(!ffn_fe100_counters_decode(wire,n,25,20,&out));
    assert(!ffn_fe100_counters_decode(wire,n,24,21,&out));
    wire[27]=0;assert(ffn_fe100_counters_decode(wire,n,24,20,&out)==-1);
    wire[27]=126;assert(ffn_fe100_counters_decode(wire,n,24,20,&out)==-1);
    wire[27]=125;
    memset(wire+32,255,1000);
    assert(ffn_fe100_counters_decode(wire,1032,24,20,&out)==1 && out.count==125);
    for(unsigned i=0;i<125;i++)assert(out.records[i].flow_id==UINT32_MAX &&
        out.records[i].reason==3 && out.records[i].packets==255 && out.records[i].octets==0x3fffff);
    assert(ffn_fe100_counters_decode(wire,1031,24,20,&out)==-1 && !out.count);
    assert(ffn_fe100_counters_decode(NULL,n,24,20,&out)==-1 && !out.count);
    assert(ffn_fe100_counters_decode(wire,n,24,20,NULL)==-1);
    n=strlen(paired_tcp_sample)/2;
    for(size_t i=0;i<n;i++){unsigned v;assert(sscanf(paired_tcp_sample+i*2,"%2x",&v)==1);wire[i]=(uint8_t)v;}
    assert(ffn_fe100_counters_decode(wire,n,24,20,&out)==1 && out.count==2);
    assert(out.records[0].flow_id==1001 && out.records[1].flow_id==1003);
    for(unsigned i=0;i<2;i++)assert(out.records[i].packets==32 && out.records[i].octets==32*141 && out.records[i].reason==1);
    wire[27]=3;assert(ffn_fe100_counters_decode(wire,48,24,20,&out)==-1 && !out.count);
    puts("FE100 compact counters: captured delta, count, padding, truncation and scope passed");
    return 0;
}
