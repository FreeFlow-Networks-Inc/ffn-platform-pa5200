// SPDX-License-Identifier: Apache-2.0
/* Bounded native commissioning I/O. No forwarding loop or hardware writes.
 * Manifest: repeated BE16 BCM destination, BE16 Ethernet length, frame bytes.
 * Capture is limited to a 16-byte per-run nonce and excludes outgoing copies.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_packet.h>
#include <linux/if_ether.h>
#include <net/if.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_FRAME 9216
#define MAX_TX 64
#define MAX_CAPTURE 256
struct packet { size_t length; unsigned char bytes[MAX_FRAME+12]; };
static struct packet packets[MAX_TX];
static unsigned be16(const unsigned char *p) { return ((unsigned)p[0]<<8)|p[1]; }
static long long milliseconds(void) {
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t)) { perror("clock"); exit(1); }
    return (long long)t.tv_sec*1000+t.tv_nsec/1000000;
}
static size_t encode(unsigned port,const unsigned char *frame,size_t length,unsigned char *out) {
    if(port>255 || length<14 || length>MAX_FRAME)return 0;
    size_t padded=length<60?60:length;
    memset(out,0,padded+12);
    out[0]=1;out[1]=(unsigned char)(port>>8);out[2]=(unsigned char)port;
    memcpy(out+4,frame,12);memcpy(out+24,frame+12,length-12);
    return padded+12;
}
static int selftest(void) {
    unsigned char f[128],p[140];memset(f,0xa5,sizeof(f));
    if(encode(34,f,14,p)!=72 || p[0]!=1 || p[2]!=34 ||
       memcmp(p+4,f,12) || memcmp(p+24,f+12,2))return 1;
    for(unsigned i=16;i<24;i++)if(p[i])return 1;
    for(unsigned i=26;i<72;i++)if(p[i])return 1;
    if(encode(256,f,64,p) || encode(1,f,13,p))return 1;
    if(encode(35,f,128,p)!=140 || memcmp(p+24,f+12,116))return 1;
    puts("native packet envelope selftest passed");return 0;
}
int main(int argc,char **argv) {
    if(argc==2 && !strcmp(argv[1],"--selftest"))return selftest();
    if(argc!=5) { fprintf(stderr,"usage: probe INTERFACE MANIFEST NONCE_HEX MILLISECONDS\n");return 2; }
    unsigned char token[16];
    if(strlen(argv[3])!=32)return 2;
    for(unsigned i=0;i<16;i++) {
        unsigned v;char h[3]={argv[3][i*2],argv[3][i*2+1],0};
        if(strspn(h,"0123456789abcdefABCDEF")!=2 || sscanf(h,"%x",&v)!=1)return 2;
        token[i]=(unsigned char)v;
    }
    char *end;errno=0;long duration=strtol(argv[4],&end,10);
    if(errno || *end || duration<100 || duration>10000)return 2;
    FILE *input=fopen(argv[2],"rb");if(!input){perror("manifest");return 1;}
    unsigned count=0;unsigned char header[4],frame[MAX_FRAME];size_t n;
    while((n=fread(header,1,4,input))!=0) {
        if(n!=4){fprintf(stderr,"truncated manifest header\n");fclose(input);return 2;}
        unsigned length=be16(header+2),port=be16(header);
        if(count==MAX_TX || length<14 || length>MAX_FRAME || port>255 ||
           fread(frame,1,length,input)!=length || !memmem(frame,length,token,sizeof(token))) {
            fprintf(stderr,"invalid scoped manifest\n");fclose(input);return 2;
        }
        packets[count].length=encode(port,frame,length,packets[count].bytes);count++;
    }
    if(ferror(input)){perror("manifest read");fclose(input);return 1;}fclose(input);
    unsigned index=if_nametoindex(argv[1]);if(!index){perror("interface");return 1;}
    int fd=socket(AF_PACKET,SOCK_RAW|SOCK_NONBLOCK|SOCK_CLOEXEC,htons(ETH_P_ALL));
    if(fd<0){perror("socket");return 1;}
    int buffer=8*1024*1024;
    if(setsockopt(fd,SOL_SOCKET,SO_RCVBUFFORCE,&buffer,sizeof(buffer))) {
        perror("capture buffer");close(fd);return 1;
    }
    struct sockaddr_ll address={.sll_family=AF_PACKET,.sll_ifindex=(int)index,.sll_protocol=htons(ETH_P_ALL)};
    if(bind(fd,(void*)&address,sizeof(address))) {perror("bind");close(fd);return 1;}
    struct packet_mreq membership={.mr_ifindex=(int)index,.mr_type=PACKET_MR_PROMISC};
    if(setsockopt(fd,SOL_PACKET,PACKET_ADD_MEMBERSHIP,&membership,sizeof(membership))) {
        perror("capture membership");close(fd);return 1;
    }
    puts("{\"ready\":true}");fflush(stdout);
    unsigned sent=0,captured=0;int failed=0;
    long long deadline=milliseconds()+duration,next_send=milliseconds();
    printf("{\"packets\":[");
    while(milliseconds()<deadline) {
        if(sent<count && milliseconds()>=next_send) {
            ssize_t result=send(fd,packets[sent].bytes,packets[sent].length,MSG_NOSIGNAL);
            if(result!=(ssize_t)packets[sent].length) {perror("send");failed=1;break;}
            sent++;next_send=milliseconds()+2;
        }
        struct pollfd pollfd={.fd=fd,.events=POLLIN};
        int rv=poll(&pollfd,1,2);
        if(rv<0) {if(errno==EINTR)continue;perror("poll");failed=1;break;}
        if(pollfd.revents&(POLLERR|POLLHUP|POLLNVAL)){failed=1;break;}
        for(unsigned burst=0;rv>0 && burst<64;burst++) {
            unsigned char raw[65536];struct sockaddr_ll sender;socklen_t size=sizeof(sender);
            ssize_t length=recvfrom(fd,raw,sizeof(raw),0,(void*)&sender,&size);
            if(length<0) {if(errno==EAGAIN || errno==EWOULDBLOCK)break;perror("receive");failed=1;break;}
            if(sender.sll_pkttype==PACKET_OUTGOING || !memmem(raw,(size_t)length,token,sizeof(token)))continue;
            if(captured==MAX_CAPTURE){failed=1;break;}
            if(captured)putchar(',');
            putchar('"');
            for(ssize_t i=0;i<length;i++)printf("%02x",raw[i]);
            putchar('"');captured++;
        }
        if(failed)break;
    }
    struct tpacket_stats stats={0};socklen_t size=sizeof(stats);
    if(getsockopt(fd,SOL_PACKET,PACKET_STATISTICS,&stats,&size))failed=1;
    printf("],\"sent\":%u,\"captured\":%u,\"capture_drops\":%u,\"failed\":%s}\n",
           sent,captured,stats.tp_drops,failed?"true":"false");
    close(fd);return failed || sent!=count || stats.tp_drops ? 1:0;
}
