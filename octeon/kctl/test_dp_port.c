// SPDX-License-Identifier: GPL-2.0-only
/* Host-side unit test of the trunk driver's pure helpers (ffn_dp_port.h):
 * header parse and build, group and queue selection, the PKO command word,
 * the punt contract view of a work-queue entry and the control grammar.
 * Builds and runs on x86 with the host compiler: `make test-host` here. */
#include "ffn_dp_port.h"
#include <stdio.h>
#include <stdlib.h>

static unsigned failures, checks;

#define CHECK(cond) do { \
	checks++; \
	if (!(cond)) { \
		failures++; \
		fprintf(stderr, "%s:%d: FAIL %s\n", __FILE__, __LINE__, #cond); \
	} \
} while (0)

/* The envelope exactly as octeon/native/ffn_packet.c transmit_burst builds it. */
static void reference_envelope(u8 *packet, const u8 *frame, unsigned source, u8 key)
{
	packet[0] = 1; packet[1] = (u8)(source >> 8); packet[2] = (u8)source; packet[3] = 0;
	memcpy(packet + 4, frame, 12); memset(packet + 16, 0, 8);
	if (key) memset(packet + 17, key, 3);
}

static void test_receive(void)
{
	u8 frame[64] = {0, 24, 0, 16, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0x08, 0x00};
	struct ffn_rx_header h;
	CHECK(ffn_rx_header_parse(frame, sizeof(frame), &h) == 0);
	CHECK(h.dest == 24 && h.src == 16);
	CHECK(ffn_rx_header_parse(frame, 17, &h) == -1);          /* shorter than envelope + Ethernet header */
	frame[1] = 25;
	CHECK(ffn_rx_header_parse(frame, sizeof(frame), &h) == -1); /* not addressed to the DP */
	frame[1] = 24;
	CHECK(!ffn_rx_is_control(frame, sizeof(frame)));
	frame[16] = 0x88; frame[17] = 0x09;
	CHECK(ffn_rx_is_control(frame, sizeof(frame)));
	frame[17] = 0xcc;
	CHECK(ffn_rx_is_control(frame, sizeof(frame)));
	CHECK(!ffn_rx_is_control(frame, 17));
	CHECK(!ffn_rx_is_return(frame, sizeof(frame)));
	/* a return: ITMH bytes 2-3 then CMH 0-1 at frame bytes 6..9 = xx 00 10 00 */
	frame[6] = 0x5a; frame[7] = 0; frame[8] = 0x10; frame[9] = 0;
	CHECK(ffn_rx_is_return(frame, sizeof(frame)));
	CHECK(!ffn_rx_is_return(frame, 9));
}

static void test_transmit(void)
{
	u8 frame[80], reference[92], envelope[24];
	unsigned i;
	for (i = 0; i < sizeof(frame); i++)
		frame[i] = (u8)(i * 7 + 3);
	reference_envelope(reference, frame, 0x8003, 0x5c);
	ffn_tx_envelope(envelope, frame, 0x8003, 0x5c);
	CHECK(!memcmp(envelope, reference, 24));
	reference_envelope(reference, frame, 28, 0);
	ffn_tx_envelope(envelope, frame, 28, 0);
	CHECK(!memcmp(envelope, reference, 24));
	CHECK(envelope[0] == 1 && envelope[1] == 0 && envelope[2] == 28 && envelope[3] == 0);
	CHECK(ffn_tx_envelope_valid(envelope, 72));
	CHECK(!ffn_tx_envelope_valid(envelope, 25));
	envelope[16] = 0x20;                 /* DSA tagged flag: the switch would rebuild a VLAN tag */
	CHECK(!ffn_tx_envelope_valid(envelope, 72));
	envelope[16] = 0; envelope[21] = 1;  /* PCP/VID bytes */
	CHECK(!ffn_tx_envelope_valid(envelope, 72));
	envelope[21] = 0; envelope[0] = 0;   /* not a TM command */
	CHECK(!ffn_tx_envelope_valid(envelope, 72));
	CHECK(ffn_tx_wire_len(14) == 72);
	CHECK(ffn_tx_wire_len(60) == 72);
	CHECK(ffn_tx_wire_len(61) == 73);
	CHECK(ffn_tx_wire_len(1514) == 1526);
}

