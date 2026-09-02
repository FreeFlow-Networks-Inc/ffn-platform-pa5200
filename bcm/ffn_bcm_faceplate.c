/*
 * FFN: steer the WHOLE faceplate to the dataplane, with source identity.
 *
 * This is the product's own model: every front-panel port hands its traffic to
 * the DP, which decides what happens next. force_forward is per INGRESS port,
 * so that is one call per faceplate port -- 25 of them -- not a workaround.
 *
 * Port 24 is additionally set tm_port_header_type_out=TM in the config copy, so
 * the chip BUILDS a header on egress toward the DP. That header is
 * [dest port : 16][source port : 16] -- captured off the wire earlier, both
 * directions, with only the changing field differing -- which hands the DP the
 * ingress port of every frame for free. That is the demux key a per-port
 * netdev layer on the DP would need, without solving the ingress ITMH encoding.
 *
 * Hop 5 -> 16 is kept as the traffic source: port 16 is cabled to port 7, so
 * injecting on CP eth0 produces genuine faceplate ingress at port 7.
 */
int u = 0;
int i;

bcm_port_t src = 5;
bcm_port_t mid = 16;
bcm_port_t dst = 24;

bcm_port_t fp[25] = { 28, 13, 14, 15, 16,  1, 18, 19,  6, 21, 22, 23,
                       7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35, 12 };
for (i = 0; i < 25; i++) {
    bcm_port_enable_set(u, fp[i], 1);
}
bcm_port_enable_set(u, src, 1);
bcm_port_enable_set(u, dst, 1);

bcm_module_t modid = 0;
bcm_stk_modid_get(u, &modid);

print "FFN_QUEUES";
int rvq = 0;
bcm_gport_t g = 0;
bcm_gport_t sp = 0;
int e2e;
bcm_cosq_voq_connector_gport_t cfg;
bcm_gport_t conn = 0;
bcm_gport_t voq = 0;
bcm_cosq_gport_connection_t cn;

/* destination 16 (so the faceplate source hop works) */
bcm_port_gport_get(u, mid, &g);
bcm_stk_gport_sysport_get(u, g, &sp);
e2e = 0x78000000 | (5 << 21) | mid;
cfg.flags = 0; cfg.port = e2e; cfg.numq = 8;
cfg.remote_modid = modid; cfg.nof_remote_cores = 2;
rvq = bcm_cosq_voq_connector_gport_add(u, &cfg, &conn);
print rvq;
rvq = bcm_cosq_gport_add(u, sp, 8, BCM_COSQ_GPORT_UCAST_QUEUE_GROUP, &voq);
print rvq;
cn.flags = BCM_COSQ_GPORT_CONNECTION_INGRESS; cn.remote_modid = modid;
cn.voq = voq; cn.voq_connector = conn;
rvq = bcm_cosq_gport_connection_set(u, &cn);
cn.flags = BCM_COSQ_GPORT_CONNECTION_EGRESS; cn.remote_modid = modid;
cn.voq = voq; cn.voq_connector = conn;
rvq = bcm_cosq_gport_connection_set(u, &cn);
rvq = bcm_cosq_gport_attach(u, e2e, conn, 0);
print rvq;

/* destination 24 (the DP) */
bcm_gport_t g2 = 0;
bcm_gport_t sp2 = 0;
bcm_port_gport_get(u, dst, &g2);
bcm_stk_gport_sysport_get(u, g2, &sp2);
int e2e2;
e2e2 = 0x78000000 | (5 << 21) | dst;
bcm_cosq_voq_connector_gport_t cfg2;
cfg2.flags = 0; cfg2.port = e2e2; cfg2.numq = 8;
cfg2.remote_modid = modid; cfg2.nof_remote_cores = 2;
bcm_gport_t conn2 = 0;
bcm_gport_t voq2 = 0;
rvq = bcm_cosq_voq_connector_gport_add(u, &cfg2, &conn2);
print rvq;
rvq = bcm_cosq_gport_add(u, sp2, 8, BCM_COSQ_GPORT_UCAST_QUEUE_GROUP, &voq2);
print rvq;
bcm_cosq_gport_connection_t cn2;
cn2.flags = BCM_COSQ_GPORT_CONNECTION_INGRESS; cn2.remote_modid = modid;
cn2.voq = voq2; cn2.voq_connector = conn2;
rvq = bcm_cosq_gport_connection_set(u, &cn2);
cn2.flags = BCM_COSQ_GPORT_CONNECTION_EGRESS; cn2.remote_modid = modid;
cn2.voq = voq2; cn2.voq_connector = conn2;
rvq = bcm_cosq_gport_connection_set(u, &cn2);
rvq = bcm_cosq_gport_attach(u, e2e2, conn2, 0);
print rvq;

print "FFN_STEER_ALL_FACEPLATE_TO_DP";
int nfail = 0;
int rvf;
for (i = 0; i < 25; i++) {
    if (fp[i] != mid) {
        rvf = bcm_port_force_forward_set(u, fp[i], dst, 1);
        if (rvf != 0) { nfail = nfail + 1; }
    }
}
print nfail;

print "FFN_SOURCE_HOP_5_TO_16";
int rv_h1;
rv_h1 = bcm_port_force_forward_set(u, src, mid, 1);
print rv_h1;
print "FFN_ALL_DONE";
