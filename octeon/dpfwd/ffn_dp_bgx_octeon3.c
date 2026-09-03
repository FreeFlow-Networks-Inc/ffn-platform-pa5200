/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_bgx_octeon3.c -- program an OCTEON-III BGX LMAC from FFN's port table.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * ffn_dp_oct.c has always had one hook, dp_port_hw_apply(), where port state was
 * supposed to reach silicon, and under -DFFN_HAVE_CVMX it called dp_bgx_apply().
 * Nothing defined dp_bgx_apply. So the CVMX build of the dataplane did not link,
 * which nobody noticed because nobody had ever built it with an SDK present.
 * This is that function.
 *
 * WHAT IT DOES AND DOES NOT DO -- read this before believing a DP_OK
 * -----------------------------------------------------------------
 * The BGX blocks are configured for the board as a whole by
 * cvmx_helper_initialize_packet_io_global(), which reads each interface's mode
 * from the board description. This function does PER-PORT work on top of that:
 * bring the LMAC up or shut it down, set the frame-size limit, and read back
 * what the link actually negotiated. It does NOT re-key an interface's mode,
 * lane-to-SDS mapping or SerDes rate at runtime -- those are per-interface,
 * touch GSER, and would need the interface quiesced. A port whose requested
 * lmac_type disagrees with the mode the interface is actually in is REFUSED
 * rather than half-applied, because half-applying it is how an LMAC ends up
 * enabled in the wrong mode with no error anywhere.
 *
 * Consequently DP_OK here means "the LMAC was enabled or disabled and the frame
 * limit was set", not "the port is at the speed you asked for". Speed and
 * duplex are reported back from the hardware into the port table, so the MP
 * shows what the link IS, never what it was asked to be.
 *
 * LICENSING: FFN's own code calling the BSD-licensed CVMX executive API. No SDK
 * source or binary is vendored or shipped.
 */
#include "ffn_dp_bgx.h"
#include "ffn_dp_oct.h"

#ifdef FFN_HAVE_CVMX

#include "cvmx.h"
#include "cvmx-helper.h"
#include "cvmx-helper-bgx.h"
#include "cvmx-helper-util.h"

/* Is the interface in a mode where an LMAC of this type can be brought up?
 * Refusing an unsupported combination is deliberate -- see the header comment. */
static int bgx_mode_usable(cvmx_helper_interface_mode_t mode)
{
    switch (mode) {
    case CVMX_HELPER_INTERFACE_MODE_SGMII:
    case CVMX_HELPER_INTERFACE_MODE_XAUI:
    case CVMX_HELPER_INTERFACE_MODE_RXAUI:
    case CVMX_HELPER_INTERFACE_MODE_XLAUI:
    case CVMX_HELPER_INTERFACE_MODE_XFI:
    case CVMX_HELPER_INTERFACE_MODE_10G_KR:
    case CVMX_HELPER_INTERFACE_MODE_40G_KR4:
    case CVMX_HELPER_INTERFACE_MODE_MIXED:
        return 1;
    default:
        return 0;
    }
}

int dp_bgx_apply(struct dp_ctx *c, struct ffn_dp_port_raw *p)
{
    int node, xiface, xipd_port, index, phy_pres, rc;
    unsigned jabber;
    cvmx_helper_interface_mode_t mode;
    cvmx_helper_link_info_t link;

    (void)c;
    if (!p)
        return DP_ERR_RANGE;

    /* Only ports that actually have an LMAC behind them. A GHOST or INTERNAL
     * entry is a table row, not a MAC. */
    if (!(p->flags & FFN_PORT_F_HAS_LMAC))
        return DP_ERR_UNSUPP;

    /* FFN numbers BGX blocks the way the SDK numbers interfaces on a CN7XXX,
     * and this dataplane owns one node. */
    node = cvmx_get_node_num();
    xiface = cvmx_helper_node_interface_to_xiface(node, p->bgx);
    index = p->lmac;
    if (index >= cvmx_helper_ports_on_interface(xiface))
        return DP_ERR_RANGE;

    mode = cvmx_helper_bgx_get_mode(xiface, index);
    if (!bgx_mode_usable(mode))
        return DP_ERR_UNSUPP;

    xipd_port = cvmx_helper_get_ipd_port(xiface, index);
    if (xipd_port < 0)
        return DP_ERR_RANGE;

    if (!p->admin_up) {
        /* Shut the LMAC down and stop claiming a link. Report what we did, not
         * what the port used to be. */
        rc = cvmx_helper_bgx_shutdown_port(xiface, index);
        p->link_up = 0;
        st_le32(p->speed_mbps, 0);
        return (rc == 0) ? DP_OK : DP_ERR_STATE;
    }

    /* phy_addr 0xff is the ABI's "no MDIO PHY", which for a fibre LMAC is the
     * normal case, not a fault. */
    phy_pres = (p->phy_addr != 0xff);

    rc = __cvmx_helper_bgx_port_init(xipd_port, phy_pres);
    if (rc < 0)
        return DP_ERR_STATE;

    /* Frame-size limit. The ABI carries an L3 MTU; BGX counts the whole frame,
     * so add Ethernet header, VLAN tag and FCS rather than silently clipping
     * tagged traffic at the MTU. */
    jabber = (unsigned)ld_le32(p->mtu);
    if (jabber == 0)
        jabber = 1500;
    jabber += 14 /* DMAC+SMAC+ethertype */ + 4 /* one VLAN tag */ + 4 /* FCS */;
    cvmx_helper_bgx_set_jabber(xiface, (unsigned)index, jabber);

    /* Read back what the link actually is. Never write the requested speed into
     * the table: an operator debugging a port needs to see the truth. */
    link = cvmx_helper_link_get(xipd_port);
    p->link_up = link.s.link_up ? 1 : 0;
    st_le32(p->speed_mbps, (uint32_t)link.s.speed);

    return DP_OK;
}

#endif /* FFN_HAVE_CVMX */
