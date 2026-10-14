/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_lldp.h"
#include "ffn_json.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const uint8_t LLDP_MULTICAST[3][6] = {
    { 0x01, 0x80, 0xc2, 0x00, 0x00, 0x0e },   /* nearest bridge */
    { 0x01, 0x80, 0xc2, 0x00, 0x00, 0x03 },   /* nearest non-TPMR bridge */
    { 0x01, 0x80, 0xc2, 0x00, 0x00, 0x00 },   /* nearest customer bridge */
};

/* ---- building ---- */

struct writer { uint8_t *p; size_t len, cap; bool failed; };

static void put_tlv(struct writer *w, unsigned type, const void *value, size_t len)
{
    if (w->failed || len > 511 || w->len + 2 + len > w->cap) { w->failed = true; return; }
    w->p[w->len++] = (uint8_t)((type << 1) | (len >> 8));
    w->p[w->len++] = (uint8_t)(len & 0xff);
    memcpy(w->p + w->len, value, len); w->len += len;
}

static void put_id_tlv(struct writer *w, unsigned type, const struct lldp_id *id)
{
    uint8_t buf[1 + LLDP_MAX_ID];
    buf[0] = id->subtype; memcpy(buf + 1, id->value, id->length);
    put_tlv(w, type, buf, 1 + id->length);
}

int lldp_build(const struct lldp_system *sys, const struct lldp_port_config *port, uint16_t ttl, uint8_t *out, size_t outsz)
{
    struct writer w = { out, 0, outsz, false };
    struct lldp_id chassis = { 4, 6, { 0 } }, portid = { 5, 0, { 0 } };   /* chassis: MAC address; port: interface name */
    memcpy(chassis.value, sys->chassis_mac, 6);
    size_t nlen = strlen(port->name);
    if (nlen == 0 || nlen > LLDP_MAX_ID) return -1;
    portid.length = (uint8_t)nlen; memcpy(portid.value, port->name, nlen);
    put_id_tlv(&w, 1, &chassis);
    put_id_tlv(&w, 2, &portid);
    uint8_t ttlb[2] = { (uint8_t)(ttl >> 8), (uint8_t)ttl };
    put_tlv(&w, 3, ttlb, 2);
    if (ttl) {
        if ((port->tlvs & LLDP_TLV_PORT_DESCRIPTION) && port->description[0]) put_tlv(&w, 4, port->description, strlen(port->description));
        if ((port->tlvs & LLDP_TLV_SYSTEM_NAME) && sys->name[0]) put_tlv(&w, 5, sys->name, strlen(sys->name));
        if ((port->tlvs & LLDP_TLV_SYSTEM_DESCRIPTION) && sys->description[0]) put_tlv(&w, 6, sys->description, strlen(sys->description));
        if (port->tlvs & LLDP_TLV_SYSTEM_CAPABILITIES) {
            uint8_t cap[4] = { (uint8_t)(sys->capabilities_supported >> 8), (uint8_t)sys->capabilities_supported,
                               (uint8_t)(sys->capabilities_enabled >> 8), (uint8_t)sys->capabilities_enabled };
            put_tlv(&w, 7, cap, 4);
        }
        if (port->tlvs & LLDP_TLV_MANAGEMENT_ADDRESS)
            for (unsigned i = 0; i < port->mgmt_count; i++) {
                const struct lldp_mgmt_address *m = &port->mgmt[i];
                uint8_t buf[32]; size_t n = 0;
                buf[n++] = (uint8_t)(1 + m->length); buf[n++] = m->family;
                memcpy(buf + n, m->address, m->length); n += m->length;
                buf[n++] = m->interface_subtype;
                buf[n++] = (uint8_t)(m->interface_number >> 24); buf[n++] = (uint8_t)(m->interface_number >> 16);
                buf[n++] = (uint8_t)(m->interface_number >> 8); buf[n++] = (uint8_t)m->interface_number;
                buf[n++] = 0;   /* OID length */
                put_tlv(&w, 8, buf, n);
            }
    }
    put_tlv(&w, 0, "", 0);
    return w.failed ? -1 : (int)w.len;
}