static void test_lag(void)
{
	u32 crc = 0x12345678u;
	unsigned counts[32] = {0}, i;
	CHECK(ffn_lag_key(crc) == (u8)(crc ^ (crc >> 8) ^ (crc >> 16) ^ (crc >> 24)));
	CHECK(ffn_lag_key(0) == 0);
	CHECK(ffn_lag_key(0x01010101u) == 0);
	CHECK(ffn_lag_member(5, 0) == -1);
	CHECK(ffn_lag_member(5, 1u << 20) == 20);       /* one member takes everything */
	CHECK(ffn_lag_member(99, 1u << 20) == 20);
	CHECK(ffn_lag_member(0, (1u << 20) | (1u << 21)) == 20);
	CHECK(ffn_lag_member(1, (1u << 20) | (1u << 21)) == 21);
	CHECK(ffn_lag_member(2, (1u << 20) | (1u << 21)) == 20);
	for (i = 0; i < 1000; i++) {
		int m = ffn_lag_member(i * 2654435761u, (1u << 3) | (1u << 20) | (1u << 21));
		CHECK(m == 3 || m == 20 || m == 21);
		if (m >= 0) counts[m]++;
	}
	CHECK(counts[3] > 200 && counts[20] > 200 && counts[21] > 200);
	CHECK(ffn_lag_member(7, 0xffffffffu) == 7 && ffn_lag_member(39, 0xffffffffu) == 7);
	CHECK(FFN_LAG_SYSPORT(1) == 0x8001 && FFN_LAG_SYSPORT(12) == 0x800c);
}

static void test_selection(void)
{
	unsigned cpu;
	CHECK(ffn_tx_queue_for(0, 8) == 0 && ffn_tx_queue_for(9, 8) == 1 && ffn_tx_queue_for(39, 8) == 7);
	CHECK(ffn_tx_queue_for(5, 1) == 0 && ffn_tx_queue_for(5, 0) == 0);
	for (cpu = 0; cpu < 40; cpu++)
		CHECK(ffn_tx_queue_for(cpu, 8) < 8);
	CHECK(ffn_pow2_in(1, 8) && ffn_pow2_in(8, 8) && !ffn_pow2_in(0, 8) && !ffn_pow2_in(3, 8) && !ffn_pow2_in(16, 8));
	CHECK(ffn_log2(1) == 0 && ffn_log2(2) == 1 && ffn_log2(8) == 3 && ffn_log2(256) == 8);
	/* the application's groups never overlap the kernel's and are aligned to their size */
	CHECK(ffn_app_groups_valid(8, 3, 8));
	CHECK(ffn_app_groups_valid(64, 5, 8));
	CHECK(ffn_app_groups_valid(16, 0, 8));
	CHECK(ffn_app_groups_valid(128, 7, 8));
	CHECK(!ffn_app_groups_valid(4, 2, 8));     /* inside the kernel's groups */
	CHECK(!ffn_app_groups_valid(12, 3, 8));    /* not aligned to 8 */
	CHECK(!ffn_app_groups_valid(192, 7, 8));   /* runs past group 255 */
	CHECK(!ffn_app_groups_valid(0, 9, 8));
	CHECK(!ffn_app_groups_valid(8, 3, 16));    /* kernel groups grown over it */
	CHECK(ffn_app_groups_valid(1, 0, 1));
}

static void test_pko(void)
{
	/* The historical single-queue command word: return at scratch 128+3*8 for
	 * a three-word send on DQ 0, then DQ 7 adds bits 25:16, open/close ops. */
	u64 send0 = ffn_pko_lmtdma_word(128 + 3 * 8, 0, 0);
	CHECK(send0 == (((u64)((128 + 24) >> 3) << 56) | (1ULL << 48) | (0x51ULL << 40)));
	CHECK(ffn_pko_lmtdma_word(128 + 3 * 8, 0, 7) == (send0 | (7ULL << 16)));
	CHECK(ffn_pko_lmtdma_word(128, 1, 3) == (((u64)(128 >> 3) << 56) | (1ULL << 48) | (0x51ULL << 40) | (1ULL << 32) | (3ULL << 16)));
	CHECK(((ffn_pko_lmtdma_word(128, 2, 1023) >> 16) & 1023) == 1023);
	CHECK(((ffn_pko_lmtdma_word(128, 2, 1024) >> 16) & 1023) == 0);
	CHECK(((ffn_pko_lmtdma_word(128, 2, 0) >> 32) & 3) == 2);
	CHECK(ffn_pko_ack_ok(0, 0));
	CHECK(ffn_pko_ack_ok(1ULL << 48, 1));
	CHECK(!ffn_pko_ack_ok(1ULL << 48, 0));
	CHECK(ffn_pko_ack_ok(2ULL << 48, 2));
	CHECK(!ffn_pko_ack_ok(1ULL << 60, 0));
	CHECK(!ffn_pko_ack_ok((1ULL << 63) | (1ULL << 48), 1));
}

