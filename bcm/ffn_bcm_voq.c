/*
 * ffn_bcm_voq.c -- build the BCM88375's missing queue layer, via cint.
 *
 * Run as:  cint /tmp/ffn_bcm_voq.c   from the vendor bcm.user diag shell,
 * which FFN reaches through its own BDE (see octeon/kctl/FFN-BDE.md).
 *
 * Why this exists
 * ---------------
 * This board forwards nothing as shipped, and the reason is not the forwarding
 * decision -- it is that the chip has no queues at all. On a VOQ device the
 * forwarding decision enqueues into a VOQ; with none provisioned every packet
 * is rejected at the ingress queue manager. Measured, from the device's own
 * diagnostics before this script existed:
 *
 *     COSQ conn ing / conn egr    both tables EMPTY
 *     GPort                       74 gports, every one of them type Scheduler
 *
 * jer.soc builds the E2E scheduler tree; the QUEUES are created at runtime by
 * the vendor's PAN-OS dataplane, which FFN does not run. So the queue layer is
 * ours to build, and this is it.
 *
 * What it does
 * ------------
 * Creates one VOQ bundle for the destination port's system port, one VOQ
 * connector on that port's egress scheduler, and binds them in both
 * directions. Every call is printed so a run is self-evidencing. Afterwards
 * COSQ conn ing / conn egr show the two tables populated and cross-linked.
 *
 * Two things here are not obvious and cost real time to find:
 *
 *   - The connector MUST be created with bcm_cosq_voq_connector_gport_add.
 *     The legacy bcm_cosq_gport_add(BCM_COSQ_GPORT_VOQ_CONNECTOR) refuses with
 *     BCM_E_CONFIG "Can't determine core ID" whenever BCM_COSQ_GPORT_WITH_ID is
 *     absent and the device has more than one core -- and this chip has two.
 *     The legacy path would need a hand-picked connector ID, which risks
 *     colliding with the scheduler elements jer.soc has already placed; the
 *     core-aware call allocates the ID itself.
 *
 *   - Do not name a variable "unit". cint predefines it, so declaring it is an
 *     error, and assigning to it bare has been seen to go "undeclared" later in
 *     the same script.
 *
 * KNOWN NOT SUFFICIENT: this builds the queue layer, but traffic still does not
 * egress. The remaining drop is measured and specific --
 *
 *     diag counters:  IQM0 IqmRjctQnvalidErrPktCnt : 216 of 218 received
 *                     IQM0 IqmEnqueuePacketCounter :   2
 *
 * "queue not valid": the forwarding decision resolves to a queue that is not
 * the one created here. config.bcm sets voq_mapping_mode=INDIRECT, so the
 * destination-to-VOQ binding is an explicit mapping rather than derived, and
 * creating the VOQ against the system port does not establish it (the same VOQ
 * id comes back either way). Finding that mapping is the next step. Already
 * ruled out: credits (the connector's max rate reads 0xffffffff, unlimited),
 * link state, and the forwarding decision itself (force_forward is confirmed
 * in-chip by bcm_port_force_forward_get).
 */
int u = 0;

bcm_port_t src = 5;    /* CP eth0, measured */
bcm_port_t dst = 24;   /* DP 40G link, measured link-up */
int i;

bcm_port_t fp[25] = { 28, 13, 14, 15, 16,  1, 18, 19,  6, 21, 22, 23,
                       7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35, 12 };
for (i = 0; i < 25; i++) {
    bcm_port_enable_set(u, fp[i], 1);
}
bcm_port_enable_set(u, src, 1);
bcm_port_enable_set(u, dst, 1);

print "FFN_SYSPORTS";
bcm_gport_t dst_gport = 0;
bcm_gport_t src_gport = 0;
bcm_port_gport_get(u, dst, &dst_gport);
bcm_port_gport_get(u, src, &src_gport);

bcm_gport_t dst_sysport = 0;
int rv_sp_dst;
rv_sp_dst = bcm_stk_gport_sysport_get(u, dst_gport, &dst_sysport);
print rv_sp_dst;
print dst_sysport;

bcm_gport_t src_sysport = 0;
int rv_sp_src;
rv_sp_src = bcm_stk_gport_sysport_get(u, src_gport, &src_sysport);
print rv_sp_src;
print src_sysport;

print "FFN_MODID";
bcm_module_t modid = 0;
bcm_stk_modid_get(u, &modid);
print modid;

print "FFN_CONNECTOR";
bcm_cosq_voq_connector_gport_t cfg;
cfg.flags = 0;
cfg.port = dst_gport;
cfg.numq = 8;
cfg.remote_modid = modid;
cfg.nof_remote_cores = 2;
bcm_gport_t connector = 0;
int rv_conn;
rv_conn = bcm_cosq_voq_connector_gport_add(u, &cfg, &connector);
print rv_conn;
print connector;

print "FFN_VOQ_ON_SYSPORT";
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

print "FFN_FORCE_FORWARD";
int rv_ff;
rv_ff = bcm_port_force_forward_set(u, src, dst, 1);
print rv_ff;


print "FFN_BANDWIDTH_BEFORE";
/*
 * Read the connector's rate rather than assuming it: a connector with no rate
 * never receives credits, so its queue never dequeues -- which would present
 * exactly as this board does, accepted at ingress with no TX counter ever
 * moving. It reads 0xffffffff here, so credits are NOT the problem.
 */
uint32 bw_min = 0;
uint32 bw_max = 0;
uint32 bw_flags = 0;
int rv_bwg;
rv_bwg = bcm_cosq_gport_bandwidth_get(u, connector, 0, &bw_min, &bw_max, &bw_flags);
print rv_bwg;
print bw_min;
print bw_max;

print "FFN_BANDWIDTH_SET";
int c;
int rv_bws = 0;
for (c = 0; c < 8; c++) {
    rv_bws = bcm_cosq_gport_bandwidth_set(u, connector, c, 0, 10000000, 0);
}
print rv_bws;

print "FFN_DONE";
