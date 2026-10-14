/* SPDX-License-Identifier: GPL-2.0-or-later */
/* LLDP codec, trunk framing, neighbour table, intent loading and published JSON. */
#include "ffn_lldp.c"
#include "ffn_json.c"
#include <assert.h>

static struct lldp_system SYS;
static struct lldp_port_config PORT;

static void fixture(void)
{
    memset(&SYS, 0, sizeof SYS); memset(&PORT, 0, sizeof PORT);
    const uint8_t mac[6] = { 0x08, 0x66, 0x1f, 0x03, 0x08, 0xcd };
    memcpy(SYS.chassis_mac, mac, 6);
    strcpy(SYS.name, "PA-5220-FFN"); strcpy(SYS.description, "FFN-NGFW on PA-5220");
    SYS.capabilities_supported = 0x14; SYS.capabilities_enabled = 0x10;
    strcpy(PORT.name, "ethernet1/5"); PORT.source = 16; PORT.mode = LLDP_TRANSMIT_RECEIVE; PORT.tlvs = LLDP_TLV_ALL;
    const uint8_t pmac[6] = { 0xb2, 0xbd, 0x5a, 0xdd, 0x09, 0x6a };
    memcpy(PORT.mac, pmac, 6);
    strcpy(PORT.description, "Verizon Enterprise CPE");
    PORT.mgmt_count = 1; PORT.mgmt[0].family = 1; PORT.mgmt[0].length = 4;
    const uint8_t ip[4] = { 152, 193, 185, 10 }; memcpy(PORT.mgmt[0].address, ip, 4); PORT.mgmt[0].interface_subtype = 1; PORT.mgmt[0].interface_number = 0;
}

static void test_build_and_parse_round_trip(void)
{
    fixture();
    uint8_t pdu[600];
    int n = lldp_build(&SYS, &PORT, 120, pdu, sizeof pdu);
    assert(n > 0);
    assert(pdu[0] == (1 << 1) && pdu[1] == 7 && pdu[2] == 4);          /* chassis TLV, 7 bytes, MAC subtype */
    assert(pdu[n - 2] == 0 && pdu[n - 1] == 0);                        /* end TLV */
    struct lldp_neighbour nb; unsigned unknown = 9;
    assert(lldp_parse(pdu, (size_t)n, &nb, &unknown) == 0 && unknown == 0);
    assert(nb.chassis.subtype == 4 && nb.chassis.length == 6 && !memcmp(nb.chassis.value, SYS.chassis_mac, 6));
    assert(nb.port.subtype == 5 && nb.port.length == 11 && !memcmp(nb.port.value, "ethernet1/5", 11));
    assert(nb.ttl == 120);
    assert(!strcmp(nb.port_description, "Verizon Enterprise CPE") && !strcmp(nb.system_name, "PA-5220-FFN") && !strcmp(nb.system_description, "FFN-NGFW on PA-5220"));
    assert(nb.has_capabilities && nb.capabilities_supported == 0x14 && nb.capabilities_enabled == 0x10);
    assert(nb.mgmt_count == 1 && nb.mgmt[0].family == 1 && nb.mgmt[0].address[0] == 152 && nb.mgmt[0].address[3] == 10);
    /* a shutdown PDU carries only the mandatory TLVs */
    n = lldp_build(&SYS, &PORT, 0, pdu, sizeof pdu);
    assert(n == 2 + 7 + 2 + 12 + 2 + 2 + 2);
    /* optional TLVs follow the profile mask */
    PORT.tlvs = LLDP_TLV_SYSTEM_NAME;
    n = lldp_build(&SYS, &PORT, 120, pdu, sizeof pdu);
    assert(lldp_parse(pdu, (size_t)n, &nb, NULL) == 0 && nb.system_name[0] && !nb.port_description[0] && !nb.has_capabilities && nb.mgmt_count == 0);
}

