# OCTEON images built by GitHub Actions

## Independent patch-server pulls

Device > Patch Management shows separate Control Plane and Data Plane image
cards when the PA-5200 extension is selected. Each has its own Check and Download
operation, revision, job and cache. The console uses the same controld resource:

```text
show platform images
request platform image cp check
request platform image dp check
request platform image cp download SHA256_FROM_CHECK
request platform image dp download SHA256_FROM_CHECK
```

The MP worker registers `plane-images` with status, validate and apply operations.
It starts a bounded systemd download worker, so closing the browser does not
interrupt a transfer. No WebUI HTTP request is needed from the console.
The update-server URL and Ed25519 public key are shared with core Patch Management;
TLS uses the MP's CA trust. Provision the patch server's CA rather than disabling
certificate verification.

Publish a completed build to the existing signed patch repository one role at a time:

```sh
python3 octeon/images/publish_patch.py --core /path/to/core \
  --dir /path/to/public/patches --build /path/to/completed-build \
  --role cp --version VERSION --seed /private/sign.key --public-key /path/to/update.pub
```

Repeat with `--role dp` when that image is ready. Publishing either image preserves
the other role and the core code patch; code-patch publication also preserves both
roles. Catalog keys are `pa5200-cp` and `pa5200-dp`. Downloads require the checked
digest, verify the signed size/hash and embedded role/ABI/source metadata, and
reject catalog rollback. Failed transfers preserve the previous staged image.
State lives below `/var/lib/ffn-ngfw/pa5200-images/{cp,dp}`.

These operations stage complete images on the MP. They do not activate them,
change boot selection, or restart the CP/DP. The UI explicitly labels unqualified
build candidates. Local root provisioning, an explicit boot operation and fresh
agent/hardware acknowledgments are required before reporting a live update.

## Build clean Debian seeds

In the isolated Debian builder with MIPS64 BE emulation and the source-built
`rebootstrap` repository, run `build_rootfs.py --role cp|dp --repository PATH
--out NEW_DIRECTORY`. It installs real Debian packages in a new root, checks their
configuration, records source versions, and removes bootstrap logs and generated
SSH/machine identity. No existing appliance root is accepted.

`build_initramfs.py` takes `--role`, `--busybox` (busybox-static .deb), `--transport`
(ffn-octeon-transport .deb), `--base-files` (.deb), `--source-date-epoch` and `--out`.
It produces a plain newc archive with static Debian/FFN executables and records
the exact input hashes. The DP starts its recovery mailbox before dpnet; both
roles retain their transports in RAM through systemd handoff. The MP must supply
`ffn.nfsroot=SERVER_IPV4:/PROVISIONED_EXPORT` in the kernel command line. There is
no default export, customer address, password or SSH key in either seed.
The root must match the image role and have locally provisioned SSH identity.
Missing provisioning keeps the recovery environment available.

Include the matching Debian source packages, patches and licensing notices in
the corresponding-source bundle, including glibc/GCC used by static executables.
Seed creation and QEMU execution checks do not establish hardware boot readiness.
`collect_sources.py --rootfs CLEAN_ROOT --cache SOURCE_CACHE --out SOURCE_DIRECTORY`
collects exact source versions from dpkg metadata and `Built-Using`. It verifies
archive SHA-256 values from each Debian source descriptor, and can recover
superseded versions from Debian Snapshot when the current APT index has moved on.
Archive that directory as the build profile's `corresponding_sources` input.

The platform's `OCTEON CP and DP images` workflow builds two **MIPS64 big-endian**
Debian/glibc bundles. The management-plane image is Ubuntu amd64. Each bundle contains `vmlinux`, its matching kernel modules and root filesystem,
`kernel.config`, and `image.json`. Both come from the same pinned platform/core
source pair. They are boot candidates until tested on hardware.

This replaces copying a configured appliance root to distribute software.
Networking, accounts, SSH identity, certificates, plane authentication and
customer configuration must be provisioned locally by the management plane.
The build adds the common FFN plane and policy modules and the role's observation
agent using the reviewed `overlay.json`. Services are installed but not enabled:
the commissioned platform controller supplies its transports and runtime config.
It does not bundle the proprietary BCM/FE100 compatibility root. Any required
vendor components stay on the appliance and require separate local integration.

## Builder setup

