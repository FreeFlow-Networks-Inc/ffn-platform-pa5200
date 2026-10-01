/* SPDX-License-Identifier: GPL-2.0-or-later
 * Bounded, receive-only commissioning through the REAL native packet owner.
 * Its TAP and TX endpoints are isolated local sockets: nothing is forwarded.
 */
#define _GNU_SOURCE
#include "ffn_packet.h"
#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
static uint64_t now(void) {
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t))exit(1);
    return (uint64_t)t.tv_sec*1000+t.tv_nsec/1000000;
}
static unsigned number(const char *s,unsigned max) {
    char *end;errno=0;unsigned long n=strtoul(s,&end,10);
    if(errno || !*s || *end || n>max)exit(2);
    return (unsigned)n;
}
static int control_probe(const char *wire,const struct ffn_packet_fe100_binding *b,
    const uint8_t *token,unsigned duration)
{
    struct ffn_aggregate_member member={b->scope.front,b->source,0};
    uint32_t source=b->source;
    int fd=ffn_aggregate_control_open(wire,&source,1),failed=0;
    uint64_t deadline=now()+duration;
    if(fd<0 || ffn_aggregate_control_fe100(fd,&member,1,b,1,deadline)) {perror("control attach");return 1;}
    puts("{\"ready\":true}");fflush(stdout);
    printf("{\"packets\":[");unsigned count=0;
    while(now()<deadline) {
        struct pollfd p={.fd=fd,.events=POLLIN};
        if(poll(&p,1,5)<0){if(errno==EINTR)continue;failed=1;break;}
        for(unsigned burst=0;burst<64;burst++) {
            uint8_t frame[2048];
            int n=ffn_aggregate_control_receive(fd,&member,1,b,1,deadline,frame,sizeof(frame));
            if(n<0){if(errno==EAGAIN || errno==EWOULDBLOCK)break;failed=1;break;}
            if(!memmem(frame,(size_t)n,token,16))continue;
            if(count==256){failed=1;break;}
            if(count)putchar(',');
            putchar('"');for(int i=4;i<n;i++)printf("%02x",frame[i]);putchar('"');count++;
        }
        if(failed)break;
    }
    struct tpacket_stats stats={0};socklen_t length=sizeof(stats);
    if(getsockopt(fd,SOL_PACKET,PACKET_STATISTICS,&stats,&length))failed=1;
    printf("],\"captured\":%u,\"capture_drops\":%u,\"failed\":%s}\n",count,stats.tp_drops,failed?"true":"false");
    close(fd);return failed || stats.tp_drops;
}
int main(int argc,char **argv) {
    int control=argc==11 && !strcmp(argv[10],"--control");
    if(argc!=10 && !control) {
        fprintf(stderr,"usage: punt-probe INTERFACE TRUNK RETURN FRONT LIF ZONE SOURCE NONCE_HEX MILLISECONDS [--control]\n");return 2;
    }
    struct ffn_packet_fe100_binding b={
        .scope={number(argv[2],65535),number(argv[3],65535),number(argv[4],24),
                number(argv[5],65535),number(argv[6],65535)},.source=number(argv[7],65535)};
    unsigned char token[16];
    if(strlen(argv[8])!=32)return 2;
    for(unsigned i=0;i<16;i++) {
        char h[3]={argv[8][2*i],argv[8][2*i+1],0};unsigned v;
        if(strspn(h,"0123456789abcdefABCDEF")!=2 || sscanf(h,"%x",&v)!=1)return 2;
        token[i]=(unsigned char)v;
    }
    unsigned duration=number(argv[9],2800);
    if(duration<100)return 2;
    if(control)return control_probe(argv[1],&b,token,duration);
    unsigned index=if_nametoindex(argv[1]);if(!index)return 1;
    int fd=socket(AF_PACKET,SOCK_RAW|SOCK_NONBLOCK|SOCK_CLOEXEC,htons(3)),tx[2],tap[2];
    struct sockaddr_ll addr={.sll_family=AF_PACKET,.sll_ifindex=(int)index,.sll_protocol=htons(3)};
    if(fd<0 || bind(fd,(void*)&addr,sizeof(addr)) ||
       socketpair(AF_UNIX,SOCK_DGRAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0,tx) ||
       socketpair(AF_UNIX,SOCK_DGRAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0,tap)) {perror("socket");return 1;}
    struct ffn_packet *p=ffn_packet_adopt(fd,tx[0],tap[0],b.source);
    uint64_t deadline=now()+duration;
    if(!p || ffn_packet_fe100(p,&b,1,deadline)) {perror("attach");return 1;}
    puts("{\"ready\":true}");fflush(stdout);
    printf("{\"packets\":[");unsigned count=0;int failed=0;
    while(now()<deadline) {
        if(ffn_packet_poll(p,5)) {perror("native poll");failed=1;break;}
        for(unsigned burst=0;burst<64;burst++) {
            unsigned char frame[2048];ssize_t n=recv(tap[1],frame,sizeof(frame),MSG_TRUNC);
            if(n<0) {if(errno==EAGAIN)break;failed=1;break;}
            if((size_t)n>sizeof(frame)){failed=1;break;}
            if(!memmem(frame,(size_t)n,token,sizeof(token)))continue;
            if(count==256){failed=1;break;}
            if(count)putchar(',');
            putchar('"');
            for(ssize_t i=0;i<n;i++)printf("%02x",frame[i]);
            putchar('"');count++;
        }
        if(failed)break;
    }
    uint64_t stats[3],counters[16];
    if(ffn_packet_fe100_stats(p,stats,3) || ffn_packet_counters(p,counters,16))failed=1;
    printf("],\"captured\":%u,\"decoded\":%llu,\"rejected\":%llu,\"expired\":%llu,\"rx\":%llu,\"failed\":%s}\n",
        count,(unsigned long long)stats[0],(unsigned long long)stats[1],(unsigned long long)stats[2],
        (unsigned long long)counters[0],failed?"true":"false");
    ffn_packet_close(p);close(fd);close(tx[0]);close(tx[1]);close(tap[0]);close(tap[1]);return failed;
}