static void test_trunk_framing(void)
{
    fixture();
    uint8_t pdu[600], frame[700];
    int n = lldp_build(&SYS, &PORT, 120, pdu, sizeof pdu);
    int f = lldp_frame_for_trunk(&PORT, pdu, (size_t)n, frame, sizeof frame);
    assert(f == 4 + 12 + 8 + 2 + n);
    assert(frame[0] == 0x01 && frame[1] == 0 && frame[2] == 16 && frame[3] == 0);     /* injected header: BCM 16 */
    assert(frame[4] == 0x01 && frame[9] == 0x0e);                                   /* nearest-bridge multicast */
    assert(!memcmp(frame + 10, PORT.mac, 6));
    for (int i = 16; i < 24; i++) assert(frame[i] == 0);                            /* eight-byte pad */
    assert(frame[24] == 0x88 && frame[25] == 0xcc);
    /* a received control frame: OTMH (24, source) + Ethernet */
    uint8_t rx[700]; size_t len = 0;
    rx[len++] = 0; rx[len++] = 24; rx[len++] = 0; rx[len++] = 16;
    memcpy(rx + len, frame + 4, 12); len += 12;
    rx[len++] = 0x88; rx[len++] = 0xcc;
    memcpy(rx + len, pdu, (size_t)n); len += (size_t)n;
    uint16_t source = 0;
    assert(lldp_frame_from_trunk(rx, len, &source) == 18 && source == 16);
    struct lldp_neighbour nb;
    assert(lldp_parse(rx + 18, len - 18, &nb, NULL) == 0 && nb.ttl == 120);
    rx[1] = 23; assert(lldp_frame_from_trunk(rx, len, &source) == -1); rx[1] = 24;    /* not the trunk destination */
    rx[17] = 0x09; assert(lldp_frame_from_trunk(rx, len, &source) == -1); rx[17] = 0xcc;   /* LACP, not LLDP */
    rx[4] = 0xff; assert(lldp_frame_from_trunk(rx, len, &source) == -1); rx[4] = 0x01;    /* not an LLDP multicast */
    assert(lldp_frame_from_trunk(rx, 10, &source) == -1);
}

static void test_parse_rejects_malformed_and_decodes_org_tlvs(void)
{
    struct lldp_neighbour nb; unsigned unknown;
    /* TTL missing */
    const uint8_t no_ttl[] = { 0x02, 0x07, 0x04, 1, 2, 3, 4, 5, 6, 0x04, 0x02, 0x05, 'p', 0x00, 0x00 };
    assert(lldp_parse(no_ttl, sizeof no_ttl, &nb, NULL) == -1);
    /* mandatory order violated (port before chassis) */
    const uint8_t swapped[] = { 0x04, 0x02, 0x05, 'p', 0x02, 0x07, 0x04, 1, 2, 3, 4, 5, 6, 0x06, 0x02, 0, 120, 0x00, 0x00 };
    assert(lldp_parse(swapped, sizeof swapped, &nb, NULL) == -1);
    /* length overflow */
    const uint8_t overflow[] = { 0x02, 0x07, 0x04, 1, 2, 3, 4, 5, 6, 0x04, 0x02, 0x05, 'p', 0x06, 0x02, 0, 120, 0x08, 0x40, 0x00 };
    assert(lldp_parse(overflow, sizeof overflow, &nb, NULL) == -1);
    /* a switch-like PDU with 802.1 VLAN, 802.3 MAC/PHY, an unknown org TLV and an unknown type */
    const uint8_t sw[] = {
        0x02, 0x07, 0x04, 0x00, 0x11, 0x22, 0x33, 0x44, 0x55,                 /* chassis MAC */
        0x04, 0x05, 0x05, 'G', 'i', '1', '/',                                  /* port interface-name "Gi1/" */
        0x06, 0x02, 0x00, 0x78,                                                /* TTL 120 */
        0x0a, 0x03, 'S', 'W', '1',                                             /* system name */
        0x0e, 0x04, 0x00, 0x04, 0x00, 0x04,                                    /* capabilities: bridge */
        0xfe, 0x06, 0x00, 0x80, 0xc2, 0x01, 0x00, 0x45,                        /* 802.1 PVID 69 */
        0xfe, 0x09, 0x00, 0x12, 0x0f, 0x01, 0x03, 0x6c, 0x01, 0x00, 0x1e,      /* 802.3 MAC/PHY: AN supported+enabled, pmd 0x6c01, MAU 30 */
        0xfe, 0x05, 0x00, 0x12, 0xbb, 0x01, 0x00,                              /* unknown org TLV */
        0x10, 0x02, 0xaa, 0xbb,                                                /* unknown type 8 (mgmt) with bad length -> handled: 9 bytes required */
        0x00, 0x00 };
    /* the mgmt TLV above is too short: parse must reject it; drop it for the positive check */
    assert(lldp_parse(sw, sizeof sw, &nb, &unknown) == -1);
    uint8_t ok[sizeof sw]; size_t len = sizeof sw - 4 - 2; memcpy(ok, sw, len); ok[len++] = 0; ok[len++] = 0;
    assert(lldp_parse(ok, len, &nb, &unknown) == 0);
    assert(!strcmp(nb.system_name, "SW1") && nb.port.subtype == 5 && !memcmp(nb.port.value, "Gi1/", 4));
    assert(nb.has_vlan && nb.vlan == 69);
    assert(nb.has_mau && nb.autoneg == 3 && nb.pmd == 0x6c01 && nb.mau == 30);
    assert(nb.has_capabilities && nb.capabilities_enabled == 0x04);
    assert(unknown == 1);
}

