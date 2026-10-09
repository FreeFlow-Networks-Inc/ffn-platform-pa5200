"""Durable, never-reused IDs for a commissioned FE100 counter namespace.

The serialized CP owner supplies a verified reserved range and its immutable
commissioning digest. This module does not claim or reset a hardware namespace.
Reservations commit before hardware writes; failed installs burn their IDs.
Deleting sessions, restarting a receiver or changing policy never resets them.
"""
from ffn_fe100_sessions import uint, validate_entry4


class FlowIds:
    def __init__(self, db, first, last, domain, *, initialize=False):
        uint(first,32,'first flow ID');uint(last,32,'last flow ID')
        if first==0 or last-first<1:raise ValueError('nonzero commissioned flow-ID range required')
        if (not isinstance(domain,str) or len(domain)!=64 or
                any(c not in '0123456789abcdef' for c in domain)):
            raise ValueError('commissioned namespace digest required')
        if type(initialize) is not bool:raise ValueError('invalid initialization option')
        if db.in_transaction:raise RuntimeError('flow-ID reservation requires its own durable transaction')
        if db.execute('PRAGMA synchronous').fetchone()[0]<2:
            raise RuntimeError('flow-ID journal must use FULL synchronous durability')
        self.db,self.pool,self.failed=db,(first,last,domain),False
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS hardware_flow_ids '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), first_id INTEGER NOT NULL, '
                       'last_id INTEGER NOT NULL, domain TEXT NOT NULL, next_id INTEGER NOT NULL)')
            row=db.execute('SELECT first_id,last_id,domain,next_id FROM hardware_flow_ids WHERE id=1').fetchone()
            if row is None:
                if not initialize:raise RuntimeError('flow-ID namespace has not been commissioned')
                db.execute('INSERT INTO hardware_flow_ids VALUES (1,?,?,?,?)',(first,last,domain,first))
            else:self.validate(row)

    def validate(self,row):
        if (row is None or tuple(row[:3])!=self.pool or type(row[3]) is not int or
                not self.pool[0]<=row[3]<=self.pool[1]+1):
            raise RuntimeError('flow-ID namespace changed or journal is corrupt')

    def reserve_pair(self):
        if self.failed:raise RuntimeError('flow-ID allocator is fenced')
        if self.db.in_transaction:raise RuntimeError('flow-ID reservation cannot share an uncommitted transaction')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT first_id,last_id,domain,next_id FROM hardware_flow_ids WHERE id=1').fetchone()
            self.validate(row)
            first=row[3]
            if first+1>self.pool[1]:raise RuntimeError('commissioned flow-ID namespace exhausted')
            self.db.execute('UPDATE hardware_flow_ids SET next_id=? WHERE id=1',(first+2,))
            self.db.commit()
            return first,first+1
        except BaseException:
            self.failed=True
            self.db.rollback()
            raise


def assign_pair(entries, allocator):
    """Replace codec placeholders only after a durable reservation succeeds."""
    if allocator is None:raise RuntimeError('durable hardware flow-ID allocator is not commissioned')
    if len(entries)!=2:raise ValueError('two flow entries required')
    entries=tuple(validate_entry4(e) for e in entries)
    ids=allocator.reserve_pair()
    if not isinstance(ids,(tuple,list)) or len(ids)!=2 or ids[0]==ids[1]:
        raise RuntimeError('allocator did not return two distinct flow IDs')
    for ident in ids:
        if uint(ident,32,'hardware flow ID')==0:raise ValueError('zero hardware flow ID is reserved')
    return tuple(e[:36]+ident.to_bytes(4,'big')+e[40:] for e,ident in zip(entries,ids))
