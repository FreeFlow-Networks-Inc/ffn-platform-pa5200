#!/usr/bin/env python3
"""CP side of the FE100 per-tuple steering commissioning: counters and field rules.

Usage: python3 steer_cp.py <op>   (JSON request on stdin, JSON reply on stdout)
  snapshot                       FE100 non-clearing counters that evidence ingress/hit/miss/egress
  rule-create {port,tagged,protocol,src,dst,sport,dport,destination,priority}
                                 exact 5-tuple ingress rule on `port` -> redirect to system port `destination`
  catchall-create {port,destination,priority}
                                 InPort-only rule (used only on the lab port experiment)
  egress-create {destination,dmac,priority}
                                 FE100 NIF egress (switch port 3, DSA tag naming `destination`) -> that front port
  rule-delete {group,entry,dqs:[...]}
  ff-get {port} / ff-set {port,destination,enabled}   (lab port 7 only, via the lab's own recipe)
All switch work goes through ffn_fe100_bcm_lab.execute (shared script staging, BCM service lock, yield).
"""
import json
import re
import socket
import sys

sys.path[:0] = ['/usr/local/sbin', '/usr/local/lib/ffn']

COUNTERS = ('nif_p0_mac_rx_pkt_stats_ctr_no_rd_clr', 'nif_p0_mac_tx_pkt_stats_ctr_no_rd_clr',
            'nif_rx_non_ha3_pkt_stats_ctr_no_rd_clr', 'nif_cr_ipq_tx_stats_ctr_no_rd_clr',
            'lif_tlu_hit_stats_ctr_no_rd_clr', 'lif_tlu_miss_stats_ctr_no_rd_clr',
            'acl_tlu_hit_stats_ctr_no_rd_clr', 'acl_tlu_miss_stats_ctr_no_rd_clr',
            'dfp_flow_lkup_flow_hit_sta_ctr_no_rd_clr', 'dfp_flow_lkup_miss_sta_ctr_no_rd_clr',
            'flu_flow_table_hit_ctr_no_rd_clr', 'flu_flow_table_miss_ctr_no_rd_clr',
            'cfp_flow_lkup_flow_hit_stats_ctr_no_rd_clr', 'cfp_flow_lkup_miss_stats_ctr_no_rd_clr',
            'tmi_tx_fifo0_pkt_stats_ctr_no_rd_clr', 'tmi_rx_fp_pkt_stats_ctr_no_rd_clr')


def snapshot():
    from ffn_fe100 import Fe100, register_map_path
    regs = {r['name']: r['addr'] for r in json.load(open(register_map_path())) if r['name'] in COUNTERS}
    fe = Fe100()
    try:
        return {name: fe.read32(regs[name]) for name in COUNTERS if name in regs}
    finally:
        fe.close()


def c_bytes(name, values):
    return 'uint8 %s[4]={%s};' % (name, ','.join(str(v) for v in values))


def tuple_qualifiers(request):
    """Byte-offset qualifiers for an IPv4 TCP/UDP 5-tuple; +4 for an 802.1Q tagged frame.

    Untagged offsets: proto at 23 (word 20..23), src 26, dst 30, ports 34..37.
    The IHL is not matched; a packet with options would carry the ports elsewhere
    and simply miss the rule, staying on the software path.
    """
    base = 4 if request['tagged'] else 0
    proto = int(request['protocol'])
    src = socket.inet_aton(request['src']); dst = socket.inet_aton(request['dst'])
    ports = int(request['sport']).to_bytes(2, 'big') + int(request['dport']).to_bytes(2, 'big')
    return [(20 + base, [0, 0, 0, proto], [0, 0, 0, 0xff]),
            (26 + base, list(src), [0xff] * 4),
            (30 + base, list(dst), [0xff] * 4),
            (34 + base, list(ports), [0xff] * 4)]


def egress_qualifiers(request):
    """The lab-proven match for FE100 XF output on NIF: DSA tag type bits and destination port
    (bytes 12..19 after the MAC addresses) plus the exact destination MAC."""
    port = int(request['destination'])
    return [(12, [0xc0, 0, 0x10, 0], [0xdf, 0, 0xf0, 0]),
            (16, [0, 0, (port << 5) >> 8, (port << 5) & 255], [0xff] * 4)]