Use an isolated Linux x64 runner labelled `ffn-octeon-builder`, with Python 3.12+,
git, make, native kernel build prerequisites and a MIPS64 big-endian cross compiler.
OCTEON source must be Linux 6.18 or newer (`os-policy.json`); OpenWrt/musl
compilers and foreign distro roots/initramfs are rejected. Python and systemd
must be owned by installed Debian `mips64` packages. Use a GNU userspace
toolchain with a Debian glibc sysroot for agents/transports; a nolibc kernel
compiler cannot build those executables.

The seed must also contain the configured role packages listed in
`../packages/runtime-requirements.json`. A CP requires native Debian NFS-server
packages; a DP requires its policy/networking tools. Missing packages fail the
build before kernel compilation. See [Debian packages](../packages/README.md)
for the transport source-package builder and the runtime inventory command.

The CP overlay installs disabled native NFS and DP transport units. The MP must
provision `/etc/exports.d/ffn-dp.exports` with the selected root, exact transport
client and filesystem ID before starting NFS. The transport unit copies its
packaged executable to `/run/ffn-dp` before starting; it must still be stopped
while the DP is reset. These units do not replace BCM/FE100 or DP boot qualification.

The CP overlay also carries the I2C mux service, thermal governor, chassis
status/LED helper and their I2C reader. Kernel checks require OCTEON I2C,
the character device, PCA954x mux support and CPLD memory access. Drivers load
from the running kernel's module tree rather than a previous kernel's loose
modules. These services remain disabled in the generic image: after identifying
the supported chassis, the MP must provision its authenticated temperature feed
and enable `ffn-thermal.service` (which requires the mux service). Missing or
stale temperatures and invalid fan feedback demand full cooling.

Qualify each deployment through the MP control daemon: check all twelve board
sensors, eight fan tachometers, both PSU presence/power-good signals, MP sensor
freshness, and the automatic governor's watchdog. A bounded full-speed request
must produce eight full PWM readbacks and increased RPM, followed by verified
restoration of automatic mode. A successful I2C read alone is not this test.

FE100 observation code is included in the CP overlay. The owner must provision
the matching register metadata separately as described in
[native FE100 observations](../../fe100/NATIVE-OBSERVATION.md). Missing metadata
keeps the agent's read-verification flag false; neither file installation nor
register readback is a forwarding acknowledgment.

Do not register the firewall itself as a build runner. Set the `OCTEON_BUILD_PROFILE`
variable in GitHub's `octeon-build` environment to the absolute path of a
runner-owned profile. Restrict that environment to reviewed refs; this workflow
does not run on pull requests. Protect `octeon-release` for publication review.

Profile structure (replace the descriptions with reviewed paths and real hashes):

```json
{
  "schema": 1,
  "redistributable_inputs_reviewed": true,
  "jobs": 8,
  "planes": {
    "cp": {
      "kernel_repository": "/srv/ffn-build/linux-cp",
      "kernel_commit": "FULL_40_CHARACTER_COMMIT",
      "cross_compile": "/srv/ffn-build/toolchain/bin/mips64-linux-",
      "config": {"path": "/srv/ffn-build/config-cp", "sha256": "SHA256"},
      "initramfs": {"path": "/srv/ffn-build/cp.cpio", "sha256": "SHA256"},
      "rootfs": {"path": "/srv/ffn-build/cp-clean.tar.xz", "sha256": "SHA256"},
      "corresponding_sources": {"path": "/srv/ffn-build/cp-sources.tar.xz", "sha256": "SHA256"}
    },
    "dp": {
      "kernel_repository": "/srv/ffn-build/linux-dp",
      "kernel_commit": "FULL_40_CHARACTER_COMMIT",
      "cross_compile": "/srv/ffn-build/toolchain/bin/mips64-linux-",
      "config": {"path": "/srv/ffn-build/config-dp", "sha256": "SHA256"},
      "initramfs": {"path": "/srv/ffn-build/dp.cpio", "sha256": "SHA256"},
      "rootfs": {"path": "/srv/ffn-build/dp-clean.tar.xz", "sha256": "SHA256"},
      "corresponding_sources": {"path": "/srv/ffn-build/dp-sources.tar.xz", "sha256": "SHA256"}
    }
  }
}
```

Kernel repositories must include the board patches and any required in-tree
transport/packet drivers in the pinned commit. Dirty/untracked kernel changes
are excluded by `git archive`. The rootfs seeds must be clean Debian MIPS64 BE
roots with Python, systemd and role-specific networking dependencies already
built. Pin these archives rather than copying a mutable running root.
`corresponding_sources` includes the exact sources, patches, build instructions
and licensing notices for everything in the seed/initramfs, including tools
statically linked into them. Its completeness requires release review.

