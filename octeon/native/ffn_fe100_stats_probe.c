/* SPDX-License-Identifier: GPL-2.0-or-later
 * Bounded, receive-only commissioning observer. Never installs or refreshes a
 * session. JSON records are control telemetry; packet parsing stays in C.
 */
#define _GNU_SOURCE
#include "ffn_fe100_stats.h"
#include <arpa/inet.h>
#include <errno.h>
#include <linux/filter.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
static uint64_t ms(void) {
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t)){perror("clock");exit(1);}
    return (uint64_t)t.tv_sec*1000+(unsigned)t.tv_nsec/1000000;
}
static unsigned number(const char *s,unsigned low,unsigned high) {
    char *end;errno=0;unsigned long n=strtoul(s,&end,10);
    if(errno || !*s || *end || n<low || n>high)exit(2);
    return (unsigned)n;
}
int main(int argc,char **argv) {
    if(argc!=5){fprintf(stderr,"usage: stats-probe INTERFACE TRUNK RETURN MILLISECONDS\n");return 2;}
    unsigned trunk=number(argv[2],1,255),port=number(argv[3],1,255);
    unsigned duration=number(argv[4],100,180000),index=if_nametoindex(argv[1]);
    if(trunk==port || !index)return 2;
    int fd=socket(AF_PACKET,SOCK_RAW|SOCK_NONBLOCK|SOCK_CLOEXEC,htons(ETH_P_ALL));
    if(fd<0){perror("socket");return 1;}
    struct sock_filter ins[]={
        BPF_STMT(BPF_LD|BPF_H|BPF_ABS,0),BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,trunk,0,5),
        BPF_STMT(BPF_LD|BPF_H|BPF_ABS,2),BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,port,0,3),
        BPF_STMT(BPF_LD|BPF_B|BPF_ABS,11),BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,19,0,1),
        BPF_STMT(BPF_RET|BPF_K,65535),BPF_STMT(BPF_RET|BPF_K,0)};
    struct sock_fprog filter={.len=sizeof(ins)/sizeof(ins[0]),.filter=ins};
    int buffer=8*1024*1024;
    struct sockaddr_ll bind_address={.sll_family=AF_PACKET,.sll_protocol=htons(ETH_P_ALL),.sll_ifindex=(int)index};
    struct packet_mreq member={.mr_ifindex=(int)index,.mr_type=PACKET_MR_PROMISC};
    if(setsockopt(fd,SOL_SOCKET,SO_RCVBUFFORCE,&buffer,sizeof(buffer)) ||
       setsockopt(fd,SOL_SOCKET,SO_ATTACH_FILTER,&filter,sizeof(filter)) ||
       bind(fd,(void*)&bind_address,sizeof(bind_address)) ||
       setsockopt(fd,SOL_PACKET,PACKET_ADD_MEMBERSHIP,&member,sizeof(member))) {
        perror("capture setup");close(fd);return 1;
    }
    uint64_t start=ms(),deadline=start+duration,sequence=0,total_records=0;
    unsigned malformed=0;int failed=0;
    puts("{\"ready\":true}");fflush(stdout);
    while(ms()<deadline) {
        struct pollfd p={.fd=fd,.events=POLLIN};
        int ready=poll(&p,1,100);
        if(ready<0){if(errno==EINTR)continue;failed=1;break;}
        if(p.revents&(POLLERR|POLLHUP|POLLNVAL)){failed=1;break;}
        if(!(p.revents&POLLIN))continue;
        uint8_t wire[32+FFN_FE100_STATS_MAX*8];struct sockaddr_ll from;socklen_t from_size=sizeof(from);
        ssize_t n=recvfrom(fd,wire,sizeof(wire),MSG_TRUNC,(void*)&from,&from_size);
        if(n<0){if(errno==EAGAIN || errno==EINTR)continue;failed=1;break;}
        if(from.sll_pkttype==PACKET_OUTGOING)continue;
        struct ffn_fe100_counters counters;
        if((size_t)n>sizeof(wire) || ffn_fe100_counters_decode(wire,(size_t)n,trunk,port,&counters)!=1) {
            malformed++;continue;
        }
        printf("{\"sequence\":%llu,\"elapsed_ms\":%llu,\"records\":[",
            (unsigned long long)++sequence,(unsigned long long)(ms()-start));
        for(unsigned i=0;i<counters.count;i++) {
            const struct ffn_fe100_counter *c=&counters.records[i];
            printf("%s{\"flow_id\":%u,\"packets\":%u,\"octets\":%u,\"reason\":%u}",
                i?",":"",c->flow_id,c->packets,c->octets,c->reason);
        }
        total_records+=counters.count;puts("]}");fflush(stdout);
    }
    struct tpacket_stats stats={0};socklen_t stats_size=sizeof(stats);
    if(getsockopt(fd,SOL_PACKET,PACKET_STATISTICS,&stats,&stats_size))failed=1;
    close(fd);
    printf("{\"messages\":%llu,\"records\":%llu,\"malformed\":%u,\"capture_drops\":%u,\"failed\":%s}\n",
        (unsigned long long)sequence,(unsigned long long)total_records,malformed,stats.tp_drops,failed?"true":"false");
    return failed || malformed || stats.tp_drops ? 1 : 0;
}
