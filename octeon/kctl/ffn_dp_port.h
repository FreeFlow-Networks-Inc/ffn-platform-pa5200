/* SPDX-License-Identifier: GPL-2.0-only */
/* Pure helpers of the shared DP trunk driver: the four-byte system header
 * both ways, the faceplate system-port table, queue and group selection, the
 * LAG load-balance key, the PKO command word, the punt contract checks on a
 * work-queue entry and the control-file grammar. No kernel calls, so
 * test_dp_port.c compiles and runs every function on x86
 * (octeon/kctl/Makefile `test-host`). Included by ffn_dp_dma.h.
 *
 * Numbers that are facts about the board or the switch program, not choices:
 * the DP is system port 24 (FACEPLATE-MAP.md: BCM port 24 = XLGE17, the one
 * populated 40G link); FE100 returns arrive from port 20 (Interlaken); the
 * receive envelope is [dest16][src16]; the transmit envelope is the Jericho
 * ITMH (0x01, dest16, 0x00) before the MACs and eight RAW_DSA bytes after
 * them, of which byte 0 (tagged flag) and bytes 4..7 must stay zero and
 * bytes 1..3 may carry the LAG load-balance key (octeon/native/ffn_packet.c,
 * ffn_aggregate_packet.inc). */
#ifndef FFN_DP_PORT_H
#define FFN_DP_PORT_H
#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/string.h>
#else
#include <stddef.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#endif

#define FFN_PORT_TRUNK 24           /* the DP's own system port */
#define FFN_PORT_RETURN_DEFAULT 20  /* FE100 returns: switch port 20, Interlaken */
#define FFN_FACEPLATE_PORTS 24
#define FFN_LAGS_MAX 8
#define FFN_RX_ENVELOPE 4
#define FFN_TX_ITMH 4
#define FFN_TX_DSA 8
#define FFN_TX_ENVELOPE 24          /* ITMH, both MACs, RAW_DSA: what precedes the ethertype */
#define FFN_ETH_HDR 14
#define FFN_ETH_MIN 60
#define FFN_SYSPORT_MAP 256         /* source system ports the receive map resolves directly */
#define FFN_KERNEL_GROUPS_MAX 8
#define FFN_SSO_GROUPS_TOTAL 256
#define FFN_TXQ_MAX 8
#define FFN_WQE_PKIND 8
#define FFN_WQE_TT_UNTAGGED 2
#define FFN_LAG_SYSPORT(tid) ((u16)(0x8000u | (tid)))

/* FACEPLATE-MAP.md: ethernet1/N -> BCM system port. 0 for an invalid N. */
static inline u16 ffn_faceplate_sysport(unsigned n)
{
	static const u16 table[FFN_FACEPLATE_PORTS] = {
		28, 13, 14, 15, 16, 1, 18, 19, 6, 21, 22, 23,
		7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35 };
	return n >= 1 && n <= FFN_FACEPLATE_PORTS ? table[n - 1] : 0;
}

static inline u16 ffn_be16(const u8 *p)
{
	return (u16)(((u16)p[0] << 8) | p[1]);
}

static inline void ffn_put_be16(u8 *p, u16 v)
{
	p[0] = (u8)(v >> 8);
	p[1] = (u8)v;
}

/* ---- receive ---- */

struct ffn_rx_header { u16 dest, src; };

/* The envelope of a frame as the PKI delivered it. 0 and h filled when the
 * frame is long enough and addressed to the DP; -1 otherwise. */
static inline int ffn_rx_header_parse(const u8 *frame, unsigned len, struct ffn_rx_header *h)
{
	if (len < FFN_RX_ENVELOPE + FFN_ETH_HDR)
		return -1;
	h->dest = ffn_be16(frame);
	h->src = ffn_be16(frame + 2);
	return h->dest == FFN_PORT_TRUNK ? 0 : -1;
}

/* An FE100 return message: its DMAC-low word is ITMH bytes 2-3 then CMH bytes
 * 0-1, always xx 00 10 00, which is what the DMACL parser CAM entry matches. */
static inline int ffn_rx_is_return(const u8 *frame, unsigned len)
{
	return len >= 10 && frame[7] == 0 && frame[8] == 0x10 && frame[9] == 0;
}

/* LACP or LLDP behind the envelope: a LAG member's control frame. */
static inline int ffn_rx_is_control(const u8 *frame, unsigned len)
{
	u16 type;
	if (len < FFN_RX_ENVELOPE + FFN_ETH_HDR)
		return 0;
	type = ffn_be16(frame + FFN_RX_ENVELOPE + 12);
	return type == 0x8809 || type == 0x88cc;
}

/* ---- transmit ---- */

/* Bytes on the trunk for an Ethernet frame of eth_len bytes: padded to 60
 * first, so the frame cannot become a runt once the switch strips the header. */
