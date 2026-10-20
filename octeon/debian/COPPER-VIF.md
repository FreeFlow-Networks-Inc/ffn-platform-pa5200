# Copper VIF driver (retired)

`ffn_copper_vif.py`, `ffn_vif_runtime.py`, `ffn_vif.py` and `ffn-vif.service`
are gone. Copper ports 1 through 4 are faceplate ports like the optical ones:

* their packet owner on the DP is `ffn-portd physical --port N`
  (`octeon/native/ffn_portd.c`), reached through `ffn_wan_runtime.py` (port 1)
  and `ffn_physical_runtime.py` (ports 2..4) under the same units and MP
  command lines as every other port;
* the commissioned wiring the driver read from `/etc/ffn/vif-copper.json`
  (front port to PHY and switch port, corrected by copper-identify) is read by
  that owner for the switch port behind a copper front port;
* the expiring PHY/MAC carrier the driver enforced is the owner's link lease:
  the MP's `ffn-port-links.timer` delivers the `faceplate` rows through
  `ffn-portd links`, and a port whose carrier policy is `observed` (copper
  2..4 by default, `/etc/ffn/port-carrier.json` overrides) keeps TAP carrier
  and frame delivery only while a delivery within its lease reports PHY, MAC
  and data path up at one speed. A delayed or replayed delivery cannot revive
  carrier: the owner refuses any sequence that is not newer than the recorded
  one.

The VIF abstraction itself (named virtual interfaces assigned to ports) has
no successor: configure `ethernet1/N` in Network > Interfaces and commit. See
`management/PHYSICAL-ATTACHMENTS.md` and `management/LINK-NEGOTIATION.md`.
