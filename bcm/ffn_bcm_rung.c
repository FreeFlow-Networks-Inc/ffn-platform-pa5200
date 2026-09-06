/*
 * ffn_bcm_rung.c -- the shortest demonstration that packets cross this board.
 *
 *   MP -> BCM port 8 -> VOQ -> E2E scheduler -> BCM port 24 -> 40G -> DP BGX
 *
 * Run as:  cint /usr/share/broadcom/ffn_bcm_rung.c   from the bcm.user shell,
 * or through ffn_bcmd's `cint.run` op, which is what stages it.
 *
 * WHY THIS PAIR OF PORTS. It needs no traffic generator and no cooperating
 * software at either end. The management plane already emits its own link
 * chatter continuously -- BCM port 8 had counted 3427 frames before anything
 * was configured -- and the dataplane end is instrumented already, because
 * ffn_bgx_bringup.c reads BGX(2)_CMR(0)_RX_STAT0/2/6/8 straight from the CSRs.
 *
 * Critically, the far end does NOT have to work for this to prove something.
 * The dataplane has no PKI and no buffer pool bound to that link, so an
 * arriving frame has nowhere to go and is DROPPED -- and RX_STAT6 counts the
 * drops. A rising drop count next to a zero receive count is proof the link
 * carried traffic, which is otherwise unprovable until the whole receive path
 * exists.
 *
 * This is ffn_bcm_voq.c with the source port changed from 5 to 8 and the
 * faceplate loop removed. That file's own header records the measurement it
 * came from: CP eth0 -> port 5 -> VOQ 4 -> port 24 -> 40G -> DP, BCM port 24
 * TX 300, DP rx_packets 300, zero errors. Everything structural is unchanged,
 * because the point of a first rung is to vary one thing.
 *
 * THE PREREQUISITE THAT IS NOT IN THIS FILE. Port 8 must be
 * tm_port_header_type_in=RAW. It ships as TM, and on a TM port the chip reads
 * the frame's first four bytes as a Traffic Manager header -- so the management
 * plane's ordinary Ethernet has its destination MAC parsed as a forwarding
 * destination, and every frame fails enqueue with "queue not valid". That is
 * the same wall ffn_bcm_voq.c hit on port 5. It is set by
 * octeon/bcmagent/ffn-bcm-overrides.conf, not here, because it is a chip
 * property read at init and cint runs long afterwards.
 *
 * Do not name a variable "unit"; cint predefines it.
 *
 * ---------------------------------------------------------------------------
 * IT FORWARDS ONE BURST PER ATTACH, NOT CONTINUOUSLY. Measured 2026-09-06, and
 * it revises what every recipe in this directory claims. ffn_bcm_voq.c carries
 * the correction too, as do ffn_bcm_chain.c and ffn_bcm_fpchain.c.
 *
 *   200 broadcast frames from the MP into port 8:
 *     port 8  RX +200
 *     port 24 TX +26      then FROZEN for minutes while port 8 kept counting
 *     error counters      none, anywhere
 *
 * THE FRAMES WERE QUEUED, NOT DROPPED. Re-running this recipe moved port 24 TX
 * +205 while port 8 took only +2, so a fresh attach released the backlog. A
 * second 200-frame burst then gave port 8 +200 and port 24 +0.
 *
 * SO ALWAYS SEND A SECOND BURST. One burst that drains proves the attach
 * happened; it cannot tell a one-shot grant from a running credit loop. The
 * original 300-frame measurement in ffn_bcm_voq.c fit inside a single grant,
 * and no second burst was ever sent -- which is how this went unnoticed.
 *
 * THREE EXPLANATIONS RULED OUT, so nobody spends the time again:
 *
 *   1. NOT link-level pause, though it looks exactly like it. Port 24 is the
 *      ONLY linked port with pause RX enabled and TX disabled -- `ps` shows
 *      `TX RX` for ports 2, 8, 9, 34, 35 and a bare `RX` for 24 -- which reads
 *      just like the dataplane asserting flow control and never releasing it.
 *      It is not: pause would not have let the +205 flush through.
 *
 *   2. NOT the parent gport. Attaching to the per-TC HR scheduling element
 *      instead -- E2E PORT TC gport, subtype 13, resolved with
 *      bcm_cosq_gport_handle_get(bcmCosqGportTypeSched) -- forwards NOTHING on
 *      either burst. The subtype-5 E2E PORT gport used below is correct, and
 *      the SDK's own TM FAP setup uses that same encoding.
 *
 *      Two traps found proving that: handle_get answers only for TC 0 and TC 1
 *      on this port (BCM_E_PARAM for 2..7) and it DOES NOT WRITE out_gport on
 *      failure -- so a loop attaching unconditionally re-parents six queues
 *      onto TC 1's element, and attach returns 0 for every one of them.
 *
 *   3. NOT a shaper rate. See FFN_RATE_RULED_OUT below for the read-back:
 *      connectors come up UNLIMITED, not zero.
 *
 * STILL UNEXPLAINED, and this is where to pick it up. The remaining suspects
 * are the credit-return path itself rather than anything shaped: the VOQ's own
 * credit state (only the CONNECTOR's rate was read back, never the VOQ's), and
 * bcmCosqControlBandwidthBurstMax, which is a separate control from rate.
 *
 * TESTING THIS NEEDS A CLEAN CHIP. These recipes are NOT idempotent -- the
 * daemon's cint session keeps its variables between cint.run calls, so each run
 * prints "identifier redeclared" for every declaration AND allocates a fresh
 * VOQ and connector, leaking the previous pair. The measurements above were
 * taken across several runs, so the chip carried leaked pairs and the six stale
 * TC-1 attaches from suspect 2. Restart ffn-bcmd first (see
 * octeon/bcmagent/ffn-bcmd-ctl.sh; it re-initialises the chip, and the
 * front-panel links must be brought back up afterwards), then run this ONCE.
 */