static inline unsigned ffn_tx_wire_len(unsigned eth_len)
{
	return (eth_len < FFN_ETH_MIN ? FFN_ETH_MIN : eth_len) + FFN_TX_ITMH + FFN_TX_DSA;
}

/* The 24 bytes that precede the ethertype on the trunk: ITMH to dest, the
 * frame's two MACs, RAW_DSA with the load-balance key replicated in bytes
 * 1..3 (zero for a plain port). */
static inline void ffn_tx_envelope(u8 *out, const u8 *macs, u16 dest, u8 key)
{
	out[0] = 1;
	ffn_put_be16(out + 1, dest);
	out[3] = 0;
	memcpy(out + 4, macs, 12);
	memset(out + 16, 0, 8);
	out[17] = out[18] = out[19] = key;
}

/* What the raw trunk admits from a user of ffnpkt0: an envelope the switch
 * reads as a TM command, never a bare Ethernet frame (ARP/IPv6 would be
 * executed as a command), the DSA tagged flag clear and PCP/VID bytes zero. */
static inline int ffn_tx_envelope_valid(const u8 *e, unsigned len)
{
	return len >= 26 && e[0] == 1 && e[3] == 0 && e[16] == 0 &&
	       e[20] == 0 && e[21] == 0 && e[22] == 0 && e[23] == 0;
}

/* The 8-bit LAG key the aggregate owner folds from its 32-bit flow hash. */
static inline u8 ffn_lag_key(u32 hash)
{
	return (u8)(hash ^ (hash >> 8) ^ (hash >> 16) ^ (hash >> 24));
}

/* Software member selection: the (hash mod n)-th set bit of the distributing
 * mask, or -1 when nothing distributes. */
static inline int ffn_lag_member(u32 hash, u32 mask)
{
	unsigned n = 0, i, pick;
	for (i = 0; i < 32; i++)
		n += (mask >> i) & 1;
	if (!n)
		return -1;
	pick = hash % n;
	for (i = 0; i < 32; i++) {
		if (!((mask >> i) & 1))
			continue;
		if (!pick)
			return (int)i;
		pick--;
	}
	return -1;
}

/* One descriptor queue per transmit queue; the sending CPU picks its own. */
static inline unsigned ffn_tx_queue_for(unsigned cpu, unsigned queues)
{
	return queues ? cpu % queues : 0;
}

static inline int ffn_pow2_in(unsigned n, unsigned max)
{
	return n >= 1 && n <= max && !(n & (n - 1));
}

static inline unsigned ffn_log2(unsigned n)
{
	unsigned l = 0;
	while (n > 1) {
		n >>= 1;
		l++;
	}
	return l;
}

/* Application groups: 2^log2n groups from base, outside the kernel's
 * 0..kernel_groups-1, aligned so the tag bits the classifier adds select
 * only groups of the set. */
static inline int ffn_app_groups_valid(unsigned base, unsigned log2n, unsigned kernel_groups)
{
	unsigned n = 1u << (log2n > 8 ? 8 : log2n);
	return log2n <= 8 && base >= kernel_groups && base + n <= FFN_SSO_GROUPS_TOTAL &&
	       !(base & (n - 1));
}

/* PKO_SEND_DMA_S, the LMTDMA/IOBDMA data word: scratch return address >> 3
 * at 63:56, return length 1 at 55:48, DID 0x51 at 47:40, node 0 at 39:36,
 * DQOP at 33:32 (0 send, 1 open, 2 close, 3 query), DQ at 25:16. Checked
 * against the SDK's union cvmx_pko_lmtdma_data (cvmx-pko3.h). */
static inline u64 ffn_pko_lmtdma_word(unsigned ret_scratch, unsigned op, unsigned dq)
{
	return ((u64)(ret_scratch >> 3) << 56) | (1ULL << 48) | (0x51ULL << 40) |
	       ((u64)(op & 3) << 32) | ((u64)(dq & 1023) << 16);
}

/* The PKO response word: a nonzero top nibble is an error; bits 49:48 carry
 * the DQ status, 1 after open and 2 after close, 0 for an accepted send. */
static inline int ffn_pko_ack_ok(u64 ack, unsigned expect_status)
{
	return !(ack >> 60) && ((ack >> 48) & 3) == expect_status;
}

/* ---- the work-queue entry: what the kernel reads, the punt contract ---- */

struct ffn_wqe_view {
	unsigned aura, bufs, pkind, len, grp, tt, errlev, opcode;
};

static inline void ffn_wqe_decode(u64 word0, u64 word1, u64 word2, struct ffn_wqe_view *v)
{
	v->aura = (unsigned)((word0 >> 48) & 4095);
	v->bufs = (unsigned)((word0 >> 24) & 255);
	v->pkind = (unsigned)(word0 & 63);
	v->len = (unsigned)(word1 >> 48);
	v->grp = (unsigned)((word1 >> 34) & 1023);
	v->tt = (unsigned)((word1 >> 32) & 3);
	v->errlev = (unsigned)((word2 >> 8) & 7);
	v->opcode = (unsigned)(word2 & 255);
}

