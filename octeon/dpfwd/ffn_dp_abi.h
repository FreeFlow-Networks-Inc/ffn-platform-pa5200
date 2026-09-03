/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_abi.h -- FFN management-plane <-> dataplane shared-region ABI.
 *
 * SINGLE SOURCE OF TRUTH for both sides:
 *   host side  (x86-64 LE) : ffn_oct.py / ffn-controld map this over a PCIe BAR
 *   DP side    (MIPS64 BE) : ffn_dp_oct.c maps it in Octeon DRAM
 *
 * ENDIANNESS POLICY
 * -----------------
 * The x86 host and an OCTEON-II dataplane have OPPOSITE byte order, so every
 * multi-byte field in this region -- and in the fastpath tables carried inside
 * it -- is defined as **little-endian on the wire**, accessed through the
 * ld_leNN / st_leNN helpers below. Consequences:
 *   * one policy.bin serves BOTH dataplanes (x86 DPDK and Octeon); the existing
 *     ffn_fastpath_compile.py output is used unchanged, no --arch variant;
 *   * the DP converts table rows to native structs ONCE at load time, so the
 *     per-packet hot path never byte-swaps.
 * Never dereference a struct field in this region directly -- always go through
 * the accessors, or the code silently breaks on the big-endian target.
 */
#ifndef FFN_DP_ABI_H
#define FFN_DP_ABI_H

#include <stdint.h>
#include <string.h>

#define FFN_DP_MAGIC      "FFNDP"
#define FFN_DP_ABI_VER    2u          /* 2 adds the port table */

/* ---- little-endian accessors (endian-agnostic, alignment-safe) ---- */
static inline uint16_t ld_le16(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return (uint16_t)(b[0] | ((uint16_t)b[1] << 8));
}
static inline uint32_t ld_le32(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8) |
           ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}
static inline uint64_t ld_le64(const void *p)
{
    return (uint64_t)ld_le32(p) | ((uint64_t)ld_le32((const uint8_t *)p + 4) << 32);
}
/* Big-endian (network order) reader. IPv4 addresses inside the fastpath tables
 * are stored as NETWORK-ORDER BYTES, so they are read with this -- never with
 * ld_le32() + a host ntohl(), which byte-swaps on little-endian hosts only and
 * silently returns swapped addresses on a big-endian target like OCTEON-II. */
static inline uint32_t ld_be32(const void *p)
{
    const uint8_t *b = (const uint8_t *)p;
    return ((uint32_t)b[0] << 24) | ((uint32_t)b[1] << 16) |
           ((uint32_t)b[2] << 8) | (uint32_t)b[3];
}
static inline void st_be32(void *p, uint32_t v)
{
    uint8_t *b = (uint8_t *)p;
    b[0] = (uint8_t)(v >> 24); b[1] = (uint8_t)(v >> 16);
    b[2] = (uint8_t)(v >> 8);  b[3] = (uint8_t)v;
}
static inline void st_le16(void *p, uint16_t v)
{
    uint8_t *b = (uint8_t *)p;
    b[0] = (uint8_t)v; b[1] = (uint8_t)(v >> 8);
}
static inline void st_le32(void *p, uint32_t v)
{
    uint8_t *b = (uint8_t *)p;
    b[0] = (uint8_t)v;         b[1] = (uint8_t)(v >> 8);
    b[2] = (uint8_t)(v >> 16); b[3] = (uint8_t)(v >> 24);
}
static inline void st_le64(void *p, uint64_t v)
{
    st_le32(p, (uint32_t)v);
    st_le32((uint8_t *)p + 4, (uint32_t)(v >> 32));
}