Initramfs seeds are plain `newc` CPIO archives with no device nodes, credentials
or customer configuration. Their boot handoff must use the platform's provisioned
boot/export settings, not a lab path. The builder checks seeds for machine state
and private keys; this is a rejection gate, not a substitute for input review.
No recovered vendor sysroot is an acceptable seed. Existing development scripts
with dated NFS paths are not called by this release workflow.

## Build and promote

1. Dispatch `octeon-images.yml` on the reviewed platform ref with a full
   `core_commit`. The builder copies exact kernel source, builds both kernels and
   modules, overlays current FFN agents, and emits paired archives and source
   packages. A GitHub-hosted job checks the checksums, attests the files and
   creates a **draft** release. No publishing credential goes to the builder.
2. Download and test both candidates on an isolated appliance. Verify boot,
   MP/CP/DP agent handshakes, transport, port/LACP recovery and policy/NAT behavior.
   Record the exact manifest digest and results. A minimal promotion report has
   `manifest_sha256`, plus `cp` and `dp` objects each containing
   `boot_verified: true` and `agent_handshake_verified: true`. Attach the detailed
   test evidence to the release; these booleans do not replace that evidence.
3. Publish the qualified release, then generate a proposed lock:

   ```sh
   python3 octeon/images/promote.py --manifest manifest.json \
     --tag RELEASE_TAG --qualification qualification.json --out plane-images.next.json
   ```

   This verifies GitHub provenance against this repository's workflow and checks
   that the published tag serves the same manifest. Review and replace
   `plane-images.json` with the generated lock; commit it and advance the core's
   platform gitlink. The image's `platform_commit` remains the build commit, not
   the later lock-promotion commit. Never edit image payloads under an existing tag.

Sources and checksums are release assets as well as temporary workflow artifacts.
Enable GitHub immutable releases where available. Attestation establishes
workflow provenance; it does not establish hardware readiness or seed licensing.

## Automatic MP download

Core `ffn_platform.py select pa5200` checks out the selected submodule, reads its
`plane-images.json` and fetches the pinned manifest and both images using the
MP's HTTPS connection. Existing checkouts can run `ffn_platform.py images pa5200`.
For deployed systems without a git checkout, run the core installer:

```sh
sudo sh image/install-plane-images.sh /path/to/selected/platform
```

It copies the trusted lock and downloader and enables `ffn-plane-images.service`
at MP startup. It also starts a staging attempt asynchronously. Retry a failed
attempt with `systemctl restart ffn-plane-images`; inspect its journal for status.
Re-run the installer when a new platform package changes the lock. Python module
imports and WebUI registration remain free of network IO.

The installed lock pins the manifest SHA-256; that manifest pins each complete
image, role, kernel release, architecture, ABI and source revisions. A mutable
GitHub tag cannot substitute different bytes. The trusted lock comes from the
reviewed platform package, never from the download server. The downloader does
not accept `latest`, skip verification, execute release scripts or extract files.
It supports public GitHub release assets; private release authentication is not
implemented. Device Internet access and normal CA verification are required for
uncached assets. Both images are made visible together only after verification.

Cache: `/var/lib/ffn-ngfw/plane-images/pa5200/<manifest-sha256>/`.
`--offline` verifies an existing pair without network access. Corrupt or missing
images fail closed and leave existing boot selections untouched. A missing
published release is reported as `not-published`; the initial checked-in lock
deliberately has `enabled: false` until real hardware qualification is complete.

**Staging is not activation.** The current commissioned CP/DP boot paths still
require local root provisioning, credentials, NFS export setup and explicit
selection of the staged kernel/root pair. This change never resets OCTEON,
restarts BCM or LACP, or makes a new image active just because it downloaded.

Tests: `python3 -m unittest discover -s octeon/images -p 'test_*.py'`.
Core tests: `python3 -m unittest discover -s tests -p test_plane_images.py`.

## Inspect inputs before a build

```sh
python3 octeon/images/image_policy.py --rootfs /path/to/clean-debian-root --role cp
python3 octeon/images/image_policy.py --kernel /path/to/pinned-linux-source
```

These commands read metadata without executing target programs. The complete
builder also audits ELF files, archives, credentials and source revisions.
A Debian bootstrap root with loose Python/SSH files is a development input,
not a release seed. Build and install corresponding Debian packages before
pinning a seed; never relabel a foreign root by replacing `os-release`.