/* The entry names the kernel packet aura, the trunk's port kind and a chain
 * of 1..max buffers; anything else is corrupt or a punt outside the contract. */
static inline int ffn_wqe_admissible(const struct ffn_wqe_view *v, unsigned aura, unsigned max_bufs)
{
	return v->aura == aura && v->pkind == FFN_WQE_PKIND && v->bufs >= 1 && v->bufs <= max_bufs;
}

/* ---- the control file ---- */

enum ffn_ctl_op {
	FFN_CTL_NONE,
	FFN_CTL_STEER_KERNEL,  /* steer kernel */
	FFN_CTL_STEER_APP,     /* steer app <base> <log2n> */
	FFN_CTL_DELIVER,       /* deliver trunk|ports|both           a = enum ffn_deliver */
	FFN_CTL_RETURN,        /* return <sysport>                   a */
	FFN_CTL_PORT_SYSPORT,  /* port <n> sysport <s>               a=n b=s */
	FFN_CTL_PORT_CARRIER,  /* port <n> carrier up|down           a=n flag */
	FFN_CTL_PORT_MAC,      /* port <n> mac xx:xx:xx:xx:xx:xx     a=n mac */
	FFN_CTL_LAG_ADD,       /* lag <id> add <tid>                 a=id b=tid */
	FFN_CTL_LAG_DEL,       /* lag <id> del                       a=id */
	FFN_CTL_LAG_MEMBER,    /* lag <id> member <n> add|del        a=id b=n flag */
	FFN_CTL_LAG_COLLECT,   /* lag <id> collect <n> on|off        a=id b=n flag */
	FFN_CTL_LAG_DISTRIBUTE,/* lag <id> distribute <n> on|off     a=id b=n flag */
	FFN_CTL_LAG_HARDWARE,  /* lag <id> hardware on|off           a=id flag */
	FFN_CTL_LAG_CARRIER,   /* lag <id> carrier up|down           a=id flag */
	FFN_CTL_LAG_MAC,       /* lag <id> mac xx:..                 a=id mac */
};

enum ffn_deliver { FFN_DELIVER_TRUNK, FFN_DELIVER_PORTS, FFN_DELIVER_BOTH };

struct ffn_ctl {
	enum ffn_ctl_op op;
	unsigned a, b;
	int flag;
	u8 mac[6];
};

#define FFN_CTL_TOKENS 6
#define FFN_CTL_TOKEN_MAX 24