/* ---- region layout (byte offsets from the base of the shared window) ---- */
#define FFN_DP_OFF_HDR        0x0000u
#define FFN_DP_OFF_CMD_RING   0x0040u          /* MP -> DP */
#define FFN_DP_OFF_EVT_RING   0x1040u          /* DP -> MP */
#define FFN_DP_OFF_STATS      0x2040u
#define FFN_DP_OFF_PORTS      0x3000u          /* port table, ABI v2        */
#define FFN_DP_OFF_BANK0      0x4000u
#define FFN_DP_BANK_SIZE      0x400000u        /* 4 MiB per policy bank */
#define FFN_DP_OFF_BANK1      (FFN_DP_OFF_BANK0 + FFN_DP_BANK_SIZE)
#define FFN_DP_REGION_SIZE    (FFN_DP_OFF_BANK1 + FFN_DP_BANK_SIZE)

/* ---- dp_caps bits: what the RUNNING DP image actually supports ----------
 * The MP must gate port programming on these rather than on ABI version
 * alone, so a new MP can drive an old DP image without wedging it. */
#define FFN_DP_CAP_PORT_CTL   (1u << 0)   /* port table + PORT_* opcodes    */
#define FFN_DP_CAP_PORT_STATS (1u << 1)   /* per-port counters maintained   */
#define FFN_DP_CAP_PORT_HW    (1u << 2)   /* writes real BGX/PHY registers; */
                                          /* without it the DP tracks state */
                                          /* but touches no hardware        */

/* ---- port table ---------------------------------------------------------
 * Modelled on PAN's own port layer, read from the 5220's
 * /opt/dpfs/usr/local/lib64/brdagent/cp/libports.so (MIPS64, unstripped).
 * Worth recording why the shape is what it is:
 *
 *  - PAN separates a LOGICAL port from a PHYSICAL one (gryphon_get_pport,
 *    gryphon_copper_pport_to_offset, map_lport_to_vbus), so both are carried.
 *  - Its port lifecycle is board_port_{reset,powerdown,startup,run} -- four
 *    states, not a bare up/down flag.
 *  - Link comes in two flavours that are tracked separately:
 *    board_port_autoneg{,_enabled,_linked} vs board_port_forced{,_enabled,
 *    _linked}.
 *  - Media presence is its own axis: gryphon_is_sfp_present,
 *    board_port_sfp_nopop_0/1, board_port_sfp_invalid_0 -- "no module" and
 *    "module present but unusable" are different states, and a firewall
 *    operator needs to see the difference.
 *
 * NOTE on where the panel really is: libports.so is full of bcm_* symbols
 * (bcm_port_enable_irq, gryphon_bcm_monitor_link, bcm_queue_sysport_set) and
 * ARAD_NIF_TYPE_*, i.e. a Broadcom switch ASIC sits between the front panel
 * and the Octeon's BGX links. So an FFN port entry may describe either a BGX
 * LMAC or a switch port; bgx/lmac are only meaningful when
 * FFN_PORT_F_HAS_LMAC is set.
 */
#define FFN_DP_PORT_ENTRY_SZ  64u
#define FFN_DP_MAX_PORTS      32u
#define FFN_DP_PORTS_SIZE     (FFN_DP_PORT_ENTRY_SZ * FFN_DP_MAX_PORTS)

/* Form factor. Mirrors PAN's BRD_PORT_TYPE_* enum verbatim in meaning. */
enum {
    FFN_PORT_TYPE_NONE = 0,
    FFN_PORT_TYPE_RJ45,          /* BRD_PORT_TYPE_RJ45           */
    FFN_PORT_TYPE_SFP,           /* BRD_PORT_TYPE_SFP            */
    FFN_PORT_TYPE_SFP_PLUS,      /* BRD_PORT_TYPE_SFP_PLUS       */
    FFN_PORT_TYPE_QSFP_PLUS,     /* BRD_PORT_TYPE_QUAD_SFP_PLUS  */
    FFN_PORT_TYPE_QSFP28,        /* BRD_PORT_TYPE_QSFP28         */
    FFN_PORT_TYPE_XFP,           /* BRD_PORT_TYPE_XFP            */
    FFN_PORT_TYPE_HA,            /* BRD_PORT_TYPE_HA             */
    FFN_PORT_TYPE_INTERNAL,      /* BRD_PORT_TYPE_INTERNAL       */
    FFN_PORT_TYPE_UPLINK,        /* BRD_PORT_TYPE_UPLINK         */
    FFN_PORT_TYPE_GHOST,         /* BRD_PORT_TYPE_GHOST          */
};

