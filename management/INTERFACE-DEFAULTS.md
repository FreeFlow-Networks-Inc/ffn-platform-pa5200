# Front-panel interface defaults

Every unconfigured front data port defaults to **None**. None forces physical
administrative down and disables its dataplane configuration. It does not merely
remove an IP address. On copper ports the faceplate owner disables both the BCM
MAC and external PHY; optical ports use the BCM administrative control.

The WebUI defaults a new interface editor to None and locks Admin State to Down
while that mode is selected. The API defaults an omitted mode to `none`, also
accepts `default`, `off` and `disabled`, and stores None as an interface without
a mode subtree plus `<link-state>down</link-state>`. Saving the editor changes
candidate configuration; Commit applies it through configd and the MP daemon.
Existing explicitly configured modes are preserved.

Configd also disables detected front ports omitted from committed XML, including
ports without a commissioned dataplane attachment. It clears any stale DP port
settings. The MP daemon rejects attempts to directly enable a None or explicitly
admin-down interface, so an operational command cannot bypass the committed
configuration. Select and commit a mode before enabling a front-panel link.

The platform CLI can stage None through the same authenticated candidate API:

```text
request platform interface ethernet1/2 mode none
```

The response reports `requires_commit: true`. Management, HA and AUX interfaces
are outside this front-data-port rule. LACP membership is an explicit mode;
ports with no committed aggregate membership remain down, even if cabled to
each other for loopback testing.
