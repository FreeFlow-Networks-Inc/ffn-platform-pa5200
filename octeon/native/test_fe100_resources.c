/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Compile-time driver harness: no vendor library, device mapping or MMIO. */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
static int fake_lstat(const char *,struct stat *);
static int fake_fstat(int,struct stat *);
static int fake_flock(int,int);
static int fake_dup(int);
static void *fake_dlopen(const char *,int);
static void *fake_dlsym(void *,const char *);
#define FFN_RESOURCE_UNIT_TEST
#define lstat fake_lstat
#define fstat fake_fstat
#define flock fake_flock
#define dup fake_dup
#define dlopen fake_dlopen
#define dlsym fake_dlsym
#include "ffn_fe100_resources.c"
#undef lstat
#undef fstat
#undef flock
#undef dup
#undef dlopen
#undef dlsym

static int calls, armed, watchdog_on, watchdog_off, wrong_lock, busy_lock;
static int native_rc, fault_on_call, loads, maps, no_symbol;
static unsigned faults;
static uint8_t contents[2][16], present[2];
static int fake_lstat(const char *path,struct stat *s)
{
    assert(!strcmp(path,"/run/ffn-fe100-tables.lock"));
    *s=(struct stat){.st_mode=S_IFREG|0600,.st_dev=1,.st_ino=2};return 0;
}
static int fake_fstat(int fd,struct stat *s)
{
    assert(fd==7 || fd==8);
    *s=(struct stat){.st_mode=S_IFREG|0600,.st_dev=1,.st_ino=fd==7?(wrong_lock?3:2):4};return 0;
}
static int fake_flock(int fd,int mode)
{
    assert(fd==7 && mode==(LOCK_EX|LOCK_NB));
    if(busy_lock) {errno=EWOULDBLOCK;return -1;}return 0;
}
static int fake_dup(int fd) {assert(fd==7);return 9;}
static void *fake_dlopen(const char *path,int mode)
{
    loads++;
    if(!strcmp(path,"/usr/local/lib/ffn/libffn-fe100-flow-memory.so")) {
        assert(mode==(RTLD_GLOBAL|RTLD_NOW));return (void *)1;
    }
    assert(!strcmp(path,"/proc/self/fd/8"));
    assert(mode==(RTLD_LOCAL|RTLD_LAZY));return (void *)2;
}
static int select_block(uint32_t block) {assert(block==0x50000);return 0;}
static int map(uint64_t bar,const char *trace,int writes)
{assert(bar==0x100000 && trace && writes==1);maps++;return 0;}
static int allow(uint32_t address) {assert(address==0x50000);return 0;}
static unsigned get_faults(void) {return faults;}
static void watchdog(unsigned value)
{
    assert(value==0 || value==10);
    if(value) {assert(!armed);armed=1;watchdog_on++;}
    else {assert(armed);armed=0;watchdog_off++;}
}
static int access_entry(unsigned op,uint32_t dev,void *raw,int index,size_t size)
{
    assert(dev==0 && (index==30 || index==31) && armed);
    if(raw) assert((uintptr_t)raw%8==0);
    calls++;unsigned slot=(unsigned)index-30;
    if(fault_on_call)faults++;
    if(op==FFN_RESOURCE_INSERT) {memcpy(contents[slot],raw,size);present[slot]=1;}
    else if(op==FFN_RESOURCE_DELETE)present[slot]=0;
    else if(present[slot])memcpy(raw,contents[slot],size);
    if(native_rc)return native_rc;
    return op==FFN_RESOURCE_FETCH && !present[slot]?3:0;
}
static int smac_get(uint32_t d,void *v,int i) {return access_entry(1,d,v,i,8);}
static int smac_put(uint32_t d,void *v,int i) {return access_entry(2,d,v,i,8);}
static int smac_del(uint32_t d,int i) {return access_entry(3,d,NULL,i,8);}
static int hop_get(uint32_t d,void *v,int t,int i) {assert(t==0);return access_entry(1,d,v,i,16);}
static int hop_put(uint32_t d,void *v,int t,int i) {assert(t==0);return access_entry(2,d,v,i,16);}
static int hop_del(uint32_t d,int t,int i) {assert(t==0);return access_entry(3,d,NULL,i,16);}
static void *fake_dlsym(void *handle,const char *name)
{
    if(no_symbol)return NULL;
#define SYM(h,n,f) if(!strcmp(name,n)) {assert(handle==(void *)(h));return (void *)(f);}
    SYM(1,"ffn_fe100_select_block",select_block)
    SYM(1,"ffn_fe100_open",map)
    SYM(1,"ffn_fe100_allow",allow)
    SYM(1,"ffn_fe100_faults",get_faults)
    SYM(1,"ffn_flow_watchdog",watchdog)
    SYM(2,"pan_fe100_fetch_smac_entry",smac_get)
    SYM(2,"pan_fe100_insert_smac_entry",smac_put)
    SYM(2,"pan_fe100_delete_smac_entry",smac_del)
    SYM(2,"pan_fe100_fetch_nexthop_entry",hop_get)
    SYM(2,"pan_fe100_insert_nexthop_entry",hop_put)
    SYM(2,"pan_fe100_delete_nexthop_entry",hop_del)
#undef SYM
    assert(!"unexpected ABI symbol");return NULL;
}
static void reset(void)
{
    memset(&state,0,sizeof(state));memset(contents,0,sizeof(contents));memset(present,0,sizeof(present));
    calls=armed=watchdog_on=watchdog_off=wrong_lock=busy_lock=native_rc=fault_on_call=loads=maps=no_symbol=0;
    faults=0;
}
static int start(unsigned kind)
{
    uint32_t pool[]={30,31},regs[]={0x50000};
    return ffn_fe100_resources_open(7,8,0x100000,kind,pool,2,regs,1,"/var/lib/ffn/fe100/resource-test.txt");
}
int main(void)
{
    assert(ffn_fe100_resources_abi()==1);
    for(unsigned kind=1;kind<=2;kind++) {
        size_t size=kind==1?8:16;
        uint8_t data[16];reset();assert(start(kind)==0);
        assert(loads==2 && maps==1 && start(kind)==-EALREADY);
        assert(ffn_fe100_resources_call(1,30,data,size)==3);
        for(size_t i=0;i<size;i++)assert(data[i]==0);
        memset(data,0xa5,size);assert(ffn_fe100_resources_call(2,30,data,size)==0);
        memset(data,0,size);assert(ffn_fe100_resources_call(1,30,data,size)==0);
        for(size_t i=0;i<size;i++)assert(data[i]==0xa5);
        int previous=calls;
        assert(ffn_fe100_resources_call(1,29,data,size)==-EPERM);
        assert(ffn_fe100_resources_call(1,30,data,size-1)==-EINVAL);
        assert(ffn_fe100_resources_call(1,30,NULL,size)==-EINVAL);
        assert(ffn_fe100_resources_call(4,30,data,size)==-EINVAL);
        assert(calls==previous);
        native_rc=12;assert(ffn_fe100_resources_call(2,31,data,size)==12);
        native_rc=0;assert(ffn_fe100_resources_call(3,31,data,size)==-EUCLEAN);
        assert(ffn_fe100_resources_call(1,30,data,size)==0);
        assert(ffn_fe100_resources_call(2,31,data,size)==-EUCLEAN);
        assert(ffn_fe100_resources_call(1,31,data,size)==0);
        assert(ffn_fe100_resources_call(3,31,data,size)==0);
        assert(ffn_fe100_resources_call(1,31,data,size)==3);
        /* NOTFOUND never acknowledges an attempted mutation. */
        native_rc=3;assert(ffn_fe100_resources_call(2,31,data,size)==3);native_rc=0;
        assert(ffn_fe100_resources_call(3,31,data,size)==-EUCLEAN);
        assert(ffn_fe100_resources_call(1,31,data,size)==0);
        fault_on_call=1;assert(ffn_fe100_resources_call(1,30,data,size)==-EIO);
        previous=calls;fault_on_call=0;faults=0;
        assert(ffn_fe100_resources_call(1,30,data,size)==-EIO && calls==previous);
        assert(!armed && watchdog_on==watchdog_off && watchdog_on==calls);
    }
    reset();wrong_lock=1;assert(start(1)==-EPERM && !loads && !maps);
    reset();busy_lock=1;assert(start(1)==-EWOULDBLOCK && !loads && !maps);
    reset();no_symbol=1;assert(start(1)==-ELIBBAD && !maps);
    assert(start(1)==-EALREADY);
    reset();uint32_t pool[]={30,30},regs[]={0x50000};
    assert(ffn_fe100_resources_open(7,8,0x100000,1,pool,2,regs,1,"/var/lib/ffn/fe100/resource-test.txt")==-EINVAL);
    pool[1]=1024;
    assert(ffn_fe100_resources_open(7,8,0x100000,1,pool,2,regs,1,"/var/lib/ffn/fe100/resource-test.txt")==-EINVAL);
    pool[1]=31;regs[0]++;
    assert(ffn_fe100_resources_open(7,8,0x100000,1,pool,2,regs,1,"/var/lib/ffn/fe100/resource-test.txt")==-EINVAL);
    assert(!loads && !maps);
    puts("FE100 resource driver: bounded ABI, ownership, uncertainty and watchdog tests passed");
    return 0;
}