/* Lifecycle, from board_port_{reset,powerdown,startup,run}. */
enum {
    FFN_PORT_ST_RESET = 0,
    FFN_PORT_ST_POWERDOWN,
    FFN_PORT_ST_STARTUP,
    FFN_PORT_ST_RUN,
};

/* Negotiation, from board_port_autoneg vs board_port_forced. */
enum { FFN_PORT_NEG_AUTONEG = 0, FFN_PORT_NEG_FORCED };

/* Media presence. NOPOP = cage unpopulated, INVALID = module rejected. */
enum {
    FFN_PORT_MEDIA_UNKNOWN = 0,
    FFN_PORT_MEDIA_ABSENT,
    FFN_PORT_MEDIA_PRESENT,
    FFN_PORT_MEDIA_NOPOP,
    FFN_PORT_MEDIA_INVALID,
};

/* Port ROLE, a separate axis from form factor.
 *
 * PAN keeps these orthogonal and so should FFN: libports.so carries both
 * BRD_PORT_TYPE_* (RJ45 / SFP / SFP+ / QUAD_SFP_PLUS / QSFP28 / XFP -- the
 * connector) and PAN_IFHW_TYPE_* (ETHERNET / HA / HSCI / AE / LFC / TCI /
 * LOOPBACK / TUNNEL / VLAN / VSYS -- what the port is FOR). Conflating them
 * was a real bug in FFN's first cut: a QSFP+ can be a data port or an HSCI
 * link, and an RJ-45 can be data, HA control, or management.
 *
 * HSCI = High Speed Chassis Interconnect, the high-speed HA link (HA2 session
 * sync / HA3 packet forwarding), distinct from PAN_IFHW_TYPE_HA which is the
 * RJ-45 HA1 control link.
 *
 * NOT Interlaken, despite the name suggesting a chassis fabric. Checked on
 * this hardware: the vendor's per-model CSR database has no ilk* registers at
 * all, and every live GSERn_CFG has the ILA bit (bit 1) clear -- so CN73XX
 * cannot do Interlaken here. The Interlaken strings in libports.so are all
 * Broadcom-side (ARAD_NIF_TYPE_ILKN, _SHR_PORT_IF_ILKN), an FE100 NIF option.
 * libports.so prints "Port HSCI: %s %s duplex" from a speed table running
 * 10Mb/s .. 40Gb/s-full, and duplex is an Ethernet notion -- so HSCI is a
 * 40 G Ethernet port on a QSFP+ cage.
 */
enum {
    FFN_PORT_ROLE_DATA = 0,      /* PAN_IFHW_TYPE_ETHERNET: inspectable    */
    FFN_PORT_ROLE_HA,            /* PAN_IFHW_TYPE_HA:   HA1 control link   */
    FFN_PORT_ROLE_HSCI,          /* PAN_IFHW_TYPE_HSCI: HA2/HA3 high speed */
    FFN_PORT_ROLE_MGMT,          /* management, never bridged              */
    FFN_PORT_ROLE_INTERNAL,      /* backplane to another complex           */
    FFN_PORT_ROLE_AE,            /* PAN_IFHW_TYPE_AE: aggregate member     */
    FFN_PORT_ROLE_LOOPBACK,
    FFN_PORT_ROLE_MAX,
};

/* Only DATA and AE ports may carry inspected traffic. HA/HSCI carry cluster
 * traffic and must never be bridged into the data path; MGMT/INTERNAL never
 * either. ffn_port_role_bridgeable() is the single place that decides. */
static inline int ffn_port_role_bridgeable(unsigned role)
{
    return role == FFN_PORT_ROLE_DATA || role == FFN_PORT_ROLE_AE;
}

#define FFN_PORT_F_HAS_LMAC   (1u << 0)   /* bgx/lmac fields are valid      */
#define FFN_PORT_F_MGMT       (1u << 1)   /* management-class, never a data  */
                                          /* port -- refuse to bridge it    */
