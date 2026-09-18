/* Host-only regression: fake register memory, never open /dev/mem. */
#include "ffn_fe100_mmio.c"
#include <assert.h>

int main(void)
{
    static uint32_t fake[0x100000 / 4];
    block_base=0xa0000;
    regs=fake; writable=1; trace=tmpfile(); assert(trace);
    assert(ffn_fe100_allow_external_ia()==0);
    assert(fe100_reg_wr(0,0x80504,0)==0);
    assert(fe100_reg_wr(0,0x80500,0x04004401)==0); /* DDR diagnostic write */
    assert(fe100_reg_wr(0,0x80500,0x02004401)==0); /* DDR diagnostic read */
    assert(fe100_reg_wr(0,0x80504,1)==0);
    assert(fe100_reg_wr(0,0x80500,0x04004401)==12); /* other DDR address */
    assert(fe100_reg_wr(0,0x80500,0x04000201)==0); /* cfg register 1 */
    assert(fe100_reg_wr(0,0x80500,0x04400201)==12); /* loop prohibited */
    assert(fe100_reg_wr(0,0x80500,0x08000401)==12); /* bulk init not audited here */
    assert(fe100_reg_wr(0,0x80508,0)==12); /* read-only completion */
    assert(fe100_reg_wr(0,0x8051c,0)==12); /* no uninitialized tail */
    assert(fe100_reg_wr(0,0x80000,0)==12); /* no TLU controls */
    assert(fe100_reg_wr(1,0x80504,0)==12); /* wrong device */
    writable=0;
    assert(fe100_reg_wr(0,0x80504,0)==12);
    uint32_t value;
    assert(fe100_reg_rd(0,0x80504,&value)==0 && value==1);
    assert(ffn_fe100_allow_readonly(0xa8080)==0);
    assert(fe100_reg_rd(0,0xa8080,&value)==0);
    assert(fe100_reg_wr(0,0xa8080,0)==12);
    fclose(trace);
    return 0;
}
