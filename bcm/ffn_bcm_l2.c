/*
 * FFN: basic L2 at the ports.
 *
 * Nothing forwarded at L2 on this chip for a long time because NO PORT WAS IN
 * THE ETHERNET CLASS: bcm_port_stp_set returns BCM_E_PORT for every port,
 * because STG_CHECK_PORT requires IS_E_PORT / IS_HG_PORT / IS_SPI_SUBPORT_PORT
 * and the ports are in none of them. The cause is the header type -- only port 3
 * shipped as in=ETH. The cure was always a config change, but at the time that
 * was discovered the config copy was never actually being read; with
 * BCM_CONFIG_FILE it now is.
 *
 * bcm_port_stp_set returning 0 is the litmus test: it is the exact call that
 * refused every port until the class changed, so rv=0 means the class is real.
 *
 * Ports and what proves them:
 *   5   XE37  10G   CP eth0      10.99.0.2   in=ETH/out=ETH
 *   8   XE38  10G   MP enp8s0f1  10.99.0.1   in=ETH/out=ETH
 *   24  XLGE17 40G  toward the DP            in=ETH/out=ETH  (2026-09-03)
 *
 * 5 <-> 8 is provable with a PING between two REAL Linux hosts rather than with
 * counters, and is measured at 0% loss / ~0.07 ms.
 *
 * Port 24 shipped as in=TM/out=TM, which is why the DP leg previously needed the
 * DNX logical-interface layer; setting both to ETH makes stp_set return 0 for it
 * too, putting the DP in the same hardware-switched L2 domain. Its link reads
 * `down` until the DP end drives it -- that is a DP boot, not a link fault.
 *
 * Pair with: vlan add 1 PortBitMap=xe5,xe8,xl24 UntagBitMap=xe5,xe8,xl24
 */
int u = 0;
int rv;
int i;

bcm_port_t ports[3];
ports[0] = 5;
ports[1] = 8;
ports[2] = 24;

print "FFN_STP_LITMUS";
for (i = 0; i < 3; i++) {
    /* The front-panel ports ship disabled -- config.bcm has SOC properties that
     * disable all of them at bcm.user startup (enable_fp_ports.c says so in its
     * own comment), so this is required, not defensive. */
    rv = bcm_port_enable_set(u, ports[i], 1);
    printf("FFN: bcm_port_enable_set(port %d, 1) rv=%d %s\n",
           ports[i], rv, bcm_errmsg(rv));
    rv = bcm_port_stp_set(u, ports[i], BCM_STG_STP_FORWARD);
    printf("FFN: bcm_port_stp_set(port %d, FORWARD) rv=%d %s\n",
           ports[i], rv, bcm_errmsg(rv));
    rv = bcm_port_learn_set(u, ports[i], BCM_PORT_LEARN_ARL | BCM_PORT_LEARN_FWD);
    printf("FFN: bcm_port_learn_set(port %d, ARL|FWD) rv=%d %s\n",
           ports[i], rv, bcm_errmsg(rv));
}

print "FFN_L2_DONE";
