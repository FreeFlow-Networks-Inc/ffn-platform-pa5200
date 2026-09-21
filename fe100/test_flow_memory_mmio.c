/* Host regression: session scope cannot reach DDR or unrelated CFP controls. */
#include "ffn_fe100_flow_memory_mmio.c"
#include <assert.h>
int main(void)
{
    static uint32_t fake[0x100000/4];
    block_base=0x40000;
    assert(ffn_flow_session_mode()==0);
    regs=fake; writable=1; trace=tmpfile(); assert(trace);
    ffn_fe100_allow(0x486c0);
    ffn_fe100_allow(0x48014);
    ffn_fe100_allow_readonly(0x48018);
    ffn_fe100_allow(0x48008); /* Known does not grant the other block. */
    ffn_fe100_allow(0xb0104);
    ffn_fe100_allow(0xa0004);
    assert(fe100_reg_wr(0,0x486c0,1)==0);
    assert(fe100_reg_wr(0,0x48014,1)==0);
    assert(fe100_reg_wr(0,0x48018,1)==12);
    assert(fe100_reg_wr(0,0x48008,1)==12);
    assert(fe100_reg_wr(0,0xb0104,1)==12);
    assert(fe100_reg_wr(0,0xa0004,1)==12);
    assert(fe100_reg_wr(1,0x48014,1)==12);
    assert(block_base==0x40000);
    writable=0;
    assert(fe100_reg_wr(0,0x48014,1)==12);
    block_base=0xb0000; session_mode=0; diagnostic_mode=1; writable=1;
    ffn_fe100_allow(0xb0500); ffn_fe100_allow(0xb0504);
    ffn_fe100_allow(0xb050c); ffn_fe100_allow(0xb0104);
    assert(fe100_reg_wr(0,0xb0504,0xc018)==0);
    assert(fe100_reg_wr(0,0xb0500,0x02000801)==0);
    assert(fe100_reg_wr(0,0xb0500,0x02001001)==0);
    assert(fe100_reg_wr(0,0xb0500,0x04000801)==12); /* PHY write */
    assert(fe100_reg_wr(0,0xb0500,0x02002001)==12); /* controller target */
    assert(fe100_reg_wr(0,0xb050c,0)==12); /* IA data write */
    assert(fe100_reg_wr(0,0xb0504,0x10000)==12); /* invalid PHY address */
    assert(fe100_reg_wr(0,0xb0104,0)==12); /* reset */
    assert(fe100_reg_wr(1,0xb0504,0xc018)==12); /* wrong device */
    block_base=0x98000;
    ffn_fe100_allow(0x98320); ffn_fe100_allow(0x98324);
    ffn_fe100_allow(0x9832c); ffn_fe100_allow(0x98100);
    assert(fe100_reg_wr(0,0x98324,0xc019)==0);
    assert(fe100_reg_wr(0,0x98320,0x02000801)==0);
    assert(fe100_reg_wr(0,0x98320,0x02001001)==0);
    assert(fe100_reg_wr(0,0x98320,0x04000801)==12);
    assert(fe100_reg_wr(0,0x9832c,0)==12);
    assert(fe100_reg_wr(0,0x98100,0)==12);
    assert(fe100_reg_wr(0,0x98324,0x10000)==12);
    fclose(trace);
    return 0;
}
