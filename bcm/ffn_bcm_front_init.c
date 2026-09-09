/* Explicitly initialize front-panel SerDes rates, not only admin state.
 * Block scope is essential: CINT retains globals across requests.
 * No queues, forwarding rules, internal links or HSCI are changed.
 */
{
    int fp_ports[24] = {28,13,14,15,16,1,18,19,6,21,22,23,
                       7,11,36,27,10,29,30,31,32,33,34,35};
    int fp_i;
    int fp_rv;
    int fp_errors = 0;
    int fp_rate;
    for (fp_i = 0; fp_i < 24; fp_i++) {
        fp_rate = fp_i < 20 ? 10000 : 40000;
        fp_rv = bcm_port_autoneg_set(0, fp_ports[fp_i], 0);
        printf("FFN_FRONT an port=%d rv=%d\n", fp_ports[fp_i], fp_rv);
        if (fp_rv != 0) { fp_errors++; }
        if (fp_i >= 20) {
            /* The generic board file selects CAUI (100G). CR4 supports
             * the PA-5220's 40G four-lane front links in PM4x25. */
            fp_rv = bcm_port_interface_set(0, fp_ports[fp_i], BCM_PORT_IF_CR4);
            printf("FFN_FRONT interface port=%d rv=%d\n", fp_ports[fp_i], fp_rv);
            if (fp_rv != 0) { fp_errors++; }
        }
        fp_rv = bcm_port_speed_set(0, fp_ports[fp_i], fp_rate);
        printf("FFN_FRONT speed port=%d mbps=%d rv=%d\n", fp_ports[fp_i], fp_rate, fp_rv);
        if (fp_rv != 0) { fp_errors++; }
        fp_rv = bcm_port_enable_set(0, fp_ports[fp_i], 1);
        printf("FFN_FRONT enable port=%d rv=%d\n", fp_ports[fp_i], fp_rv);
        if (fp_rv != 0) { fp_errors++; }
    }
    if (fp_errors == 0) { print "FFN_FRONT_DONE"; }
    else { printf("FFN_FRONT_FAILED errors=%d\n", fp_errors); }
}
