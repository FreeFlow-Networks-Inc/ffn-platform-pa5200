#!/usr/bin/env python3
"""Sample FE100 lookup queues without clearing counters or changing tables.

An idle lookup engine is not proof that offload works. Reports deliberately
separate observed queue pressure, historical flags and untested lookups.
"""
import argparse
import fcntl
import json
import re
import time

BLOCKS = ('lif', 'acl', 'dfp', 'fwd', 'tlu', 'tdi')
PAIR_NAMES = {
    'acl_packets': ('acl_cr_lif_acl_pca_rx', 'acl_cr_acl_dfp_pca_tx'),
    'acl_lookups': ('acl_cr_acl_tlu_req_tx', 'acl_cr_tlu_acl_rslt_rx'),
    'tlu_acl_lookups': ('tlu_cr_acl_lkup_rx', 'tlu_cr_acl_rslt_tx'),
    'dfp_packets': ('dfp_cr_acl_dfp_pca_rx', 'dfp_cr_dfp_fwd_pca_tx'),
}


def selected(name):
    if name.split('_', 1)[0] not in BLOCKS:
        return False
    # Never select clear-on-read aliases, including names containing "status".
    if name.endswith('_no_rd_clr'):
        return True
    if 'rd_clr' in name:
        return False
    return (name.endswith(('_cr_mode', '_cr_flow_control_status',
                           '_fifo_status', '_init_status'))
            or re.fullmatch(r'tlu_table_cfg_\d+', name) is not None)


def decode(reg, value):
    return {f['name']: (value >> f['lsb']) & ((1 << (f['msb']-f['lsb']+1))-1)
            for f in reg['fields'] if not f['name'].startswith('rsvd')}


def analyze(samples):
    first, last = samples[0]['registers'], samples[-1]['registers']
    pairs = {}
    for label, names in PAIR_NAMES.items():
        rx, tx = (n + '_stats_ctr_no_rd_clr' for n in names)
        if rx not in first or tx not in first:
            raise ValueError('required counters absent: ' + label)
        received = (last[rx]['raw']-first[rx]['raw']) & 0xffffffff
        sent = (last[tx]['raw']-first[tx]['raw']) & 0xffffffff
        pairs[label] = {'received_delta_mod32': received, 'sent_delta_mod32': sent,
                        'final_balance_mod32': (last[rx]['raw']-last[tx]['raw']) & 0xffffffff}
    queues, flags, faults = [], [], []
    for name, reg in last.items():
        fields = reg['fields']
        if name.endswith('_fifo_status') and fields.get('level', 0):
            levels = [s['registers'][name]['fields']['level'] for s in samples]
            queues.append({'register': name, 'levels': levels,
                           'nonempty_all_samples': all(v > 0 for v in levels)})
        if name.endswith('_cr_mode'):
            faults.extend(name+'.'+k for k in ('fatal_err_stat', 'non_fatal_err_stat', 'xmit_pause')
                          if fields.get(k))
        if name.endswith('_cr_flow_control_status'):
            flags.extend({'register': name, 'field': k,
                          'current': k.startswith('curr_fc_')}
                         for k, v in fields.items() if v)
    # These fields are prerequisites for the external-memory path, not a claim
    # that a particular ACL packet uses it. Preserve all table IDs for auditing.
    external = [int(n.rsplit('_', 1)[1]) for n, r in last.items()
                if n.startswith('tlu_table_cfg_') and r['fields'].get('external_sel')]
    tdi = last.get('tdi_init_status', {}).get('fields', {})
    return {'offload_verified': False,
            'interpretation': 'Register samples alone cannot establish functional forwarding. '
                              'Counter deltas assume no hardware reset during sampling; reads are not atomic. '
                              'TDI queue values must not be treated as valid occupancy when their clocks are off.',
            'counter_pairs': pairs, 'nonempty_queues': queues,
            'mode_faults_or_pauses': faults, 'flow_control_flags': flags,
            'external_table_ids': external,
            # Owner pdt/fe100.py associates table2 with ACL and table4 with FWD.
            'lookup_table_location': {
                label: last.get('tlu_table_cfg_'+str(index), {}).get('fields', {}).get('external_sel')
                for label, index in (('acl_external_sel', 2), ('fwd_external_sel', 4))},
            'external_clock_status': {k: tdi.get(k) for k in
                                      ('dram_ddr_clk', 'dram_pclk', 'tcam_2x_clk', 'tcam_1x_clk')},
            'acl_lookup_observed': pairs['acl_lookups']['received_delta_mod32'] > 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--samples', type=int, default=3)
    p.add_argument('--interval', type=float, default=1)
    args = p.parse_args()
    if not 2 <= args.samples <= 30 or not 0.05 <= args.interval <= 2:
        p.error('use 2..30 samples and an interval of 0.05..2 seconds')
    from ffn_fe100 import Fe100, bar0_base_and_size, memory_decode_on
    with open('/run/ffn-fe100-tables.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if bar0_base_and_size()[1] != 0x100000 or not memory_decode_on():
            raise RuntimeError('FE100 BAR unavailable')
        with open('/opt/ffn-compat/opt/ffn/fe100-csr.json') as f:
            regs = [r for r in json.load(f) if selected(r['name'])]
        samples = []
        fe = Fe100()
        try:
            for i in range(args.samples):
                if i:
                    time.sleep(args.interval)
                sample = {'monotonic_time': time.monotonic(), 'registers': {}}
                for r in regs:
                    raw = fe.read32(r['addr'])
                    sample['registers'][r['name']] = {'raw': raw, 'fields': decode(r, raw)}
                samples.append(sample)
        finally:
            fe.close()
    print(json.dumps({'summary': analyze(samples), 'samples': samples}, indent=2))


if __name__ == '__main__':
    main()
