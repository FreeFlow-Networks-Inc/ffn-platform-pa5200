/*
 * ffn_bcm_fplink.c -- light the two faceplate ports that are cabled together.
 *
 *   bcm port 16 = ethernet1/5   ---- physical cable ----   bcm port 7 = ethernet1/13
 *
 * Both SFP+. This pair is chosen because every other faceplate blocker is
 * absent from it: SFP+ means no external copper PHY and no gearbox, their TX
 * FIR is already applied by jer.soc's phy_tx_settings.c, and whatever is fitted
 * between them is passive -- so the SFP+ TX_DISABLE expander on the control
 * plane's I2C, which gates the optical cages, has no bearing.
 *
 * ENABLE IS NOT ENOUGH, AND THAT IS THE WHOLE POINT OF THIS FILE.
 *
 * config.bcm carries port_init_speed_<N>=-1 for every faceplate port, and that
 * single property does TWO things:
 *
 *   1. bcm_petra_port_enable_set(..., FALSE) at init -- the `!ena` state that
 *      `ps` shows for all 25 ports.
 *   2. PORTMOD_PORT_ADD_F_SKIP_SPEED_INIT, which skips
 *      portmod_port_phychain_interface_config_set -- so the SerDes interface
 *      and rate were NEVER PROGRAMMED on those ports.
 *
 * bcm_port_enable_set undoes only the first. Without the second, the port is
 * administratively up with a SerDes that was never told what to be, which
 * presents as a port that is enabled and stubbornly down -- and looks exactly
 * like a cabling fault. Hence the explicit speed set.
 *
 * The vendor's own enable_fp_ports.c does the enable half and says in its
 * header that it is "only for diagnostic purposes"; nothing in jer.soc calls
 * it, because on a real appliance the management stack configures ports from
 * the running config. That stack is what FFN replaces.
 *
 * Do not name a variable "unit"; cint predefines it.
 */

int u = 0;
int rv;

bcm_port_t a = 16;   /* ethernet1/5  */
bcm_port_t b = 7;    /* ethernet1/13 */

print "FFN_ENABLE";
rv = bcm_port_enable_set(u, a, 1);
print rv;
rv = bcm_port_enable_set(u, b, 1);
print rv;

print "FFN_SPEED";
/*
 * 10G, explicitly. These are SFP+ cages and the config left the rate
 * unprogrammed; there is nothing to negotiate with on a passive link, so the
 * rate has to be stated rather than discovered.
 */
rv = bcm_port_speed_set(u, a, 10000);
print rv;
rv = bcm_port_speed_set(u, b, 10000);
print rv;

print "FFN_AUTONEG_OFF";
/*
 * Autonegotiation off on both. With a fixed rate at each end and a passive
 * medium between them there is no partner to negotiate with, and leaving AN on
 * is a way to sit forever waiting for one.
 */
rv = bcm_port_autoneg_set(u, a, 0);
print rv;
rv = bcm_port_autoneg_set(u, b, 0);
print rv;

print "FFN_DONE";
