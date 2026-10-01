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

int ffn_fe100_resources_open(int lock_fd, int owner_fd, uint64_t bar,
    unsigned kind, const uint32_t *pool, size_t count,
    const uint32_t *registers, size_t register_count, const char *trace)
{
    struct stat expected, locked, library;
    uint16_t endian=1;
    char owner_path[64];
    int (*select_block)(uint32_t);
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
    if((kind!=FFN_RESOURCE_SMAC && kind!=FFN_RESOURCE_NEXTHOP) ||
       !pool || !count || count>MAX_POOL || !registers || !register_count ||
       register_count>0x40000 || !bar || (bar&4095) || !trace ||
       strncmp(trace,"/var/lib/ffn/fe100/resource-",strlen("/var/lib/ffn/fe100/resource-")) || strstr(trace,".."))
        return -EINVAL;
    for(size_t i=0;i<count;i++) {
        if(pool[i]>=(kind==FFN_RESOURCE_SMAC?1024U:65536U)) return -EINVAL;
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
    if(select_block(0x50000) || map(bar,trace,1)) return -EIO;
    for(size_t i=0;i<register_count;i++) if(allow(registers[i])) return -EIO;
    /* Use the exact file that the controller hashed, not a reopened pathname. */
    snprintf(owner_path,sizeof(owner_path),"/proc/self/fd/%d",owner_fd);
    state.owner=dlopen(owner_path,RTLD_LOCAL|RTLD_LAZY);
    if(!state.owner) return -ELIBACC;
    if(kind==FFN_RESOURCE_SMAC) {
        SYMBOL(state.owner,state.smac_get,"pan_fe100_fetch_smac_entry");
        SYMBOL(state.owner,state.smac_put,"pan_fe100_insert_smac_entry");
        SYMBOL(state.owner,state.smac_del,"pan_fe100_delete_smac_entry");
    } else {
        SYMBOL(state.owner,state.hop_get,"pan_fe100_fetch_nexthop_entry");
        SYMBOL(state.owner,state.hop_put,"pan_fe100_insert_nexthop_entry");
        SYMBOL(state.owner,state.hop_del,"pan_fe100_delete_nexthop_entry");
    }
#undef SYMBOL
    if(state.faults()) return -EIO;
    state.ready=1;
    return 0;
}

int ffn_fe100_resources_call(unsigned operation,uint32_t index,uint8_t *data,size_t size)
{
    /* Explicit alignment for the owner's packed entry access on MIPS64. */
    union { uint64_t align[2]; uint8_t bytes[16]; } entry={{0,0}};
    size_t slot;
    int rc;
    if(!state.ready || state.poisoned) return -EIO;
    if(!data || size!=(state.kind==FFN_RESOURCE_SMAC?8U:16U) ||
       operation<FFN_RESOURCE_FETCH || operation>FFN_RESOURCE_DELETE) return -EINVAL;
    for(slot=0;slot<state.count;slot++) if(state.pool[slot]==index) break;
    if(slot==state.count) return -EPERM;
    if(state.uncertain[slot] && operation!=FFN_RESOURCE_FETCH) return -EUCLEAN;
    if(state.faults()) { state.poisoned=1;return -EIO; }
    if(operation==FFN_RESOURCE_INSERT) memcpy(entry.bytes,data,size);
    state.uncertain[slot]=1;
    state.watchdog(10);
    if(state.kind==FFN_RESOURCE_SMAC) {
        if(operation==FFN_RESOURCE_FETCH) rc=state.smac_get(0,entry.bytes,(int)index);
        else if(operation==FFN_RESOURCE_INSERT) rc=state.smac_put(0,entry.bytes,(int)index);
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
