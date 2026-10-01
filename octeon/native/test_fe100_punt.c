/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_fe100_punt.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
/* Actual isolated 23/24 TCP miss capture. Benchmark addresses only. */
static const char sample[]=
"001800140100040010000008812c00384d05d42b000000000000000d00000000"
"40060ffebf68bf69c6120001c612000205c0408d00170a0e"
"02ff0000000202ff0000000108004500007f000040004006ae51c6120001c6120002"
"bf68bf6912340000567800005018ffff67dc0000"
"46464e2d46453130302d53455353494f4e3ae4b7bf02b2424fe9a9facbc5247d7309"
"00000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30313233";
static const char ttl_sample[]=
"001800140100080010000005811c00b80000000000000000000003e90000000205c0408d00170a0e"
"02ff0000000202ff0000000108004500007f000040000106ed51c6120001c6120002bf68bf6912340000567800005018ffffa3250000"
"46464e2d46453130302d53455353494f4e3a1618ec0d676d45098c22c8ba9dc5d6a400000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30313233";
static const char fragment_sample[]=
"0018001401000b0010000004811c00b80000000000000000000000000000001e05c0408d00170a0e"
"02ff0000000202ff0000000108004500007f000020004006ce51c6120001c6120002bf68bf6912340000567800005018ffff15250000"
"46464e2d46453130302d53455353494f4e3a94f0c17750734bdc8a57c567b121124c00000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30313233";
static const char udp_miss_sample[]=
"001800140100040010000008812c00387d5c1ef400000000000000130000000040110ffebf68bf69c6120001c612000205c0"
"408100170a0e02ff0000000202ff00000001080045000073000040004011ae52c6120001c6120002bf68bf69005fb30e4646"
"4e2d46453130302d53455353494f4e3ab1320d1db1934a53b5c9c823b4ac339100000102030405060708090a0b0c0d0e0f10"
"1112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f30313233";
static const char udp_ttl_expired_sample[]=
"001800140100080010000005811c00b80000000000000000000003e90000000205c0408100170a0e02ff0000000202ff0000"
"0001080045000073000040000111ed52c6120001c6120002bf68bf69005fbcdd46464e2d46453130302d53455353494f4e3a"
"c805b0fd785d4246a73ee73380ead38e00000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
"2122232425262728292a2b2c2d2e2f30313233";
static const char udp_fragment_sample[]=
"0018001401000e0010000004811c00b80000000000000000000000000000001e05c0408100170a0e02ff0000000202ff0000"
"0001080045000073000020004011ce52c6120001c6120002bf68bf69005f4b6146464e2d46453130302d53455353494f4e3a"
"45cd1004add4416ba054e09c43387ed400000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
"2122232425262728292a2b2c2d2e2f30313233";
static const char udp_mtu_exceeded_sample[]=
"001800140100080010000005811c00b80000000000000000000003e90000000d05c0408100170a0e02ff0000000202ff0000"
"0001080045000073000040004011ae52c6120001c6120002bf68bf69005f111946464e2d46453130302d53455353494f4e3a"
"e23cbf25d9fd4024beecc75e90b1efd500000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
"2122232425262728292a2b2c2d2e2f30313233";
int main(void) {
    uint8_t wire[256]={0},copy[256];size_t n=strlen(sample)/2;
    struct ffn_fe100_punt_scope scope={24,20,23,23,4094};
    struct ffn_fe100_punt result;
    for(size_t i=0;i<n;i++){unsigned v;assert(sscanf(sample+2*i,"%2x",&v)==1);wire[i]=(uint8_t)v;}
    assert(ffn_fe100_punt_decode(wire,n,&scope,&result)==1);
    assert(result.frame==wire+56 && result.length==141 && result.flow_id==13);
    assert(result.front==23 && result.in_lif==23 && result.zone==4094 && result.message==8);
    for(size_t i=0;i<n;i++)assert(!ffn_fe100_punt_decode(wire,i,&scope,&result));
    assert(!ffn_fe100_punt_decode(wire,n+1,&scope,&result));
    const unsigned invalid_offsets[]={0,2,4,7,8,9,11,30,31,32,33,34,36,40,44,48,50,52,54,55,68,70,77,79,82,86,90};
    for(size_t i=0;i<sizeof(invalid_offsets)/sizeof(invalid_offsets[0]);i++) {
        memcpy(copy,wire,n);copy[invalid_offsets[i]]^=0x80;
        assert(!ffn_fe100_punt_decode(copy,n,&scope,&result));
        assert(!result.frame && !result.length);
    }
    for(unsigned type=0;type<256;type++) {
        if(type==8)continue;
        memcpy(copy,wire,n);copy[11]=(uint8_t)type;
        assert(!ffn_fe100_punt_decode(copy,n,&scope,&result));
    }
    scope.front=24;assert(!ffn_fe100_punt_decode(wire,n,&scope,&result));scope.front=23;
    scope.zone=1;assert(!ffn_fe100_punt_decode(wire,n,&scope,&result));scope.zone=4094;
    scope.in_lif=24;assert(!ffn_fe100_punt_decode(wire,n,&scope,&result));
    assert(!ffn_fe100_punt_decode(NULL,n,&scope,&result));
    assert(!ffn_fe100_punt_decode(wire,n,NULL,&result));
    assert(!ffn_fe100_punt_decode(wire,n,&scope,NULL));
    scope.in_lif=23;
    const char *exceptions[]={ttl_sample,fragment_sample};
    for(unsigned k=0;k<2;k++) {
        n=strlen(exceptions[k])/2;
        for(size_t i=0;i<n;i++){unsigned v;assert(sscanf(exceptions[k]+2*i,"%2x",&v)==1);wire[i]=(uint8_t)v;}
        assert(ffn_fe100_punt_decode(wire,n,&scope,&result));
        assert(result.frame==wire+40 && result.length==141 && result.message==(k?4:5));
        for(size_t i=0;i<n;i++)assert(!ffn_fe100_punt_decode(wire,i,&scope,&result));
        wire[31]=0;assert(!ffn_fe100_punt_decode(wire,n,&scope,&result));
    }
    const char *udp_samples[]={udp_miss_sample,udp_ttl_expired_sample,udp_fragment_sample,udp_mtu_exceeded_sample};
    for(unsigned k=0;k<4;k++) {
        n=strlen(udp_samples[k])/2;
        for(size_t i=0;i<n;i++){unsigned v;assert(sscanf(udp_samples[k]+2*i,"%2x",&v)==1);wire[i]=(uint8_t)v;}
        assert(ffn_fe100_punt_decode(wire,n,&scope,&result));
        assert(result.length==129);
        for(size_t i=0;i<n;i++)assert(!ffn_fe100_punt_decode(wire,i,&scope,&result));
    }
    puts("FE100 native punt decoder: real wire fixture, truncation, scope and type rejection passed");
    return 0;
}
