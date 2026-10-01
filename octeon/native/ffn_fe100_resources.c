/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _GNU_SOURCE
#include "ffn_fe100_resources.h"
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#define MAX_POOL 4096
static struct {
    unsigned kind;
    uint32_t pool[MAX_POOL];
    uint8_t uncertain[MAX_POOL];
    size_t count;
    int attempted, ready, poisoned, lock;
    void *shim, *owner;
    unsigned (*faults)(void);
    void (*watchdog)(unsigned);
    int (*smac_get)(uint32_t,void *,int);
    int (*smac_put)(uint32_t,void *,int);
    int (*smac_del)(uint32_t,int);
    int (*hop_get)(uint32_t,void *,int,int);
    int (*hop_put)(uint32_t,void *,int,int);
    int (*hop_del)(uint32_t,int,int);
} state;

unsigned ffn_fe100_resources_abi(void) { return 1; }

static size_t entry_size(unsigned kind)
{
    switch(kind) {
    case FFN_RESOURCE_SMAC:return 8;
    case FFN_RESOURCE_NEXTHOP:return 16;
    case FFN_RESOURCE_LIF:return 36;
    case FFN_RESOURCE_LEF:return 10;
    case FFN_RESOURCE_QMAP4:return 84;
    default:return 0;
    }
}
static unsigned index_limit(unsigned kind)
{
    /* LIF/LEF/QMAP restricted to the reference-verified commissioning window.
     * This is deliberately not a claim about total chip table capacity. */
    return kind==FFN_RESOURCE_SMAC?1024:kind==FFN_RESOURCE_NEXTHOP?65536:32;
}
static void be32(uint8_t *p,uint32_t v)
{ for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(v>>(24-i*8)); }
static void key80(uint8_t *p,uint64_t v)
{ p[0]=p[1]=0;for(unsigned i=0;i<8;i++)p[2+i]=(uint8_t)(v>>(56-i*8)); }
int ffn_fe100_lif_encode(unsigned port,unsigned vlan,unsigned zone,
    unsigned miss_next_hop,uint8_t *out,size_t size)
{
    if(!out || size!=36 || !port || port>63 || vlan>4094 || zone>65535 || miss_next_hop>65535)
        return -EINVAL;
    memset(out,0,size);
    be32(out+4,0x80040000);be32(out+8,(zone<<16)|miss_next_hop);be32(out+12,port<<16);
    key80(out+16,((uint64_t)4095<<38)|((uint64_t)63<<32));
    key80(out+26,((uint64_t)vlan<<38)|((uint64_t)port<<32));
    return 0;
}
int ffn_fe100_lef_encode(unsigned port,uint8_t *out,size_t size)
{
    if(!out || size!=10 || !port || port>63)return -EINVAL;
    memset(out,0,size);be32(out,0x80000000|(port<<16));return 0;
}

