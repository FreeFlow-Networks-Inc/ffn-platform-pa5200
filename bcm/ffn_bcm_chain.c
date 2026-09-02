/*
 * FFN: forward through the FACEPLATE, using the operator's loopback cable.
 *
 *   CP eth0 -> port 5 -> port 16 -> [cable] -> port 7 -> port 24 -> DP eth0
 *
 * Ports 7 and 16 are a measured loopback pair and both link at 10G once
 * enabled; 34/35 are the other pair, at 100G. That gives a real front-panel
 * traffic source without any external generator: whatever port 16 transmits
 * arrives at port 7, which is genuine faceplate ingress.
 *
 * Each DESTINATION needs its own queue layer -- a VOQ bound to its system port,
 * a VOQ connector parented into that port's E2E scheduler, and the two
 * connection_set calls binding them. Without the attach the connector is never
 * scheduled, gets no credits, and the VOQ fills and stalls; that one call is
 * the difference between dequeue 0 and dequeue N.
 *
 * e2e gport = COSQ type 30 << 26 | E2E_PORT subtype 5 << 21 | port.
 */
int u = 0;
int i;

bcm_port_t src  = 5;    /* CP eth0, in=RAW so plain Ethernet is accepted */
bcm_port_t mid  = 16;   /* front panel, cabled to port 7 */
bcm_port_t fpin = 7;    /* front panel ingress, the far end of that cable */
bcm_port_t dst  = 24;   /* the DP 40G link */

bcm_port_t fp[25] = { 28, 13, 14, 15, 16,  1, 18, 19,  6, 21, 22, 23,
                       7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35, 12 };
for (i = 0; i < 25; i++) {
    bcm_port_enable_set(u, fp[i], 1);
}
bcm_port_enable_set(u, src, 1);
bcm_port_enable_set(u, dst, 1);

bcm_module_t modid = 0;
bcm_stk_modid_get(u, &modid);

/* ---------- queue layer for destination = port 16 ---------- */
print "FFN_Q_MID";
bcm_gport_t g_mid = 0;
bcm_port_gport_get(u, mid, &g_mid);
bcm_gport_t sp_mid = 0;
bcm_stk_gport_sysport_get(u, g_mid, &sp_mid);
int e2e_mid;
e2e_mid = 0x78000000 | (5 << 21) | mid;

bcm_cosq_voq_connector_gport_t cfg_mid;
cfg_mid.flags = 0;
cfg_mid.port = e2e_mid;
cfg_mid.numq = 8;
cfg_mid.remote_modid = modid;
cfg_mid.nof_remote_cores = 2;
bcm_gport_t conn_mid = 0;
int rv_cm;
rv_cm = bcm_cosq_voq_connector_gport_add(u, &cfg_mid, &conn_mid);
print rv_cm;

bcm_gport_t voq_mid = 0;
int rv_vm;
rv_vm = bcm_cosq_gport_add(u, sp_mid, 8, BCM_COSQ_GPORT_UCAST_QUEUE_GROUP, &voq_mid);
print rv_vm;

bcm_cosq_gport_connection_t cn_mid;
cn_mid.flags = BCM_COSQ_GPORT_CONNECTION_INGRESS;
cn_mid.remote_modid = modid;
cn_mid.voq = voq_mid;
cn_mid.voq_connector = conn_mid;
int rv_im;
rv_im = bcm_cosq_gport_connection_set(u, &cn_mid);
print rv_im;
cn_mid.flags = BCM_COSQ_GPORT_CONNECTION_EGRESS;
cn_mid.remote_modid = modid;
cn_mid.voq = voq_mid;
cn_mid.voq_connector = conn_mid;
int rv_em;
rv_em = bcm_cosq_gport_connection_set(u, &cn_mid);
print rv_em;
int rv_am;
rv_am = bcm_cosq_gport_attach(u, e2e_mid, conn_mid, 0);
print rv_am;

/* ---------- queue layer for destination = port 24 (the DP) ---------- */
print "FFN_Q_DST";
bcm_gport_t g_dst = 0;
bcm_port_gport_get(u, dst, &g_dst);
bcm_gport_t sp_dst = 0;
bcm_stk_gport_sysport_get(u, g_dst, &sp_dst);
int e2e_dst;
e2e_dst = 0x78000000 | (5 << 21) | dst;

bcm_cosq_voq_connector_gport_t cfg_dst;
cfg_dst.flags = 0;
cfg_dst.port = e2e_dst;
cfg_dst.numq = 8;
cfg_dst.remote_modid = modid;
cfg_dst.nof_remote_cores = 2;
bcm_gport_t conn_dst = 0;
int rv_cd;
rv_cd = bcm_cosq_voq_connector_gport_add(u, &cfg_dst, &conn_dst);
print rv_cd;

bcm_gport_t voq_dst = 0;
int rv_vd;
rv_vd = bcm_cosq_gport_add(u, sp_dst, 8, BCM_COSQ_GPORT_UCAST_QUEUE_GROUP, &voq_dst);
print rv_vd;

bcm_cosq_gport_connection_t cn_dst;
cn_dst.flags = BCM_COSQ_GPORT_CONNECTION_INGRESS;
cn_dst.remote_modid = modid;
cn_dst.voq = voq_dst;
cn_dst.voq_connector = conn_dst;
int rv_id;
rv_id = bcm_cosq_gport_connection_set(u, &cn_dst);
print rv_id;
cn_dst.flags = BCM_COSQ_GPORT_CONNECTION_EGRESS;
cn_dst.remote_modid = modid;
cn_dst.voq = voq_dst;
cn_dst.voq_connector = conn_dst;
int rv_ed;
rv_ed = bcm_cosq_gport_connection_set(u, &cn_dst);
print rv_ed;
int rv_ad;
rv_ad = bcm_cosq_gport_attach(u, e2e_dst, conn_dst, 0);
print rv_ad;

/* ---------- the two hops ---------- */
print "FFN_HOP1_5_TO_16";
int rv_h1;
rv_h1 = bcm_port_force_forward_set(u, src, mid, 1);
print rv_h1;

print "FFN_HOP2_7_TO_24";
int rv_h2;
rv_h2 = bcm_port_force_forward_set(u, fpin, dst, 1);
print rv_h2;

print "FFN_CHAIN_DONE";