static void test_table_lifecycle(void)
{
    fixture();
    struct lldp_port_state p; memset(&p, 0, sizeof p); p.config = PORT;
    uint8_t pdu[600];
    struct lldp_neighbour nb;
    int n = lldp_build(&SYS, &PORT, 10, pdu, sizeof pdu);
    assert(lldp_parse(pdu, (size_t)n, &nb, NULL) == 0);
    assert(lldp_table_update(&p, &nb, 100) == 0 && lldp_table_count(&p) == 1);
    assert(p.neighbours[0].first_seen == 100 && p.neighbours[0].expires == 110 && p.neighbours[0].updates == 1);
    assert(lldp_table_update(&p, &nb, 105) == 0 && lldp_table_count(&p) == 1);
    assert(p.neighbours[0].first_seen == 100 && p.neighbours[0].last_seen == 105 && p.neighbours[0].expires == 115 && p.neighbours[0].updates == 2);
    assert(lldp_table_expire(&p, 114) == 0 && lldp_table_expire(&p, 115) == 1 && lldp_table_count(&p) == 0 && p.rx_ageouts == 1);
    /* shutdown removes */
    assert(lldp_table_update(&p, &nb, 200) == 0);
    nb.ttl = 0;
    assert(lldp_table_update(&p, &nb, 201) == 1 && lldp_table_count(&p) == 0);
    /* distinct port ids are distinct neighbours; the table has a bound */
    nb.ttl = 100;
    for (unsigned i = 0; i < LLDP_MAX_NEIGHBOURS; i++) { nb.port.value[0] = (uint8_t)('a' + i); assert(lldp_table_update(&p, &nb, 300) == 0); }
    nb.port.value[0] = 'z';
    assert(lldp_table_update(&p, &nb, 300) == -1 && lldp_table_count(&p) == LLDP_MAX_NEIGHBOURS);
}

static const char INTENT[] =
    "{\"revision\": 12, \"configuration\": \"acc0e09a\", \"interval\": 30, \"hold\": 4,"
    " \"system\": {\"chassis_id\": \"08:66:1f:03:08:cd\", \"name\": \"PA-5220-FFN\", \"description\": \"FFN-NGFW\"},"
    " \"ports\": [{\"name\": \"ethernet1/5\", \"source\": 16, \"mac\": \"b2:bd:5a:dd:09:6a\", \"mode\": \"transmit-receive\","
    "             \"tlvs\": [\"system-name\", \"management-address\"], \"description\": \"WAN\", \"management\": [\"152.193.185.10\", \"bad\"]},"
    "            {\"name\": \"ethernet1/2\", \"source\": 13, \"mac\": \"aa:bc:a4:87:54:e3\", \"mode\": \"receive-only\"}]}";

