# PA-5220 LACP controls

The optional PA-5220 management module provides LACP group profiles and a Linux
802.3ad bond lifecycle. Installation assigns no members and creates no bonds.
It does not alter the core FFN platform or configure the attached peer.

## Configuration and operation

The MP `/usr/local/sbin/ffn-lacp` wrapper invokes the DP `ffn_lacp.py` using the
existing pinned plane connection. Profiles are stored in `/etc/ffn/lacp.json`.
An absent file means revision 0 and an empty groups object. The controller shares
the network lock with ordinary port configuration.

* `status`: profiles, backend capability and live bond/member/partner diagnostics.
* `set`: JSON `{revision, groups}` replaces saved profiles, incrementing revision.
  Saving does not activate or assign any live interface.
* `activate`: JSON `{revision, group}` creates the selected Linux 802.3ad bond.
* `deactivate`: the same request shape removes the owned bond and restores its
  members' disabled network configuration. An inactive group is a no-op.

Groups are named lag1..lag12. Each requires these fields:

| Field | Values |
|---|---|
| members | 2..8 unique p1..p24 names; no overlap across profiles |
| activity | active or passive |
| rate | fast or slow |
| hash | layer2 or layer2+3 |
| min_links | 1..member count |
| system_priority | 1..65535 |
| network | Existing L2 VLAN/pvid or L3 addresses/optional VRF settings |

The WebUI has save/delete controls, protocol settings, member selection by
name, L2/L3 settings, separate activate/deactivate actions and raw kernel partner
diagnostics under Dataplane Interfaces. API endpoints `/api/pa5200/lacp` and
`/api/pa5200/lacp/{set,activate,deactivate}` use core authentication, admin checks,
bounded JSON input and audit logging. Read-only users cannot change profiles.
The current WebUI uses the equivalent `/api/system/runtime/lacp` routes. The
management adapter sends mutations through the MP control daemon as `apply`
with an explicit operation; it does not bypass that daemon to execute helpers.

Activation requires members disabled in network controls, without addresses or
another master. Ordinary network edits reject active bond members. Editing or
deleting an active profile is rejected. The controller marks owned bonds with
an interface alias and does not manage unrelated bonds. Failed activation
attempts detach members, restore their original MTU/MAC and remove the new bond;
cleanup failures are reported rather than hidden.

## Current qualification and limits

The current MP SSH relay is **not qualified for physical LACP activation**.
Its TAP carrier does not represent actual front-port carrier and the complete
LACPDU path has not been tested. The controller rejects activation on that
backend before writing to interfaces, and the UI withholds the activation
button. A future backend must explicitly advertise `lacp_qualified: true` in
its root-owned runtime state after carrier, speed/duplex and slow-protocol
transport qualification. Virtual member devices remain rejected in production.
Do not set that capability merely to bypass the checks.

Profiles persist; runtime activation is explicit and is not replayed after a
reboot. This version supports connected routing on a group, not integration of
bond names into the static-route/overlay editors. Hardware trunk offload,
multi-chassis LAG and physical PA-5220 negotiation are not claimed.

Linux 802.3ad requires appropriate member speed/duplex reporting and a configured
LACP partner; both ends being passive will not initiate negotiation. See the
[Linux bonding documentation](https://docs.kernel.org/networking/bonding.html).

## Verification, 2026-09-11

Nine controller tests cover schema, overlapping members, revisions, separation
of save/activate, active profile protection, unsupported relay rejection and
partial activation cleanup. Fifteen network tests cover existing configuration
behavior and member protection. Fourteen API/adapter tests and the WebUI tests passed.

`test-lacp-kernel.py` created two temporary namespaces on the MP, using only veth
interfaces. It exercised the bond creation implementation with an explicitly
substituted test preflight, verified nonzero partners and two aggregated members,
passed IPv4 traffic, then passed traffic after disabling one member. Both test
namespaces were removed in finally. This tests the Linux implementation, not
the BCM/FE100/DP physical path. No physical ports or persistent profiles were
assigned for testing.

Installed on the MP/DP with an empty revision-0 profile set. The manager is
active. The final read-only check found network revision 60 and the fabric
relay inactive; neither was changed by LACP installation. Runtime status
correctly reports no physical backend attached and activation unavailable.

The live management package was newer than the initial checkout. Its daemon,
runtime-router, faceplate and inspection interfaces were retained while adding
LACP. Both API paths reject unauthenticated requests with 401, and a read-only
MP daemon RPC successfully returned the empty profile and capability state.

For daemon-based installations, add a `lacp` entry to the `commands` object in
`/etc/ffn/planes/mp.json`, with `status`, `validate`, and `apply` mapped to the
same Python interpreter and `daemon_backend.py` used by the other resources,
followed by arguments `lacp` and the action. Restart `ffn-plane@mp.service` and
the manager after installing the updated management package. The installed
unit's existing command paths should be retained rather than guessed.
