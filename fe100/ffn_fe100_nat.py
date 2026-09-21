#!/usr/bin/env python3
"""Prepare paired FE100 IPv4 actions from kernel-selected conntrack tuples.

No policy evaluation, NAT allocation, hardware I/O or admission occurs here.
The caller must verify the producer, policy, session state and attachment, and
the adapter independently requires NAT packet qualification before insertion.
"""
from ffn_fe100_sessions import key4, forwarding_entry4, nat_entry4, uint


def capabilities():
    """Implemented codecs are distinct from commissioned packet processing."""
    return dict(schema=1, family='FE100', tuple_source='dataplane conntrack original/reply',
                address_families=['ipv4'], protocols=['tcp','udp'],
                encoded_actions=['forward','drop','ttl-decrement','snat','dnat','snat-and-dnat','port-translation'],
                native_nat_layout='ports-then-addresses', wire_nat_layout='addresses-then-ports',
                session_planning='on-demand acknowledged DP observation via MP controld to CP',
                nat_packet_qualification=False, production_admission=False,
                pending=['NAT wire/checksum qualification','ordered session lifecycle and invalidation feed',
                         'aggregate/VLAN attachment qualification','hardware aging/counter handoff'])


def session_pair4(session_id, original, reply, zone, next_hops):
    fields={'source','destination','source_port','destination_port','protocol'}
    for row in (original,reply):
        if not isinstance(row,dict) or set(row)!=fields:
            raise ValueError('complete TCP/UDP conntrack tuples required')
    if original['protocol']!=reply['protocol']:
        raise ValueError('conntrack directions have different protocols')
    if not isinstance(next_hops,(tuple,list)) or len(next_hops)!=2:
        raise ValueError('two verified directional next hops required')
    sid=uint(session_id,31,'session ID');entries=[]
    for i,(row,peer,hop) in enumerate(zip((original,reply),(reply,original),next_hops)):
        key=key4(row['source'],row['destination'],row['source_port'],row['destination_port'],row['protocol'],zone)
        translated=dict(source=peer['destination'],destination=peer['source'],
                        source_port=peer['destination_port'],destination_port=peer['source_port'])
        if translated=={k:v for k,v in row.items() if k!='protocol'}:
            entries.append(forwarding_entry4(key,sid*2+i,hop,decrement_ttl=True))
        else:entries.append(nat_entry4(key,sid*2+i,hop,translated))
    return tuple(entries)