int ffn_fe100_resources_open(int lock_fd, int owner_fd, uint64_t bar,
    unsigned kind, const uint32_t *pool, size_t count,
    const uint32_t *registers, size_t register_count, const char *trace)
{
    struct stat expected, locked, library;
    uint16_t endian=1;
    char owner_path[64];
    int (*select_block)(uint32_t);
    int (*select_lif)(unsigned);
    int (*map)(uint64_t,const char *,int);
    int (*allow)(uint32_t);
    /* Test binaries exercise the ABI on x86 as well as emulated MIPS. This
     * branch is never compiled into the installed shared object. */
#ifndef FFN_RESOURCE_UNIT_TEST
    if(sizeof(void *)!=8 || *(uint8_t *)&endian) return -ENOTSUP;
#else
    (void)endian;
#endif
    if(state.attempted) return -EALREADY;
    if(!entry_size(kind) ||
       !pool || !count || count>MAX_POOL || !registers || !register_count ||
       register_count>0x40000 || !bar || (bar&4095) || !trace ||
       strncmp(trace,"/var/lib/ffn/fe100/resource-",strlen("/var/lib/ffn/fe100/resource-")) || strstr(trace,".."))
        return -EINVAL;
    for(size_t i=0;i<count;i++) {
        if(pool[i]>=index_limit(kind)) return -EINVAL;
        for(size_t j=0;j<i;j++) if(pool[i]==pool[j]) return -EINVAL;
    }
    for(size_t i=0;i<register_count;i++)
        if((registers[i]&3) || registers[i]>=0x100000) return -EINVAL;
    if(lstat("/run/ffn-fe100-tables.lock",&expected) ||
       fstat(lock_fd,&locked) || fstat(owner_fd,&library)) return -errno;
    if(!S_ISREG(expected.st_mode) || !S_ISREG(locked.st_mode) ||
       expected.st_dev!=locked.st_dev || expected.st_ino!=locked.st_ino ||
       !S_ISREG(library.st_mode)) return -EPERM;
    if(flock(lock_fd,LOCK_EX|LOCK_NB)) return -errno;
    state.attempted=1;
    state.lock=dup(lock_fd);
    if(state.lock<0) return -errno;
    state.kind=kind;state.count=count;
    memcpy(state.pool,pool,count*sizeof(*pool));
    state.shim=dlopen("/usr/local/lib/ffn/libffn-fe100-flow-memory.so",RTLD_GLOBAL|RTLD_NOW);
    if(!state.shim) return -ELIBACC;
#define SYMBOL(handle, target, name) do { \
    *(void **)(&(target))=dlsym(handle,name); \
    if(!(target)) return -ELIBBAD; \
} while(0)
    SYMBOL(state.shim,select_block,"ffn_fe100_select_block");
    SYMBOL(state.shim,map,"ffn_fe100_open");
    SYMBOL(state.shim,allow,"ffn_fe100_allow");
    SYMBOL(state.shim,state.faults,"ffn_fe100_faults");
    SYMBOL(state.shim,state.watchdog,"ffn_flow_watchdog");
    if(select_block((kind==FFN_RESOURCE_LIF || kind==FFN_RESOURCE_QMAP4)?0x80000:kind==FFN_RESOURCE_LEF?0x58000:0x50000)) return -EIO;
    if(kind==FFN_RESOURCE_LIF || kind==FFN_RESOURCE_QMAP4) {
        SYMBOL(state.shim,select_lif,"ffn_fe100_select_lif_table");
        if(select_lif(0))return -EIO;
    }
    if(map(bar,trace,1)) return -EIO;
    for(size_t i=0;i<register_count;i++) if(allow(registers[i])) return -EIO;
    /* Use the exact file that the controller hashed, not a reopened pathname. */
    snprintf(owner_path,sizeof(owner_path),"/proc/self/fd/%d",owner_fd);
    state.owner=dlopen(owner_path,RTLD_LOCAL|RTLD_LAZY);
    if(!state.owner) return -ELIBACC;
    if(kind==FFN_RESOURCE_SMAC) {
        SYMBOL(state.owner,state.smac_get,"pan_fe100_fetch_smac_entry");
        SYMBOL(state.owner,state.smac_put,"pan_fe100_insert_smac_entry");
        SYMBOL(state.owner,state.smac_del,"pan_fe100_delete_smac_entry");
    } else if(kind==FFN_RESOURCE_NEXTHOP) {
        SYMBOL(state.owner,state.hop_get,"pan_fe100_fetch_nexthop_entry");
        SYMBOL(state.owner,state.hop_put,"pan_fe100_insert_nexthop_entry");
        SYMBOL(state.owner,state.hop_del,"pan_fe100_delete_nexthop_entry");
    } else if(kind==FFN_RESOURCE_LIF) {
        SYMBOL(state.owner,state.smac_get,"pan_fe100_fetch_lif_entry");
        SYMBOL(state.owner,state.smac_put,"pan_fe100_insert_lif_entry");
        SYMBOL(state.owner,state.smac_del,"pan_fe100_delete_lif_entry");
    } else if(kind==FFN_RESOURCE_LEF) {
        SYMBOL(state.owner,state.smac_get,"pan_fe100_fetch_lef_entry");
        SYMBOL(state.owner,state.smac_put,"pan_fe100_insert_lef_entry");
        SYMBOL(state.owner,state.smac_del,"pan_fe100_delete_lef_entry");
    } else {
        SYMBOL(state.owner,state.smac_get,"pan_fe100_fetch_qm_entry");
        SYMBOL(state.owner,state.smac_put,"pan_fe100_insert_qm_entry");
        SYMBOL(state.owner,state.hop_del,"pan_fe100_delete_qm_entry");
    }
#undef SYMBOL
    if(state.faults()) return -EIO;
    state.ready=1;
    return 0;
}

int ffn_fe100_resources_call(unsigned operation,uint32_t index,uint8_t *data,size_t size)
{
    /* Explicit alignment for the owner's packed entry access on MIPS64. */
    union { uint64_t align[11]; uint8_t bytes[88]; } entry={{0}};
    size_t slot;
    int rc;
    if(!state.ready || state.poisoned) return -EIO;
    if(!data || size!=entry_size(state.kind) ||
       operation<FFN_RESOURCE_FETCH || operation>FFN_RESOURCE_DELETE) return -EINVAL;
    if(state.kind==FFN_RESOURCE_QMAP4 && operation==FFN_RESOURCE_INSERT) {
        /* Only the verified IPv4 view. A fetched entry loses its pt selector;
         * accept that readback for restoration, but never select another table. */
        if((data[7]&3)>1)return -EINVAL;
        for(size_t i=36;i<size;i++)if(data[i])return -EINVAL;
    }
    for(slot=0;slot<state.count;slot++) if(state.pool[slot]==index) break;
    if(slot==state.count) return -EPERM;
    if(state.uncertain[slot] && operation!=FFN_RESOURCE_FETCH) return -EUCLEAN;
    if(state.faults()) { state.poisoned=1;return -EIO; }
    if(operation==FFN_RESOURCE_INSERT) memcpy(entry.bytes,data,size);
    if(state.kind==FFN_RESOURCE_QMAP4)entry.bytes[7]=(entry.bytes[7]&0xfc)|1;
    state.uncertain[slot]=1;
    state.watchdog(10);
    if(state.kind!=FFN_RESOURCE_NEXTHOP) {
        if(operation==FFN_RESOURCE_FETCH) rc=state.smac_get(0,entry.bytes,(int)index);
        else if(operation==FFN_RESOURCE_INSERT) rc=state.smac_put(0,entry.bytes,(int)index);
        else if(state.kind==FFN_RESOURCE_QMAP4)rc=state.hop_del(0,1,(int)index);
        else rc=state.smac_del(0,(int)index);
    } else {
        if(operation==FFN_RESOURCE_FETCH) rc=state.hop_get(0,entry.bytes,0,(int)index);
        else if(operation==FFN_RESOURCE_INSERT) rc=state.hop_put(0,entry.bytes,0,(int)index);
        else rc=state.hop_del(0,0,(int)index);
    }
    state.watchdog(0);
    if(state.faults()) { state.poisoned=1;return -EIO; }
    if(rc==0 || (operation==FFN_RESOURCE_FETCH && rc==3)) state.uncertain[slot]=0;
    memcpy(data,entry.bytes,size);
    return rc;
}
