/* SPDX-License-Identifier: GPL-2.0-or-later
 * Unit test of the FFN Protect tunnel worker's ESP and control parsing, built
 * on the host against its libcrypto: no tun device, no sockets.
 */
#define FFN_PROTECT_TUNNEL_TEST 1
#include "ffn_protect_tunnel.c"
#include <assert.h>

static int failures;
#define CHECK(cond, what) do { if (cond) printf("  ok   %s\n", what); else { printf("  FAIL %s\n", what); failures++; } } while (0)

static void fill(struct session *s, int enc_len) {
    memset(s, 0, sizeof(*s));
    s->used = 1; s->mode = SESSION_ESP; s->id = 1; s->client = 0x0ac80005; /* 10.200.0.5 */
    s->spi_in = 0x12345678; s->spi_out = 0x9abcdef0; s->enc_len = enc_len; s->ssl_fd = -1;
    for (int i = 0; i < enc_len; i++) s->enc[i] = (unsigned char)(i * 7 + 1);
    for (int i = 0; i < 20; i++) s->auth[i] = (unsigned char)(i * 13 + 3);
    memcpy(s->enc_out, s->enc, sizeof(s->enc)); memcpy(s->auth_out, s->auth, sizeof(s->auth));
}

static int ipv4_packet(unsigned char *p, int payload) {
    int len = 20 + payload;
    memset(p, 0, (size_t)len);
    p[0] = 0x45; p[2] = (unsigned char)(len >> 8); p[3] = (unsigned char)len; p[8] = 64; p[9] = 17;
    *(uint32_t *)(p + 12) = htonl(0x0ac80005); *(uint32_t *)(p + 16) = htonl(0x0a010003);
    for (int i = 0; i < payload; i++) p[20 + i] = (unsigned char)i;
    return len;
}

int main(void) {
    struct session gw, client;
    unsigned char packet[1500], wire[1600], plain[1600];
    for (int enc_len = 16; enc_len <= 32; enc_len += 16) {
        /* the client encrypts with the gateway's inbound SPI and the gateway unwraps */
        fill(&client, enc_len); client.spi_out = 0x12345678; client.spi_in = 0x9abcdef0;
        fill(&gw, enc_len);
        for (int payload = 0; payload <= 1400; payload += 353) {
            int len = ipv4_packet(packet, payload);
            int wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire));
            CHECK(wire_len == ESP_HDR + ESP_IV + ((len + 2 + 15) / 16) * 16 + ESP_ICV, "wrapped length is header + iv + padded body + icv");
            int out = esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain));
            CHECK(out == len && !memcmp(plain, packet, (size_t)len), "unwrap restores the packet exactly");
        }
        int len = ipv4_packet(packet, 100), wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire));
        CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) == len, "fresh sequence accepted");
        CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) < 0 && gw.replays == 1, "the same datagram again is a replay");
        wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire)); wire[wire_len - 1] ^= 1;
        CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) < 0 && gw.auth_failures == 1, "a flipped ICV bit fails authentication");
        wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire)); wire[ESP_HDR + ESP_IV] ^= 1;
        CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) < 0 && gw.auth_failures == 2, "a flipped ciphertext bit fails authentication");
        /* window: 70 packets ahead, then one inside the window twice, one far behind */
        for (int i = 0; i < 70; i++) { wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire)); CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) == len, "in-order sequence accepted"); }
        unsigned char saved[1600]; int saved_len;
        saved_len = esp_wrap(&client, packet, len, saved, sizeof(saved));
        for (int i = 0; i < 10; i++) { wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire)); esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)); }
        CHECK(esp_unwrap(&gw, saved, saved_len, plain, sizeof(plain)) == len, "a packet 10 behind the window edge is accepted once");
        CHECK(esp_unwrap(&gw, saved, saved_len, plain, sizeof(plain)) < 0, "and rejected the second time");
        wire[0] = 0; CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) < 0, "a datagram with another SPI is refused");
        char label[64]; snprintf(label, sizeof(label), "aes-%d-cbc round trips complete", enc_len * 8); CHECK(1, label);
    }
    /* distinct keys per direction: the client encrypts with what the gateway decrypts with, and vice versa */
    fill(&client, 16); fill(&gw, 16); client.spi_out = gw.spi_in; client.spi_in = gw.spi_out;
    for (int i = 0; i < 32; i++) { client.enc_out[i] = gw.enc[i] = (unsigned char)(0xa0 + i); client.enc[i] = gw.enc_out[i] = (unsigned char)(0xc0 + i); }
    for (int i = 0; i < 20; i++) { client.auth_out[i] = gw.auth[i] = (unsigned char)(0xb0 + i); client.auth[i] = gw.auth_out[i] = (unsigned char)(0xd0 + i); }
    {
        int len = ipv4_packet(packet, 64), wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire));
        CHECK(esp_unwrap(&gw, wire, wire_len, plain, sizeof(plain)) == len && !memcmp(plain, packet, (size_t)len), "client -> gateway with the c2s keys");
        wire_len = esp_wrap(&gw, packet, len, wire, sizeof(wire));
        CHECK(esp_unwrap(&client, wire, wire_len, plain, sizeof(plain)) == len && !memcmp(plain, packet, (size_t)len), "gateway -> client with the s2c keys");
        struct session wrong = gw; memcpy(wrong.enc, gw.enc_out, 32); memcpy(wrong.auth, gw.auth_out, 20); wrong.replay = 0; wrong.seq_high = 0;
        wire_len = esp_wrap(&client, packet, len, wire, sizeof(wire));
        CHECK(esp_unwrap(&wrong, wire, wire_len, plain, sizeof(plain)) < 0 && wrong.auth_failures == 1, "the outbound keys do not authenticate inbound traffic");
    }
    /* control parsing */
    char value[128];
    const char *msg = "{\"op\":\"add\",\"id\":7,\"client\":\"10.200.0.5\",\"spi_in\":\"0x12345678\",\"enc\":\"00112233445566778899aabbccddeeff\"}";
    CHECK(json_string(msg, "op", value, sizeof(value)) && !strcmp(value, "add"), "op parsed");
    CHECK(json_string(msg, "id", value, sizeof(value)) && !strcmp(value, "7"), "numeric id parsed");
    CHECK(json_string(msg, "client", value, sizeof(value)) && !strcmp(value, "10.200.0.5"), "client parsed");
    CHECK(!json_string(msg, "missing", value, sizeof(value)), "a missing key is NULL");
    unsigned char key[32];
    CHECK(hex_decode("00112233445566778899aabbccddeeff", key, sizeof(key)) == 16 && key[0] == 0x00 && key[15] == 0xff, "hex key decoded");
    CHECK(hex_decode("0011223", key, sizeof(key)) < 0, "odd-length hex rejected");
    printf("%s\n", failures ? "FAILED" : "OK");
    return failures ? 1 : 0;
}