#define FFN_PORT_F_VALID      (1u << 7)   /* entry populated                */

/* 64 bytes. All multi-byte fields little-endian, like the rest of the ABI. */
struct ffn_dp_port_raw {
    uint8_t lport[2];        /* logical port id (dense, 0..n-1)  */
    uint8_t pport[2];        /* board/physical port number       */
    uint8_t bgx;             /* BGX block, if FFN_PORT_F_HAS_LMAC */
    uint8_t lmac;            /* LMAC within that block           */
    uint8_t port_type;       /* FFN_PORT_TYPE_*                  */
    uint8_t lmac_type;       /* BGX LMAC_TYPE [10:8], 0..7       */
    uint8_t lane_to_sds;     /* BGX LANE_TO_SDS [7:0]            */
    uint8_t state;           /* FFN_PORT_ST_*                    */
    uint8_t neg_mode;        /* FFN_PORT_NEG_*                   */
    uint8_t media;           /* FFN_PORT_MEDIA_*                 */
    uint8_t admin_up;        /* operator intent                  */
    uint8_t link_up;         /* observed                         */
    uint8_t phy_addr;        /* MDIO address, 0xff = none        */
    uint8_t flags;           /* FFN_PORT_F_*                     */
    uint8_t speed_mbps[4];
    uint8_t mtu[4];
    uint8_t name[16];        /* NUL-padded, e.g. "ethernet1/1"   */
    uint8_t rx_pkts[8];
    uint8_t tx_pkts[8];
    uint8_t role;            /* FFN_PORT_ROLE_*                  */
    uint8_t reserved[7];
};

static inline void *ffn_dp_port(void *base, unsigned i)
{
    return (uint8_t *)base + FFN_DP_OFF_PORTS +
           (size_t)(i % FFN_DP_MAX_PORTS) * FFN_DP_PORT_ENTRY_SZ;
}

/* ---- DP / host lifecycle states ---- */
enum {
    DP_STATE_RESET = 0,
    DP_STATE_BOOT,          /* DP image running, region not yet validated */
    DP_STATE_HANDSHAKE,     /* magic+version agreed, awaiting tables      */
    DP_STATE_READY,         /* tables loaded, forwarding                  */
    DP_STATE_ERROR,
};

/* ---- command opcodes (MP -> DP) ---- */
enum {
    DP_CMD_NOP = 0,
    DP_CMD_PING,
    DP_CMD_SET_BANK,        /* arg0 = bank index to activate            */
    DP_CMD_GET_STATS,
    DP_CMD_SET_DEFAULT,     /* arg0 = default decision (FP_*)           */
    DP_CMD_FLUSH_FLOWS,
    DP_CMD_SHUTDOWN,
    /* --- ABI v2: port control. Gate on FFN_DP_CAP_PORT_CTL. --- */
    DP_CMD_PORT_ENUM,       /* a0 ignored; emits DP_EVT_PORT_INFO per port  */
    DP_CMD_PORT_CONFIG,     /* a0 = lport, a1 = cfg word, a2 = speed|mtu    */
    DP_CMD_PORT_ADMIN,      /* a0 = lport, a1 = 1 up / 0 down               */
    DP_CMD_PORT_STATS,      /* a0 = lport; emits DP_EVT_PORT_STATS          */
};

/* PORT_CONFIG arg1 packing. Kept explicit rather than a bitfield struct so
 * both ends agree without depending on compiler layout. */
