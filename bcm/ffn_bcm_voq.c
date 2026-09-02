/*
 * ffn_bcm_voq.c -- bring up the BCM88375 datapath: VOQ, connector, credits.
 *
 * Run as:  cint /tmp/ffn_bcm_voq.c   from the vendor bcm.user diag shell,
 * which FFN reaches through its own BDE (see octeon/kctl/FFN-BDE.md).
 *
 * PROVEN END TO END:
 *   CP eth0 -> port 5 -> VOQ 4 -> E2E scheduler -> port 24 -> 40G -> DP eth0
 *   BCM port 5 RX 300, port 24 TX 300 with 0 errors, IQM enqueue 300 /
 *   dequeue 300, and the DP counts rx_packets 300 / rx_errors 0.
 *
 * The chip forwards nothing as shipped because it has no queues at all:
 * jer.soc builds the E2E scheduler tree, but the QUEUES are created at
 * runtime by the vendor dataplane, which FFN does not run. COSQ conn ing
 * and conn egr were both empty and all 74 gports were type Scheduler.
 *
 * Three things here are not obvious and each cost real time:
 *
 *   1. bcm_cosq_gport_attach() is what makes traffic move. Creating the
 *      connector and binding it to the VOQ is NOT enough -- the connector
 *      is a scheduling node and needs a PARENT in the E2E hierarchy.
 *      Unparented it is never scheduled, so no credits are generated and
 *      the VOQ never drains. That single call is the difference between
 *      IqmDequeuePacketCounter 0 and 300.
 *
 *   2. The connector MUST be created with bcm_cosq_voq_connector_gport_add.
 *      The legacy bcm_cosq_gport_add(BCM_COSQ_GPORT_VOQ_CONNECTOR) refuses
 *      with BCM_E_CONFIG "Can't determine core ID" whenever WITH_ID is
 *      absent and the device has more than one core, and this chip has two.
 *
 *   3. The E2E port gport is computed, not returned by any API:
 *        e2e_port = 0x78000000 | (5 << 21) | port    (COSQ 30<<26, E2E 5<<21)
 *      confirmed by the SDK's own allocation -- the connector came back as
 *      0x78200010, the same family with subtype 1.
 *
 * Two prerequisites live outside this file. BCM_CONFIG_FILE must name the
 * config copy (cd alone is inert), and port 5 must be in=RAW -- on a TM port
 * the frame's first bytes are parsed as an ITMH, which is what made every
 * packet fail enqueue with "queue not valid".
 *
 * The fabric is NOT involved: fabric_connect_mode=SINGLE_FAP, zero fabric
 * links, and the credit loop is internal once the connector is parented.
 *
 * Do not name a variable "unit"; cint predefines it.
 */
int u = 0;
int i;

bcm_port_t src = 5;    /* CP eth0, measured; now in=RAW so plain Ethernet is parsed */
bcm_port_t dst = 24;   /* DP 40G link, measured link-up */

bcm_port_t fp[25] = { 28, 13, 14, 15, 16,  1, 18, 19,  6, 21, 22, 23,
                       7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35, 12 };
for (i = 0; i < 25; i++) {
    bcm_port_enable_set(u, fp[i], 1);
}
bcm_port_enable_set(u, src, 1);
bcm_port_enable_set(u, dst, 1);

print "FFN_BASE";
bcm_gport_t dst_gport = 0;
bcm_port_gport_get(u, dst, &dst_gport);
bcm_gport_t dst_sysport = 0;
bcm_stk_gport_sysport_get(u, dst_gport, &dst_sysport);
bcm_module_t modid = 0;
bcm_stk_modid_get(u, &modid);

/* COSQ type 30 << 26 | E2E_PORT subtype 5 << 21 | port */
int e2e_port;
e2e_port = 0x78000000 | (5 << 21) | dst;
print e2e_port;

print "FFN_CONNECTOR_ON_E2E";
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
 * The connector is created and bound to the VOQ, and the egress scheduler it
 * feeds already carries a real rate (42 Gbit/s, read back from the E2E port
 * gport). What was missing is that the connector had no PARENT: creating a
 * scheduling node and parenting it are separate steps, and an unparented flow
 * is never scheduled, so no credits are generated and the VOQ never drains.
 *
 * Before this call: enqueue 300, dequeue 0, IpsDeqCmdCounter 3, no TX.
 * After it:         enqueue 300, dequeue 300, IpsDeqCmdCounter 300, port 24
 *                   TX 300 with zero errors, and the DP receives all 300.
 *
 * bcm_cosq_gport_attach_get returns BCM_E_UNAVAIL on this device, so the
 * attachment cannot be read back -- verify with traffic, not with a getter.
 * bcm_cosq_gport_sched_set on the connector returns BCM_E_PARAM ("error in
 * retreiving parent info corresponding to child element scheduling mode") and
 * is not required; attach alone is sufficient.
 */
int rv_attach;
rv_attach = bcm_cosq_gport_attach(u, e2e_port, connector, 0);
print rv_attach;

print "FFN_FORCE_FORWARD";
int rv_ff;
rv_ff = bcm_port_force_forward_set(u, src, dst, 1);
print rv_ff;
print "FFN_DONE";
