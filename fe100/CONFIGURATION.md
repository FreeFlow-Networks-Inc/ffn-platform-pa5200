# FFN-owned FE100 configuration

`fe100.cfgdb.xml` supplies the PA-5220 FE100 profile to FFN's native adapters.
It uses the familiar `configdb/entry/value` envelope and the `hw.fe100` key.
The value is a `+` prefix followed by strict JSON, interpreted as overrides of
a private native configuration copy. No Python expression is evaluated and
PAN-OS's cfgdb service is not required. This is an FFN consumer; compatibility
with the vendor cfgdb loader has not been tested.

```xml
<configdb>
  <entry name="hw.fe100">
    <value>+ {"usecase": 1, "cfg_mode": 4, "v4_v6_choice": 2}</value>
  </entry>
</configdb>
```

The initial supported tuple is the PA-5220 profile read from the owner's VM
at `/mnt/clones/5220-sysroot1-full/etc/cfgdb/dp/5200/fe100.cfgdb.xml`.
It retains the settings already used by FFN's audited native adapters.
Unknown keys, duplicate entries/keys, malformed XML/JSON, DTDs, booleans in
numeric fields and other profile combinations are rejected. Broader memory
partitions and use cases require separate native and hardware qualification.

`ffn_fe100_config.py` only reads and validates files and copies three fields
into the existing 2,812-byte native configuration. Existing owner-library
hash checks, register scopes, calibration journals, locks and readiness gates
remain the responsibility of the adapters. Reading the profile cannot mark
hardware initialized or enable session offload.

The block initializer, TDI DDR training/audit, FHM/FDT memory initializer,
FLU initializer, SEM initializer and native session owner now use this shared
loader. The block initializer retains its existing explicit lab `--usecase 2`
override; that diagnostic mode cannot be selected through the XML profile.

Validate a file without hardware access:

```sh
python3 fe100/ffn_fe100_config.py --config fe100/fe100.cfgdb.xml
```

For a coordinated installation, place the helper alongside the FE100 Python
adapters on the CP and the profile at `/etc/ffn/fe100.cfgdb.xml`. Install the
helper with all changed adapters; copying only an adapter will fail its import.
The offline audit environment needs the helper too. No automatic installer or
hot-apply action is introduced by this change.

During migration, an absent default file retains the previously hard-coded
audited profile. An explicitly selected missing file, an unreadable file or an
invalid installed file is an error, never a fallback. Each native configuration
construction reads the file; existing initialized hardware and open session
owners are not reconfigured by editing XML. A valid profile is desired input,
not evidence of applied hardware state.

The MP should own any future configuration commit API and coordinate CP
application with draining sessions and checking readback. Until that lifecycle
is implemented, alternate profiles and live repartitioning remain unsupported.
No PHY, port mapping, policy, flow or readiness settings are accepted in this
hardware profile.
