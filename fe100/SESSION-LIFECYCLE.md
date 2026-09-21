# FE100 session lease component

`ffn_fe100_lifecycle.SessionLifecycle` wraps a single `PolicyOwner`. The future
trusted evaluator calls `start` only after software policy application and
producer identity verification. It sends strictly ordered open/refresh/close
events plus periodic heartbeats. Refreshes require the exact decision digest;
they cannot keep a session beyond its maximum lifetime without reevaluation.

The owner loop must call `tick` independently of incoming events, with the same
journal/adapter locks used by admission. Expiry, lost sequence, producer restart,
policy replacement, changed bindings or qualification failure fences admissions
and deletes exact journal-owned entries. Unacknowledged deletes retain recovery
state. Monotonic leases are never adopted across process restarts.

This is an internal component with isolated hardware-adapter tests. It is not
wired into the production CP policy endpoint or live session source. It does not
make the FE100 qualified, implement NAT, or change the running admission gate.
Keep the existing recovery timer until the persistent owner and independent
watchdog replace its responsibility with equivalent verified cleanup.
