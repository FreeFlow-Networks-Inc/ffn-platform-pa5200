#include <assert.h>
#include <errno.h>
#include <linux/i2c.h>
#include <linux/i2c-dev.h>
#include <stdarg.h>
#include <stdint.h>
#include <string.h>
int ffn_i2c_read(unsigned,unsigned,unsigned,unsigned,uint8_t *,unsigned);
int ffn_i2c_write8(unsigned,unsigned,unsigned,unsigned);
static unsigned transfers,closed,expected_width,offset;
static int result_override=99;
int __wrap_open(const char *name,int flags,...)
{
    (void)flags;assert(!strcmp(name,"/dev/i2c-17"));return 77;
}
int __wrap_close(int fd) { assert(fd==77);closed++;return 0; }
int __wrap_ioctl(int fd,unsigned long request,...)
{
    va_list ap;unsigned i;
    assert(fd==77 && request==I2C_RDWR);
    va_start(ap,request);struct i2c_rdwr_ioctl_data *r=va_arg(ap,struct i2c_rdwr_ioctl_data *);va_end(ap);
    transfers++;
    if(result_override!=99) {errno=ENXIO;return result_override;}
    assert(r->nmsgs==(expected_width?2u:1u));
    for(i=0;i<r->nmsgs;i++) {
        struct i2c_msg *m=&r->msgs[i];assert(m->addr==0x50);
        if(m->flags&I2C_M_RD) {unsigned j;for(j=0;j<m->len;j++)m->buf[j]=(uint8_t)(0x80+j);}
        else {
            assert(m->len==expected_width);
            if(expected_width==2)assert(m->buf[0]==(offset>>8));
            assert(m->buf[expected_width-1]==(offset&255));
        }
    }
    return (int)r->nmsgs;
}
int main(void)
{
    uint8_t out[16];
    for(expected_width=0;expected_width<=2;expected_width++) {
        offset=expected_width==2?0x1234:expected_width?0x34:0;
        assert(!ffn_i2c_read(17,0x50,offset,expected_width,out,sizeof(out)));
        assert(out[0]==0x80 && out[15]==0x8f);
    }
    result_override=0;assert(ffn_i2c_read(17,0x50,0,1,out,1)==-1 && errno==EIO);
    result_override=-1;assert(ffn_i2c_read(17,0x50,0,1,out,1)==-1 && errno==ENXIO);
    result_override=1;assert(!ffn_i2c_write8(17,0x50,2,0xfe));
    assert(closed==transfers && transfers==6);
    assert(ffn_i2c_read(17,0x50,256,1,out,1)==-1);
    assert(ffn_i2c_read(17,0x50,0,1,out,4097)==-1);
    assert(ffn_i2c_write8(17,0x50,0,256)==-1);
    assert(ffn_i2c_write8(17,0xff,0,0)==-1);
    assert(transfers==6);
    return 0;
}
