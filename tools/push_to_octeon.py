#!/usr/bin/env python3
"""Push a small binary to the OCTEON over the serial console, as printf hex.

The lean initramfs has no base64/scp and the OCTEON has no network until pcnet is
up (and we are bootstrapping the NFS mount that pcnet enables), so the console is
the transport of last resort. printf with \\xNN escapes is universally available
in the OCTEON's bash, and a few KB at 115200 is a second or two.
"""
import sys
import time

binpath = sys.argv[1]
dest = sys.argv[2]
CI = "/run/ffn-octeon-console.in"
CHUNK = 256

data = open(binpath, "rb").read()
with open(CI, "w") as f:
    f.write("rm -f %s\n" % dest)
    f.flush()
    time.sleep(0.3)
    n = 0
    for i in range(0, len(data), CHUNK):
        chunk = data[i:i + CHUNK]
        esc = "".join("\\x%02x" % b for b in chunk)
        f.write("printf '%s' >> %s\n" % (esc, dest))
        f.flush()
        n += 1
        time.sleep(0.2)
    f.write("chmod +x %s; ls -la %s\n" % (dest, dest))
    f.flush()
print("sent %d bytes in %d chunks to %s" % (len(data), n, dest))