int lldp_frame_for_trunk(const struct lldp_port_config *port, const uint8_t *lldpdu, size_t len, uint8_t *out, size_t outsz)
{
    if (4 + 12 + 8 + 2 + len > outsz) return -1;
    size_t n = 0;
    out[n++] = 0x01; out[n++] = (uint8_t)(port->source >> 8); out[n++] = (uint8_t)port->source; out[n++] = 0x00;
    memcpy(out + n, LLDP_MULTICAST[0], 6); n += 6;
    memcpy(out + n, port->mac, 6); n += 6;
    memset(out + n, 0, 8); n += 8;
    out[n++] = LLDP_ETHERTYPE >> 8; out[n++] = LLDP_ETHERTYPE & 0xff;
    memcpy(out + n, lldpdu, len); n += len;
    return (int)n;
}

int lldp_frame_from_trunk(const uint8_t *frame, size_t len, uint16_t *source)
{
    if (len < 4 + 14 + 2) return -1;
    if (((frame[0] << 8) | frame[1]) != LLDP_TRUNK_DESTINATION) return -1;
    if (((frame[16] << 8) | frame[17]) != LLDP_ETHERTYPE) return -1;
    bool multicast = false;
    for (int i = 0; i < 3; i++) if (!memcmp(frame + 4, LLDP_MULTICAST[i], 6)) multicast = true;
    if (!multicast) return -1;
    *source = (uint16_t)((frame[2] << 8) | frame[3]);
    return 18;
}

/* ---- parsing ---- */

static void copy_text(char *out, size_t outsz, const uint8_t *value, size_t len)
{
    if (len >= outsz) len = outsz - 1;
    for (size_t i = 0; i < len; i++) out[i] = (value[i] >= 0x20 && value[i] != 0x7f) ? (char)value[i] : '.';
    out[len] = 0;
}

static int parse_id(const uint8_t *value, size_t len, struct lldp_id *id)
{
    if (len < 2 || len - 1 > LLDP_MAX_ID) return -1;
    id->subtype = value[0]; id->length = (uint8_t)(len - 1); memcpy(id->value, value + 1, len - 1);
    return 0;
}

int lldp_parse(const uint8_t *pdu, size_t len, struct lldp_neighbour *n, unsigned *unknown)
{
    memset(n, 0, sizeof *n);
    if (unknown) *unknown = 0;
    bool have[4] = { false, false, false, false }, ended = false;
    size_t off = 0; unsigned index = 0;
    while (off + 2 <= len && !ended) {
        unsigned type = pdu[off] >> 1;
        size_t tlen = (size_t)((pdu[off] & 1) << 8 | pdu[off + 1]);
        const uint8_t *v = pdu + off + 2;
        if (off + 2 + tlen > len) return -1;
        /* the three mandatory TLVs must open the PDU in order */
        if (index < 3 && type != index + 1) return -1;
        switch (type) {
        case 0: ended = true; break;
        case 1: if (parse_id(v, tlen, &n->chassis)) return -1; have[1] = true; break;
        case 2: if (parse_id(v, tlen, &n->port)) return -1; have[2] = true; break;
        case 3: if (tlen < 2) return -1; n->ttl = (uint16_t)((v[0] << 8) | v[1]); have[3] = true; break;
        case 4: copy_text(n->port_description, sizeof n->port_description, v, tlen); break;
        case 5: copy_text(n->system_name, sizeof n->system_name, v, tlen); break;
        case 6: copy_text(n->system_description, sizeof n->system_description, v, tlen); break;
        case 7:
            if (tlen < 4) return -1;
            n->has_capabilities = true;
            n->capabilities_supported = (uint16_t)((v[0] << 8) | v[1]); n->capabilities_enabled = (uint16_t)((v[2] << 8) | v[3]);
            break;
        case 8: {
            if (tlen < 9) return -1;
            size_t alen = v[0];
            if (alen < 2 || alen > 17 || 1 + alen + 6 > tlen) return -1;
            if (n->mgmt_count < LLDP_MAX_MGMT) {
                struct lldp_mgmt_address *m = &n->mgmt[n->mgmt_count++];
                m->family = v[1]; m->length = (uint8_t)(alen - 1);
                memcpy(m->address, v + 2, m->length);
                m->interface_subtype = v[1 + alen];
                m->interface_number = ((uint32_t)v[2 + alen] << 24) | ((uint32_t)v[3 + alen] << 16) | ((uint32_t)v[4 + alen] << 8) | v[5 + alen];
            }
            break;
        }
        case 127:
            if (tlen < 4) return -1;
            if (v[0] == 0x00 && v[1] == 0x80 && v[2] == 0xc2 && v[3] == 1 && tlen >= 6) { n->has_vlan = true; n->vlan = (uint16_t)((v[4] << 8) | v[5]); }
            else if (v[0] == 0x00 && v[1] == 0x12 && v[2] == 0x0f && v[3] == 1 && tlen >= 9) {
                n->has_mau = true; n->autoneg = v[4]; n->pmd = (uint16_t)((v[5] << 8) | v[6]); n->mau = (uint16_t)((v[7] << 8) | v[8]);
            } else if (unknown) (*unknown)++;
            break;
        default: if (unknown) (*unknown)++; break;
        }
        off += 2 + tlen; index++;
    }
    if (!have[1] || !have[2] || !have[3]) return -1;
    return 0;
}

