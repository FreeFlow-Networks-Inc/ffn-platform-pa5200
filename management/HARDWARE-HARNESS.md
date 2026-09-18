# Optional PA-5200 hardware harness

Importing `hardware_harness` performs no hardware I/O. Only construct it from
the already-selected platform provider. Do not infer PA hardware from an API
request parameter supplied by a client.

```python
from hardware_harness import create_harness, installed_adapters, router

harness = create_harness(selected_platform.id, installed_adapters)
if harness is not None:
    app.include_router(router(harness, current_user, require_admin, record_audit))
```

Mount this specific router before generic `/{resource}/{action}` routes.
The existing FFN authentication callbacks must be supplied; there are no
anonymous or permissive default callbacks. The production mount is pending.

Routes:

- `GET /api/pa5200/hardware/status`: independent CP/DP availability, DDR status,
  external-table recovery state, DMA capacities, SSO/PKO preparation and explicit
  unqualified forwarding/session capabilities.
- `POST /api/pa5200/hardware/prepare`: administrator only; exactly
  `{"operation":"prepare-sso","boot_id":"<current DP boot ID>"}`.
  Supported operations are prepare-pki, prepare-dma, prepare-sso,
  prepare-pko-memory, prepare-pko-queues and prepare-trunk. Current physical engine status must
  be idle. Existing completed stages return unchanged. No automatic retry.
- `POST /api/pa5200/hardware/runtime`: administrator only;
  `{"operation":"start-trunk","boot_id":"<current DP boot ID>"}` or
  `stop-trunk`. Requires the registered `ffnpkt0` runtime; checks the boot ID,
  engine enables, queue state and errors before reporting success. A fault
  blocks starting but still permits an attempt to stop. No automatic retry.

These API controls commission the internal packet trunk. They do not establish
end-to-end forwarding, reset live tables, accept raw addresses, assign ports, or expose an
unqualified session backend. Session lifecycle code resides in
`fe100/ffn_fe100_sessions.py` and `ffn_fe100_journal.py`; native hardware adapter
integration still requires session/packet qualification. TCAM configuration
readback has passed after NOP synchronization recovery; see
`../fe100/TCAM-RECOVERY-20260914.md`.

Installation layout:

- MP: hardware_harness.py and hardware_backend.py in the optional provider;
  `octeon/debian/ffn-dp-prepare` at `/usr/local/sbin/ffn-dp-prepare` mode0755.
- DP: ffn_dp_hardware_status.py, ffn_dp_packet_init.py and existing DP agent
  modules in `/usr/local/sbin`; initialization kernel module loaded separately.
- CP: FE100 clock, DDR, external-runtime and hardware-status helpers in
  `/usr/local/sbin`; audited native adapter in `/usr/local/lib/ffn`.

The MP bridge uses the existing pinned CP/DP SSH configuration. State files
under `/var/lib/ffn/fe100` are commissioning journals, never proof by themselves
that table entries survived a device reset. Failed journals block reinitialization.
