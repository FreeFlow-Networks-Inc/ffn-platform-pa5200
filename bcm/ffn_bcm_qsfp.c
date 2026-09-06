/*
 * ffn_bcm_qsfp.c -- bring up the four QSFP28 faceplate cages.
 *
 *   bcm 32 = CGE3    bcm 33 = CGE5    bcm 34 = CGE4    bcm 35 = CGE2
 *
 * All four read `!ena` at 100G, which is the same starting state the SFP+
 * faceplate ports were in: config.bcm sets port_init_speed_<N>=-1 on every
 * faceplate port, which both disables the port AND sets
 * PORTMOD_PORT_ADD_F_SKIP_SPEED_INIT so the SerDes rate was never programmed.
 * See ffn_bcm_fplink.c -- enable alone leaves a port administratively up with a
 * SerDes that was never told what to be, which looks exactly like a cable fault.
 *
 * PORTS 34 AND 35 ARE CABLED TO EACH OTHER. That is a measured fact recorded in
 * ffn_bcmports.py ("MEASURED: connected to port 35 (medium unknown)"), and it
 * makes this test self-contained: if the rate is right, that pair links with no
 * external equipment and nothing else on the board involved.
 *
 * THE RATE IS THE OPEN QUESTION, WHICH IS WHY THIS TRIES TWO.
 *
 * A QSFP28 cage carries either 4x10G (40G, QSFP+) or 4x25G (100G, QSFP28), and
 * which one links depends on the module or cable actually fitted -- something we
 * cannot read, because the module EEPROMs at 0x50 sit on I2C segments behind
 * multiplexers the control plane cannot currently select (see
 * ../octeon/i2c/README.md). The GPIO presence bits say two cages are populated
 * and three are not, which is consistent with one cable between 34 and 35, but
 * says nothing about its speed.
 *
 * So: try 40G, read the link, and only if it is down try 100G. Reading the
 * result rather than assuming it is the entire point -- a speed_set that the
 * SerDes cannot honour returns non-zero, and a rate that is wrong for the
 * medium returns zero and simply never links. Those two failures look nothing
 * alike in the output and should not be conflated.
 *
 * Autoneg stays OFF. There is no autonegotiation on a passive QSFP DAC, and
 * leaving it on is a way to wait forever for a partner that will not answer.
 *
 * Do not name a variable "unit"; cint predefines it.
 */

int u = 0;
int rv;
int i;
int link;

bcm_port_t q[4];
q[0] = 32;
q[1] = 33;
q[2] = 34;
q[3] = 35;

/* The cabled pair, tested for link. The other two have nothing to link to. */
bcm_port_t a = 34;
bcm_port_t b = 35;

print "FFN_ENABLE";
for (i = 0; i < 4; i++) {
    rv = bcm_port_enable_set(u, q[i], 1);
    print rv;
}

print "FFN_AUTONEG_OFF";
for (i = 0; i < 4; i++) {
    rv = bcm_port_autoneg_set(u, q[i], 0);
    print rv;
}

print "FFN_SPEED_40G";
for (i = 0; i < 4; i++) {
    rv = bcm_port_speed_set(u, q[i], 40000);
    print rv;
}

/*
 * Give the SerDes time to train before believing the link bit. A read taken
 * immediately after a rate change reports the old state and invites the
 * conclusion that 40G failed when it was merely not finished.
 */
print "FFN_SETTLE_40G";
sal_sleep(4);

print "FFN_LINK_40G";
rv = bcm_port_link_status_get(u, a, &link);
print rv;
print link;
rv = bcm_port_link_status_get(u, b, &link);
print rv;
print link;

if (link == 0) {
    print "FFN_SPEED_100G";
    for (i = 0; i < 4; i++) {
        rv = bcm_port_speed_set(u, q[i], 100000);
        print rv;
    }

    print "FFN_SETTLE_100G";
    sal_sleep(4);

    print "FFN_LINK_100G";
    rv = bcm_port_link_status_get(u, a, &link);
    print rv;
    print link;
    rv = bcm_port_link_status_get(u, b, &link);
    print rv;
    print link;
}

print "FFN_DONE";
