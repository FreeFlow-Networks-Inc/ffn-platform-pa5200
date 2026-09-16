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
int ffn_dp_queue_inventory(int unit, int port, int numq, uint32 flags, int gport, void *data)
{
    int rc;
    int *count=data;
    /* DPP traverse deliberately supplies zero port/flags. Query each VOQ. */
    if (BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
        if (count!=NULL) *count=*count+1;
        rc=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);
        printf("FFN_DP_VOQ physical=0x%x gport=0x%x queues=%d flags=0x%x rv=%d\n",port,gport,numq,flags,rc);
        if (rc) return rc;
    }
    return 0;
}
int ffn_session_queue_preflight(int unit, int port, int numq, uint32 flags, int gport, void *data)
{
    int rc;
    if (BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
        rc=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);
        if (rc) return rc;
        /* The verified MODPORT encoding contains the local port in bits10:0.
         * Never allocate over either existing FE100 or MP queue bundle. */
        if ((port & 0x7ff)==3 || (port & 0x7ff)==8) return -8;
    }
    return 0;
}
int ffn_copper4_queue_preflight(int unit, int port, int numq, uint32 flags, int gport, void *data)
{
    int rc;
    if (BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
        rc=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);
        if(rc)return rc;
        if((port & 0x7ff)==15)return -8;
    }
    return 0;
}
int ffn_copper_return_preflight(int unit, int port, int numq, uint32 flags, int gport, void *data)
{
    int rc;
    int *found=data;
    if (BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {
        rc=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);
        if(rc)return rc;
        if(numq!=8)return 0;
        if((port & 0x7ff)==14)*found=*found | 1;
        if((port & 0x7ff)==15)*found=*found | 2;
        if((port & 0x7ff)==24)*found=*found | 4;
    }
    return 0;
}
{
    int rv = 0;
    int fe100_test = 0;
    int single_front = fe100_test == 9 || fe100_test == 12 || fe100_test == 15 || fe100_test == 16 || fe100_test == 23 || fe100_test == 24 || fe100_test == 37 || fe100_test == 64;
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
    int dp_ports[7] = {24,28,14,16,7,34,35};
    int session_ports[8] = {3,7,8,16,20,24,14,28};
    int hw_cross = fe100_test>=55 && fe100_test<=58;
    int dp_header;
    int dp_queue_count=0;
    int copper_queues=0;
    int copper_previous[2];
    int copper_changed=0;
    int copper_rollback;
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
    int hw_trap = -1;
    bcm_rx_trap_config_t hw_trap_cfg;
    bcm_field_group_config_t hw_group_cfg;
    bcm_field_presel_set_t hw_presels;
    bcm_field_data_qualifier_t hw_data;
    uint8 hw_bytes1[4] = {0x02, 0x52, 0x20, 0xab};
    uint8 hw_bytes2[4] = {0xcd, 0x91, 0x88, 0xb5};
    uint8 hw_bytes_mask[4] = {0xff, 0xff, 0xff, 0xff};
    uint8 hw_bytes1_mask[4] = {0xff, 0xff, 0xff, 0xff};
    bcm_field_qset_t hw_qset;
    bcm_field_qset_t hw_existing_qset;
    bcm_field_aset_t hw_aset;
    bcm_field_stat_t hw_counter = bcmFieldStatPackets;
    uint64 hw_packets;
    bcm_mac_t hw_mac = {0x02, 0x52, 0x20, 0xab, 0xcd, 0x91};
    bcm_mac_t hw_mask = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
    rv = bcm_stk_modid_get(0, &modid);
    if(fe100_test==64 && rv==0) {
        rv=bcm_cosq_gport_traverse(0,ffn_copper4_queue_preflight,NULL);
        printf("FFN_COPPER4_QUEUE_PREFLIGHT rv=%d\n",rv);
    }
    if (fe100_test == 37 && rv==0) {
        /* Dedicated commissioning allocation: never overwrite/adopt queues
         * from an existing owner or retry a partially completed allocation. */
        rv=bcm_cosq_gport_traverse(0,ffn_dp_queue_inventory,&dp_queue_count);
        if (rv==0 && dp_queue_count!=0) rv=-8;
        printf("FFN_DP_ALLOCATION_PREFLIGHT queues=%d rv=%d\n",dp_queue_count,rv);
    }
    if (fe100_test==46 && rv==0) {
        rv=bcm_cosq_gport_traverse(0,ffn_session_queue_preflight,NULL);
        printf("FFN_SESSION_QUEUE_PREFLIGHT rv=%d\n",rv);
    }
    for (n = 0; n < (fe100_test == 37 ? 7 : (single_front ? 1 : 2)) && rv == 0 && (fe100_test < 2 || fe100_test == 4 || fe100_test==46 || single_front); n++) {
        src = n == 0 ? 8 : (fe100_test == 4 ? 20 : (fe100_test ? 3 : 7));
        dst = n == 0 ? (fe100_test == 4 ? 20 : (fe100_test ? 3 : 16)) : 8;
        if (fe100_test == 9) dst = 16;
        if (fe100_test == 12) dst = 7;
        if (fe100_test == 15) dst = 14;
        if (fe100_test == 16) dst = 28;
        if (fe100_test == 64) dst = 15;
        if (fe100_test == 23) dst = 34;
        if (fe100_test == 24) dst = 35;
        if (fe100_test == 37) dst = dp_ports[n];
        if (fe100_test == 46) dst = n==0 ? 3 : 8;
        rv = bcm_port_get(0, dst, &flags, &intf, &mapping);
        if (rv) break;
        if(fe100_test==64 && (mapping.core!=1 || mapping.tm_port!=15)) {rv=-1;break;}
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
        if (!single_front && fe100_test!=46) rv = bcm_port_force_forward_set(0, src, dst, 1);
        if (!single_front && fe100_test!=46) printf("FFN_FORWARD src=%d dst=%d rv=%d\n", src, dst, rv);
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
    if ((fe100_test == 29 || fe100_test == 34 || fe100_test == 35 || fe100_test==53 || fe100_test==54 || hw_cross) && rv == 0) {
        /* Exact lab flow only: front1, experimental EtherType, synthetic SA.
         * Redirect directly to front5's existing VOQ. No global SDK reinit.
         */
        /* Distinct-port DAC test: exact destination MAC plus source prefix.
         * Original probes go to FE100; rewritten returns go to DP capture. */
        if (hw_cross) {
            hw_bytes1[0]=0x02;hw_bytes1[1]=fe100_test<=56 ? 0xff : 0x52;
            hw_bytes1[2]=fe100_test<=56 ? 0 : 0x20;hw_bytes1[3]=fe100_test<=56 ? 0 : 0xab;
            hw_bytes2[0]=fe100_test<=56 ? 0 : 0xcd;hw_bytes2[1]=fe100_test<=56 ? 2 : 0xee;
            hw_bytes2[2]=0x02;hw_bytes2[3]=0xff;
        }
        BCM_FIELD_QSET_INIT(hw_qset);
        BCM_FIELD_QSET_ADD(hw_qset, bcmFieldQualifyStageIngress);
        BCM_FIELD_QSET_ADD(hw_qset, bcmFieldQualifyInPort);
        if (fe100_test==53 || fe100_test==54) {
            /* FE100 XF emits Marvell DSA on NIF. Match its exact synthetic
             * destination MAC and target tag before choosing the front VOQ.
             * RAW_DSA egress removes the eight-byte tag on the front port. */
            hw_bytes1[0]=0xc0;hw_bytes1[1]=0;hw_bytes1[2]=0x10;hw_bytes1[3]=0;
            hw_bytes2[0]=0;hw_bytes2[1]=0;
            hw_bytes2[2]=fe100_test==53 ? 0 : 2;
            hw_bytes2[3]=fe100_test==53 ? 0xe0 : 0;
            hw_mac[5]=0xee;
            /* Allow tagged/untagged test egress and its VLAN field. The
             * synthetic DA, NIF ingress and destination port remain exact. */
            hw_bytes1_mask[0]=0xdf;hw_bytes1_mask[2]=0xf0;hw_bytes1_mask[3]=0;
            BCM_FIELD_QSET_ADD(hw_qset,bcmFieldQualifyDstMac);
        }
        if (fe100_test == 34 || fe100_test == 35 || fe100_test==53 || fe100_test==54 || hw_cross) {
            /* Owner front ports use RAW ingress. Match source MAC and
             * EtherType by byte offsets instead of parsed L2 metadata. */
            bcm_field_data_qualifier_t_init(&hw_data);
            hw_data.offset_base = bcmFieldDataOffsetBasePacketStart;
            hw_data.offset = hw_cross ? 0 : (fe100_test>=53 ? 12 : 6);
            hw_data.length = 4;
            rv = bcm_field_data_qualifier_create(0, &hw_data);
            if (rv == 0) {
                hw_dq1 = hw_data.qual_id;
                rv = bcm_field_qset_data_qualifier_add(0, &hw_qset, hw_dq1);
            }
            bcm_field_data_qualifier_t_init(&hw_data);
            hw_data.offset_base = bcmFieldDataOffsetBasePacketStart;
            hw_data.offset = hw_cross ? 4 : (fe100_test>=53 ? 16 : 10);
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
                hw_group_cfg.priority = hw_cross ? fe100_test : 20;
                rv = bcm_field_group_config_create(0, &hw_group_cfg);
                if (rv == 0) hw_group = hw_group_cfg.group;
            }
        } else {
            if (rv == 0 && hw_cross) {
                /* A low automatic ID collided with an allocated TCAM DB.
                 * Never adopt or delete it. Reserve an explicitly absent lab
                 * group and let the SDK reject any underlying conflict. */
                rv=bcm_field_group_get(0,fe100_test,&hw_existing_qset);
                if(rv==0) rv=-8;
                else if(rv==-7) {
                    rv=bcm_field_group_create_id(0,hw_qset,fe100_test,fe100_test);
                    if(rv==0) hw_group=fe100_test;
                }
            } else if (rv == 0) rv = bcm_field_group_create(0, hw_qset, 20, &hw_group);
        }
        if (rv == 0) {
            BCM_FIELD_ASET_INIT(hw_aset);
            if(hw_cross) BCM_FIELD_ASET_ADD(hw_aset,bcmFieldActionTrap);
            else BCM_FIELD_ASET_ADD(hw_aset, bcmFieldActionRedirect);
            rv = bcm_field_group_action_set(0, hw_group, hw_aset);
        }
        if (rv == 0) rv = bcm_field_entry_create(0, hw_group, &hw_entry);
        if (rv == 0) rv = bcm_field_qualify_InPort(0, hw_entry, hw_cross ? (fe100_test==55 || fe100_test==57 ? 7 : 16) : (fe100_test>=53 ? 3 : 28), 0xffffffff);
        if (rv==0 && (fe100_test==53 || fe100_test==54)) rv=bcm_field_qualify_DstMac(0,hw_entry,hw_mac,hw_mask);
        if (fe100_test == 34 || fe100_test == 35 || fe100_test==53 || fe100_test==54 || hw_cross) {
            if (rv == 0) rv = bcm_field_qualify_data(0, hw_entry, hw_dq1, hw_bytes1, hw_bytes1_mask, 4);
            if (rv == 0) rv = bcm_field_qualify_data(0, hw_entry, hw_dq2, hw_bytes2, hw_bytes_mask, 4);
        } else {
            if (rv == 0) rv = bcm_field_qualify_EtherType(0, hw_entry, 0x88b5, 0xffff);
            if (rv == 0) rv = bcm_field_qualify_SrcMac(0, hw_entry, hw_mac, hw_mask);
        }
        src=hw_cross ? (fe100_test<=56 ? 3 : 24) : (fe100_test==53 ? 7 : 16);
        BCM_GPORT_SYSTEM_PORT_ID_SET(dst, src);
        if(rv==0 && hw_cross) {
            /* Experimental RAW ingress: reproduce control/trap flags from
             * the SDK port-force-forward implementation. This has not yet
             * qualified packet delivery through the RAW ingress pipeline. */
            rv=bcm_rx_trap_type_create(0,0,bcmRxTrapUserDefine,&hw_trap);
            bcm_rx_trap_config_t_init(&hw_trap_cfg);
            hw_trap_cfg.flags=BCM_RX_TRAP_UPDATE_DEST|BCM_RX_TRAP_TRAP|BCM_RX_TRAP_BYPASS_FILTERS|BCM_RX_TRAP_LEARN_DISABLE;
            hw_trap_cfg.dest_port=dst;
            if(rv==0) rv=bcm_rx_trap_set(0,hw_trap,&hw_trap_cfg);
            BCM_GPORT_TRAP_SET(dst,hw_trap,7,0);
            if(rv==0) rv=bcm_field_action_add(0,hw_entry,bcmFieldActionTrap,dst,0);
        } else if (rv == 0) rv = bcm_field_action_add(0, hw_entry, bcmFieldActionRedirect, 0, dst);
        if (rv == 0) {
          rv = bcm_field_stat_create(0, hw_group, 1, &hw_counter, &hw_stat);
          if (rv == -15) {
            /* Current owner config has no PMF counter processor. Packet-path
             * evidence is still possible; never claim a flow counter exists. */
            print "FFN_HW_COUNTER_UNAVAILABLE";
            hw_stat = -1;
            rv = 0;
          }
        }
        if (rv == 0 && hw_stat >= 0) rv = bcm_field_entry_stat_attach(0, hw_entry, hw_stat);
        if (rv == 0) rv = bcm_field_entry_install(0, hw_entry);
        printf("FFN_HW_RULE group=%d entry=%d stat=%d rv=%d\n", hw_group, hw_entry, hw_stat, rv);
        printf("FFN_HW_DATA dq1=%d dq2=%d\n", hw_dq1, hw_dq2);
        printf("FFN_HW_PRESEL presel=%d\n", hw_presel);
        printf("FFN_HW_TRAP trap=%d\n",hw_trap);
        if (rv != 0) {
            if (hw_entry >= 0) bcm_field_entry_destroy(0, hw_entry);
            if (hw_stat >= 0) bcm_field_stat_destroy(0, hw_stat);
            if (hw_group >= 0) bcm_field_group_destroy(0, hw_group);
            if (hw_dq1 >= 0) bcm_field_data_qualifier_destroy(0, hw_dq1);
            if (hw_dq2 >= 0) bcm_field_data_qualifier_destroy(0, hw_dq2);
            if (hw_presel >= 0) bcm_field_presel_destroy(0, hw_presel);
            if (hw_trap >= 0) bcm_rx_trap_type_destroy(0,hw_trap);
        }
    }
    if (fe100_test == 30 && hw_group >= 0 && hw_entry >= 0) {
        rv = bcm_field_entry_destroy(0, hw_entry);
        if (rv == 0 && hw_stat >= 0) rv = bcm_field_stat_destroy(0, hw_stat);
        if (rv == 0) rv = bcm_field_group_destroy(0, hw_group);
        if (rv == 0 && hw_dq1 >= 0) rv = bcm_field_data_qualifier_destroy(0, hw_dq1);
        if (rv == 0 && hw_dq2 >= 0) rv = bcm_field_data_qualifier_destroy(0, hw_dq2);
        if (rv == 0 && hw_presel >= 0) rv = bcm_field_presel_destroy(0, hw_presel);
        if (rv == 0 && hw_trap >= 0) rv = bcm_rx_trap_type_destroy(0,hw_trap);
    }
    if (fe100_test == 31 && hw_stat >= 0) {
        rv = bcm_field_stat_get(0, hw_stat, bcmFieldStatPackets, &hw_packets);
        if (rv == 0) print hw_packets;
    }
    if (fe100_test == 63 && hw_group >= 0) {
        an_value=bcm_field_group_get(0,hw_group,&hw_existing_qset);
        printf("FFN_GROUP_ABSENT group=%d rv=%d\n",hw_group,an_value);
        if(an_value!=-7) rv=an_value==0 ? -8 : an_value;
    }
    /* Commissioning only: release/restore front1's pre-PMF force-forward
     * trap. Restore immediately after the isolated exact-flow test. */
    if (fe100_test == 32) rv = bcm_port_force_forward_set(0, 28, 3, 0);
    if (fe100_test == 33) rv = bcm_port_force_forward_set(0, 28, 3, 1);
    if (fe100_test == 38) {
        rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&dp_header);
        if (rv==0 && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_ETH && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_TM) rv=-15;
        if (rv==0 && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_TM)
            rv=bcm_switch_control_port_set(0,24,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_TM);
        if (rv==0) rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&dp_header);
        printf("FFN_DP_HEADER_SET port=24 type=%d rv=%d\n",dp_header,rv);
    }
    if (fe100_test == 41) {
        /* Source-system-port extension is required to demultiplex RX. */
        rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&dp_header);
        if (rv==0 && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_TM && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP) rv=-15;
        if (rv==0 && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP)
            rv=bcm_switch_control_port_set(0,24,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP);
        if (rv==0) rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&dp_header);
        printf("FFN_DP_HEADER_SSP port=24 type=%d rv=%d\n",dp_header,rv);
    }
    if (fe100_test == 42) {
        bshell(0,"getreg XLMAC_RX_CTRL.xl24");
        bshell(0,"getreg XLMAC_TX_CTRL.xl24");
        bshell(0,"getreg XLMAC_RX_CTRL.xe16");
        bshell(0,"getreg XLMAC_TX_CTRL.xe16");
    }
    if (fe100_test == 39 || fe100_test == 40) {
        /* Only the observed quiet 5<->13 DAC pair. Do not redirect unrelated
         * live ingress; both ports had force-forward disabled at preflight. */
        for (n=0;n<2 && rv==0;n++) {
            rv=bcm_port_force_forward_get(0,an_ports[n],&dst,&an_value);
            if (rv==0 && an_value && dst!=24) rv=-8;
        }
        for (n=0;n<2 && rv==0;n++) {
            rv=bcm_port_force_forward_set(0,an_ports[n],24,fe100_test==39);
            if (rv==0) rv=bcm_port_force_forward_get(0,an_ports[n],&dst,&an_value);
            printf("FFN_DP_RETURN port=%d destination=%d enabled=%d rv=%d\n",an_ports[n],dst,an_value,rv);
        }
    }
    if(fe100_test==65 || fe100_test==66) {
        /* Only the confirmed copper 3<->4 cable. Never touch WAN/BCM28.
         * Ingress goes to the DP, never back to the cabled peer. */
        if(fe100_test==65 && rv==0) {
            rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&dp_header);
            if(rv==0 && dp_header!=BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP)rv=-1;
            if(rv==0)rv=bcm_cosq_gport_traverse(0,ffn_copper_return_preflight,&copper_queues);
            if(rv==0 && copper_queues!=7)rv=-1;
            printf("FFN_COPPER_RETURN_PREFLIGHT queues=%d rv=%d\n",copper_queues,rv);
        }
        for(n=14;n<=15 && rv==0;n++) {
            rv=bcm_port_force_forward_get(0,n,&dst,&an_value);
            if(rv==0 && an_value && dst!=24)rv=-8;
            copper_previous[n-14]=an_value;
        }
        for(n=14;n<=15 && rv==0;n++) {
            /* Include the attempted port in rollback if the SDK errors. */
            copper_changed=n-13;
            rv=bcm_port_force_forward_set(0,n,24,fe100_test==65);
            if(rv==0)rv=bcm_port_force_forward_get(0,n,&dst,&an_value);
            printf("FFN_COPPER_RETURN port=%d destination=%d enabled=%d rv=%d\n",n,dst,an_value,rv);
            if(rv==0 && (an_value!=(fe100_test==65) || (an_value && dst!=24)))rv=-1;
        }
        if(rv && copper_changed) {
            for(n=0;n<copper_changed;n++) {
                copper_rollback=bcm_port_force_forward_set(0,n+14,24,copper_previous[n]);
                printf("FFN_COPPER_RETURN_ROLLBACK port=%d enabled=%d rv=%d\n",n+14,copper_previous[n],copper_rollback);
            }
        }
    }
    if (fe100_test>=59 && fe100_test<=62) {
        src=fe100_test==59 || fe100_test==61 ? 7 : 16;
        cos=fe100_test>=61;
        rv=bcm_port_force_forward_get(0,src,&dst,&an_value);
        if(rv==0 && (cos ? (an_value && dst!=24) : (!an_value || dst!=24))) rv=-8;
        if(rv==0) rv=bcm_port_force_forward_set(0,src,24,cos);
        if(rv==0) rv=bcm_port_force_forward_get(0,src,&dst,&an_value);
        printf("FFN_CROSS_FORCE port=%d destination=%d enabled=%d rv=%d\n",src,dst,an_value,rv);
        if(rv==0 && (an_value!=cos || (cos && dst!=24))) rv=-1;
    }
    if (fe100_test == 43) {
        rv=bcm_cosq_gport_traverse(0,ffn_dp_queue_inventory,NULL);
        for (n=0;n<8 && rv==0;n++) {
            rv=bcm_port_force_forward_get(0,session_ports[n],&dst,&an_value);
            printf("FFN_SESSION_PATH port=%d destination=%d enabled=%d rv=%d\n",session_ports[n],dst,an_value,rv);
            if (rv==0) rv=bcm_port_get(0,session_ports[n],&flags,&intf,&mapping);
            if (rv==0) {
                destination_module=modid+mapping.core;
                tmport=mapping.tm_port;
                BCM_GPORT_MODPORT_SET(modport,destination_module,tmport);
                rv=bcm_stk_gport_sysport_get(0,modport,&sysport);
            }
            if (rv==0) {
                printf("FFN_SESSION_SYSTEM port=%d sysport=0x%x\n",session_ports[n],sysport);
                an_value=bcm_switch_control_port_get(0,session_ports[n],bcmSwitchPortHeaderType,&dp_header);
                printf("FFN_SESSION_HEADER port=%d type=%d rv=%d\n",session_ports[n],dp_header,an_value);
            }
        }
    }
    if (fe100_test == 44 || fe100_test == 45 || fe100_test==51 || fe100_test==52) {
        /* Isolated 5->DAC->13->FE100->MP capture. Leave all other routes
         * intact. Refuse to adopt an unexpected forwarding configuration. */
        rv=bcm_port_force_forward_get(0,20,&dst,&an_value);
        if (rv==0 && an_value) rv=-8;
        src=fe100_test>=51 ? 16 : 7;
        cos=fe100_test==44 || fe100_test==51;
        if (rv==0) rv=bcm_port_force_forward_get(0,src,&dst,&an_value);
        if (rv==0 && (!an_value || dst!=(cos ? 24 : 3))) rv=-8;
        if (rv==0) rv=bcm_port_force_forward_set(0,src,cos ? 3 : 24,1);
        if (rv==0) rv=bcm_port_force_forward_get(0,src,&dst,&an_value);
        printf("FFN_SESSION_ROUTE port=%d destination=%d enabled=%d rv=%d\n",src,dst,an_value,rv);
        if (rv==0 && (!an_value || dst!=(cos ? 3 : 24))) rv=-1;
    }
    if (fe100_test == 36) {
        /* Read-only inventory before DP transport commissioning. */
        rv=bcm_cosq_gport_traverse(0,ffn_dp_queue_inventory,NULL);
        printf("FFN_DP_TRAVERSE rv=%d\n",rv);
        for (n=0; n<7 && rv==0; n++) {
            dst=dp_ports[n];
            rv=bcm_port_get(0,dst,&flags,&intf,&mapping);
            printf("FFN_DP_PORT port=%d core=%d tm=%d priorities=%d rv=%d\n",dst,mapping.core,mapping.tm_port,mapping.num_priorities,rv);
            if (rv) break;
            tmport=mapping.tm_port;
            destination_module=modid+mapping.core;
            BCM_GPORT_MODPORT_SET(modport,destination_module,tmport);
            rv=bcm_stk_gport_sysport_get(0,modport,&sysport);
            printf("FFN_DP_SYSPORT port=%d gport=0x%x rv=%d\n",dst,sysport,rv);
            if (rv) break;
            an_value=bcm_cosq_sysport_ingress_queue_map_get(0,0,sysport,&voq);
            printf("FFN_DP_QUEUE port=%d voq=0x%x rv=%d\n",dst,voq,an_value);
            an_value=bcm_switch_control_port_get(0,dst,bcmSwitchPortHeaderType,&dp_header);
            printf("FFN_DP_HEADER port=%d type=%d rv=%d\n",dst,dp_header,an_value);
            rv=bcm_port_force_forward_get(0,dst,&src,&an_value);
            printf("FFN_DP_FORCE port=%d destination=%d enabled=%d rv=%d\n",dst,src,an_value,rv);
        }
    }
    if (rv) printf("FFN_FAIL rv=%d\n", rv);
    else print "FFN_DONE";
}
