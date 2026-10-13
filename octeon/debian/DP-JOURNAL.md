# The plane journals

`journald-ffn.conf` (installed as `/etc/systemd/journald.conf.d/50-ffn.conf`
on the CP and the DP) keeps each plane's journal across reboots on its NFS
root, bounded to 4 GB, rotated daily and synced every minute.

## What is and is not captured

* Everything userspace logs, up to the last second before a reset. The DP
  reset of 2026-10-10 18:25:44 UTC was reconstructed this way: the previous
  boot's journal ended with ordinary ssh sessions and the aggregate
  watchdog's usual pass, which is what showed the reset was not a crash but
  the CP's `ffn-cold-dp-boot.service`, dispatched by the MP lifecycle after
  its ssh reachability probe had failed for 180 s.
* `journalctl --list-boots` on the plane lists the boots on record;
  `journalctl -b -1 -n 50` is the previous boot's tail.
* Not captured: the kernel's last words on a panic or a hardware reset. The
  DP kernel (6.18.49-ffn-dp-timers) has no `CONFIG_NETCONSOLE`, no pstore and
  no crash kernel. The follow-up is a kernel with `CONFIG_NETCONSOLE=y` and
  `CONFIG_NETCONSOLE_DYNAMIC=y`, the boot argument
  `netconsole=6665@127.1.2.2/ffndp0,6666@127.1.2.1/` on the DP, and a small
  UDP receiver on the CP writing `/var/log/ffn/dp-netconsole.log`; it needs
  a DP reboot to take effect, so it waits for a window.

## The sync warning

journald writes "Failed to sync system journal, ignoring: Permission denied"
once per sync interval on the DP. The DP root is the CP's re-export of an
MP NFS mount; the commit journald issues is refused there while `fsync()`
from any other process on the same file succeeds, and every entry has
reached the file in practice. Treat it as noise unless entries go missing.