static inline int ffn_ctl_space(char c)
{
	return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

/* Split a line into at most FFN_CTL_TOKENS tokens of at most 23 characters. */
static inline int ffn_ctl_tokens(const char *line, char tokens[FFN_CTL_TOKENS][FFN_CTL_TOKEN_MAX])
{
	int count = 0;
	while (*line) {
		unsigned n = 0;
		while (ffn_ctl_space(*line))
			line++;
		if (!*line)
			break;
		if (count == FFN_CTL_TOKENS)
			return -1;
		while (*line && !ffn_ctl_space(*line)) {
			if (n + 1 >= FFN_CTL_TOKEN_MAX)
				return -1;
			tokens[count][n++] = *line++;
		}
		tokens[count][n] = 0;
		count++;
	}
	return count;
}

static inline int ffn_ctl_uint(const char *s, unsigned max, unsigned *out)
{
	unsigned v = 0;
	if (!*s)
		return -1;
	while (*s) {
		if (*s < '0' || *s > '9' || v > max / 10)
			return -1;
		v = v * 10 + (unsigned)(*s - '0');
		if (v > max)
			return -1;
		s++;
	}
	*out = v;
	return 0;
}

static inline int ffn_ctl_hex(char c)
{
	if (c >= '0' && c <= '9')
		return c - '0';
	if (c >= 'a' && c <= 'f')
		return c - 'a' + 10;
	if (c >= 'A' && c <= 'F')
		return c - 'A' + 10;
	return -1;
}

static inline int ffn_ctl_mac(const char *s, u8 *mac)
{
	unsigned i;
	for (i = 0; i < 6; i++) {
		int hi = ffn_ctl_hex(s[i * 3]), lo = ffn_ctl_hex(s[i * 3 + 1]);
		if (hi < 0 || lo < 0 || (i < 5 ? s[i * 3 + 2] != ':' : s[i * 3 + 2] != 0))
			return -1;
		mac[i] = (u8)((hi << 4) | lo);
	}
	return 0;
}

static inline int ffn_ctl_switch(const char *s, const char *yes, const char *no, int *flag)
{
	if (!strcmp(s, yes))
		*flag = 1;
	else if (!strcmp(s, no))
		*flag = 0;
	else
		return -1;
	return 0;
}

/* Parse one command. 0 and c filled, -1 for anything the grammar above does
 * not say. Ranges: port 1..24, lag 1..8, tid 1..12, system port 0..65535,
 * group base 0..255, log2n 0..8. */
static inline int ffn_ctl_parse(const char *line, struct ffn_ctl *c)
{
	char t[FFN_CTL_TOKENS][FFN_CTL_TOKEN_MAX];
	int n = ffn_ctl_tokens(line, t);
	memset(c, 0, sizeof(*c));
	if (n < 1)
		return -1;
	if (!strcmp(t[0], "steer")) {
		if (n == 2 && !strcmp(t[1], "kernel")) {
			c->op = FFN_CTL_STEER_KERNEL;
			return 0;
		}
		if (n == 4 && !strcmp(t[1], "app") && !ffn_ctl_uint(t[2], 255, &c->a) &&
		    !ffn_ctl_uint(t[3], 8, &c->b)) {
			c->op = FFN_CTL_STEER_APP;
			return 0;
		}
		return -1;
	}
	if (!strcmp(t[0], "deliver") && n == 2) {
		if (!strcmp(t[1], "trunk"))
			c->a = FFN_DELIVER_TRUNK;
		else if (!strcmp(t[1], "ports"))
			c->a = FFN_DELIVER_PORTS;
		else if (!strcmp(t[1], "both"))
			c->a = FFN_DELIVER_BOTH;
		else
			return -1;
		c->op = FFN_CTL_DELIVER;
		return 0;
	}
	if (!strcmp(t[0], "return") && n == 2 && !ffn_ctl_uint(t[1], 65535, &c->a)) {
		c->op = FFN_CTL_RETURN;
		return 0;
	}
	if (!strcmp(t[0], "port") && n >= 4 && !ffn_ctl_uint(t[1], FFN_FACEPLATE_PORTS, &c->a) && c->a) {
		if (n == 4 && !strcmp(t[2], "sysport") && !ffn_ctl_uint(t[3], 65535, &c->b)) {
			c->op = FFN_CTL_PORT_SYSPORT;
			return 0;
		}
		if (n == 4 && !strcmp(t[2], "carrier") && !ffn_ctl_switch(t[3], "up", "down", &c->flag)) {
			c->op = FFN_CTL_PORT_CARRIER;
			return 0;
		}
		if (n == 4 && !strcmp(t[2], "mac") && !ffn_ctl_mac(t[3], c->mac)) {
			c->op = FFN_CTL_PORT_MAC;
			return 0;
		}
		return -1;
	}
	if (!strcmp(t[0], "lag") && n >= 3 && !ffn_ctl_uint(t[1], FFN_LAGS_MAX, &c->a) && c->a) {
		if (n == 4 && !strcmp(t[2], "add") && !ffn_ctl_uint(t[3], 12, &c->b) && c->b) {
			c->op = FFN_CTL_LAG_ADD;
			return 0;
		}
		if (n == 3 && !strcmp(t[2], "del")) {
			c->op = FFN_CTL_LAG_DEL;
			return 0;
		}
		if (n == 5 && !strcmp(t[2], "member") && !ffn_ctl_uint(t[3], FFN_FACEPLATE_PORTS, &c->b) &&
		    c->b && !ffn_ctl_switch(t[4], "add", "del", &c->flag)) {
			c->op = FFN_CTL_LAG_MEMBER;
			return 0;
		}
		if (n == 5 && (!strcmp(t[2], "collect") || !strcmp(t[2], "distribute")) &&
		    !ffn_ctl_uint(t[3], FFN_FACEPLATE_PORTS, &c->b) && c->b &&
		    !ffn_ctl_switch(t[4], "on", "off", &c->flag)) {
			c->op = t[2][0] == 'c' ? FFN_CTL_LAG_COLLECT : FFN_CTL_LAG_DISTRIBUTE;
			return 0;
		}
		if (n == 4 && !strcmp(t[2], "hardware") && !ffn_ctl_switch(t[3], "on", "off", &c->flag)) {
			c->op = FFN_CTL_LAG_HARDWARE;
			return 0;
		}
		if (n == 4 && !strcmp(t[2], "carrier") && !ffn_ctl_switch(t[3], "up", "down", &c->flag)) {
			c->op = FFN_CTL_LAG_CARRIER;
			return 0;
		}
		if (n == 4 && !strcmp(t[2], "mac") && !ffn_ctl_mac(t[3], c->mac)) {
			c->op = FFN_CTL_LAG_MAC;
			return 0;
		}
		return -1;
	}
	return -1;
}

#endif /* FFN_DP_PORT_H */