def create_source(in_port, qualifiers, destination, priority, dmac=None):
    n = len(qualifiers)
    lines = ['{', ' int rv=0;int group=-1;int entry=-1;int dst=0;int i;',
             ' int dq[4]={-1,-1,-1,-1};',
             ' bcm_field_qset_t qset;bcm_field_aset_t aset;bcm_field_data_qualifier_t d;']
    for i, (off, values, masks) in enumerate(qualifiers):
        lines.append(' ' + c_bytes('v%d' % i, values) + c_bytes('m%d' % i, masks))
    if dmac:
        mac = bytes.fromhex(dmac.replace(':', ''))
        lines.append(' bcm_mac_t mac={%s};bcm_mac_t mask={0xff,0xff,0xff,0xff,0xff,0xff};' % ','.join(str(b) for b in mac))
    lines += [' BCM_FIELD_QSET_INIT(qset);BCM_FIELD_QSET_ADD(qset,bcmFieldQualifyStageIngress);',
              ' BCM_FIELD_QSET_ADD(qset,bcmFieldQualifyInPort);']
    if dmac:
        lines.append(' BCM_FIELD_QSET_ADD(qset,bcmFieldQualifyDstMac);')
    for i, (off, values, masks) in enumerate(qualifiers):
        lines += [' bcm_field_data_qualifier_t_init(&d);d.offset_base=bcmFieldDataOffsetBasePacketStart;d.offset=%d;d.length=4;' % off,
                  ' if(rv==0)rv=bcm_field_data_qualifier_create(0,&d);',
                  ' if(rv==0){dq[%d]=d.qual_id;rv=bcm_field_qset_data_qualifier_add(0,&qset,dq[%d]);}' % (i, i)]
    lines += [' if(rv==0)rv=bcm_field_group_create(0,qset,%d,&group);' % priority,
              ' if(rv==0){BCM_FIELD_ASET_INIT(aset);BCM_FIELD_ASET_ADD(aset,bcmFieldActionRedirect);rv=bcm_field_group_action_set(0,group,aset);}',
              ' if(rv==0)rv=bcm_field_entry_create(0,group,&entry);',
              ' if(rv==0)rv=bcm_field_qualify_InPort(0,entry,%d,0xffffffff);' % in_port]
    if dmac:
        lines.append(' if(rv==0)rv=bcm_field_qualify_DstMac(0,entry,mac,mask);')
    for i in range(n):
        lines.append(' if(rv==0)rv=bcm_field_qualify_data(0,entry,dq[%d],v%d,m%d,4);' % (i, i, i))
    lines += [' BCM_GPORT_SYSTEM_PORT_ID_SET(dst,%d);' % destination,
              ' if(rv==0)rv=bcm_field_action_add(0,entry,bcmFieldActionRedirect,0,dst);',
              ' if(rv==0)rv=bcm_field_entry_install(0,entry);',
              ' printf("FFN_STEER group=%d entry=%d dq0=%d dq1=%d dq2=%d dq3=%d rv=%d\\n",group,entry,dq[0],dq[1],dq[2],dq[3],rv);',
              ' if(rv!=0){if(entry>=0)bcm_field_entry_destroy(0,entry);if(group>=0)bcm_field_group_destroy(0,group);',
              '  for(i=0;i<4;i++){if(dq[i]>=0)bcm_field_data_qualifier_destroy(0,dq[i]);}}',
              ' if(rv==0)printf("FFN_DONE\\n");', '}']
    return '\n'.join(lines) + '\n'


def delete_source(group, entry, dqs):
    lines = ['{', ' int rv=0;int i;', ' int dq[4]={%s};' % ','.join(str(d) for d in (list(dqs) + [-1] * 4)[:4]),
             ' rv=bcm_field_entry_destroy(0,%d);' % entry,
             ' if(rv==0)rv=bcm_field_group_destroy(0,%d);' % group,
             ' for(i=0;i<4 && rv==0;i++){if(dq[i]>=0)rv=bcm_field_data_qualifier_destroy(0,dq[i]);}',
             ' printf("FFN_STEER_DELETE group=%d entry=%d rv=%%d\\n",rv);' % (group, entry),
             ' if(rv==0)printf("FFN_DONE\\n");', '}']
    return '\n'.join(lines) + '\n'


