/*
 * FFN: basic L2 at the ports.
 *
 * Nothing has ever forwarded at L2 on this chip because NO PORT IS IN THE
 * ETHERNET CLASS: bcm_port_stp_set returns BCM_E_PORT for every port, because
 * STG_CHECK_PORT requires IS_E_PORT / IS_HG_PORT / IS_SPI_SUBPORT_PORT and the
 * ports are in none of them. The cause is the header type -- only port 3 shipped
 * as in=ETH. The cure was always a config change, but at the time that was
 * discovered the config copy was never actually being read; with BCM_CONFIG_FILE
 * it now is.
 *
 * Ports 5 (CP eth0) and 8 (MP enp8s0f1) are now in=ETH/out=ETH, which puts two
 * REAL Linux hosts on Ethernet-class ports, so a successful bridge is provable
 * with a ping rather than with counters.
 *
 * bcm_port_stp_set returning 0 is the litmus test: it is the exact call that has
 * refused every port until now, so success means the port class really changed.
 */
int u = 0;

bcm_port_t a = 5;    /* CP eth0,      10.99.0.2 */
bcm_port_t b = 8;    /* MP enp8s0f1,  10.99.0.1 */

bcm_port_enable_set(u, a, 1);
bcm_port_enable_set(u, b, 1);

print "FFN_STP_LITMUS";
int rv_a;
rv_a = bcm_port_stp_set(u, a, BCM_STG_STP_FORWARD);
print rv_a;
int rv_b;
rv_b = bcm_port_stp_set(u, b, BCM_STG_STP_FORWARD);
print rv_b;

print "FFN_L2_DONE";
