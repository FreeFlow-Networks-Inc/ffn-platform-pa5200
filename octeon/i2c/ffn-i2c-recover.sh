#!/bin/sh
# Recover a stuck PA-5200 control-plane I2C bus (default: bus 1, TWSI at
# 1180000001200, the management mux tree, SFP cages and thermal devices).
#
# A slave holding SDA low (TWSI_INT shows SDA=0 with SCL=1) makes every
# transaction fail with ETIMEDOUT in milliseconds; the thermal governor then
# cannot write fan PWM and the SFP transmitter control is unavailable. The
# OCTEON driver's probe fails with "init low level failed" while the line is
# held, so the order matters: release the line first through the controller's
# own SCL/SDA override bits (the nine clocks plus STOP the kernel's generic
# recovery would issue), then rebind the controller, then re-create the muxes
# with the management-I2C script (same adapter numbers: the kernel hands out
# the lowest free ones in the same order), then restart the dependents.
# Reads and writes touch only TWSI_INT's override bits; the interrupt bits are
# write-one-to-clear and are never written. Run as root on the CP.
set -u
BASE=${FFN_TWSI_BASE:-0x1180000001200}
DEV=${FFN_TWSI_DEVICE:-1180000001200.i2c}
BUS=${FFN_I2C_BUS:-1}
DRV=/sys/bus/platform/drivers/i2c-octeon

twsi() {
python3 - "$BASE" "$1" <<'PY'
import mmap, os, struct, sys, time
base = int(sys.argv[1], 16); mode = sys.argv[2]
flags = os.O_RDWR if mode == 'release' else os.O_RDONLY
fd = os.open('/dev/mem', flags | os.O_SYNC); page = mmap.PAGESIZE
prot = mmap.PROT_READ | (mmap.PROT_WRITE if mode == 'release' else 0)
m = mmap.mmap(fd, page, mmap.MAP_SHARED, prot, offset=base & ~(page - 1)); off = base & (page - 1)
def rd(): return struct.unpack('>Q', m[off + 0x10: off + 0x18])[0]
def wr(v): m[off + 0x10: off + 0x18] = struct.pack('>Q', v)
v = rd()
if mode == 'lines':
    print('SDA=%d SCL=%d' % ((v >> 10) & 1, (v >> 11) & 1))
else:
    SDA_OVR, SCL_OVR = 1 << 8, 1 << 9
    keep = v & 0x70
    wr(keep); time.sleep(0.00005)
    released = False
    for i in range(9):
        wr(keep | SCL_OVR); time.sleep(0.00005); wr(keep); time.sleep(0.00005)
        if (rd() >> 10) & 1:
            print('SDA released after %d clock(s)' % (i + 1)); released = True; break
    if not released: print('SDA still low after 9 clocks')
    wr(keep | SDA_OVR); time.sleep(0.00005); wr(keep); time.sleep(0.00005)
    v = rd(); print('SDA=%d SCL=%d' % ((v >> 10) & 1, (v >> 11) & 1))
m.close(); os.close(fd)
PY
}

echo "lines before: $(twsi lines)"
case "$(twsi lines)" in *SDA=0*) echo "releasing SDA:"; twsi release ;; esac
if [ -e "$DRV/$DEV" ]; then echo "$DEV" > "$DRV/unbind" && sleep 1; fi
if echo "$DEV" > "$DRV/bind"; then echo "controller bound"; else echo "controller bind FAILED; lines: $(twsi lines)"; exit 1; fi
sleep 1
sh /usr/local/sbin/ffn-management-i2c.sh && echo "muxes re-created" || { echo "mux script FAILED"; exit 1; }
python3 - "$BUS" <<'PY'
import sys, time
sys.path.insert(0, '/usr/local/sbin')
from ffn_i2cread import read_regs
bus = int(sys.argv[1]); t = time.time()
try: read_regs(bus, 0x22, 0, 1); print('expander read ok in %.3f s' % (time.time() - t))
except Exception as e: print('expander read FAILED: %r' % e); sys.exit(1)
PY
for unit in ffn-thermal.service; do
    systemctl reset-failed "$unit" 2>/dev/null; systemctl restart "$unit"; sleep 3
    echo "$unit: $(systemctl is-active "$unit")"
done
echo "lines after: $(twsi lines)"
