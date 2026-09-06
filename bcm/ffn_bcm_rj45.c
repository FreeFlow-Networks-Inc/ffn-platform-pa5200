/*
 * ffn_bcm_rj45.c -- try to light the four RJ45 faceplate ports.
 *
 *   bcm 28 = ethernet1/1    bcm 13 = ethernet1/2
 *   bcm 14 = ethernet1/3    bcm 15 = ethernet1/4
 *
 * ethernet1/1 and ethernet1/3 (bcm 28 and 14) are the operator's cabled pair.
 *
 * THIS IS THE CHEAP TEST, AND IT MAY WELL FAIL. These four are not like the
 * SFP+ pair: they sit behind a BCM84848 quad 10GBase-T copper PHY on the
 * switch's EXTERNAL MDIO, and config.bcm declares no PHY for them at all --
 * no portmap, no phy_*, no xphy properties. The SDK therefore believes XE32-35
 * are direct SerDes ports and will not drive that PHY. A previous measurement
 * on this board got MAC and PHY loopback up on both ends and identified the
 * break as exactly that: an uninitialised external copper PHY.
 *
 * It is still worth five minutes, because some copper PHYs come out of reset
 * already autonegotiating, and if this one does then the link is free. If it
 * does not, the failure is informative in itself -- it means the PHY genuinely
 * needs driving over MDIO, and rules out the possibility that only the same
 * enable+speed gap as the SFP+ ports was in the way.
 *
 * AUTONEG STAYS ON, unlike ffn_bcm_fplink.c. That file turns it off because a
 * passive SFP+ cable has no partner to negotiate with. 10GBase-T is the
 * opposite: the standard MANDATES autonegotiation, it is how the two ends agree
 * rate and master/slave timing, and forcing it off on copper is a way to
 * guarantee no link rather than to speed one up.
 *
 * Do not name a variable "unit"; cint predefines it.
 */

int u = 0;
int i;
int rv;

bcm_port_t rj[4] = { 28, 13, 14, 15 };

print "FFN_ENABLE";
for (i = 0; i < 4; i++) {
    rv = bcm_port_enable_set(u, rj[i], 1);
    print rv;
}

print "FFN_SPEED";
/*
 * The same half that bcm_port_enable_set does not do: port_init_speed_<N>=-1
 * left SKIP_SPEED_INIT set, so the SerDes rate was never programmed on these
 * either. Necessary whether or not the external PHY is the real blocker.
 */
for (i = 0; i < 4; i++) {
    rv = bcm_port_speed_set(u, rj[i], 10000);
    print rv;
}

print "FFN_AUTONEG_ON";
for (i = 0; i < 4; i++) {
    rv = bcm_port_autoneg_set(u, rj[i], 1);
    print rv;
}

print "FFN_DONE";
