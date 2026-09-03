/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_bgx.h -- the one hook where DP port state reaches real silicon.
 *
 * ffn_dp_oct.c is portable and holds no chip code; when built with
 * -DFFN_HAVE_CVMX its dp_port_hw_apply() calls through here instead of
 * reporting DP_ERR_UNSUPP. The implementation lives in ffn_dp_bgx_octeon3.c.
 *
 * Contract, and the reason this is not allowed to be a stub that returns
 * success: DP_OK means the port state was ACTUALLY pushed to hardware, so the
 * port table may say RUN. Anything else leaves the port in STARTUP, which is
 * what the MP shows an operator. A hook that lied here would produce a firewall
 * reporting healthy ports that pass no traffic.
 */
#ifndef FFN_DP_BGX_H
#define FFN_DP_BGX_H

#include "ffn_dp_abi.h"

struct dp_ctx;

/* Apply `p`'s admin/link configuration to its BGX LMAC.
 * Returns DP_OK if hardware was programmed, DP_ERR_UNSUPP if this build or this
 * port cannot be driven, DP_ERR_RANGE for a port whose BGX/LMAC identity is not
 * usable, or DP_ERR_STATE if the hardware refused. */
int dp_bgx_apply(struct dp_ctx *c, struct ffn_dp_port_raw *p);

#endif /* FFN_DP_BGX_H */
