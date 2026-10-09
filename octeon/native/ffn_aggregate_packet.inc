/* SPDX-License-Identifier: GPL-2.0-or-later
 * Included by the native packet owner; shares its descriptors and pause barrier.
 * No packet callback crosses into Python. All hardware IDs come from control.
 */
#define AG_UNITS 65
#define AG_PARENT 4096
#define AG_STATS 8
enum { AG_GATE, AG_VLAN, AG_NETWORK, AG_OFFLOAD, AG_RX_PRESSURE,
       AG_TX_PRESSURE, AG_CONTROL, AG_INSPECT };
struct ffn_aggregate {
    struct ffn_aggregate_member members[8];
    struct ffn_aggregate_unit units[AG_UNITS];
    unsigned members_count, units_count, tid;
    int unit_index[AG_PARENT + 1];
    unsigned collect, distribute, inspect, ready;
    uint64_t deadline, offload_deadline;
    _Atomic uint64_t stats[AG_STATS], member_stats[8][8], unit_stats[AG_PARENT + 1][4];
};
#define AG_ADD(counter, amount) atomic_fetch_add_explicit(&(counter), (amount), memory_order_relaxed)
unsigned ffn_aggregate_abi(void) { return 1; }
static int aggregate_setup(struct ffn_packet *p, const struct ffn_aggregate_member *m,
                           unsigned count, unsigned tid)
{
    if (!p || !m || count < 2 || count > 8 || tid < 1 || tid > 12) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) {
        if (!m[i].front || m[i].front > 24 || m[i].source > 65535 || m[i].alias > 65535 ||
            (i && m[i].front <= m[i-1].front) || (m[i].alias && m[i].alias == m[i].source)) {
            errno = EINVAL; return -1;
        }
        for (unsigned j = 0; j < i; j++)
            if (m[i].source == m[j].source || (m[i].alias && (m[i].alias == m[j].source || m[i].alias == m[j].alias)) ||
                (m[j].alias && m[i].source == m[j].alias)) { errno = EINVAL; return -1; }
    }
    p->aggregate = calloc(1, sizeof(*p->aggregate));
    if (!p->aggregate) return -1;
    struct ffn_aggregate *a = p->aggregate;
    memcpy(a->members, m, count * sizeof(*m)); a->members_count = count; a->tid = tid;
    for (unsigned i = 0; i <= AG_PARENT; i++) a->unit_index[i] = -1;
    return 0;
}
struct ffn_packet *ffn_aggregate_adopt(int rx, int tx, int tap,
    const struct ffn_aggregate_member *m, unsigned count, unsigned tid)
{
    struct ffn_packet *p = ffn_packet_adopt(rx, tx, tap, 0);
    if (!p) return NULL;
    if (aggregate_setup(p, m, count, tid)) { int error = errno; ffn_packet_close(p); errno = error; return NULL; }
    return p;
}
struct ffn_packet *ffn_aggregate_open(const char *wire, const char *ns, const char *tap,
    const struct ffn_aggregate_member *m, unsigned count, unsigned tid)
{
    struct ffn_packet *p = ffn_packet_open(wire, ns, tap, 0);
    uint32_t sources[16]; unsigned used = 0;
    if (!p) return NULL;
    if (aggregate_setup(p, m, count, tid)) goto fail;
    for (unsigned i = 0; i < count; i++) {
        sources[used++] = m[i].source;
        if (m[i].alias) sources[used++] = m[i].alias;
    }
    close(p->rx); p->rx = packet_socket_many(wire, sources, used, 3);
    if (p->rx < 0) goto fail;
    return p;
fail: {
    int error = errno; ffn_packet_close(p); errno = error; return NULL;
}}
int ffn_aggregate_control_open(const char *wire, const uint32_t *sources, unsigned count)
{
    if (!valid_name(wire)) { errno = EINVAL; return -1; }
    return packet_socket_many(wire, sources, count, 2);
}
/* Successful return retains the mutex until the caller finishes copying. */
static int aggregate_config_lock(struct ffn_packet *p)
{
    if (!p || !p->aggregate) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    if (p->stopped || (p->started && (!p->pause_requested || !p->paused[0] || !p->paused[1]))) {
        pthread_mutex_unlock(&p->control); errno = EBUSY; return -1;
    }
    return 0;
}
int ffn_aggregate_network(struct ffn_packet *p, const struct ffn_aggregate_unit *units, unsigned count)
{
    if (count > AG_UNITS || (count && !units)) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) {
        const struct ffn_aggregate_unit *u = &units[i];
        if (!(u->tag == AG_PARENT || (u->tag >= 1 && u->tag <= 4094)) ||
            u->mtu < 576 || u->mtu > 1500 || u->n4 > 32 || u->n6 > 32 || (u->n6 && u->mtu < 1280)) {
            errno = EINVAL; return -1;
        }
        for (unsigned j = 0; j < i; j++) if (units[j].tag == u->tag) { errno = EINVAL; return -1; }
    }
    if (aggregate_config_lock(p)) return -1;
    struct ffn_aggregate *a = p->aggregate;
    if (count) memcpy(a->units, units, count * sizeof(*units));
    a->units_count = count;
    for (unsigned i = 0; i <= AG_PARENT; i++) a->unit_index[i] = -1;
    for (unsigned i = 0; i < count; i++) a->unit_index[units[i].tag] = (int)i;
    pthread_mutex_unlock(&p->control); return 0;
}
int ffn_aggregate_gates(struct ffn_packet *p, unsigned collect, unsigned distribute, unsigned inspect,
    uint64_t deadline, uint64_t offload_deadline, unsigned ready, void *engine, ffn_scan_fn scan)
{
    if (!p || !p->aggregate || ready > 1 || (!!engine != !!scan) ||
        ((collect | distribute | inspect) >> p->aggregate->members_count) || (distribute & ~collect) ||
        (offload_deadline && distribute != (1u << p->aggregate->members_count) - 1)) { errno = EINVAL; return -1; }
    if (aggregate_config_lock(p)) return -1;
    struct ffn_aggregate *a = p->aggregate;
    a->collect = collect; a->distribute = distribute; a->inspect = inspect;
    a->deadline = deadline; a->offload_deadline = offload_deadline; a->ready = ready;
    p->inspection = engine; p->scan = scan;
    pthread_mutex_unlock(&p->control); return 0;
}
int ffn_aggregate_stats(struct ffn_packet *p, uint64_t *out, unsigned count)
{
    if (!p || !p->aggregate || !out || count != AG_STATS) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) out[i] = atomic_load_explicit(&p->aggregate->stats[i], memory_order_relaxed);
    return 0;
}
int ffn_aggregate_member_stats(struct ffn_packet *p, unsigned index, uint64_t *out, unsigned count)
{
    if (!p || !p->aggregate || index >= p->aggregate->members_count || !out || count != 8) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) out[i] = atomic_load_explicit(&p->aggregate->member_stats[index][i], memory_order_relaxed);
    return 0;
}
int ffn_aggregate_unit_stats(struct ffn_packet *p, unsigned tag, uint64_t *out, unsigned count)
{
    if (!p || !p->aggregate || tag > AG_PARENT || !out || count != 4) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) out[i] = atomic_load_explicit(&p->aggregate->unit_stats[tag][i], memory_order_relaxed);
    return 0;
}
static int aggregate_classify(struct ffn_aggregate *a, const uint8_t *frame, size_t size,
                              uint8_t *plain, size_t *plain_size)
{
    unsigned tag = AG_PARENT, overhead = 14;
    if (size < 14 || size > MAX_FRAME) return -1;
    unsigned kind = be16(frame + 12);
    if (kind == 0x8100) {
        if (size < 18) return -1;
        tag = be16(frame + 14) & 4095; overhead = 18;
        kind = be16(frame + 16);
        if (kind == 0x8100 || kind == 0x88a8) return -1;
    } else if (kind == 0x88a8) return -1;
    int index = a->unit_index[tag];
    if (index < 0 || size > a->units[index].mtu + overhead) return -1;
    *plain_size = size - (overhead - 14);
    if (overhead == 18) {
        memcpy(plain, frame, 12); memcpy(plain + 12, frame + 16, size - 16);
    } else memcpy(plain, frame, size);
    return index;
}
static int aggregate_allow(struct ffn_packet *p, unsigned member, unsigned unit,
                           const uint8_t *plain, size_t size)
{
    struct ffn_aggregate *a = p->aggregate;
    const struct ffn_aggregate_unit *u = &a->units[unit];
    if (size >= 34 && be16(plain + 12) == 0x0800 && plain[14] >> 4 == 4)
        for (unsigned i = 0; i < u->n4; i++) if (!memcmp(plain + 30, u->local4[i], 4)) goto local;
    if (size >= 54 && be16(plain + 12) == 0x86dd && plain[14] >> 4 == 6)
        for (unsigned i = 0; i < u->n6; i++) if (!memcmp(plain + 38, u->local6[i], 16)) goto local;
    unsigned global, per_member;
    if (!p->scan || !(a->inspect & (1u << member))) { global = BYPASSED; per_member = 2; }
    else {
        int verdict = p->scan(p->inspection, (const char *)plain, (unsigned)size);
        if (verdict < -2 || verdict > 2) { AG_ADD(p->counters[BAD_VERDICT], 1); errno = EPROTO; return -1; }
        static const unsigned counters[] = {UNSUPPORTED, MALFORMED, NO_MATCH, ALERT, BLOCK};
        global = counters[verdict + 2]; per_member = (unsigned)(verdict + 5);
    }
    AG_ADD(p->counters[global], 1); AG_ADD(a->member_stats[member][per_member], 1);
    if (global == BLOCK) { AG_ADD(p->counters[INSPECTION_DROP], 1); AG_ADD(a->stats[AG_INSPECT], 1); return 0; }
    return 1;
local: AG_ADD(p->counters[LOCAL_INPUT], 1); return 1;
}
static int aggregate_receive(struct ffn_packet *p, const uint8_t *packet, ssize_t size)
{
    struct ffn_aggregate *a = p->aggregate;
    if (size < 18 || size > MAX_FRAME + 4 || be16(packet) != 24) { AG_ADD(p->counters[ENVELOPE_DROP], 1); return 0; }
    unsigned source = be16(packet + 2), member;
    for (member = 0; member < a->members_count; member++)
        if (a->members[member].source == source || (a->members[member].alias && a->members[member].alias == source)) break;
    if (member == a->members_count) { AG_ADD(p->counters[ENVELOPE_DROP], 1); return 0; }
    if (be16(packet + 16) == 0x8809 || be16(packet + 16) == 0x88cc) { AG_ADD(a->stats[AG_CONTROL], 1); return 0; }
    if (!a->ready) { AG_ADD(a->stats[AG_NETWORK], 1); return 0; }
    if (!(a->collect & (1u << member)) || (uint64_t)milliseconds() >= a->deadline) { AG_ADD(a->stats[AG_GATE], 1); return 0; }
    uint8_t plain[MAX_FRAME]; size_t plain_size;
    int unit = aggregate_classify(a, packet + 4, (size_t)size - 4, plain, &plain_size);
    if (unit < 0) { AG_ADD(a->stats[AG_VLAN], 1); return 0; }
    int accepted = aggregate_allow(p, member, (unsigned)unit, plain, plain_size);
    if (accepted <= 0) return accepted < 0 ? -1 : 0;
    ssize_t written = write(p->tap, packet + 4, (size_t)size - 4);
    if (written < 0) { AG_ADD(a->stats[AG_RX_PRESSURE], 1); return io_error(p); }
    if (written != size - 4) { errno = EIO; return -1; }
    AG_ADD(p->counters[RX], 1); AG_ADD(a->member_stats[member][0], 1);
    AG_ADD(a->unit_stats[a->units[unit].tag][0], 1); AG_ADD(a->unit_stats[a->units[unit].tag][1], written);
    return 0;
}
static uint32_t aggregate_crc_table[256];
static pthread_once_t aggregate_crc_once = PTHREAD_ONCE_INIT;
static void aggregate_crc_init(void)
{
    for (unsigned i = 0; i < 256; i++) {
        uint32_t value = i;
        for (unsigned bit = 0; bit < 8; bit++) value = value & 1 ? (value >> 1) ^ 0xedb88320u : value >> 1;
        aggregate_crc_table[i] = value;
    }
}
static uint32_t aggregate_hash(const uint8_t *frame, size_t size, unsigned front)
{
    uint8_t key[44]; unsigned used = 0, offset = 14, kind = be16(frame + 12);
    pthread_once(&aggregate_crc_once, aggregate_crc_init);
    if (kind == 0x8100) {
        memcpy(key, frame + 14, 2); used = 2; offset = 18; kind = be16(frame + 16);
    }
    if (kind == 0x0800 && size >= offset + 20 && frame[offset] >> 4 == 4) {
        key[used++] = 8; key[used++] = 0;
        memcpy(key + used, frame + offset + 12, 8); used += 8; key[used++] = frame[offset + 9];
    } else if (kind == 0x86dd && size >= offset + 40 && frame[offset] >> 4 == 6) {
        key[used++] = 0x86; key[used++] = 0xdd;
        memcpy(key + used, frame + offset + 8, 32); used += 32;
    } else {
        memcpy(key + used, frame, 12); used += 12;
        key[used++] = (uint8_t)(kind >> 8); key[used++] = (uint8_t)kind;
    }
    key[used++] = (uint8_t)(front >> 8); key[used++] = (uint8_t)front;
    uint32_t crc = 0xffffffffu;
    for (unsigned i = 0; i < used; i++) crc = aggregate_crc_table[(crc ^ key[i]) & 255] ^ (crc >> 8);
    return crc ^ 0xffffffffu;
}
/* Hardware member selection. The switch resolves a LAG destination with the
 * load-balance key its ingress pipeline computed, but injected frames enter
 * on a TM-header port where no packet header is parsed, so that key would be
 * one constant and every flow would leave through one member. The DP
 * therefore folds the same flow fields its own rendezvous hash uses into an
 * 8-bit key and carries it in the RAW_DSA area, which the switch strips
 * before faceplate transmission; a direct-extraction field entry on the
 * trunk port copies the byte into the LAG LB key (ffn_aggregate_bcm_lag.py).
 * Only bytes 1..3 of the area may carry it: the switch's egress program reads
 * the area as a DSA tag whose byte 0 holds the tagged flag (bit 5; a set flag
 * makes it rebuild a VLAN tag from the other bytes and the frame is lost on
 * the LAN switch), and the DP trunk driver refuses an envelope with byte 0 or
 * the upper four bytes set. The byte is replicated across 1..3 so the
 * extraction offset does not depend on how the system header is counted.
 * Transport ports are not hashed, so every fragment of a flow keeps one
 * member. */
