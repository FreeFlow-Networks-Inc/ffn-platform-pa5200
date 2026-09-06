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
 * THE CALL THAT MAKES TRAFFIC MOVE.
 *
 * Creating the connector and binding it to the VOQ is not enough: the
 * connector is a scheduling node and needs a PARENT in the E2E hierarchy.
 * Unparented it is never scheduled, so no credits are generated and the VOQ
 * never drains. Measured on this silicon: before this call, enqueue 300 and
 * dequeue 0; after it, dequeue 300 and port 24 TX 300.
 *
 * bcm_cosq_gport_attach_get returns BCM_E_UNAVAIL here, so the attachment
 * cannot be read back -- verify with traffic, not with a getter.
 */
int rv_attach;
rv_attach = bcm_cosq_gport_attach(u, e2e_port, connector, 0);
print rv_attach;

print "FFN_RATE";
/*
 * A CANDIDATE FIX FOR THE ONE-SHOT BEHAVIOUR BELOW. NOT YET VERIFIED -- read
 * the whole comment before believing it, and re-test from a freshly
 * initialised chip.
 *
 * The theory: a connector is a shaped scheduling node, so parenting it is
 * necessary but not sufficient -- it also needs a RATE. With the rate left at
 * its default the node has a burst allowance and no replenishment, so it
 * passes one burst and then stops for good. That is not a stall; it is a
 * shaper doing exactly what a zero rate asks of it. The SDK's own TM FAP setup
 * sets a rate and a max burst on the connector for this reason.
 *
 * WHY IT IS UNVERIFIED. All three calls below returned 0 on silicon and
 * forwarding did NOT resume -- but that measurement is worthless, because by
 * the time it was taken the chip's scheduling state had been polluted by the
 * attempts described under "NOT the parent gport": six connector queues
 * re-parented onto TC 1's scheduling element, never undone, plus four leaked
 * VOQ/connector pairs from repeated runs of this file. A clean test needs an
 * ffn-bcmd restart first, which re-initialises the chip -- see
 * octeon/bcmagent/ffn-bcmd-ctl.sh, and note that the front-panel links have to
 * be brought back up afterwards.
 *
 * So the state of knowledge is: the one-shot behaviour is real and measured,
 * the two explanations below are ruled out, and this is the next thing to try.
 *
 * MEASURED, because this file previously claimed the opposite. 200 broadcast
 * frames from the MP: BCM port 8 RX +200, BCM port 24 TX +26, no error counter
 * anywhere, and port 24 then frozen for minutes while port 8 kept counting.
 * The frames were not lost -- re-running this recipe moved port 24 TX +205
 * while port 8 took only +2, so the backlog came out when a fresh attach reset
 * the burst bucket. A second 200-frame burst then gave port 8 +200 and port 24
 * +0. One grant per attach.
 *
 * WHY THE ORIGINAL MEASUREMENT LOOKED CONVINCING. ffn_bcm_voq.c recorded
 * "enqueue 300 / dequeue 300, port 24 TX 300" and concluded, in its own words,
 * that "the credit loop is internal once the connector is parented." Three
 * hundred frames fit inside a single burst allowance. One burst that drains
 * proves the attach happened; it cannot tell a one-shot grant from a running
 * credit loop, and no second burst was ever sent. ffn_bcm_voq.c and
 * ffn_bcm_chain.c still have this defect.
 *
 * TWO THINGS THAT ARE NOT THE CAUSE, both checked so nobody re-checks them:
 *
 *   * NOT link-level pause. Port 24 is the one linked port with pause RX
 *     enabled and TX disabled (`ps` shows `TX RX` for 2, 8, 9, 34, 35 and bare
 *     `RX` for 24), which looks like the dataplane asserting flow control and
 *     never releasing it. Pause would not have let the +205 flush through.
 *   * NOT the parent gport. Attaching to the per-TC HR scheduling element
 *     instead -- built from an E2E PORT TC gport, subtype 13, and resolved with
 *     bcm_cosq_gport_handle_get(bcmCosqGportTypeSched) -- forwards NOTHING at
 *     all, on either burst. The E2E PORT gport used above, subtype 5, is
 *     correct, and the SDK's own TM FAP setup uses that same encoding.
 *     Incidentally, handle_get answers only for TC 0 and TC 1 on this port and
 *     returns BCM_E_PARAM for TC 2..7, and it does not write out_gport on
 *     failure -- so a loop that attaches unconditionally re-parents six queues
 *     onto TC 1's element and gets rv 0 for every one of them.
 *
 * Rate is the link rate: this connector feeds a 40G port and there is no
 * reason to shape below it. Burst is the SDK's own default scale.
 */
int rate_kbps = 40000000;   /* 40 Gbps, in kbit/s -- the DP link's line rate */
int max_burst = 3000;

int rv_bw_e2e;
rv_bw_e2e = bcm_cosq_gport_bandwidth_set(u, e2e_port, 0, 0, rate_kbps, 0);
print rv_bw_e2e;

int rv_bw_conn;
rv_bw_conn = bcm_cosq_gport_bandwidth_set(u, connector, 0, 0, rate_kbps, 0);
print rv_bw_conn;

int rv_burst;
rv_burst = bcm_cosq_control_set(u, connector, 0, bcmCosqControlBandwidthBurstMax, max_burst);
print rv_burst;

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