/* ---- table ---- */

struct lldp_port_state *lldp_port_by_source(struct lldp_agent *a, uint16_t source)
{
    for (unsigned i = 0; i < a->port_count; i++) if (a->ports[i].config.source == source) return &a->ports[i];
    return NULL;
}

static bool same_identity(const struct lldp_neighbour *a, const struct lldp_neighbour *b)
{
    return a->chassis.subtype == b->chassis.subtype && a->chassis.length == b->chassis.length && !memcmp(a->chassis.value, b->chassis.value, a->chassis.length) &&
           a->port.subtype == b->port.subtype && a->port.length == b->port.length && !memcmp(a->port.value, b->port.value, a->port.length);
}

int lldp_table_update(struct lldp_port_state *p, const struct lldp_neighbour *parsed, time_t now)
{
    struct lldp_neighbour *slot = NULL, *free_slot = NULL;
    for (unsigned i = 0; i < LLDP_MAX_NEIGHBOURS; i++) {
        if (p->neighbours[i].used && same_identity(&p->neighbours[i], parsed)) { slot = &p->neighbours[i]; break; }
        if (!p->neighbours[i].used && !free_slot) free_slot = &p->neighbours[i];
    }
    if (parsed->ttl == 0) {   /* shutdown LLDPDU */
        if (slot) memset(slot, 0, sizeof *slot);
        return 1;
    }
    if (!slot) {
        if (!free_slot) return -1;
        slot = free_slot;
        *slot = *parsed; slot->used = true; slot->first_seen = now; slot->updates = 0;
    } else {
        time_t first = slot->first_seen; uint64_t updates = slot->updates;
        *slot = *parsed; slot->used = true; slot->first_seen = first; slot->updates = updates;
    }
    slot->last_seen = now; slot->expires = now + parsed->ttl; slot->updates++;
    return 0;
}

unsigned lldp_table_expire(struct lldp_port_state *p, time_t now)
{
    unsigned removed = 0;
    for (unsigned i = 0; i < LLDP_MAX_NEIGHBOURS; i++)
        if (p->neighbours[i].used && now >= p->neighbours[i].expires) { memset(&p->neighbours[i], 0, sizeof p->neighbours[i]); removed++; }
    p->rx_ageouts += removed;
    return removed;
}

unsigned lldp_table_count(const struct lldp_port_state *p)
{
    unsigned n = 0;
    for (unsigned i = 0; i < LLDP_MAX_NEIGHBOURS; i++) if (p->neighbours[i].used) n++;
    return n;
}

/* ---- JSON documents ---- */

