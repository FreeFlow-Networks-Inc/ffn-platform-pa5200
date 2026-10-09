# Hardware flow-ID ownership

The policy owner now requires an explicit flow-ID allocator before activation.
Logical session IDs no longer determine the IDs written into hardware. Reopening
the same logical session receives different directional IDs, so a delayed
FLOWSTATS packet cannot update the replacement session's kernel accounting.

`FlowIds` reserves pairs in the owner's SQLite journal with FULL synchronous
durability and commits before returning them. An aborted or failed hardware
installation consumes its reservation. Restart, session deletion and policy
replacement never reclaim IDs. Exhaustion blocks allocation without wrapping;
database errors fence that allocator instance. Reservations cannot join a
caller's transaction that could later roll back.

The commissioning caller must supply a verified hardware-reserved range and
its immutable namespace digest. There is no default production range and no
automatic first-use commissioning. A changed range or digest is rejected even
with the initialization option. The journal must survive receiver restarts;
restoring an older database requires a separately verified hardware reset and
new namespace before admission. This allocator does not establish ownership of
ASIC-generated miss IDs or replace counter-receiver supervision.

The CP image contains the allocator, and the deployed policy module requires it.
Since 2026-10-08 the supervised control owner commissions the namespace itself
(`ffn_fe100_flow_namespace.py`): once this boot's FE100 reads initialised it
proves the range 0x10000..0xFFFFFFFE on the hardware in a subprocess (two
isolated entries carrying the first and the last id are inserted, read back
and removed with the tables empty before and after), journals the generation
(CP boot, readiness owner, range) and creates the allocator with a digest over
it. The same boot resumes the journal; a new boot archives the previous
generation with its last counter and rolls over; a failed proof commissions
nothing and is retried a minute later. The flow id is an opaque 32-bit tag in
every session entry and counter record, and the range starts above the fixed
benchmark ids the isolated validators keep. Admission itself is still not
enabled: activation and front-port qualification remain. Existing isolated table-only
validators explicitly retain their fixed benchmark IDs because they transmit
no packets and require empty session tables. Their backend is not a production
allocator.

Tests cover restart after reservation, logical session reuse, delayed statistics
after receiver restart, write failures, exhaustion, changed namespaces and
unchanged NAT action bytes. The allocator and policy/lifecycle tests also pass
on the actual MIPS64 control processor.
