#!/usr/bin/env python3
"""PA-5200 SFP cage controls, separate from the BCM MAC/SerDes.

Board wiring is derived from the PA-5200 version 11 port map. The active-high
TX_DISABLE polarity is verified against live SFF-8472 diagnostics. No customer
interface configuration belongs here. Only the selected cage bit is written.
"""
import fcntl
import math
from pathlib import Path
import re
import struct

from ffn_i2cread import read_regs, write_reg

LOCK = Path('/run/ffn-sfp-control.lock')
SYSFS = Path('/sys/bus/i2c/devices')
BUS = 1
BITS = (0, 1, 2, 3, 5, 4, 7, 6, 12, 13, 14, 15, 9, 8, 11, 10)


def bit_for(port):
    if type(port) is not int or not 5 <= port <= 20:
        raise ValueError('SFP control requires a faceplate port from 5 through 20')
    return BITS[port - 5]


def _inventory():
    # PCA9555 sequential reads toggle within a register pair, not across all
    # eight registers. Explicit offsets are required for polarity/direction.
    presence = bytes(read_regs(BUS, 0x22, r, 1)[0] for r in range(8))
    control = bytes(read_regs(BUS, 0x23, r, 1)[0] for r in range(8))
    ports = {}
    for port in range(5, 21):
        bit = bit_for(port)
        bank, mask = bit // 8, 1 << (bit % 8)
        # PCA9555 input registers already include the polarity register XOR.
        present = not bool((presence[bank] ^ presence[4 + bank]) & mask)
        driven = not bool(control[6 + bank] & mask)
        disabled = bool((control[bank] ^ control[4 + bank]) & mask)
        enabled = driven and not disabled and not bool(control[2 + bank] & mask)
        ports[port] = dict(present=present, tx_enabled=enabled,
                           tx_disable=disabled, control_ready=True)
    return ports


def inventory():
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _inventory()


def _write_bit(register, mask, value):
    old = read_regs(BUS, 0x23, register, 1)[0]
    new = old | mask if value else old & ~mask
    if new != old:
        write_reg(BUS, 0x23, register, new)
    if read_regs(BUS, 0x23, register, 1)[0] != new:
        raise RuntimeError('SFP control register readback mismatch')


def set_enabled(port, enabled):
    bit = bit_for(port)
    if type(enabled) is not bool:
        raise ValueError('SFP enabled must be boolean')
    bank, mask = bit // 8, 1 << (bit % 8)
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Preload the latch before enabling output; never alter another cage.
        _write_bit(2 + bank, mask, not enabled)
        _write_bit(6 + bank, mask, False)
        state = _inventory()[port]
        if state['tx_disable'] != (not enabled) or state['tx_enabled'] != enabled:
            raise RuntimeError('SFP transmitter control readback mismatch')
        return state


def module_bus(port):
    bit = bit_for(port)
    mux = SYSFS / ('1-0075' if bit < 8 else '1-0077')
    channel = mux / ('channel-%d' % (bit % 8))
    if not channel.is_symlink():
        raise OSError('SFP diagnostic I2C multiplexer is not bound')
    if (mux / 'driver').resolve().name != 'pca954x':
        raise OSError('Unexpected SFP multiplexer driver')
    return int(channel.resolve(strict=True).name.split('-')[-1])


def decode_diagnostics(identity, data):
    """Decode calibrated SFF-8472 values; reject corrupt or unready EEPROMs."""
    if len(identity) != 96 or identity[0] != 3:
        raise ValueError('Not an SFP identity page')
    if sum(identity[:63]) & 255 != identity[63] or sum(identity[64:95]) & 255 != identity[95]:
        raise ValueError('SFP identity checksum mismatch')
    result = dict(vendor=identity[20:36].decode('ascii', 'replace').strip(),
                  model=identity[40:56].decode('ascii', 'replace').strip(),
                  wavelength_nm=int.from_bytes(identity[60:62], 'big'))
    if not identity[92] & 0x40:
        return dict(result, diagnostics_available=False)
    if len(data) < 118 or sum(data[:95]) & 255 != data[95]:
        raise ValueError('SFP diagnostic checksum mismatch')
    if data[110] & 1:
        raise ValueError('SFP diagnostic data is not ready')
    external, internal = bool(identity[92] & 0x10), bool(identity[92] & 0x20)
    if external == internal:
        raise ValueError('SFP diagnostic calibration is unspecified or ambiguous')
    values = [int.from_bytes(data[i:i+2], 'big', signed=(i == 96)) for i in range(96, 106, 2)]
    if external:
        # Slope is unsigned 8.8, offset signed 16-bit; RX uses a float polynomial.
        for index, offset in ((0, 84), (1, 88), (2, 76), (3, 80)):
            slope = int.from_bytes(data[offset:offset+2], 'big') / 256
            intercept = int.from_bytes(data[offset+2:offset+4], 'big', signed=True)
            values[index] = values[index] * slope + intercept
        coefficients = struct.unpack('>5f', data[56:76])
        rx = 0.0
        for coefficient in coefficients:
            rx = rx * values[4] + coefficient
        values[4] = rx
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid SFP calibration values')
    def dbm(raw):
        return round(10 * math.log10(raw / 10000), 3) if raw > 0 else None
    result.update(diagnostics_available=True, temperature_c=values[0]/256,
                  voltage_v=values[1]/10000, tx_bias_ma=values[2]*0.002,
                  tx_power_dbm=dbm(values[3]), rx_power_dbm=dbm(values[4]),
                  tx_disable=bool(data[110] & 0x80),
                  rx_los=bool(data[110] & 2) if identity[93] & 0x10 else None,
                  tx_fault=bool(data[110] & 4) if identity[93] & 0x20 else None)
    return result


