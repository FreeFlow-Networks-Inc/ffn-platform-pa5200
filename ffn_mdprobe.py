#!/usr/bin/env python3
"""Read an md RAID superblock read-only, without mdadm and without assembling.

Assembling an array can start a resync, and these drives hold the previous
owner's logs, so nothing here writes or assembles. It locates the superblock,
reports the array geometry, and works out where the member's DATA starts so the
filesystem inside can be mounted read-only with an offset if wanted.
"""
import struct
import sys

MD_MAGIC = 0xA92B4EFC
LEVELS = {0: "raid0", 1: "raid1", 4: "raid4", 5: "raid5", 6: "raid6",
          10: "raid10", -1: "linear", -4: "multipath"}


def read_at(fh, off, n):
    fh.seek(off)
    return fh.read(n)


def parse_v1(sb):
    """md superblock version 1.x (little-endian)."""
    magic, major, feature, pad0 = struct.unpack_from("<IIII", sb, 0)
    if magic != MD_MAGIC:
        return None
    set_uuid = sb[16:32].hex()
    name = sb[32:64].split(b"\x00")[0].decode("ascii", "replace")
    ctime, level, layout, size = struct.unpack_from("<QiiQ", sb, 64)
    chunk, raid_disks = struct.unpack_from("<II", sb, 88)
    data_offset, data_size = struct.unpack_from("<QQ", sb, 128)
    sb_size = struct.unpack_from("<Q", sb, 152)[0]
    events = struct.unpack_from("<Q", sb, 200)[0]
    return {
        "version": "1.x (major=%d)" % major,
        "set_uuid": set_uuid,
        "name": name,
        "level": LEVELS.get(level, str(level)),
        "raid_disks": raid_disks,
        "array_size_sectors": size,
        "data_offset_sectors": data_offset,
        "data_size_sectors": data_size,
        "chunk_bytes": chunk,
        "events": events,
    }


def probe(dev):
    out = {"device": dev, "found": False}
    try:
        with open(dev, "rb") as fh:
            fh.seek(0, 2)
            devsize = fh.tell()
            out["device_bytes"] = devsize

            # v1.1 at 0, v1.2 at 4096, v1.0 near the end (8K before the last 4K)
            cands = [("1.1", 0), ("1.2", 4096),
                     ("1.0", (devsize - 8 * 1024) & ~0xFFF)]
            # v0.90 lives at a 64K-aligned offset near the END of the device,
            # and puts the array DATA at offset 0 -- so the filesystem is
            # mountable directly with no offset. Different struct entirely.
            v09_off = (((devsize // 1024) - 64) & ~63) * 1024
            if v09_off > 0 and v09_off + 4096 <= devsize:
                sb = read_at(fh, v09_off, 4096)
                if len(sb) >= 64 and struct.unpack_from("<I", sb, 0)[0] == MD_MAGIC:
                    lvl = struct.unpack_from("<i", sb, 28)[0]
                    size_kb = struct.unpack_from("<I", sb, 32)[0]
                    nr_disks = struct.unpack_from("<I", sb, 36)[0]
                    raid_disks = struct.unpack_from("<I", sb, 40)[0]
                    u = struct.unpack_from("<I", sb, 20)[0]
                    u1, u2, u3 = struct.unpack_from("<III", sb, 52)
                    out.update({
                        "version": "0.90",
                        "sb_variant": "0.90",
                        "sb_offset": v09_off,
                        "name": "(0.90 has no name)",
                        "set_uuid": "%08x:%08x:%08x:%08x" % (u, u1, u2, u3),
                        "level": LEVELS.get(lvl, str(lvl)),
                        "raid_disks": raid_disks,
                        "array_size_sectors": size_kb * 2,
                        "data_offset_sectors": 0,
                        "events": 0,
                        "found": True,
                        "data_byte_offset": 0,
                    })

            for tag, off in cands:
                if out["found"]:
                    break
                if off < 0 or off + 4096 > devsize:
                    continue
                sb = read_at(fh, off, 4096)
                if len(sb) < 256:
                    continue
                info = parse_v1(sb)
                if info:
                    info["sb_variant"] = tag
                    info["sb_offset"] = off
                    out.update(info)
                    out["found"] = True
                    out["data_byte_offset"] = info["data_offset_sectors"] * 512
                    break

            if out["found"]:
                # Peek at the filesystem sitting at the data offset. ext
                # superblock magic 0xEF53 lives 0x438 into the filesystem.
                d = out["data_byte_offset"]
                fsb = read_at(fh, d + 0x400, 0x400)
                if len(fsb) >= 0x40:
                    magic = struct.unpack_from("<H", fsb, 0x38)[0]
                    if magic == 0xEF53:
                        blocks = struct.unpack_from("<I", fsb, 0x04)[0]
                        logbs = struct.unpack_from("<I", fsb, 0x18)[0]
                        bs = 1024 << logbs
                        label = fsb[0x78:0x88].split(b"\x00")[0]
                        out["fs"] = {
                            "type": "ext2/3/4",
                            "block_size": bs,
                            "blocks": blocks,
                            "size_bytes": blocks * bs,
                            "label": label.decode("ascii", "replace"),
                        }
                    else:
                        out["fs"] = {"type": "unrecognised",
                                     "magic": "0x%04x" % magic}
    except PermissionError:
        out["error"] = "permission denied (run as root)"
    except Exception as e:
        out["error"] = str(e)
    return out


if __name__ == "__main__":
    import json
    for dev in sys.argv[1:]:
        r = probe(dev)
        print("=== %s ===" % dev)
        if not r.get("found"):
            print("  no md superblock found%s"
                  % ("" if "error" not in r else " (%s)" % r["error"]))
            continue
        print("  superblock   : %s at 0x%x" % (r["sb_variant"], r["sb_offset"]))
        print("  array name   : %s" % (r["name"] or "(unnamed)"))
        print("  array uuid   : %s" % r["set_uuid"])
        print("  level        : %s across %d device(s)"
              % (r["level"], r["raid_disks"]))
        print("  array size   : %.1f GB" % (r["array_size_sectors"] * 512 / 1e9))
        print("  data starts  : 0x%x (%d sectors in)"
              % (r["data_byte_offset"], r["data_offset_sectors"]))
        print("  events       : %d" % r["events"])
        fs = r.get("fs") or {}
        if fs.get("type", "").startswith("ext"):
            print("  filesystem   : %s, label=%r, %.1f GB"
                  % (fs["type"], fs["label"], fs["size_bytes"] / 1e9))
            print("  mount r/o    : mount -o ro,noload,offset=%d %s /mnt/x"
                  % (r["data_byte_offset"], dev))
        elif fs:
            print("  filesystem   : %s" % fs)
