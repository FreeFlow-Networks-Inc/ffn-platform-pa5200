"""Owner-referenced external TCAM NOP synchronization, before table access."""
import time
from ffn_fe100_clocks import RST,INIT,CLOCK_MASK,TCAM_STATUS

NOP_DONE=1<<20


def synchronize(io,sleep=time.sleep):
    before=io.read(RST)
    if before&0x200:
        raise RuntimeError('NOP control already asserted; interrupted operation requires inspection')
    if (before&0x1bb)!=0x181 or io.read(TCAM_STATUS)&1!=1 or io.read(INIT)&CLOCK_MASK!=CLOCK_MASK:
        raise RuntimeError('TCAM clocks/reset state not ready for NOP synchronization')
    if io.read(INIT)&NOP_DONE: return {'changed':False}
    try:
        io.write(RST,before|0x200)
        if io.read(RST)!=before|0x200: raise RuntimeError('NOP control readback failed')
        sleep(.001) # owner pan_fe100_set_tdi_config 0x102b841c..0x102b8478
    finally:
        io.write(RST,before)
    if io.read(RST)!=before: raise RuntimeError('NOP control did not restore')
    for _ in range(500):
        if io.read(INIT)&NOP_DONE: return {'changed':True}
        sleep(.002)
    raise TimeoutError('TCAM NOP synchronization did not complete')