static uint8_t aggregate_lb_key(const uint8_t *frame, size_t size)
{
    uint32_t crc = aggregate_hash(frame, size, 0);
    return (uint8_t)(crc ^ (crc >> 8) ^ (crc >> 16) ^ (crc >> 24));
}
static int aggregate_transmit(struct ffn_packet *p, const uint8_t *frame, ssize_t size)
{
    struct ffn_aggregate *a = p->aggregate;
    if (!a->ready) { AG_ADD(a->stats[AG_NETWORK], 1); return 0; }
    uint64_t now = (uint64_t)milliseconds();
    if (!a->distribute || now >= a->deadline) { AG_ADD(a->stats[AG_GATE], 1); return 0; }
    uint8_t plain[MAX_FRAME]; size_t plain_size;
    int unit = aggregate_classify(a, frame, (size_t)size, plain, &plain_size);
    if (unit < 0) { AG_ADD(a->stats[AG_VLAN], 1); return 0; }
    int selected = -1; uint32_t best = 0; unsigned destination; uint8_t key = 0;
    int offloaded = now < a->offload_deadline;
    if (offloaded) { destination = 0x8000 | a->tid; key = aggregate_lb_key(frame, (size_t)size); }
    else {
        for (unsigned i = 0; i < a->members_count; i++) if (a->distribute & (1u << i)) {
            uint32_t hash = aggregate_hash(frame, (size_t)size, a->members[i].front);
            if (selected < 0 || hash > best) { best = hash; selected = (int)i; }
        }
        destination = a->members[selected].source;
    }
    uint8_t packet[MAX_FRAME + 12] = {1, (uint8_t)(destination >> 8), (uint8_t)destination, 0};
    memcpy(packet + 4, frame, 12); memcpy(packet + 24, frame + 12, (size_t)size - 12);
    if (offloaded) memset(packet + 17, key, 3);
    size_t length = (size_t)(size < 60 ? 60 : size) + 12;
    ssize_t sent = send(p->tx, packet, length, MSG_NOSIGNAL);
    if (sent < 0) { AG_ADD(a->stats[AG_TX_PRESSURE], 1); return io_error(p); }
    if ((size_t)sent != length) { errno = EIO; return -1; }
    AG_ADD(p->counters[TX], 1);
    if (offloaded) AG_ADD(a->stats[AG_OFFLOAD], 1); else AG_ADD(a->member_stats[selected][1], 1);
    AG_ADD(a->unit_stats[a->units[unit].tag][2], 1); AG_ADD(a->unit_stats[a->units[unit].tag][3], size);
    return 0;
}
#undef AG_ADD
