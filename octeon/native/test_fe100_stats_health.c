/* SPDX-License-Identifier: GPL-2.0-or-later
 * Inject kernel counter loss/error into the real observer's health boundary.
 */
#define main stats_probe_main
#include "ffn_fe100_stats_probe.c"
#undef main
#include <assert.h>
static unsigned lost;
static int broken, short_reply;
int __wrap_getsockopt(int fd,int level,int option,void *value,socklen_t *size)
{
    assert(fd==17 && level==SOL_PACKET && option==PACKET_STATISTICS);
    assert(*size==sizeof(struct tpacket_stats));
    if(broken){errno=EIO;return -1;}
    ((struct tpacket_stats *)value)->tp_drops=lost;
    lost=0; /* Linux clears its counters on read. */
    if(short_reply)*size=0;
    return 0;
}
int main(void)
{
    uint64_t drops=0;
    assert(capture_health(17,&drops)==0 && drops==0);
    lost=3;
    assert(capture_health(17,&drops)==-1 && drops==3);
    assert(capture_health(17,&drops)==-1 && drops==3);
    lost=2;
    assert(capture_health(17,&drops)==-1 && drops==5);
    drops=0;broken=1;
    assert(capture_health(17,&drops)==-1);
    broken=0;short_reply=1;
    assert(capture_health(17,&drops)==-1);
    puts("FE100 observer: loss latches across read/reset, read errors fence");
    return 0;
}