static void test_wqe(void)
{
	struct ffn_wqe_view v;
	u64 word0 = ((u64)1021 << 48) | (1ULL << 24) | 8;
	u64 word1 = ((u64)98 << 48) | (5ULL << 34) | (2ULL << 32) | 0xabcdef;
	ffn_wqe_decode(word0, word1, 0, &v);
	CHECK(v.aura == 1021 && v.bufs == 1 && v.pkind == 8 && v.len == 98 && v.grp == 5 && v.tt == 2);
	CHECK(v.errlev == 0 && v.opcode == 0);
	CHECK(ffn_wqe_admissible(&v, 1021, 64));
	ffn_wqe_decode(word0, word1, (3ULL << 8) | 0x1f, &v);
	CHECK(v.errlev == 3 && v.opcode == 0x1f);
	ffn_wqe_decode(((u64)1022 << 48) | (1ULL << 24) | 8, word1, 0, &v);
	CHECK(!ffn_wqe_admissible(&v, 1021, 64));     /* another aura */
	ffn_wqe_decode(((u64)1021 << 48) | (1ULL << 24) | 9, word1, 0, &v);
	CHECK(!ffn_wqe_admissible(&v, 1021, 64));     /* another port kind */
	ffn_wqe_decode(((u64)1021 << 48) | 8, word1, 0, &v);
	CHECK(!ffn_wqe_admissible(&v, 1021, 64));     /* no buffers */
	ffn_wqe_decode(((u64)1021 << 48) | (65ULL << 24) | 8, word1, 0, &v);
	CHECK(!ffn_wqe_admissible(&v, 1021, 64));     /* too many */
	ffn_wqe_decode(word0, ((u64)1500 << 48) | (1023ULL << 34) | (1ULL << 32), 0, &v);
	CHECK(v.len == 1500 && v.grp == 1023 && v.tt == 1);
}

static void test_faceplate(void)
{
	unsigned i, j;
	CHECK(ffn_faceplate_sysport(1) == 28 && ffn_faceplate_sysport(2) == 13);
	CHECK(ffn_faceplate_sysport(5) == 16 && ffn_faceplate_sysport(6) == 1);
	CHECK(ffn_faceplate_sysport(21) == 32 && ffn_faceplate_sysport(24) == 35);
	CHECK(ffn_faceplate_sysport(0) == 0 && ffn_faceplate_sysport(25) == 0);
	for (i = 1; i <= 24; i++) {
		CHECK(ffn_faceplate_sysport(i) != FFN_PORT_TRUNK && ffn_faceplate_sysport(i) != FFN_PORT_RETURN_DEFAULT);
		CHECK(ffn_faceplate_sysport(i) < FFN_SYSPORT_MAP);
		for (j = 1; j < i; j++)
			CHECK(ffn_faceplate_sysport(i) != ffn_faceplate_sysport(j));
	}
}

