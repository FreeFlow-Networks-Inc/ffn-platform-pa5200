# SFP+ transmit check

`ffn_sfp_check.py` (CP) answers one question per SFP+ cage, ethernet1/5 to
1/20: can this port carry traffic, and if not, which layer stops it? It
exists because the insertion re-link ([SFP-LINK.md](SFP-LINK.md)) proved
insufficient on 2026-10-09: ethernet1/5 read link up with its laser at
−5.2 dBm, and no frame left the port, because the dataplane attachment for
the port had been left pending by an interrupted apply. Nothing looked past
the link.

## The chain, in packet order

| Layer | Read from | Failing verdicts |
| --- | --- | --- |
| cage | PCA9555 presence and TX_DISABLE control line (bus 1) | `no-module`, `transmitter-off` |
| module | cached identity; SFF-8472 diagnostics (A2h): launch power, bias, TX fault, RX LOS | `transmitter-off`, `tx-fault`, `laser-off`, `no-rx-light` |
| link | the faceplate's switch MAC view: enabled, link, interface mode against the module's rate | `link-mode-mismatch`, `link-down` |
| attachment | `/etc/ffn/physical-N.json` against the live BCM owner epoch | `attachment-pending`, `attachment-stale`, `attachment-off` |

The first failing layer wins; a port that passes all four is `ok`. A port
the committed configuration disables is `disabled` and judged no further.
Each result carries the measurements (TX/RX dBm, bias, the interface mode
seen and expected, the attachment journal) and a `remedy`:

| Remedy | Who acts |
| --- | --- |
| `enable-transmitter` | the watcher: it deasserts the cage control line through the faceplate |
| `relink` | the watcher: the module recipe again ([SFP-LINK.md](SFP-LINK.md)) |
| `reapply` | the operator: re-apply the committed configuration (Device > Boot & Convergence, or a commit); the MP provider re-attaches the port |
| `hardware` | hands: a dead laser or a faulted transmitter is the module |
| `far-end` | hands: no light from the far end, or a link that does not come up |

## Where it runs

* `ffn-sfp-watch.service` runs it once a minute over the cages that hold a
  module (one diagnostics read per present module, the only new I2C load),
  journals verdict changes as `transmit` events, applies the two local
  remedies once per verdict occurrence and re-judges on the next pass. A bus
  fault backs the check off like a presence fault. The latest results are
  published at `/run/ffn-sfp-health.json`.
* On the CP by hand: `python3 /usr/local/sbin/ffn_sfp_check.py status
  [--ports 5,13] [--no-diagnostics]`.
* From the MP: `ffn-sfp-check status` (resource `sfp-check` through the MP
  daemon), which the console's Faceplate Ports page reads for its Transmit
  column: the verdict as a badge, TX/RX power beside it, the detail and the
  remedy when it is not `ok`.

## Thresholds

Launch power below −20 dBm is `laser-off` (a launched signal sits far
above it on every supported optic); receive power below −30 dBm, or the
module's RX LOS flag, is `no-rx-light`. Both live in the module, not in any
configuration, and are reported as measured.

## What it does not do

It does not re-attach a port: that is the committed configuration's apply,
which the convergence page can replay. It does not read the dataplane's
frame counters; the attachment journal is the dataplane-side evidence it
uses. It does not touch copper ports (1-4), which have their own PHY path.
