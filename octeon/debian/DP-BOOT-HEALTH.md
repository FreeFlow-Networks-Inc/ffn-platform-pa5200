# Dataplane boot qualification

The DP readiness agent previously set `ready` from OCTEON detection alone and
reported `forwarding` when a fresh inspection runtime file existed. Neither
condition demonstrates a completed Debian boot or physical forwarding.

`ffn_dp_boot_health.py` now checks the OS identity in the process root and PID
1's root, compares their filesystem identities, and requires PID 1's executable
to be Debian systemd. A Debian filesystem mounted under a legacy `/init`, an
initramfs console shell, and a separate staged Debian copy are distinct states.
Missing observations fail readiness and return reasons.

`ffn_dp_agent.py` additionally requires an OCTEON with at least 16 visible CPU
entries to distinguish this platform's DP from its 8-core CP. It reports
`boot-incomplete`, `ready`, or `inspection-active` on DP hardware. Inspection
activity alone no longer produces a forwarding claim. `forwarding_verified`
remains false until separate packet-path evidence is integrated.

Install both Python files together in the DP's `/usr/local/sbin`. Existing
agent processes must restart to load the new implementation. The root-only
`diagnose` action produces a one-shot snapshot without starting an agent or
requiring its socket; `status` still performs the nonce-checked socket handshake.
The management UI displays boot reasons when an agent response includes them.

## Live check, 2026-09-11

The CP's `ffn-dpnet.service` was inactive. Starting it restored two successful
pings to 127.1.2.2 without resetting either processor. DP SSH still refused
connections. The diagnostic was run through the console transport using
`chroot /proc/1/root /usr/bin/python3 /usr/local/sbin/ffn_dp_agent.py diagnose`.

It reported MIPS64, 40 OCTEON CPU entries, Debian for both roots, identical root
identities, PID 1 executable `/init`, and `ready: false` / `boot-incomplete`.
Literal, credit-card, SSN and API-key inspection entry points loaded; no active
inspection runtime was observed. This verifies loading, not detector accuracy
or packet throughput. The running kernel was 6.18.49-00007-g616977ba1b11.

Five fixture tests cover a completed Debian/systemd boot, legacy init over a
Debian root, a staged Debian copy, missing proc observations and another OS.
The next boot task is the PID 1 systemd handoff. No DP reboot, physical port
assignment, routing change or LACP activation was performed in this pass.