def diagnostics(port):
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bus = module_bus(port)
        identity = read_regs(bus, 0x50, 0, 96)
        data = read_regs(bus, 0x51, 0, 128) if identity[92] & 0x40 else b''
        return decode_diagnostics(identity, data)


def gigabit_fiber(port):
    """Recognize 1.25 GBd optical modules, including EEPROMs lacking GbE flags.

    RJ45 SFPs must keep their copper/SGMII path. Unknown, absent or inaccessible
    modules are not evidence of a gigabit fiber module.
    """
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bus=module_bus(port)
        identity=read_regs(bus,0x50,0,96)
        _validate_identity(identity)
        return (identity[2] in (1,7) and 10 <= identity[12] <= 15 and
                int.from_bytes(identity[60:62],'big') > 0)


def _validate_identity(identity):
    if len(identity)!=96 or identity[0]!=3 or sum(identity[:63])&255!=identity[63] or sum(identity[64:95])&255!=identity[95]:
        raise ValueError('Invalid SFP identity or checksum')


def configure_gigabit_fiber(port,chip,speed):
    """Apply the BCM 1000BASE-X recipe only to a detected gigabit fiber SFP."""
    bit_for(port)
    from ffn_faceplate import PORTS
    if type(chip) is not int or chip!=PORTS[port-1] or speed not in ('auto','1000','10000'):
        raise ValueError('Invalid SFP link request')
    try: recognized=gigabit_fiber(port)
    except OSError:return False
    if not recognized:return False
    if speed=='10000':raise ValueError('Detected SFP supports 1G; 10G cannot be selected')
    # The existing serialized BCM CINT endpoint is used by the CP controller.
    # No user-supplied CINT or register addresses enter this recipe.
    from ffn_aggregate_hardware import SCRIPT,acquire,call
    auto=1 if speed=='auto' else 0
    body='''{
int rv=0; int an=0; int rate=0; int changed=0; uint32 mode=0; bcm_port_if_t iface;
rv=bcm_port_interface_get(0,PORT,&iface);
if(rv==0) rv=bcm_port_autoneg_get(0,PORT,&an);
if(rv==0) rv=bcm_port_speed_get(0,PORT,&rate);
if(rv==0 && an && AUTO) rv=bcm_port_phy_control_get(0,PORT,BCM_PORT_PHY_CONTROL_AUTONEG_MODE,&mode);
if(rv==0 && (iface!=BCM_PORT_IF_GMII || an!=AUTO || (AUTO && mode!=1) || (!AUTO && rate!=1000))) {
 changed=1;
 rv=bcm_port_autoneg_set(0,PORT,0);
 if(rv==0) rv=bcm_port_interface_set(0,PORT,BCM_PORT_IF_GMII);
 if(rv==0) rv=bcm_port_speed_set(0,PORT,1000);
 if(rv==0 && AUTO) rv=bcm_port_phy_control_set(0,PORT,BCM_PORT_PHY_CONTROL_AUTONEG_MODE,1);
 if(rv==0 && AUTO) rv=bcm_port_autoneg_set(0,PORT,1);
}
if(rv==0) rv=bcm_port_interface_get(0,PORT,&iface);
if(rv==0) rv=bcm_port_autoneg_get(0,PORT,&an);
if(rv==0 && AUTO) rv=bcm_port_phy_control_get(0,PORT,BCM_PORT_PHY_CONTROL_AUTONEG_MODE,&mode);
printf("FFN_SFP_LINK %d %d %d %d %d\\n",rv,iface,an,mode,changed);
printf("FFN_SFP_LINK_DONE\\n");
}'''.replace('PORT,',str(chip)+',')
    body=re.sub(r'\bAUTO\b',str(auto),body)
    with open('/run/ffn-forward-test.lock','a') as lock:
        acquire(lock)
        prior=SCRIPT.read_bytes()
        try:
            SCRIPT.write_text(body)
            result=call({'op':'cint.run','script':SCRIPT.name,'timeout':10})
        finally:SCRIPT.write_bytes(prior)
    markers=result.get('markers',[])
    line=next((m for m in markers if m.startswith('FFN_SFP_LINK ')),None)
    if not result.get('ok') or not result.get('completed') or not line:
        raise RuntimeError('SFP link recipe incomplete; inspect BCM diagnostics')
    fields=[int(v) for v in line.split()[1:]]
    if len(fields)!=5 or fields[0]!=0 or fields[1]!=3 or fields[2]!=auto or (auto and fields[3]!=1):
        raise RuntimeError('SFP 1000BASE-X/Clause 37 readback mismatch')
    return True