#define FFN_PORT_CFG_TYPE(w)      ((uint8_t)((w) & 0xff))
#define FFN_PORT_CFG_LMAC_TYPE(w) ((uint8_t)(((w) >> 8) & 0x7))
#define FFN_PORT_CFG_LANE(w)      ((uint8_t)(((w) >> 16) & 0xff))
#define FFN_PORT_CFG_NEG(w)       ((uint8_t)(((w) >> 24) & 0xff))
#define FFN_PORT_CFG_PHY(w)       ((uint8_t)(((w) >> 32) & 0xff))
#define FFN_PORT_CFG_FLAGS(w)     ((uint8_t)(((w) >> 40) & 0xff))
#define FFN_PORT_CFG_ROLE(w)      ((uint8_t)(((w) >> 48) & 0xff))
#define FFN_PORT_CFG_PACK(type, lmac_type, lane, neg, phy, flags) \
    (((uint64_t)(type) & 0xff) | (((uint64_t)(lmac_type) & 0x7) << 8) | \
     (((uint64_t)(lane) & 0xff) << 16) | (((uint64_t)(neg) & 0xff) << 24) | \
     (((uint64_t)(phy) & 0xff) << 32) | (((uint64_t)(flags) & 0xff) << 40))
/* Same fields plus the role. Prefer this; the 6-arg form defaults to DATA. */
#define FFN_PORT_CFG_PACK_R(type, lmac_type, lane, neg, phy, flags, role) \
    (FFN_PORT_CFG_PACK(type, lmac_type, lane, neg, phy, flags) | \
     (((uint64_t)(role) & 0xff) << 48))

/* PORT_CONFIG arg2 packing: speed in the low 32, MTU in the next 16. */
#define FFN_PORT_A2_SPEED(w) ((uint32_t)((w) & 0xffffffffu))
#define FFN_PORT_A2_MTU(w)   ((uint16_t)(((w) >> 32) & 0xffff))
#define FFN_PORT_A2_PACK(speed, mtu) \
    (((uint64_t)(speed) & 0xffffffffu) | (((uint64_t)(mtu) & 0xffff) << 32))

/* ---- event opcodes (DP -> MP) ---- */
enum {
    DP_EVT_NONE = 0,
    DP_EVT_PONG,
    DP_EVT_READY,
    DP_EVT_STATS,
    DP_EVT_ERROR,
    DP_EVT_FLOW_DROP,
    /* --- ABI v2 --- */
    DP_EVT_PORT_INFO,       /* a0 = lport, a1 = state word, a2 = speed|mtu  */
    DP_EVT_PORT_LINK,       /* a0 = lport, a1 = link_up,   a2 = media       */
    DP_EVT_PORT_STATS,      /* a0 = lport, a1 = rx_pkts,   a2 = tx_pkts     */
};

/* PORT_INFO arg1 packing -- what the DP reports back. */
#define FFN_PORT_ST_PACK(type, state, neg, media, admin, link, flags, role) \
    (((uint64_t)(type) & 0xff) | (((uint64_t)(state) & 0xff) << 8) | \
     (((uint64_t)(neg) & 0xff) << 16) | (((uint64_t)(media) & 0xff) << 24) | \
     (((uint64_t)(admin) & 0x1) << 32) | (((uint64_t)(link) & 0x1) << 33) | \
     (((uint64_t)(flags) & 0xff) << 40) | (((uint64_t)(role) & 0xff) << 48))

/* Region header. 64 bytes. All multi-byte fields little-endian. */
#define FFN_DP_HDR_SIZE 64
struct ffn_dp_hdr_raw {
    uint8_t magic[6];        /* "FFNDP\0"                     */
    uint8_t abi_version[2];  /* le16                          */
    uint8_t dp_state[4];     /* le32, DP_STATE_*              */
    uint8_t host_state[4];   /* le32                          */
    uint8_t dp_heartbeat[8]; /* le64, DP increments           */
    uint8_t host_heartbeat[8];
    uint8_t active_bank[4];  /* le32, 0 or 1                  */
    uint8_t default_dec[4];  /* le32, FP_* when no rule hits  */
    uint8_t dp_caps[4];      /* le32 bitmap                   */
    uint8_t dp_error[4];     /* le32                          */
    uint8_t reserved[16];
};

/* Ring: fixed 32-byte descriptors, single-producer/single-consumer.
 * head = producer index, tail = consumer index, both monotonic le32. */