static void test_control(void)
{
	struct ffn_ctl c;
	CHECK(!ffn_ctl_parse("steer kernel\n", &c) && c.op == FFN_CTL_STEER_KERNEL);
	CHECK(!ffn_ctl_parse("steer app 64 5", &c) && c.op == FFN_CTL_STEER_APP && c.a == 64 && c.b == 5);
	CHECK(ffn_ctl_parse("steer app 64 9", &c) == -1);
	CHECK(ffn_ctl_parse("steer app 256 3", &c) == -1);
	CHECK(ffn_ctl_parse("steer", &c) == -1);
	CHECK(!ffn_ctl_parse("deliver both", &c) && c.op == FFN_CTL_DELIVER && c.a == FFN_DELIVER_BOTH);
	CHECK(!ffn_ctl_parse("  deliver   ports  ", &c) && c.a == FFN_DELIVER_PORTS);
	CHECK(!ffn_ctl_parse("deliver trunk", &c) && c.a == FFN_DELIVER_TRUNK);
	CHECK(ffn_ctl_parse("deliver all", &c) == -1);
	CHECK(!ffn_ctl_parse("return 20", &c) && c.op == FFN_CTL_RETURN && c.a == 20);
	CHECK(ffn_ctl_parse("return 65536", &c) == -1);
	CHECK(!ffn_ctl_parse("port 1 sysport 28", &c) && c.op == FFN_CTL_PORT_SYSPORT && c.a == 1 && c.b == 28);
	CHECK(ffn_ctl_parse("port 0 sysport 28", &c) == -1);
	CHECK(ffn_ctl_parse("port 25 sysport 28", &c) == -1);
	CHECK(!ffn_ctl_parse("port 5 carrier up", &c) && c.op == FFN_CTL_PORT_CARRIER && c.a == 5 && c.flag == 1);
	CHECK(!ffn_ctl_parse("port 5 carrier down", &c) && c.flag == 0);
	CHECK(ffn_ctl_parse("port 5 carrier sideways", &c) == -1);
	CHECK(!ffn_ctl_parse("port 24 mac 02:8b:77:FB:05:f9", &c) && c.op == FFN_CTL_PORT_MAC && c.a == 24);
	CHECK(c.mac[0] == 0x02 && c.mac[1] == 0x8b && c.mac[3] == 0xfb && c.mac[5] == 0xf9);
	CHECK(ffn_ctl_parse("port 24 mac 02:8b:77:fb:05", &c) == -1);
	CHECK(ffn_ctl_parse("port 24 mac 02:8b:77:fb:05:f9:00", &c) == -1);
	CHECK(ffn_ctl_parse("port 24 mac 0g:8b:77:fb:05:f9", &c) == -1);
	CHECK(!ffn_ctl_parse("lag 1 add 3", &c) && c.op == FFN_CTL_LAG_ADD && c.a == 1 && c.b == 3);
	CHECK(ffn_ctl_parse("lag 1 add 13", &c) == -1);
	CHECK(ffn_ctl_parse("lag 9 add 1", &c) == -1);
	CHECK(!ffn_ctl_parse("lag 1 del", &c) && c.op == FFN_CTL_LAG_DEL && c.a == 1);
	CHECK(!ffn_ctl_parse("lag 1 member 21 add", &c) && c.op == FFN_CTL_LAG_MEMBER && c.b == 21 && c.flag == 1);
	CHECK(!ffn_ctl_parse("lag 1 member 22 del", &c) && c.flag == 0);
	CHECK(!ffn_ctl_parse("lag 1 collect 21 on", &c) && c.op == FFN_CTL_LAG_COLLECT && c.b == 21 && c.flag);
	CHECK(!ffn_ctl_parse("lag 1 distribute 21 off", &c) && c.op == FFN_CTL_LAG_DISTRIBUTE && !c.flag);
	CHECK(!ffn_ctl_parse("lag 1 hardware on", &c) && c.op == FFN_CTL_LAG_HARDWARE && c.flag);
	CHECK(!ffn_ctl_parse("lag 2 carrier down", &c) && c.op == FFN_CTL_LAG_CARRIER && c.a == 2 && !c.flag);
	CHECK(!ffn_ctl_parse("lag 2 mac 00:11:22:33:44:55", &c) && c.op == FFN_CTL_LAG_MAC && c.mac[5] == 0x55);
	CHECK(ffn_ctl_parse("lag 1 member 21", &c) == -1);
	CHECK(ffn_ctl_parse("lag 1 member 21 add extra", &c) == -1);
	CHECK(ffn_ctl_parse("", &c) == -1);
	CHECK(ffn_ctl_parse("   \n", &c) == -1);
	CHECK(ffn_ctl_parse("prepare-trunk", &c) == -1);
	CHECK(ffn_ctl_parse("port 1 sysport 28 a b c d", &c) == -1);                 /* too many tokens */
	CHECK(ffn_ctl_parse("port 1 sysport 0123456789012345678901234", &c) == -1); /* token too long */
}

int main(void)
{
	test_receive();
	test_transmit();
	test_lag();
	test_selection();
	test_pko();
	test_wqe();
	test_faceplate();
	test_control();
	if (failures) {
		fprintf(stderr, "test_dp_port: %u of %u checks FAILED\n", failures, checks);
		return 1;
	}
	printf("test_dp_port: %u checks passed\n", checks);
	return 0;
}
