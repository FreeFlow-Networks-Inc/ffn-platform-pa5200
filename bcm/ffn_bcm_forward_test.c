/* Lab path: MP/BCM8 -> faceplate5/BCM16 -> cable -> faceplate13/BCM7 -> MP.
 * Run only once after BCM init. Check every result, attach all eight queues,
 * and select an explicit ingress credit profile. Modes: 0 front cable,
 * 1 allocate NIF test, 2 enable NIF test, 3 disable NIF test,
 * 4 allocate Interlaken test, 5 enable it, 6 disable it.
 * 7 capture IL output as RAW, 8 restore IL TM headers.
 * Allocation modes require a fresh switch init.
 * 9 add only front5's VOQ (MP8's VOQ must already exist), return front13
 *   to MP8 for testing destination-addressed injection on MP9.
 * 10 disable that return route. Mode 9 must be run only once.
 */
{
    int rv = 0;
    int fe100_test = 0;
    int single_front = fe100_test == 9 || fe100_test == 12 || fe100_test == 15 || fe100_test == 16 || fe100_test == 23 || fe100_test == 24;
    int n;
    int cos;
    int src;
    int dst;
    int modid;
    int e2e;
    int modport;
    int tmport;
    int destination_module;
    int sysport;
    int connector;
    int voq;
    uint32 flags;
    bcm_port_interface_info_t intf;
    bcm_port_mapping_info_t mapping;
    bcm_cosq_voq_connector_gport_t cfg;
    bcm_cosq_ingress_queue_bundle_gport_config_t ingress;
    bcm_cosq_gport_connection_t connection;
    int an_ports[4] = {16, 7, 34, 35};
    int an_front[4] = {5, 13, 23, 24};
    int an_value;
    int an_link;
    int an_speed;
    bcm_port_ability_t an_ability;
    bcm_port_if_t an_interface;
    rv = bcm_stk_modid_get(0, &modid);
    for (n = 0; n < (single_front ? 1 : 2) && rv == 0 && (fe100_test < 2 || fe100_test == 4 || single_front); n++) {
        src = n == 0 ? 8 : (fe100_test == 4 ? 20 : (fe100_test ? 3 : 7));
        dst = n == 0 ? (fe100_test == 4 ? 20 : (fe100_test ? 3 : 16)) : 8;
        if (fe100_test == 9) dst = 16;
        if (fe100_test == 12) dst = 7;
        if (fe100_test == 15) dst = 14;
        if (fe100_test == 16) dst = 28;
        if (fe100_test == 23) dst = 34;
        if (fe100_test == 24) dst = 35;
        rv = bcm_port_get(0, dst, &flags, &intf, &mapping);
        if (rv) break;
        printf("FFN_PORT dst=%d core=%d tm=%d priorities=%d modid=%d\n", dst, mapping.core, mapping.tm_port, mapping.num_priorities, modid);
        BCM_COSQ_GPORT_E2E_PORT_SET(e2e, dst);
        tmport = mapping.tm_port;
        destination_module = modid + mapping.core;
        BCM_GPORT_MODPORT_SET(modport, destination_module, tmport);
        rv = bcm_stk_gport_sysport_get(0, modport, &sysport);
        if (rv) break;
        cfg.flags = BCM_COSQ_GPORT_VOQ_CONNECTOR;
        cfg.port = e2e;
        cfg.numq = 8;
        cfg.remote_modid = modid;
        cfg.nof_remote_cores = 2;
        connector = 0;
        rv = bcm_cosq_voq_connector_gport_add(0, &cfg, &connector);
        printf("FFN_CONNECTOR dst=%d gport=0x%x rv=%d\n", dst, connector, rv);
        if (rv) break;
        for (cos = 0; cos < 8; cos++) {
            rv = bcm_cosq_gport_sched_set(0, connector, cos, BCM_COSQ_SP0, 0);
            if (rv) break;
            rv = bcm_cosq_gport_attach(0, e2e, connector, cos);
            if (rv) break;
        }
        printf("FFN_ATTACH dst=%d queues=%d rv=%d\n", dst, cos, rv);
        if (rv) break;
        ingress.flags = BCM_COSQ_GPORT_UCAST_QUEUE_GROUP;
        ingress.port = modport;
        ingress.local_core_id = BCM_CORE_ALL;
        ingress.numq = 8;
        for (cos = 0; cos < 8; cos++) {
            ingress.queue_atrributes[cos].delay_tolerance_level = BCM_COSQ_DELAY_TOLERANCE_10G_SLOW_ENABLED;
            ingress.queue_atrributes[cos].rate_class = 0;
        }
        voq = 0;
        rv = bcm_cosq_ingress_queue_bundle_gport_add(0, &ingress, &voq);
        printf("FFN_VOQ dst=%d sysport=0x%x gport=0x%x rv=%d\n", dst, sysport, voq, rv);
        if (rv) break;
        connection.flags = BCM_COSQ_GPORT_CONNECTION_INGRESS;
        connection.remote_modid = modid + mapping.core;
        connection.voq = voq;
        connection.voq_connector = connector;
        rv = bcm_cosq_gport_connection_set(0, &connection);
        if (rv) break;
        connection.flags = BCM_COSQ_GPORT_CONNECTION_EGRESS;
        connection.remote_modid = modid;
        rv = bcm_cosq_gport_connection_set(0, &connection);
        if (rv) break;
        if (!single_front) rv = bcm_port_force_forward_set(0, src, dst, 1);
        if (!single_front) printf("FFN_FORWARD src=%d dst=%d rv=%d\n", src, dst, rv);
    }
    if (fe100_test == 2 && rv == 0) rv = bcm_port_force_forward_set(0, 8, 3, 1);
    if (fe100_test == 2 && rv == 0) rv = bcm_port_force_forward_set(0, 3, 8, 1);
    if (fe100_test && fe100_test < 3 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 1);
    if (fe100_test == 3 && rv == 0) rv = bcm_port_force_forward_set(0, 8, 3, 0);
    if (fe100_test == 3 && rv == 0) rv = bcm_port_force_forward_set(0, 3, 8, 0);
    if (fe100_test == 3 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 0);
    if (fe100_test == 5 && rv == 0) rv = bcm_port_force_forward_set(0, 8, 20, 1);
    if (fe100_test == 5 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 1);
    if (fe100_test == 6 && rv == 0) rv = bcm_port_force_forward_set(0, 8, 20, 0);
    if (fe100_test == 6 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 0);
    if (fe100_test == 7 && rv == 0) rv = bcm_switch_control_port_set(0, 20, bcmSwitchPortHeaderType, BCM_SWITCH_PORT_HEADER_TYPE_RAW);
    if (fe100_test == 7 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 1);
    if (fe100_test == 8 && rv == 0) rv = bcm_switch_control_port_set(0, 20, bcmSwitchPortHeaderType, BCM_SWITCH_PORT_HEADER_TYPE_TM);
    if (fe100_test == 9 && rv == 0) rv = bcm_port_force_forward_set(0, 7, 8, 1);
    if (fe100_test == 10 && rv == 0) rv = bcm_port_force_forward_set(0, 7, 8, 0);
    if (fe100_test == 11 && rv == 0) rv = bcm_port_force_forward_set(0, 7, 3, 1);
    if (fe100_test == 11 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 1);
    if (fe100_test == 13 && rv == 0) rv = bcm_port_force_forward_set(0, 7, 3, 1);
    if (fe100_test == 13 && rv == 0) rv = bcm_port_force_forward_set(0, 16, 3, 1);
    if (fe100_test == 13 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 1);
    if (fe100_test == 14 && rv == 0) rv = bcm_port_force_forward_set(0, 7, 3, 0);
    if (fe100_test == 14 && rv == 0) rv = bcm_port_force_forward_set(0, 16, 3, 0);
    if (fe100_test == 14 && rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, 0);
    if (fe100_test == 17 || fe100_test == 18) {
        if (rv == 0) rv = bcm_port_force_forward_set(0, 28, 3, fe100_test == 17);
        if (rv == 0) rv = bcm_port_force_forward_set(0, 14, 3, fe100_test == 17);
        if (rv == 0) rv = bcm_port_force_forward_set(0, 16, 3, fe100_test == 17);
        if (rv == 0) rv = bcm_port_force_forward_set(0, 7, 3, fe100_test == 17);
        if (rv == 0) rv = bcm_port_force_forward_set(0, 20, 8, fe100_test == 17);
    }
    if (fe100_test == 19) {
        bshell(0, "led 0");
        bshell(0, "led 1");
        bshell(0, "led 2");
        bshell(0, "getreg CMIC_LEDUP0_CTRL");
        bshell(0, "getreg CMIC_LEDUP1_CTRL");
        bshell(0, "getreg CMIC_LEDUP2_CTRL");
        bshell(0, "getreg CMIC_LEDUP0_CLK_DIV");
        bshell(0, "getreg CMIC_LEDUP1_CLK_DIV");
        bshell(0, "getreg CMIC_LEDUP2_CLK_DIV");
        bshell(0, "getreg CMIC_LEDUP0_CLK_PARAMS");
        bshell(0, "getreg CMIC_LEDUP1_CLK_PARAMS");
        bshell(0, "getreg CMIC_LEDUP2_CLK_PARAMS");
        bshell(0, "led 0 dump");
        bshell(0, "led 1 dump");
        bshell(0, "led 2 dump");
    }
    if (fe100_test >= 20 && fe100_test <= 22) {
        for (n = 0; n < 4; n++) {
            if (fe100_test != 20) {
                rv = bcm_port_autoneg_set(0, an_ports[n], fe100_test == 21);
                printf("FFN_AN_SET front=%d bcm=%d enable=%d rv=%d\n", an_front[n], an_ports[n], fe100_test == 21, rv);
                if (rv) break;
            }
            rv = bcm_port_autoneg_get(0, an_ports[n], &an_value);
            if (rv) break;
            rv = bcm_port_link_status_get(0, an_ports[n], &an_link);
            if (rv) break;
            rv = bcm_port_speed_get(0, an_ports[n], &an_speed);
            if (rv) break;
            rv = bcm_port_interface_get(0, an_ports[n], &an_interface);
            if (rv) break;
            printf("FFN_AN_STATUS front=%d bcm=%d an=%d link=%d speed=%d interface=%d\n", an_front[n], an_ports[n], an_value, an_link, an_speed, an_interface);
            rv = bcm_port_force_forward_get(0, an_ports[n], &dst, &an_value);
            printf("FFN_AN_FORWARD front=%d dest=%d enabled=%d rv=%d\n", an_front[n], dst, an_value, rv);
            if (rv) break;
            rv = bcm_port_ability_local_get(0, an_ports[n], &an_ability);
            printf("FFN_AN_LOCAL front=%d rv=%d\n", an_front[n], rv);
            if (rv) break;
            print an_ability;
            rv = bcm_port_ability_advert_get(0, an_ports[n], &an_ability);
            printf("FFN_AN_ADVERT front=%d rv=%d\n", an_front[n], rv);
            if (rv) break;
            print an_ability;
            an_value = bcm_port_ability_remote_get(0, an_ports[n], &an_ability);
            printf("FFN_AN_REMOTE front=%d rv=%d\n", an_front[n], an_value);
            if (an_value == 0) print an_ability;
        }
    }
    if (fe100_test == 25 || fe100_test == 26) {
        rv = bcm_port_force_forward_set(0, 34, 8, fe100_test == 25);
        if (rv == 0) rv = bcm_port_force_forward_set(0, 35, 8, fe100_test == 25);
    }
    if (fe100_test == 27 || fe100_test == 28) {
        for (n = 2; n < 4; n++) {
            rv = bcm_port_autoneg_set(0, an_ports[n], 0);
            if (rv) break;
            rv = bcm_port_ability_advert_get(0, an_ports[n], &an_ability);
            if (rv) break;
            /* Restore mask is the captured pre-test value on both ports.
             * Recheck the saved baseline before reusing on another board. */
            an_ability.speed_full_duplex = fe100_test == 27 ? BCM_PORT_ABILITY_40GB : 0x5000008;
            rv = bcm_port_ability_advert_set(0, an_ports[n], &an_ability);
            printf("FFN_AN_ADVERT_SET front=%d mask=0x%x rv=%d\n", an_front[n], an_ability.speed_full_duplex, rv);
            if (rv) break;
            rv = bcm_port_autoneg_set(0, an_ports[n], 1);
            if (rv) break;
        }
    }
    if (rv) printf("FFN_FAIL rv=%d\n", rv);
    else print "FFN_DONE";
}
