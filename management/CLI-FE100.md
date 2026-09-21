# FE100 operational CLI

The PA5200 extension reads CP observations through the authenticated MP
`/api/system/control` endpoint. These commands do not modify configuration or
program hardware:

```text
show platform fe100 status
show platform fe100 driver
show platform fe100 counters
show platform fe100 policy
show platform fe100 recovery
show platform fe100 json
help platform
```

`status` summarizes register access, policy generation, journaled session
count, recovery outcome and hardware verification. Every view includes agent
freshness and observation age. Historical observations are explicitly labelled;
the summary does not present stale acknowledgements as current verification.
Missing observations are unknown, rather than zero counters or a successful
drain. There is no CLI activation/reset shortcut for unqualified hardware.

Append `json` to a specific view for structured output, for example
`show platform fe100 recovery json`. The original bare `show platform fe100`
continues to return the complete JSON observation for compatibility.

## Installation

Deploy `cli_extension.py` into the selected PA5200 management extension, then
run `python3 install-cli.py` on MP. The installer merges dispatch, help and
completion into the installed `/usr/local/bin/ffn-cli`, preserving its other
commands and policy hook. It backs up the shell, rejects unknown integration
changes, and supports repeat invocation. Reconnect existing CLI sessions to
load shell changes. No service restart or reboot is required.

Command parsing precedes platform dispatch. Invalid syntax or a failed
platform request returns an error without terminating the CLI. Tab completion
uses the extension's command vocabulary without issuing API requests.

The API endpoint is resolved from `FFN_CLI_API`, then the `api_url` property in
`/etc/ffn-ngfw/cli.json`. If neither exists, the previous local port 8443
default remains for existing images. Configure the file to match the deployed
manager listener; it is appliance configuration, not a hardcoded platform
address. Only an HTTP(S) base URL is accepted, without credentials, query
parameters, or a fragment. A malformed file fails explicitly. The file needs
to be readable by CLI users and writable only by the administrator.

## Validation

Run the platform CLI unit and actual shell integration tests on Linux:

```sh
FFN_CLI_TEST_SOURCE=/usr/local/bin/ffn-cli python3 -m unittest test_cli_extension test_cli_install
```

Fourteen tests passed against the installed shell. All seven commands above
also passed live through the shell's own API client. The live summary correctly
reported verified register access and an empty recovered journal while hardware
activation and forwarding remained unverified.