def run_cint(source):
    from ffn_faceplate import call
    from ffn_fe100_bcm_lab import execute
    result = execute(source, call)
    ids = {}
    for marker in result.get('markers', []):
        m = re.fullmatch(r'FFN_STEER group=(-?\d+) entry=(-?\d+) dq0=(-?\d+) dq1=(-?\d+) dq2=(-?\d+) dq3=(-?\d+) rv=(-?\d+)', marker)
        if m:
            group, entry, *dqs, rv = map(int, m.groups())
            ids = dict(group=group, entry=entry, dqs=[d for d in dqs if d >= 0], rv=rv)
    return dict(markers=result.get('markers', []), ids=ids)


def main():
    op = sys.argv[1]
    raw = sys.stdin.read()
    request = json.loads(raw) if raw.strip() else {}
    if op == 'snapshot':
        reply = snapshot()
    elif op == 'rule-create':
        reply = run_cint(create_source(int(request['port']), tuple_qualifiers(request), int(request['destination']), int(request['priority'])))
    elif op == 'catchall-create':
        reply = run_cint(create_source(int(request['port']), [], int(request['destination']), int(request['priority'])))
    elif op == 'egress-create':
        reply = run_cint(create_source(3, egress_qualifiers(request), int(request['destination']), int(request['priority']), request.get('dmac')))
    elif op == 'rule-delete':
        reply = run_cint(delete_source(int(request['group']), int(request['entry']), request.get('dqs') or []))
    elif op == 'cpu-capture-begin':
        # The lab's IL-output capture (template modes 7/8): FE100 CPU messages leave on switch port 20;
        # make it RAW and force-forward it to the MP capture port 8. Returns the previous header type.
        source = '''{int rv=0;int hdr=-1;int dst=0;int en=0;
 rv=bcm_switch_control_port_get(0,20,bcmSwitchPortHeaderType,&hdr);
 if(rv==0)rv=bcm_port_force_forward_get(0,20,&dst,&en);
 printf("FFN_CPUCAP previous_header=%d destination=%d enabled=%d rv=%d\n",hdr,dst,en,rv);
 if(rv==0 && en)rv=-8;
 if(rv==0)rv=bcm_switch_control_port_set(0,20,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_RAW);
 if(rv==0)rv=bcm_port_force_forward_set(0,20,8,1);
 if(rv==0)printf("FFN_DONE\n");}
'''
        from ffn_faceplate import call
        from ffn_fe100_bcm_lab import execute
        result = execute(source, call)
        m = [re.fullmatch(r'FFN_CPUCAP previous_header=(-?\d+) destination=(-?\d+) enabled=(\d) rv=(-?\d+)', s) for s in result.get('markers', [])]
        m = [x for x in m if x]
        reply = dict(markers=result.get('markers', []), previous_header=int(m[0].group(1)) if m else None)
    elif op == 'cpu-capture-end':
        source = '''{int rv=0;int hdr=-1;
 rv=bcm_port_force_forward_set(0,20,8,0);
 if(rv==0)rv=bcm_switch_control_port_set(0,20,bcmSwitchPortHeaderType,%d);
 if(rv==0)rv=bcm_switch_control_port_get(0,20,bcmSwitchPortHeaderType,&hdr);
 printf("FFN_CPUCAP_END header=%%d rv=%%d\n",hdr,rv);
 if(rv==0)printf("FFN_DONE\n");}
''' % int(request['header'])
        from ffn_faceplate import call
        from ffn_fe100_bcm_lab import execute
        reply = dict(markers=execute(source, call).get('markers', []))
    elif op in ('ff-get', 'ff-set'):
        from ffn_faceplate import call
        from ffn_fe100_bcm_lab import execute, port_recipe, read_port
        port = int(request['port'])
        if port != 7:
            raise ValueError('force-forward changes are confined to the lab port')
        recipe = port_recipe(port) if op == 'ff-get' else port_recipe(port, int(request['destination']), int(request['enabled']))
        reply = read_port(execute(recipe, call), port)
    else:
        raise ValueError('unknown op')
    print(json.dumps(reply))


if __name__ == '__main__':
    main()