#define FFN_DP_RING_DESCS   64u
#define FFN_DP_DESC_SIZE    32u
#define FFN_DP_RING_HDR_SZ  16u
struct ffn_dp_desc_raw {
    uint8_t opcode[2];       /* le16 */
    uint8_t flags[2];        /* le16 */
    uint8_t seq[4];          /* le32 */
    uint8_t arg0[8];         /* le64 */
    uint8_t arg1[8];         /* le64 */
    uint8_t arg2[8];         /* le64 */
};

/* ---- helpers over the raw header ---- */
static inline int ffn_dp_hdr_valid(const void *base)
{
    const struct ffn_dp_hdr_raw *h = (const struct ffn_dp_hdr_raw *)base;
    return memcmp(h->magic, FFN_DP_MAGIC, 5) == 0 &&
           ld_le16(h->abi_version) == FFN_DP_ABI_VER;
}
static inline void ffn_dp_hdr_init(void *base, uint32_t state)
{
    struct ffn_dp_hdr_raw *h = (struct ffn_dp_hdr_raw *)base;
    memset(h, 0, sizeof(*h));
    memcpy(h->magic, FFN_DP_MAGIC, 6);
    st_le16(h->abi_version, (uint16_t)FFN_DP_ABI_VER);
    st_le32(h->dp_state, state);
}

/* ring accessors: ring base -> [head le32][tail le32][pad 8][descs...] */
static inline uint32_t ffn_dp_ring_head(const void *ring) { return ld_le32(ring); }
static inline uint32_t ffn_dp_ring_tail(const void *ring)
{
    return ld_le32((const uint8_t *)ring + 4);
}
static inline void ffn_dp_ring_set_head(void *ring, uint32_t v) { st_le32(ring, v); }
static inline void ffn_dp_ring_set_tail(void *ring, uint32_t v)
{
    st_le32((uint8_t *)ring + 4, v);
}
static inline void *ffn_dp_ring_desc(void *ring, uint32_t idx)
{
    return (uint8_t *)ring + FFN_DP_RING_HDR_SZ +
           (size_t)(idx % FFN_DP_RING_DESCS) * FFN_DP_DESC_SIZE;
}

/* Push one descriptor. Returns 0 on success, -1 if the ring is full. */
static inline int ffn_dp_ring_push(void *ring, uint16_t opcode, uint64_t a0,
                                   uint64_t a1, uint64_t a2)
{
    uint32_t head = ffn_dp_ring_head(ring), tail = ffn_dp_ring_tail(ring);
    if (head - tail >= FFN_DP_RING_DESCS)
        return -1;
    struct ffn_dp_desc_raw *d =
        (struct ffn_dp_desc_raw *)ffn_dp_ring_desc(ring, head);
    st_le16(d->opcode, opcode);
    st_le16(d->flags, 0);
    st_le32(d->seq, head);
    st_le64(d->arg0, a0);
    st_le64(d->arg1, a1);
    st_le64(d->arg2, a2);
    ffn_dp_ring_set_head(ring, head + 1);
    return 0;
}

/* Pop one descriptor. Returns 1 if one was dequeued, 0 if empty. */
static inline int ffn_dp_ring_pop(void *ring, uint16_t *opcode, uint64_t *a0,
                                  uint64_t *a1, uint64_t *a2)
{
    uint32_t head = ffn_dp_ring_head(ring), tail = ffn_dp_ring_tail(ring);
    if (head == tail)
        return 0;
    const struct ffn_dp_desc_raw *d =
        (const struct ffn_dp_desc_raw *)ffn_dp_ring_desc(ring, tail);
    if (opcode) *opcode = ld_le16(d->opcode);
    if (a0) *a0 = ld_le64(d->arg0);
    if (a1) *a1 = ld_le64(d->arg1);
    if (a2) *a2 = ld_le64(d->arg2);
    ffn_dp_ring_set_tail(ring, tail + 1);
    return 1;
}

static inline void ffn_dp_ring_init(void *ring)
{
    memset(ring, 0, FFN_DP_RING_HDR_SZ +
           (size_t)FFN_DP_RING_DESCS * FFN_DP_DESC_SIZE);
}

#endif /* FFN_DP_ABI_H */
