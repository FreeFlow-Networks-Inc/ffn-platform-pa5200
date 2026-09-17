# Management console hardware integration

The core console supplies the FFN favicon, header icon, persistent Light/Dark
selection, and Device > Setup editor. This selected platform supplies hardware
telemetry and commit application; other platforms do not import these helpers.

## Data-port traffic

`data_port_stats_version: 1` selects the `data_port_stats()` provider. The manager
requests `front-traffic/status` through controld and the MP worker. Its fixed
helper runs `ffn_front_traffic.py` on the CP, reading `port.counters` through the
existing BCM owner and the boot-qualified port-event snapshot. Only the 24
numbered data connectors are included. MP NICs, HSCI and internal links are
excluded. Counter resets, boot changes and long gaps restart rate sampling.

The commissioned BCM counter table exposes packet counters only. The dashboard
therefore shows RX/TX packets per second and explicitly labels byte rates
unavailable. No guessed packet size or management-NIC fallback is used. No BCM
restart is required by this integration.

## External MP interfaces

`mp_interfaces_version: 1` selects the controller inventory of MGT, HA1-A,
HA1-B, AUX-1 and AUX-2, identified by the platform PCI map. The core API writes
`deviceconfig/system/mp-interfaces/entry` in the candidate. It requires an
administrator, the candidate lock and a matching entry revision. The editor's
Cancel has no side effects; OK stages settings; Commit invokes configd.

Configd requests `mp-interfaces/apply` with the exact committed XML digest.
The daemon validates it, writes only per-port `05-ffn-mp-*.network` files matched
to stable PCI paths, then reloads and reconfigures those networkd links. Static
address, admin state and MTU are checked; failures restore prior files and
reconfigure again. DHCP acceptance does not mean a lease has arrived. Active
management address changes can disconnect the session. Installing the feature
does not create MP interface entries or alter current networking.

CLI uses the same candidate endpoint:

```
show platform mp-interfaces
request platform mp-interface AUX-1 '{"mode":"static","address":"192.0.2.10/24","gateway":"","dns":[],"mtu":1500,"description":"Auxiliary management"}'
```

## Rear chassis

The original SVG/CSS view follows the [PA-5200 rear-panel reference](https://docs.paloaltonetworks.com/hardware/pa-5200-hardware-reference/pa-5200-series-firewall-overview/pa-5200-back-panel).
Fan rotation follows the eight reported tachometers; PSU glow follows power-good
readback. Failed or expired observations stop animations. Reduced-motion browser
preferences disable motion without hiding status.

`chassis-storage/status` reads disk statistics without probing/waking drives.
Physical bay mapping is independent of Linux disk enumeration. The reference
sysroot's `usr/lib/python2.7/site-packages/raid/__init__.py:465–483` maps SCSI
hosts 0/1/3/4 to SYS 1/SYS 2/LOG 1/LOG 2. Live PA-5220 paths correlate these to
AHCI controller `0000:00:1f.2`, ATA ports 1/2/4/5. This is the default map.
An administrator can override `/etc/ffn-ngfw/drive-bays.json`, mapping `SYS 1`,
`SYS 2`, `LOG 1`, `LOG 2` to verified basenames from `/dev/disk/by-path` (whole
disks, no partitions). An explicitly unmapped bay says unknown and the detected
disk inventory is shown separately. Mapped drive LEDs animate only on actual I/O.

## Installation

Install core `ffn_port_traffic.py` and `ffn_mp_interfaces.py` alongside the manager
and in the daemon Python path. Install platform `mp_interfaces.py` and
`chassis_storage.py` in the management directory, and `ffn_front_traffic.py`
alongside `ffn_port_events.py` on the CP. Install updated core/extension assets,
manifest, loader, configd adapter and CLI adapter.

Add fixed MP-worker commands to `/etc/ffn/planes/mp.json`:

- `front-traffic/status`: daemon_backend.py front-traffic status
- `chassis-storage/status`: daemon_backend.py chassis-storage status
- `mp-interfaces/status`: mp_interfaces.py status
- `mp-interfaces/apply`: mp_interfaces.py apply

Use the installed MP virtualenv Python and absolute management helper paths as
argv arrays. Restart the MP worker and manager. No CP, DP, BCM or cooling restart
is needed. Configd loads its provider at reconciliation time.

Tests: core `test_port_traffic.py`, `test_console_hardware_api.py`, and
`test_console_hardware_browser.cjs`; platform `test_console_telemetry.py`.
Set `FFN_CORE_ROOT` for standalone platform tests and `TEST_PA5200_ROOT` for
browser tests outside a populated core platform submodule.
