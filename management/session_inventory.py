"""MP session views over the relay's published inventory.

The core manager imports this through `platform_mod('session_inventory')` for
its dashboard counters, session browser and `show session` verbs. Everything
here is observation data read from the relay's reports: nothing programs
hardware, and a report from a dead or restarted relay is reported as
unavailable rather than as an empty table.
"""
import time

from session_stream import RELAY_INVENTORY,RELAY_REPORT,read_inventory,read_status

PROTOCOLS={1:'icmp',6:'tcp',17:'udp'}
# nf_conntrack's TCP conntrack states (TCP_CONNTRACK_*); a UDP flow has none.
TCP_STATES={0:'NONE',1:'SYN_SENT',2:'SYN_RECV',3:'ESTABLISHED',4:'FIN_WAIT',5:'CLOSE_WAIT',
            6:'LAST_ACK',7:'TIME_WAIT',8:'CLOSE',9:'SYN_SENT2'}
OFFLOAD_FILTERS=('candidate','blocked','l3-ready')
SOURCE='dataplane-session-stream'


def offload(row):
    """One word per session: what the FE100 path would do with it today."""
    if (row.get('l3') or {}).get('available') is True:return 'l3-ready'
    return 'candidate' if row.get('candidate') else 'blocked'


def present(row,now):
    """A compact row plus the names and ages the views print."""
    proto=row.get('proto');state=row.get('state');since=row.get('since')
    value=dict(row,protocol=PROTOCOLS.get(proto,str(proto) if proto is not None else '-'),
               state_name=(TCP_STATES.get(state,'UNKNOWN') if proto==6 else 'ACTIVE'),offload=offload(row),
               age_seconds=(round(max(now-since,0),1) if type(since) in (int,float) else None))
    value['short_id']=row['id'][:12]
    return value


def summary(report=RELAY_REPORT,inventory=RELAY_INVENTORY):
    status=read_status(report);table=read_inventory(inventory)
    ready=table['ready'];counters=(table.get('counters') or {}) if ready else {}
    limits=(table.get('limits') or {}) if table['available'] else {}
    value=dict(source=SOURCE,mode='observation-only',hardware_admission=False,
               available=table['available'],fresh=table['fresh'],ready=ready,age_seconds=table.get('age_seconds'),
               sessions=counters.get('sessions') if ready else None,
               software_candidates=counters.get('software_candidates') if ready else None,
               l3_candidates=counters.get('l3_candidates') if ready else None,
               hardware_admitted=0 if ready else None,blocked=counters.get('blocked') if ready else None,
               tcp=counters.get('tcp') if ready else None,udp=counters.get('udp') if ready else None,
               other=counters.get('other') if ready else None,
               nat_source=counters.get('nat_source') if ready else None,
               nat_destination=counters.get('nat_destination') if ready else None,
               blockers=(counters.get('blockers') or {}) if ready else {},
               max_sessions=limits.get('conntrack_max'),stream_capacity=limits.get('stream_capacity'),
               sequence=table.get('sequence') if table['fresh'] else None,
               policy_revision=table.get('policy_revision') if table['fresh'] else None,
               cp_acknowledged=bool(status['fresh'] and status.get('cp_acknowledged') is True),
               configuration_digest=status.get('configuration_digest') if status['fresh'] else None,
               reason=None if ready else (table.get('reason') if table['available'] else 'session relay report unavailable'))
    if ready and value['max_sessions']:
        value['capacity_percent']=round(value['sessions']/value['max_sessions']*100,2)
    return value


def matches(row,query=None,vsys=None,offload_filter=None,protocol=None):
    if vsys not in (None,''):
        scope=row.get('vsys')
        if scope!=vsys and scope!='vsys'+str(vsys) and str(scope)!=str(vsys):return False
    if offload_filter is not None and row['offload']!=offload_filter:return False
    if protocol is not None:
        wanted=str(protocol).lower()
        if wanted!=row['protocol'] and wanted!=str(row.get('proto')):return False
    if query:
        needle=str(query).lower()
        fields=[row.get(k) for k in ('id','src','dst','xsrc','xdst','rule','vsys','protocol','state_name','offload')]
        fields+=[('%s:%s'%(row.get('src'),row.get('sport'))),('%s:%s'%(row.get('dst'),row.get('dport')))]
        fields+=list(row.get('blockers') or [])
        if not any(needle in str(f).lower() for f in fields if f is not None):return False
    return True


def entries(limit=100,offset=0,query=None,vsys=None,offload_filter=None,protocol=None,inventory=RELAY_INVENTORY,now=None):
    """The session browser page: filtered, oldest first, with the inventory's counters."""
    if offload_filter not in (None,'')+OFFLOAD_FILTERS:raise ValueError('offload filter must be one of '+', '.join(OFFLOAD_FILTERS))
    table=read_inventory(inventory);now=time.monotonic() if now is None else now
    rows=[present(row,now) for row in table['sessions']]
    rows=[row for row in rows if matches(row,query,vsys,offload_filter or None,protocol)]
    rows.sort(key=lambda row:(-(row['age_seconds'] or 0),row['id']))
    limit=max(0,int(limit));offset=max(0,int(offset))
    return dict(source=SOURCE,mode='observation-only',hardware_admission=False,
                available=table['available'],fresh=table['fresh'],ready=table['ready'],
                age_seconds=table.get('age_seconds'),sequence=table.get('sequence') if table['fresh'] else None,
                policy_revision=table.get('policy_revision') if table['fresh'] else None,
                counters=(table.get('counters') or {}) if table['ready'] else {},
                limits=(table.get('limits') or {}) if table['available'] else {},
                reason=None if table['ready'] else (table.get('reason') if table['available'] else 'session relay report unavailable'),
                total=len(table['sessions']),matched=len(rows),entries=rows[offset:offset+limit])


def breakdown(inventory=RELAY_INVENTORY):
    """The dashboard's protocol/state histogram, in the shape the conntrack histogram uses."""
    counts=dict(tcp_established=0,tcp_other=0,udp=0,icmp=0,other=0)
    for row in read_inventory(inventory)['sessions']:
        proto=row.get('proto')
        if proto==6:counts['tcp_established' if row.get('state')==3 else 'tcp_other']+=1
        elif proto==17:counts['udp']+=1
        elif proto==1:counts['icmp']+=1
        else:counts['other']+=1
    return counts


def entry(identity,inventory=RELAY_INVENTORY,now=None):
    """One session by full identity or a unique prefix of at least eight hex digits."""
    if not isinstance(identity,str) or len(identity)<8 or any(c not in '0123456789abcdef' for c in identity.lower()):
        raise ValueError('session id is a hex identity prefix of at least eight digits')
    table=read_inventory(inventory);now=time.monotonic() if now is None else now
    found=[row for row in table['sessions'] if row['id'].startswith(identity.lower())]
    if len(found)>1:raise ValueError('session id prefix is ambiguous')
    return dict(available=table['available'],fresh=table['fresh'],ready=table['ready'],source=SOURCE,
                session=present(found[0],now) if found else None)
