/* SPDX-License-Identifier: GPL-2.0-or-later */
/* LLDP (IEEE 802.1AB) codec and neighbour table for the FFN dataplane agent.
 *
 * Frames travel on the OCTEON packet trunk: received control frames carry a
 * 4-byte OTMH prefix (destination 24, then the BCM source port, both BE16)
 * ahead of the Ethernet header; transmitted frames carry the injected header
 * 0x01, BE16 BCM port, 0x00, then the two MACs, eight zero bytes, and the
 * ethertype onward. Nothing here touches a socket; ffn_lldpd.c owns I/O. */
#ifndef FFN_LLDP_H
#define FFN_LLDP_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#define LLDP_ETHERTYPE 0x88cc
#define LLDP_TRUNK_DESTINATION 24
#define LLDP_MAX_FRAME 1522
#define LLDP_MAX_ID 256
#define LLDP_MAX_TEXT 256
#define LLDP_MAX_MGMT 8
#define LLDP_MAX_PORTS 24
#define LLDP_MAX_NEIGHBOURS 16      /* per port */

enum lldp_mode { LLDP_OFF = 0, LLDP_TRANSMIT_RECEIVE, LLDP_TRANSMIT_ONLY, LLDP_RECEIVE_ONLY };

/* Optional TLVs a port transmits (the PAN-OS LLDP profile option set). */
#define LLDP_TLV_PORT_DESCRIPTION 0x01
#define LLDP_TLV_SYSTEM_NAME 0x02
#define LLDP_TLV_SYSTEM_DESCRIPTION 0x04
#define LLDP_TLV_SYSTEM_CAPABILITIES 0x08
#define LLDP_TLV_MANAGEMENT_ADDRESS 0x10
#define LLDP_TLV_ALL 0x1f

struct lldp_id { uint8_t subtype; uint8_t length; uint8_t value[LLDP_MAX_ID]; };

struct lldp_mgmt_address {
    uint8_t family;          /* IANA AFN: 1 IPv4, 2 IPv6, 6 802 (MAC) */
    uint8_t length;          /* address bytes */
    uint8_t address[16];
    uint8_t interface_subtype;
    uint32_t interface_number;
};

struct lldp_system {
    uint8_t chassis_mac[6];
    char name[LLDP_MAX_TEXT];
    char description[LLDP_MAX_TEXT];
    uint16_t capabilities_supported, capabilities_enabled;
};

struct lldp_port_config {
    char name[32];            /* ethernet1/N */
    uint16_t source;          /* BCM port on the trunk */
    uint8_t mac[6];           /* frame source address */
    enum lldp_mode mode;
    unsigned tlvs;            /* LLDP_TLV_* mask */
    char description[LLDP_MAX_TEXT];
    unsigned mgmt_count;
    struct lldp_mgmt_address mgmt[LLDP_MAX_MGMT];
};

struct lldp_neighbour {
    bool used;
    struct lldp_id chassis, port;
    uint16_t ttl;
    char port_description[LLDP_MAX_TEXT], system_name[LLDP_MAX_TEXT], system_description[LLDP_MAX_TEXT];
    bool has_capabilities; uint16_t capabilities_supported, capabilities_enabled;
    unsigned mgmt_count; struct lldp_mgmt_address mgmt[LLDP_MAX_MGMT];
    bool has_vlan; uint16_t vlan;                      /* 802.1 port VLAN ID */
    bool has_mau; uint8_t autoneg; uint16_t pmd; uint16_t mau;   /* 802.3 MAC/PHY */
    time_t first_seen, last_seen, expires;             /* monotonic seconds */
    uint64_t updates;
};

struct lldp_port_state {
    struct lldp_port_config config;
    struct lldp_neighbour neighbours[LLDP_MAX_NEIGHBOURS];
    uint64_t tx_frames, rx_frames, rx_discarded, rx_ageouts, rx_unknown_tlvs;
    time_t next_tx;
};

struct lldp_agent {
    struct lldp_system system;
    unsigned interval;        /* seconds between transmissions */
    unsigned hold;            /* TTL = interval * hold, capped at 65535 */
    unsigned port_count;
    struct lldp_port_state ports[LLDP_MAX_PORTS];
};

/* Build the LLDPDU (ethertype payload only) for one port. ttl 0 announces shutdown. Returns bytes or -1. */
int lldp_build(const struct lldp_system *, const struct lldp_port_config *, uint16_t ttl, uint8_t *out, size_t outsz);
/* Wrap an LLDPDU in the trunk's injected header + Ethernet header. Returns bytes or -1. */
int lldp_frame_for_trunk(const struct lldp_port_config *, const uint8_t *lldpdu, size_t len, uint8_t *out, size_t outsz);
/* Strip a received trunk control frame: validates the OTMH destination, the LLDP multicast
 * destination and the ethertype; sets *source and returns the LLDPDU offset, or -1. */
int lldp_frame_from_trunk(const uint8_t *frame, size_t len, uint16_t *source);
/* Parse an LLDPDU into a neighbour record (identity, TTL, optional and org TLVs). 0 or -1 (malformed). */
int lldp_parse(const uint8_t *lldpdu, size_t len, struct lldp_neighbour *out, unsigned *unknown_tlvs);

/* Table maintenance; now is monotonic seconds. */
struct lldp_port_state *lldp_port_by_source(struct lldp_agent *, uint16_t source);
int lldp_table_update(struct lldp_port_state *, const struct lldp_neighbour *parsed, time_t now);   /* 0 kept/added, 1 removed (ttl 0), -1 full */
unsigned lldp_table_expire(struct lldp_port_state *, time_t now);
unsigned lldp_table_count(const struct lldp_port_state *);

/* JSON documents the daemon publishes; both return malloc'd text. */
char *lldp_neighbours_json(const struct lldp_agent *, time_t now);
char *lldp_status_json(const struct lldp_agent *, time_t now, const char *revision, const char *configuration, bool socket_open);

/* Intent document: parses the JSON written by the dataplane plane verb into the agent. 0 or -1 with err. */
int lldp_load_intent(struct lldp_agent *, const char *json, size_t len, char *revision, size_t revsz, char *configuration, size_t cfgsz, char *err, size_t errsz);

#endif