static void test_intent_loading_and_reload(void)
{
    struct lldp_agent a; memset(&a, 0, sizeof a);
    char rev[32], cfg[64], err[160];
    assert(lldp_load_intent(&a, INTENT, sizeof INTENT - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == 0);
    assert(!strcmp(rev, "12") && !strcmp(cfg, "acc0e09a") && a.interval == 30 && a.hold == 4 && a.port_count == 2);
    assert(a.system.chassis_mac[0] == 0x08 && a.system.chassis_mac[5] == 0xcd && !strcmp(a.system.name, "PA-5220-FFN"));
    assert(a.ports[0].config.source == 16 && a.ports[0].config.mode == LLDP_TRANSMIT_RECEIVE && a.ports[0].config.tlvs == (LLDP_TLV_SYSTEM_NAME | LLDP_TLV_MANAGEMENT_ADDRESS));
    assert(a.ports[0].config.mgmt_count == 1 && a.ports[0].config.mgmt[0].address[0] == 152);
    assert(a.ports[1].config.mode == LLDP_RECEIVE_ONLY && a.ports[1].config.tlvs == LLDP_TLV_ALL);
    /* a neighbour and counters survive a reload that keeps the port */
    a.ports[0].rx_frames = 7; a.ports[0].neighbours[0].used = true; a.ports[0].neighbours[0].ttl = 120; a.ports[0].neighbours[0].expires = 999;
    const char again[] = "{\"revision\": 13, \"interval\": 10, \"hold\": 3, \"system\": {\"chassis_id\": \"08:66:1f:03:08:cd\", \"name\": \"X\"},"
                         " \"ports\": [{\"name\": \"ethernet1/5\", \"source\": 16, \"mac\": \"b2:bd:5a:dd:09:6a\"}]}";
    assert(lldp_load_intent(&a, again, sizeof again - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == 0);
    assert(a.port_count == 1 && a.ports[0].rx_frames == 7 && a.ports[0].neighbours[0].used && a.interval == 10);
    /* rejections */
    struct lldp_agent b; memset(&b, 0, sizeof b);
    const char nomac[] = "{\"system\": {\"chassis_id\": \"08:66:1f:03:08:cd\"}, \"ports\": [{\"name\": \"ethernet1/5\", \"source\": 16}]}";
    assert(lldp_load_intent(&b, nomac, sizeof nomac - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == -1 && strstr(err, "mac required"));
    const char dup[] = "{\"system\": {\"chassis_id\": \"08:66:1f:03:08:cd\"}, \"ports\": [{\"name\": \"a\", \"source\": 16, \"mac\": \"00:00:00:00:00:01\"}, {\"name\": \"b\", \"source\": 16, \"mac\": \"00:00:00:00:00:02\"}]}";
    assert(lldp_load_intent(&b, dup, sizeof dup - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == -1 && strstr(err, "duplicate source"));
    const char badmode[] = "{\"system\": {\"chassis_id\": \"08:66:1f:03:08:cd\"}, \"ports\": [{\"name\": \"a\", \"source\": 16, \"mac\": \"00:00:00:00:00:01\", \"mode\": \"loud\"}]}";
    assert(lldp_load_intent(&b, badmode, sizeof badmode - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == -1 && strstr(err, "unsupported mode"));
    const char badhold[] = "{\"hold\": 1, \"system\": {\"chassis_id\": \"08:66:1f:03:08:cd\"}, \"ports\": []}";
    assert(lldp_load_intent(&b, badhold, sizeof badhold - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == -1 && strstr(err, "hold"));
    assert(b.port_count == 0);   /* a rejected intent leaves the agent untouched */
}

static void test_published_json(void)
{
    struct lldp_agent a; memset(&a, 0, sizeof a);
    char rev[32], cfg[64], err[160];
    assert(lldp_load_intent(&a, INTENT, sizeof INTENT - 1, rev, sizeof rev, cfg, sizeof cfg, err, sizeof err) == 0);
    fixture();
    uint8_t pdu[600]; struct lldp_neighbour nb;
    int n = lldp_build(&SYS, &PORT, 120, pdu, sizeof pdu);
    assert(lldp_parse(pdu, (size_t)n, &nb, NULL) == 0);
    assert(lldp_table_update(&a.ports[0], &nb, 1000) == 0);
    char *text = lldp_neighbours_json(&a, 1030);
    assert(text);
    struct ffn_json *doc = ffn_json_parse(text, strlen(text), NULL, 0);
    assert(doc);
    const struct ffn_json *ports = ffn_json_get(doc, "ports");
    assert(ports && ports->count == 2);
    const struct ffn_json *list = ffn_json_get(ports->items[0], "neighbours");
    assert(list && list->count == 1);
    const struct ffn_json *entry = list->items[0];
    assert(!strcmp(ffn_json_get_string(entry, "system_name"), "PA-5220-FFN"));
    assert(!strcmp(ffn_json_get_string(ffn_json_get(entry, "chassis_id"), "value"), "08:66:1f:03:08:cd"));
    assert(!strcmp(ffn_json_get_string(ffn_json_get(entry, "port_id"), "value"), "ethernet1/5"));
    long long v; assert(ffn_json_get_int(entry, "age", &v) && v == 30 && ffn_json_get_int(entry, "expires_in", &v) && v == 90);
    const struct ffn_json *m = ffn_json_get(entry, "management_addresses");
    assert(m && m->count == 1 && !strcmp(ffn_json_get_string(m->items[0], "address"), "152.193.185.10"));
    ffn_json_free(doc); free(text);
    text = lldp_status_json(&a, 1030, rev, cfg, true);
    doc = ffn_json_parse(text, strlen(text), NULL, 0);
    assert(doc && !strcmp(ffn_json_get_string(doc, "revision"), "12"));
    bool running; assert(ffn_json_get_bool(doc, "running", &running) && running);
    const struct ffn_json *sp = ffn_json_get(doc, "ports");
    assert(sp && sp->count == 2 && ffn_json_get_int(sp->items[0], "neighbours", &v) && v == 1);
    assert(!strcmp(ffn_json_get_string(sp->items[1], "mode"), "receive-only"));
    ffn_json_free(doc); free(text);
}

int main(void)
{
    test_build_and_parse_round_trip();
    test_trunk_framing();
    test_parse_rejects_malformed_and_decodes_org_tlvs();
    test_table_lifecycle();
    test_intent_loading_and_reload();
    test_published_json();
    puts("test_lldp: ok");
    return 0;
}