static char *hex_or_text(const struct lldp_id *id)
{
    static char buf[3 * LLDP_MAX_ID + 1];
    bool printable = id->length > 0;
    for (unsigned i = 0; i < id->length && printable; i++) if (id->value[i] < 0x20 || id->value[i] > 0x7e) printable = false;
    /* subtype 4 (MAC) and 5/7 text are the common ones; show MACs as hex pairs, text as text, else hex */
    if (id->subtype == 4 && id->length == 6) { snprintf(buf, sizeof buf, "%02x:%02x:%02x:%02x:%02x:%02x", id->value[0], id->value[1], id->value[2], id->value[3], id->value[4], id->value[5]); return buf; }
    if (printable && (id->subtype == 5 || id->subtype == 7 || id->subtype == 1 || id->subtype == 2 || id->subtype == 3 || id->subtype == 6)) {
        memcpy(buf, id->value, id->length); buf[id->length] = 0; return buf;
    }
    size_t n = 0;
    for (unsigned i = 0; i < id->length && n + 3 < sizeof buf; i++) n += (size_t)snprintf(buf + n, sizeof buf - n, "%02x", id->value[i]);
    return buf;
}

static const char *ID_SUBTYPES[] = { "reserved", "chassis-component", "interface-alias", "port-component", "mac-address", "network-address", "interface-name", "local" };
static const char *PORT_SUBTYPES[] = { "reserved", "interface-alias", "port-component", "mac-address", "network-address", "interface-name", "agent-circuit-id", "local" };
static const char *CAPABILITY_NAMES[] = { "other", "repeater", "bridge", "wlan-access-point", "router", "telephone", "docsis-cable-device", "station-only" };

static struct ffn_json *id_json(const struct lldp_id *id, bool chassis)
{
    struct ffn_json *o = ffn_json_object();
    const char **names = chassis ? ID_SUBTYPES : PORT_SUBTYPES;
    ffn_json_set(o, "subtype", ffn_json_string(id->subtype < 8 ? names[id->subtype] : "unknown"));
    ffn_json_set(o, "value", ffn_json_string(hex_or_text(id)));
    return o;
}

static struct ffn_json *capabilities_json(uint16_t bits)
{
    struct ffn_json *a = ffn_json_array();
    for (int i = 0; i < 8; i++) if (bits & (1u << i)) ffn_json_push(a, ffn_json_string(CAPABILITY_NAMES[i]));
    return a;
}

static struct ffn_json *mgmt_json(const struct lldp_mgmt_address *m)
{
    struct ffn_json *o = ffn_json_object();
    char text[64] = "";
    if (m->family == 1 && m->length == 4) snprintf(text, sizeof text, "%u.%u.%u.%u", m->address[0], m->address[1], m->address[2], m->address[3]);
    else if (m->family == 2 && m->length == 16) {
        size_t n = 0;
        for (int i = 0; i < 16; i += 2) n += (size_t)snprintf(text + n, sizeof text - n, "%s%02x%02x", i ? ":" : "", m->address[i], m->address[i + 1]);
    } else { size_t n = 0; for (unsigned i = 0; i < m->length && n + 3 < sizeof text; i++) n += (size_t)snprintf(text + n, sizeof text - n, "%02x", m->address[i]); }
    ffn_json_set(o, "family", ffn_json_string(m->family == 1 ? "ipv4" : m->family == 2 ? "ipv6" : m->family == 6 ? "mac" : "other"));
    ffn_json_set(o, "address", ffn_json_string(text));
    ffn_json_set(o, "interface_number", ffn_json_int(m->interface_number));
    return o;
}

