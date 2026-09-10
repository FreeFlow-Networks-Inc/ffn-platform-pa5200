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
    int hw_group = -1;
    int hw_entry = -1;
    int hw_stat = -1;
    int hw_dq1 = -1;
    int hw_dq2 = -1;
    int hw_presel = -1;
    bcm_field_group_config_t hw_group_cfg;
    bcm_field_presel_set_t hw_presels;
    bcm_field_data_qualifier_t hw_data;
    uint8 hw_bytes1[4] = {0x02, 0x52, 0x20, 0xab};
    uint8 hw_bytes2[4] = {0xcd, 0x91, 0x88, 0xb5};
    uint8 hw_bytes_mask[4] = {0xff, 0xff, 0xff, 0xff};
    bcm_field_qset_t hw_qset;
    bcm_field_aset_t hw_aset;
    bcm_field_stat_t hw_counter = bcmFieldStatPackets;
    uint64 hw_packets;
    bcm_mac_t hw_mac = {0x02, 0x52, 0x20, 0xab, 0xcd, 0x91};
    bcm_mac_t hw_mask = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
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
    if ((fe100_test == 29 || fe100_test == 34 || fe100_test == 35) && rv == 0) {
        /* Exact lab flow only: front1, experimental EtherType, synthetic SA.
         * Redirect directly to front5's existing VOQ. No global SDK reinit.
         */
        BCM_FIELD_QSET_INIT(hw_qset);
        BCM_FIELD_QSET_ADD(hw_qset, bcmFieldQualifyStageIngress);
        BCM_FIELD_QSET_ADD(hw_qset, bcmFieldQualifyInPort);
        if (fe100_test == 34 || fe100_test == 35) {
            /* Owner front ports use RAW ingress. Match source MAC and
             * EtherType by byte offsets instead of parsed L2 metadata. */
            bcm_field_data_qualifier_t_init(&hw_data);
            hw_data.offset_base = bcmFieldDataOffsetBasePacketStart;
            hw_data.offset = 6;
            hw_data.length = 4;
            rv = bcm_field_data_qualifier_create(0, &hw_data);
            if (rv == 0) {
                hw_dq1 = hw_data.qual_id;
                rv = bcm_field_qset_data_qualifier_add(0, &hw_qset, hw_dq1);
            }
            bcm_field_data_qualifier_t_init(&hw_data);
            hw_data.offset_base = bcmFieldDataOffsetBasePacketStart;
            hw_data.offset = 10;
            hw_data.length = 4;
            if (rv == 0) rv = bcm_field_data_qualifier_create(0, &hw_data);
            if (rv == 0) {
                hw_dq2 = hw_data.qual_id;
                rv = bcm_field_qset_data_qualifier_add(0, &hw_qset, hw_dq2);
            }
        } else {
            BCM_FIELD_QSET_ADD(hw_qset, bcmFieldQualifyEtherType);
            BCM_FIELD_QSET_ADD(hw_qset, bcmFieldQualifySrcMac);
        }
        if (fe100_test == 35) {
            /* SDK TM examples require an explicit TM program preselector. */
            if (rv == 0 && soc_property_get(0, "field_presel_mgmt_advanced_mode", 0)) rv = -16;
            if (rv == 0) rv = bcm_field_presel_create(0, &hw_presel);
            if (rv == 0) rv = bcm_field_qualify_ForwardingType(0, hw_presel | BCM_FIELD_QUALIFY_PRESEL, bcmFieldForwardingTypeTrafficManagement);
            if (rv == 0) {
                BCM_FIELD_PRESEL_INIT(hw_presels);
                BCM_FIELD_PRESEL_ADD(hw_presels, hw_presel);
                bcm_field_group_config_t_init(&hw_group_cfg);
                hw_group_cfg.flags = BCM_FIELD_GROUP_CREATE_WITH_PRESELSET;
                hw_group_cfg.qset = hw_qset;
                hw_group_cfg.preselset = hw_presels;
                hw_group_cfg.priority = 20;
                rv = bcm_field_group_config_create(0, &hw_group_cfg);
                if (rv == 0) hw_group = hw_group_cfg.group;
            }
        } else {
            if (rv == 0) rv = bcm_field_group_create(0, hw_qset, 20, &hw_group);
        }
        if (rv == 0) {
            BCM_FIELD_ASET_INIT(hw_aset);
            BCM_FIELD_ASET_ADD(hw_aset, bcmFieldActionRedirect);
            rv = bcm_field_group_action_set(0, hw_group, hw_aset);
        }
        if (rv == 0) rv = bcm_field_entry_create(0, hw_group, &hw_entry);
        if (rv == 0) rv = bcm_field_qualify_InPort(0, hw_entry, 28, 0xffffffff);
        if (fe100_test == 34 || fe100_test == 35) {
            if (rv == 0) rv = bcm_field_qualify_data(0, hw_entry, hw_dq1, hw_bytes1, hw_bytes_mask, 4);
            if (rv == 0) rv = bcm_field_qualify_data(0, hw_entry, hw_dq2, hw_bytes2, hw_bytes_mask, 4);
        } else {
            if (rv == 0) rv = bcm_field_qualify_EtherType(0, hw_entry, 0x88b5, 0xffff);
            if (rv == 0) rv = bcm_field_qualify_SrcMac(0, hw_entry, hw_mac, hw_mask);
        }
        BCM_GPORT_SYSTEM_PORT_ID_SET(dst, 16);
        if (rv == 0) rv = bcm_field_action_add(0, hw_entry, bcmFieldActionRedirect, 0, dst);
        if (rv == 0) rv = bcm_field_stat_create(0, hw_group, 1, &hw_counter, &hw_stat);
        if (rv == -15) {
            /* Current owner config has no PMF counter processor. Packet-path
             * evidence is still possible; never claim a flow counter exists. */
            print "FFN_HW_COUNTER_UNAVAILABLE";
            hw_stat = -1;
            rv = 0;
        }
        if (rv == 0 && hw_stat >= 0) rv = bcm_field_entry_stat_attach(0, hw_entry, hw_stat);
        if (rv == 0) rv = bcm_field_entry_install(0, hw_entry);
        printf("FFN_HW_RULE group=%d entry=%d stat=%d rv=%d\n", hw_group, hw_entry, hw_stat, rv);
        printf("FFN_HW_DATA dq1=%d dq2=%d\n", hw_dq1, hw_dq2);
        printf("FFN_HW_PRESEL presel=%d\n", hw_presel);
        if (rv != 0) {
            if (hw_entry >= 0) bcm_field_entry_destroy(0, hw_entry);
            if (hw_stat >= 0) bcm_field_stat_destroy(0, hw_stat);
            if (hw_group >= 0) bcm_field_group_destroy(0, hw_group);
            if (hw_dq1 >= 0) bcm_field_data_qualifier_destroy(0, hw_dq1);
            if (hw_dq2 >= 0) bcm_field_data_qualifier_destroy(0, hw_dq2);
            if (hw_presel >= 0) bcm_field_presel_destroy(0, hw_presel);
        }
    }
    if (fe100_test == 30 && hw_group >= 0 && hw_entry >= 0) {
        rv = bcm_field_entry_destroy(0, hw_entry);
        if (rv == 0 && hw_stat >= 0) rv = bcm_field_stat_destroy(0, hw_stat);
        if (rv == 0) rv = bcm_field_group_destroy(0, hw_group);
        if (rv == 0 && hw_dq1 >= 0) rv = bcm_field_data_qualifier_destroy(0, hw_dq1);
        if (rv == 0 && hw_dq2 >= 0) rv = bcm_field_data_qualifier_destroy(0, hw_dq2);
        if (rv == 0 && hw_presel >= 0) rv = bcm_field_presel_destroy(0, hw_presel);
    }
    if (fe100_test == 31 && hw_stat >= 0) {
        rv = bcm_field_stat_get(0, hw_stat, bcmFieldStatPackets, &hw_packets);
        if (rv == 0) print hw_packets;
    }
    /* Commissioning only: release/restore front1's pre-PMF force-forward
     * trap. Restore immediately after the isolated exact-flow test. */
    if (fe100_test == 32) rv = bcm_port_force_forward_set(0, 28, 3, 0);
    if (fe100_test == 33) rv = bcm_port_force_forward_set(0, 28, 3, 1);
    if (rv) printf("FFN_FAIL rv=%d\n", rv);
    else print "FFN_DONE";
}