int u = 0;

bcm_port_t src = 8;    /* MP link 0 -- up, and already receiving */
bcm_port_t dst = 24;   /* DP 40G link -- up, brought up by FFN's own BGX code */

bcm_port_enable_set(u, src, 1);
bcm_port_enable_set(u, dst, 1);

print "FFN_BASE";
bcm_gport_t dst_gport = 0;
bcm_port_gport_get(u, dst, &dst_gport);
bcm_gport_t dst_sysport = 0;
bcm_stk_gport_sysport_get(u, dst_gport, &dst_sysport);
bcm_module_t modid = 0;
bcm_stk_modid_get(u, &modid);

/*
 * The E2E port gport is COMPUTED, not returned by any API:
 *   COSQ type 30 << 26 | E2E_PORT subtype 5 << 21 | port
 * Confirmed by the SDK's own allocation -- the connector came back as
 * 0x78200010, the same family with subtype 1.
 */
int e2e_port;
e2e_port = 0x78000000 | (5 << 21) | dst;
print e2e_port;

print "FFN_CONNECTOR_ON_E2E";
/*
 * Must be bcm_cosq_voq_connector_gport_add. The legacy
 * bcm_cosq_gport_add(BCM_COSQ_GPORT_VOQ_CONNECTOR) refuses with BCM_E_CONFIG
 * "Can't determine core ID" whenever WITH_ID is absent and the device has more
 * than one core, and this chip has two.
 */
bcm_cosq_voq_connector_gport_t cfg;
cfg.flags = 0;
cfg.port = e2e_port;
cfg.numq = 8;
cfg.remote_modid = modid;
cfg.nof_remote_cores = 2;
bcm_gport_t connector = 0;
int rv_conn;
rv_conn = bcm_cosq_voq_connector_gport_add(u, &cfg, &connector);
print rv_conn;
print connector;

print "FFN_VOQ";
bcm_gport_t voq = 0;
int rv_voq;
rv_voq = bcm_cosq_gport_add(u, dst_sysport, 8, BCM_COSQ_GPORT_UCAST_QUEUE_GROUP, &voq);
print rv_voq;
print voq;

print "FFN_CONNECT";
bcm_cosq_gport_connection_t conn;
conn.flags = BCM_COSQ_GPORT_CONNECTION_INGRESS;
conn.remote_modid = modid;
conn.voq = voq;
conn.voq_connector = connector;
int rv_ing;
rv_ing = bcm_cosq_gport_connection_set(u, &conn);
print rv_ing;

conn.flags = BCM_COSQ_GPORT_CONNECTION_EGRESS;
conn.remote_modid = modid;
conn.voq = voq;
conn.voq_connector = connector;
int rv_egr;
rv_egr = bcm_cosq_gport_connection_set(u, &conn);
print rv_egr;

print "FFN_ATTACH";
/*
 * THE CALL THAT MAKES TRAFFIC MOVE -- ONCE. Necessary, not sufficient; see the
 * one-shot finding in the header.
 *
 * Creating the connector and binding it to the VOQ is not enough: the
 * connector is a scheduling node and needs a PARENT in the E2E hierarchy.
 * Unparented it is never scheduled at all. Measured on this silicon: before
 * this call, enqueue 300 and dequeue 0; after it, dequeue 300 and port 24
 * TX 300.
 *
 * That 300-frame measurement is exactly the trap. It is real, and it means
 * only that ONE burst drained -- every subsequent burst forwards nothing until
 * this call is made again. Do not read it as proof of a working credit loop.
 *
 * bcm_cosq_gport_attach_get returns BCM_E_UNAVAIL here, so the attachment
 * cannot be read back -- verify with a SECOND burst of traffic, never with a
 * getter and never with one burst.
 */
int rv_attach;
rv_attach = bcm_cosq_gport_attach(u, e2e_port, connector, 0);
print rv_attach;

print "FFN_RATE_RULED_OUT";
/*
 * NO RATE CALLS HERE, DELIBERATELY. A shaper with rate 0 would explain the
 * one-shot behaviour in the header exactly -- it passes a burst allowance and never
 * replenishes, and a fresh attach resets the bucket -- so rate was the
 * candidate fix, and it is WRONG. Read back off the live chip with
 * bcm_cosq_gport_bandwidth_get:
 *
 *   E2E port  0x78a00018   kbits_sec_max = 40000768     (a real 40G rate)
 *   connector 0xc4000010   kbits_sec_max = 0xFFFFFFFF   (UNLIMITED)
 *   connector 0xc4000020   kbits_sec_max = 0xFFFFFFFF   (UNLIMITED)
 *
 * Connectors come up UNLIMITED, not zero. Nothing is being shaped, so nothing
 * is being starved by a shaper. ffn_bcm_voq.c had already recorded the E2E
 * port's 42 Gbit/s, which should have been read before this was theorised.
 *
 * A version of this file did briefly call bcm_cosq_gport_bandwidth_set on the
 * connector and the E2E port. It returned 0, changed nothing about the
 * forwarding behaviour, and on one connector (0xc4000030) it made things
 * marginally worse by replacing UNLIMITED with 40000000. Do not re-add it.
 */

print "FFN_FORCE_FORWARD";
/*
 * force_forward bypasses the L2 lookup entirely, which is why none of this
 * needs a VLAN, a MAC table entry, or the ingress ITMH destination encoding
 * that is still unsolved on this chip.
 */
int rv_ff;
rv_ff = bcm_port_force_forward_set(u, src, dst, 1);
print rv_ff;
print "FFN_DONE";