static struct ffn_json *neighbour_json(const struct lldp_neighbour *n, time_t now)
{
    struct ffn_json *o = ffn_json_object();
    ffn_json_set(o, "chassis_id", id_json(&n->chassis, true));
    ffn_json_set(o, "port_id", id_json(&n->port, false));
    ffn_json_set(o, "ttl", ffn_json_int(n->ttl));
    ffn_json_set(o, "port_description", ffn_json_string(n->port_description));
    ffn_json_set(o, "system_name", ffn_json_string(n->system_name));
    ffn_json_set(o, "system_description", ffn_json_string(n->system_description));
    if (n->has_capabilities) {
        struct ffn_json *c = ffn_json_object();
        ffn_json_set(c, "supported", capabilities_json(n->capabilities_supported));
        ffn_json_set(c, "enabled", capabilities_json(n->capabilities_enabled));
        ffn_json_set(o, "capabilities", c);
    } else ffn_json_set(o, "capabilities", ffn_json_null());
    struct ffn_json *m = ffn_json_array();
    for (unsigned i = 0; i < n->mgmt_count; i++) ffn_json_push(m, mgmt_json(&n->mgmt[i]));
    ffn_json_set(o, "management_addresses", m);
    ffn_json_set(o, "vlan_id", n->has_vlan ? ffn_json_int(n->vlan) : ffn_json_null());
    if (n->has_mau) {
        struct lldp_mgmt_address dummy; (void)dummy;
        struct ffn_json *p = ffn_json_object();
        ffn_json_set(p, "autoneg_supported", ffn_json_bool(n->autoneg & 1));
        ffn_json_set(p, "autoneg_enabled", ffn_json_bool(n->autoneg & 2));
        ffn_json_set(p, "pmd_advertised", ffn_json_int(n->pmd));
        ffn_json_set(p, "mau_type", ffn_json_int(n->mau));
        ffn_json_set(o, "mac_phy", p);
    } else ffn_json_set(o, "mac_phy", ffn_json_null());
    ffn_json_set(o, "age", ffn_json_int((long long)(now - n->first_seen)));
    ffn_json_set(o, "last_seen", ffn_json_int((long long)(now - n->last_seen)));
    ffn_json_set(o, "expires_in", ffn_json_int((long long)(n->expires > now ? n->expires - now : 0)));
    ffn_json_set(o, "updates", ffn_json_int((long long)n->updates));
    return o;
}

char *lldp_neighbours_json(const struct lldp_agent *a, time_t now)
{
    struct ffn_json *doc = ffn_json_object(), *ports = ffn_json_array();
    for (unsigned i = 0; i < a->port_count; i++) {
        const struct lldp_port_state *p = &a->ports[i];
        struct ffn_json *po = ffn_json_object(), *list = ffn_json_array();
        ffn_json_set(po, "interface", ffn_json_string(p->config.name));
        ffn_json_set(po, "source", ffn_json_int(p->config.source));
        for (unsigned k = 0; k < LLDP_MAX_NEIGHBOURS; k++) if (p->neighbours[k].used) ffn_json_push(list, neighbour_json(&p->neighbours[k], now));
        ffn_json_set(po, "neighbours", list);
        ffn_json_push(ports, po);
    }
    ffn_json_set(doc, "ports", ports);
    char *text = ffn_json_dump(doc);
    ffn_json_free(doc);
    return text;
}

static const char *mode_name(enum lldp_mode m)
{
    return m == LLDP_TRANSMIT_RECEIVE ? "transmit-receive" : m == LLDP_TRANSMIT_ONLY ? "transmit-only" : m == LLDP_RECEIVE_ONLY ? "receive-only" : "off";
}

char *lldp_status_json(const struct lldp_agent *a, time_t now, const char *revision, const char *configuration, bool socket_open)
{
    struct ffn_json *doc = ffn_json_object(), *ports = ffn_json_array();
    ffn_json_set(doc, "revision", ffn_json_string(revision ? revision : ""));
    ffn_json_set(doc, "configuration", ffn_json_string(configuration ? configuration : ""));
    ffn_json_set(doc, "running", ffn_json_bool(socket_open));
    ffn_json_set(doc, "interval", ffn_json_int(a->interval));
    ffn_json_set(doc, "hold", ffn_json_int(a->hold));
    char mac[18];
    snprintf(mac, sizeof mac, "%02x:%02x:%02x:%02x:%02x:%02x", a->system.chassis_mac[0], a->system.chassis_mac[1], a->system.chassis_mac[2], a->system.chassis_mac[3], a->system.chassis_mac[4], a->system.chassis_mac[5]);
    struct ffn_json *sys = ffn_json_object();
    ffn_json_set(sys, "chassis_id", ffn_json_string(mac)); ffn_json_set(sys, "name", ffn_json_string(a->system.name));
    ffn_json_set(doc, "system", sys);
    for (unsigned i = 0; i < a->port_count; i++) {
        const struct lldp_port_state *p = &a->ports[i];
        struct ffn_json *po = ffn_json_object();
        ffn_json_set(po, "interface", ffn_json_string(p->config.name));
        ffn_json_set(po, "source", ffn_json_int(p->config.source));
        ffn_json_set(po, "mode", ffn_json_string(mode_name(p->config.mode)));
        ffn_json_set(po, "tx_frames", ffn_json_int((long long)p->tx_frames));
        ffn_json_set(po, "rx_frames", ffn_json_int((long long)p->rx_frames));
        ffn_json_set(po, "rx_discarded", ffn_json_int((long long)p->rx_discarded));
        ffn_json_set(po, "rx_ageouts", ffn_json_int((long long)p->rx_ageouts));
        ffn_json_set(po, "rx_unknown_tlvs", ffn_json_int((long long)p->rx_unknown_tlvs));
        ffn_json_set(po, "neighbours", ffn_json_int(lldp_table_count(p)));
        ffn_json_set(po, "next_tx_in", ffn_json_int((long long)(p->next_tx > now ? p->next_tx - now : 0)));
        ffn_json_push(ports, po);
    }
    ffn_json_set(doc, "ports", ports);
    char *text = ffn_json_dump(doc);
    ffn_json_free(doc);
    return text;
}

