/* Host regression: session scope cannot reach DDR or unrelated CFP controls. */
#include "ffn_fe100_flow_memory_mmio.c"
#include <assert.h>
#include <string.h>
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

    /* The trace limit. A long-lived endpoint must not grow its trace without
     * bound; after the limit exactly one marker line is written and nothing
     * else, while register access itself keeps working. */
    {
        uint32_t value;
        long before, after;
        block_base=0x40000; diagnostic_mode=0; writable=1;
        trace=tmpfile(); assert(trace); trace_bytes=0; trace_stopped=0;
        ffn_fe100_allow(0x40800);
        assert(ffn_fe100_trace_limit(100)==0);
        for (int i = 0; i < 50; i++) assert(fe100_reg_wr(0,0x40800,(uint32_t)i)==0);
        fflush(trace); before=ftell(trace);
        assert(before>100 && before<100+120);           /* lines, then the marker */
        for (int i = 0; i < 50; i++) assert(fe100_reg_rd(0,0x40800,&value)==0 && value==49);
        fflush(trace); after=ftell(trace);
        assert(after==before);                            /* nothing after the marker */
        assert(trace_stopped==1);
        {
            char line[160]; int seen=0;
            rewind(trace);
            while (fgets(line,sizeof(line),trace)) if (!strncmp(line,"TRACE LIMIT",11)) seen++;
            assert(seen==1);
        }
        assert(ffn_fe100_trace_limit(0)==0);             /* unlimited again: still stopped */
        assert(fe100_reg_wr(0,0x40800,7)==0);
        fflush(trace); assert(ftell(trace)==after);
        fclose(trace);
    }
    return 0;
}