/* ---- intent ---- */

static int parse_mac(const char *text, uint8_t *out)
{
    unsigned v[6];
    if (!text || sscanf(text, "%2x:%2x:%2x:%2x:%2x:%2x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return -1;
    for (int i = 0; i < 6; i++) out[i] = (uint8_t)v[i];
    return 0;
}

static int parse_address(const char *text, struct lldp_mgmt_address *m)
{
    unsigned v4[4];
    memset(m, 0, sizeof *m);
    if (text && sscanf(text, "%u.%u.%u.%u", &v4[0], &v4[1], &v4[2], &v4[3]) == 4 && v4[0] < 256 && v4[1] < 256 && v4[2] < 256 && v4[3] < 256 && !strchr(text, ':')) {
        m->family = 1; m->length = 4;
        for (int i = 0; i < 4; i++) m->address[i] = (uint8_t)v4[i];
        m->interface_subtype = 1;   /* unknown */
        return 0;
    }
    return -1;
}

static void copy_str(char *out, size_t outsz, const char *in) { snprintf(out, outsz, "%s", in ? in : ""); }

int lldp_load_intent(struct lldp_agent *a, const char *json, size_t len, char *revision, size_t revsz, char *configuration, size_t cfgsz, char *err, size_t errsz)
{
    char perr[96];
    struct ffn_json *doc = ffn_json_parse(json, len, perr, sizeof perr);
    if (!doc || doc->type != FFN_JSON_OBJECT) { ffn_json_free(doc); snprintf(err, errsz, "intent is not a JSON object"); return -1; }
    struct lldp_agent fresh; memset(&fresh, 0, sizeof fresh);
    const struct ffn_json *sys = ffn_json_get(doc, "system");
    long long interval = 30, hold = 4;
    ffn_json_get_int(doc, "interval", &interval); ffn_json_get_int(doc, "hold", &hold);
    if (interval < 5 || interval > 32768 || hold < 2 || hold > 10) { ffn_json_free(doc); snprintf(err, errsz, "interval 5..32768 and hold 2..10 required"); return -1; }
    fresh.interval = (unsigned)interval; fresh.hold = (unsigned)hold;
    if (!sys || parse_mac(ffn_json_get_string(sys, "chassis_id"), fresh.system.chassis_mac)) { ffn_json_free(doc); snprintf(err, errsz, "system.chassis_id MAC required"); return -1; }
    copy_str(fresh.system.name, sizeof fresh.system.name, ffn_json_get_string(sys, "name"));
    copy_str(fresh.system.description, sizeof fresh.system.description, ffn_json_get_string(sys, "description"));
    fresh.system.capabilities_supported = 0x14;   /* bridge + router */
    fresh.system.capabilities_enabled = 0x10;     /* router */
    const struct ffn_json *ports = ffn_json_get(doc, "ports");
    if (!ports || ports->type != FFN_JSON_ARRAY || ports->count > LLDP_MAX_PORTS) { ffn_json_free(doc); snprintf(err, errsz, "ports array (max %d) required", LLDP_MAX_PORTS); return -1; }
    for (size_t i = 0; i < ports->count; i++) {
        const struct ffn_json *po = ports->items[i];
        struct lldp_port_config *c = &fresh.ports[fresh.port_count].config;
        const char *name = ffn_json_get_string(po, "name"), *mode = ffn_json_get_string(po, "mode");
        long long source = -1;
        if (!name || strlen(name) >= sizeof c->name || !ffn_json_get_int(po, "source", &source) || source < 0 || source > 65535 || parse_mac(ffn_json_get_string(po, "mac"), c->mac)) {
            ffn_json_free(doc); snprintf(err, errsz, "port %zu: name, source and mac required", i); return -1;
        }
        for (unsigned k = 0; k < fresh.port_count; k++) if (fresh.ports[k].config.source == source) { ffn_json_free(doc); snprintf(err, errsz, "port %zu: duplicate source", i); return -1; }
        copy_str(c->name, sizeof c->name, name);
        c->source = (uint16_t)source;
        c->mode = !mode || !strcmp(mode, "transmit-receive") ? LLDP_TRANSMIT_RECEIVE : !strcmp(mode, "transmit-only") ? LLDP_TRANSMIT_ONLY : !strcmp(mode, "receive-only") ? LLDP_RECEIVE_ONLY : LLDP_OFF;
        if (c->mode == LLDP_OFF) { ffn_json_free(doc); snprintf(err, errsz, "port %zu: unsupported mode", i); return -1; }
        const struct ffn_json *tlvs = ffn_json_get(po, "tlvs");
        c->tlvs = 0;
        if (!tlvs) c->tlvs = LLDP_TLV_ALL;
        else if (tlvs->type == FFN_JSON_ARRAY)
            for (size_t k = 0; k < tlvs->count; k++) {
                const char *t = tlvs->items[k]->type == FFN_JSON_STRING ? tlvs->items[k]->string : "";
                if (!strcmp(t, "port-description")) c->tlvs |= LLDP_TLV_PORT_DESCRIPTION;
                else if (!strcmp(t, "system-name")) c->tlvs |= LLDP_TLV_SYSTEM_NAME;
                else if (!strcmp(t, "system-description")) c->tlvs |= LLDP_TLV_SYSTEM_DESCRIPTION;
                else if (!strcmp(t, "system-capabilities")) c->tlvs |= LLDP_TLV_SYSTEM_CAPABILITIES;
                else if (!strcmp(t, "management-address")) c->tlvs |= LLDP_TLV_MANAGEMENT_ADDRESS;
                else { ffn_json_free(doc); snprintf(err, errsz, "port %zu: unknown tlv %s", i, t); return -1; }
            }
        copy_str(c->description, sizeof c->description, ffn_json_get_string(po, "description"));
        const struct ffn_json *mgmt = ffn_json_get(po, "management");
        if (mgmt && mgmt->type == FFN_JSON_ARRAY)
            for (size_t k = 0; k < mgmt->count && c->mgmt_count < LLDP_MAX_MGMT; k++)
                if (mgmt->items[k]->type == FFN_JSON_STRING && parse_address(mgmt->items[k]->string, &c->mgmt[c->mgmt_count]) == 0) c->mgmt_count++;
        fresh.port_count++;
    }
    /* keep live tables and counters for ports that persist across a reload */
    for (unsigned i = 0; i < fresh.port_count; i++) {
        struct lldp_port_state *old = lldp_port_by_source(a, fresh.ports[i].config.source);
        if (old) {
            memcpy(fresh.ports[i].neighbours, old->neighbours, sizeof old->neighbours);
            fresh.ports[i].tx_frames = old->tx_frames; fresh.ports[i].rx_frames = old->rx_frames; fresh.ports[i].rx_discarded = old->rx_discarded;
            fresh.ports[i].rx_ageouts = old->rx_ageouts; fresh.ports[i].rx_unknown_tlvs = old->rx_unknown_tlvs; fresh.ports[i].next_tx = old->next_tx;
        }
    }
    const char *rev = ffn_json_get_string(doc, "revision"), *cfg = ffn_json_get_string(doc, "configuration");
    long long revn;
    if (rev) snprintf(revision, revsz, "%s", rev); else if (ffn_json_get_int(doc, "revision", &revn)) snprintf(revision, revsz, "%lld", revn); else snprintf(revision, revsz, "%s", "");
    snprintf(configuration, cfgsz, "%s", cfg ? cfg : "");
    ffn_json_free(doc);
    *a = fresh;
    return 0;
}
