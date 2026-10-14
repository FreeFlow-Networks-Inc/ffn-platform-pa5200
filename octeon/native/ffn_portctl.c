/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_portctl.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

/* ---- board map: front faceplate port -> BCM port (commissioned PA-5220 map) ---- */

static const int SFP_PORTS[] = { 16, 1, 18, 19, 6, 21, 22, 23, 7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35 }; /* ethernet1/5..24 */
static const int COPPER_PORTS[] = { 28, 13, 14, 15 };                                                           /* ethernet1/1..4 */

int portctl_bcm_port(int front)
{
    if (front >= 1 && front <= 4) return COPPER_PORTS[front - 1];
    if (front >= 5 && front <= 24) return SFP_PORTS[front - 5];
    return 0;
}

/* ---- bounded SDK recipes (cint), identical to the Python owners' text ---- */

/* The WAN1 recipe with the port selectable; it reads the queue inventory and the
 * trunk header, applies the ingress force-forward for mode 1 (start) / 2 (stop),
 * and reads it back. Mode 0 observes only. */
static const char REDIRECT_RECIPE[] =
"\n"
"int ffn_wan_queues(int unit,int port,int numq,uint32 flags,int gport,void *data) {\n"
" int rv; int *queues=data;\n"
" if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {\n"
"  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags); if(rv)return rv;\n"
"  port=port & 0x7ff; if(port<37)queues[port]=queues[port]+numq;\n"
" }\n"
" return 0;\n"
"}\n"
"{\n"
" int q[37]={0}; int rv; int header; int dst; int enabled; int mode=@MODE@; int p=@PORT@;\n"
" rv=bcm_cosq_gport_traverse(0,ffn_wan_queues,q);\n"
" if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);\n"
" if(mode==1 && rv==0 && (header!=11 || q[p]!=8 || q[24]!=8))rv=-1;\n"
" if(rv==0)rv=bcm_port_force_forward_get(0,p,&dst,&enabled);\n"
" if(mode && rv==0 && enabled && dst!=24)rv=-8;\n"
" if(mode && rv==0)rv=bcm_port_force_forward_set(0,p,24,mode==1);\n"
" if(rv==0)rv=bcm_port_force_forward_get(0,p,&dst,&enabled);\n"
" if(mode && rv==0 && (enabled!=(mode==1) || (enabled && dst!=24)))rv=-1;\n"
" printf(\"FFN_WAN_STATE header=%d wanq=%d trunkq=%d dst=%d enabled=%d rv=%d\\n\",header,q[p],q[24],dst,enabled,rv);\n"
" if(rv==0)printf(\"FFN_WAN_DONE\\n\");\n"
"}\n";

/* The packet-fabric recipe: op 0 observes, op 1 allocates the eight VOQ/connector
 * queues of one port, op 2 moves the empty trunk 24 to the TM_SSP header. */
static const char FABRIC_RECIPE[] =
"\n"
"int ffn_fabric_inventory(int unit,int port,int numq,uint32 flags,int gport,void *data) {\n"
" int rv;int *q=data;\n"
" if(BCM_GPORT_IS_UCAST_QUEUE_GROUP(gport)) {\n"
"  rv=bcm_cosq_gport_get(unit,gport,&port,&numq,&flags);if(rv)return rv;\n"
"  q[37]=q[37]+1;port=port & 0x7ff;if(port<37)q[port]=q[port]+numq;\n"
" } return 0;\n"
"}\n"
"{\n"
" int rv=0;int q[38]={0};int p=@PORT@;int op=@OP@;int header=0;\n"
" int modid;int e2e;int modport;int tmport;int destination_module;int sysport;int connector=0;int voq=0;int cos;\n"
" uint32 flags;\n"
" bcm_port_interface_info_t intf;\n"
" bcm_port_mapping_info_t mapping;\n"
" bcm_cosq_voq_connector_gport_t cfg;\n"
" bcm_cosq_ingress_queue_bundle_gport_config_t ingress;\n"
" bcm_cosq_gport_connection_t connection;\n"
" rv=bcm_cosq_gport_traverse(0,ffn_fabric_inventory,q);\n"
" if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);\n"
" if(op==1 && rv==0 && q[p]!=0)rv=-8;\n"
" if(op==1 && rv==0)rv=bcm_stk_modid_get(0,&modid);\n"
" if(op==1 && rv==0)rv=bcm_port_get(0,p,&flags,&intf,&mapping);\n"
" if(op==1 && rv==0) {\n"
"  BCM_COSQ_GPORT_E2E_PORT_SET(e2e,p);\n"
"  tmport=mapping.tm_port;destination_module=modid+mapping.core;\n"
"  BCM_GPORT_MODPORT_SET(modport,destination_module,tmport);\n"
"  rv=bcm_stk_gport_sysport_get(0,modport,&sysport);\n"
"  cfg.flags=BCM_COSQ_GPORT_VOQ_CONNECTOR;cfg.port=e2e;cfg.numq=8;\n"
"  cfg.remote_modid=modid;cfg.nof_remote_cores=2;\n"
"  if(rv==0)rv=bcm_cosq_voq_connector_gport_add(0,&cfg,&connector);\n"
"  printf(\"FFN_FABRIC_CONNECTOR port=%d id=0x%x rv=%d\\n\",p,connector,rv);\n"
"  for(cos=0;cos<8 && rv==0;cos++) {\n"
"   rv=bcm_cosq_gport_sched_set(0,connector,cos,BCM_COSQ_SP0,0);\n"
"   if(rv==0)rv=bcm_cosq_gport_attach(0,e2e,connector,cos);\n"
"  }\n"
"  ingress.flags=BCM_COSQ_GPORT_UCAST_QUEUE_GROUP;ingress.port=modport;\n"
"  ingress.local_core_id=BCM_CORE_ALL;ingress.numq=8;\n"
"  for(cos=0;cos<8;cos++) {\n"
"   ingress.queue_atrributes[cos].delay_tolerance_level=BCM_COSQ_DELAY_TOLERANCE_10G_SLOW_ENABLED;\n"
"   ingress.queue_atrributes[cos].rate_class=0;\n"
"  }\n"
"  if(rv==0)rv=bcm_cosq_ingress_queue_bundle_gport_add(0,&ingress,&voq);\n"
"  printf(\"FFN_FABRIC_VOQ port=%d id=0x%x rv=%d\\n\",p,voq,rv);\n"
"  connection.flags=BCM_COSQ_GPORT_CONNECTION_INGRESS;connection.remote_modid=modid+mapping.core;\n"
"  connection.voq=voq;connection.voq_connector=connector;\n"
"  if(rv==0)rv=bcm_cosq_gport_connection_set(0,&connection);\n"
"  connection.flags=BCM_COSQ_GPORT_CONNECTION_EGRESS;connection.remote_modid=modid;\n"
"  if(rv==0)rv=bcm_cosq_gport_connection_set(0,&connection);\n"
" }\n"
" if(op==2 && rv==0 && header!=BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP) {\n"
"  if(q[37]!=0 || (header!=BCM_SWITCH_PORT_HEADER_TYPE_ETH && header!=BCM_SWITCH_PORT_HEADER_TYPE_RAW))rv=-8;\n"
"  if(rv==0)rv=bcm_switch_control_port_set(0,24,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_TM);\n"
"  if(rv==0)rv=bcm_switch_control_port_set(0,24,bcmSwitchPortHeaderType,BCM_SWITCH_PORT_HEADER_TYPE_TM_SSP);\n"
" }\n"
" if(rv==0) {\n"
"  for(cos=0;cos<38;cos++)q[cos]=0;\n"
"  rv=bcm_cosq_gport_traverse(0,ffn_fabric_inventory,q);\n"
" }\n"
" if(rv==0)rv=bcm_switch_control_port_get(0,24,bcmSwitchPortHeaderType,&header);\n"
" printf(\"FFN_FABRIC_STATE port=%d queues=%d bundles=%d header=%d rv=%d\\n\",p,q[p],q[37],header,rv);\n"
" if(rv==0)printf(\"FFN_FABRIC_DONE\\n\");\n"
"}\n";

/* ---- small helpers ---- */

static void seterr(char *err, size_t errsz, const char *message)
{
    if (err && errsz) snprintf(err, errsz, "%s", message);
}

static char *substitute(const char *text, const char *marker, int value)
{
    size_t mlen = strlen(marker), count = 0;
    for (const char *p = text; (p = strstr(p, marker)); p += mlen) count++;
    char number[24];
    snprintf(number, sizeof number, "%d", value);
    size_t nlen = strlen(number);
    char *out = malloc(strlen(text) + count * nlen + 1);
    if (!out) return NULL;
    char *w = out;
    for (const char *p = text;;) {
        const char *hit = strstr(p, marker);
        if (!hit) { strcpy(w, p); break; }
        memcpy(w, p, (size_t)(hit - p)); w += hit - p;
        memcpy(w, number, nlen); w += nlen;
        p = hit + mlen;
    }
    return out;
}

static int path_join(char *out, size_t outsz, const char *dir, const char *name)
{
    return snprintf(out, outsz, "%s/%s", dir, name) < (int)outsz ? 0 : -1;
}

static const char *basename_of(const char *path)
{
    const char *slash = strrchr(path, '/');
    return slash ? slash + 1 : path;
}

int portctl_canonical_uuid(const char *s)
{
    if (!s || strlen(s) != 36) return -1;
    for (int i = 0; i < 36; i++) {
        char c = s[i];
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (c != '-') return -1; continue; }
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return -1;
    }
    return 0;
}

/* ---- readback parsers ---- */

static int marker_count(const struct ffn_json *markers)
{
    return markers && markers->type == FFN_JSON_ARRAY ? (int)markers->count : -1;
}

static const char *marker_at(const struct ffn_json *markers, size_t i)
{
    const struct ffn_json *m = markers->items[i];
    return m->type == FFN_JSON_STRING ? m->string : "";
}

int portctl_parse_redirect(const struct ffn_json *markers, struct ffn_json **hardware, char *err, size_t errsz)
{
    int n = marker_count(markers), found = 0;
    long long header = 0, wanq = 0, trunkq = 0, dst = 0, enabled = 0;
    for (int i = 0; i < n; i++) {
        long long h, w, t, d, e, rv; char tail[4] = "";
        if (sscanf(marker_at(markers, (size_t)i), "FFN_WAN_STATE header=%lld wanq=%lld trunkq=%lld dst=%lld enabled=%lld rv=%lld%1s", &h, &w, &t, &d, &e, &rv, tail) == 6
            && rv == 0 && (e == 0 || e == 1)) { header = h; wanq = w; trunkq = t; dst = d; enabled = e; found++; }
    }
    if (found != 1 || n < 1 || strcmp(marker_at(markers, (size_t)n - 1), "FFN_WAN_DONE") != 0) {
        seterr(err, errsz, "WAN hardware readback failed"); return -1;
    }
    struct ffn_json *o = ffn_json_object();
    if (!o || ffn_json_set(o, "header", ffn_json_int(header)) || ffn_json_set(o, "wan_queues", ffn_json_int(wanq)) ||
        ffn_json_set(o, "trunk_queues", ffn_json_int(trunkq)) || ffn_json_set(o, "destination", ffn_json_int(dst)) ||
        ffn_json_set(o, "enabled", ffn_json_int(enabled))) { ffn_json_free(o); seterr(err, errsz, "out of memory"); return -1; }
    *hardware = o;
    return 0;
}

int portctl_parse_fabric(const struct ffn_json *markers, int bcm_port, struct ffn_json **row, char *err, size_t errsz)
{
    int n = marker_count(markers), found = 0;
    long long port = 0, queues = 0, bundles = 0, header = 0;
    for (int i = 0; i < n; i++) {
        long long p, q, b, h, rv; char tail[4] = "";
        if (sscanf(marker_at(markers, (size_t)i), "FFN_FABRIC_STATE port=%lld queues=%lld bundles=%lld header=%lld rv=%lld%1s", &p, &q, &b, &h, &rv, tail) == 5 && rv == 0) {
            port = p; queues = q; bundles = b; header = h; found++;
        }
    }
    if (found != 1 || n < 1 || strcmp(marker_at(markers, (size_t)n - 1), "FFN_FABRIC_DONE") != 0) {
        seterr(err, errsz, "Invalid packet fabric readback"); return -1;
    }
    if (port != bcm_port) { seterr(err, errsz, "Packet fabric port mismatch"); return -1; }
    struct ffn_json *o = ffn_json_object(), *allocations = ffn_json_array();
    if (!o || !allocations) { ffn_json_free(o); ffn_json_free(allocations); seterr(err, errsz, "out of memory"); return -1; }
    for (int i = 0; i < n; i++) {
        const char *m = marker_at(markers, (size_t)i);
        if (!strncmp(m, "FFN_FABRIC_CONNECTOR", 20) || !strncmp(m, "FFN_FABRIC_VOQ", 14)) ffn_json_push(allocations, ffn_json_string(m));
    }
    if (ffn_json_set(o, "port", ffn_json_int(port)) || ffn_json_set(o, "queues", ffn_json_int(queues)) ||
        ffn_json_set(o, "bundles", ffn_json_int(bundles)) || ffn_json_set(o, "header", ffn_json_int(header)) ||
        ffn_json_set(o, "allocations", allocations)) { ffn_json_free(o); seterr(err, errsz, "out of memory"); return -1; }
    *row = o;
    return 0;
}

/* ---- BCM service helpers ---- */

struct run {
    const struct portctl_paths *paths;
    const struct portctl_io *io;
    char *err; size_t errsz;
    int status;            /* 1 runtime, 2 rejected request */
};

static int fail(struct run *r, int status, const char *message)
{
    r->status = status; seterr(r->err, r->errsz, message); return -1;
}

static struct ffn_json *bcm(struct run *r, struct ffn_json *request)
{
    char transport[256] = "";
    struct ffn_json *reply = r->io->bcm_call(r->io->ctx, request, transport, sizeof transport);
    ffn_json_free(request);
    if (!reply) { fail(r, 1, transport[0] ? transport : "BCM service unavailable"); return NULL; }
    bool ok = false;
    if (!ffn_json_get_bool(reply, "ok", &ok) || !ok) { ffn_json_free(reply); fail(r, 1, "BCM operation failed; refresh hardware status"); return NULL; }
    return reply;
}

/* Administrative state of one BCM port through port.list: 0/1, or -1 when absent/not boolean. */
static int admin_state(struct run *r, int bcm_port)
{
    struct ffn_json *req = ffn_json_object();
    if (!req || ffn_json_set(req, "op", ffn_json_string("port.list"))) { ffn_json_free(req); fail(r, 1, "out of memory"); return -2; }
    struct ffn_json *reply = bcm(r, req);
    if (!reply) return -2;
    int result = -1;
    const struct ffn_json *ports = ffn_json_get(reply, "ports");
    if (ports && ports->type == FFN_JSON_ARRAY)
        for (size_t i = 0; i < ports->count; i++) {
            long long p; bool enabled;
            if (ffn_json_get_int(ports->items[i], "port", &p) && p == bcm_port) { result = ffn_json_get_bool(ports->items[i], "enabled", &enabled) ? enabled : -1; break; }
        }
    ffn_json_free(reply);
    return result;
}

static int admin_set(struct run *r, int bcm_port, bool enable)
{
    struct ffn_json *req = ffn_json_object();
    if (!req || ffn_json_set(req, "op", ffn_json_string("port.set")) || ffn_json_set(req, "port", ffn_json_int(bcm_port)) ||
        ffn_json_set(req, "enable", ffn_json_bool(enable))) { ffn_json_free(req); return fail(r, 1, "out of memory"); }
    struct ffn_json *reply = bcm(r, req);
    if (!reply) return -1;
    ffn_json_free(reply);
    return 0;
}

/* Run one cint recipe through the service: the script file is swapped under the
 * forward-test lock and restored afterwards; returns the markers array or NULL. */
static struct ffn_json *recipe(struct run *r, const char *text, const char *incomplete)
{
    char lockpath[512];
    if (path_join(lockpath, sizeof lockpath, r->paths->run, "ffn-forward-test.lock")) { fail(r, 1, "path too long"); return NULL; }
    char lerr[128] = "";
    int fd = r->io->lock(r->io->ctx, lockpath, PORTCTL_LOCK_SECONDS, lerr, sizeof lerr);
    if (fd < 0) { fail(r, 1, lerr[0] ? lerr : "Aggregate hardware lock busy"); return NULL; }
    char *previous = NULL; size_t plen = 0;
    bool existed = r->io->read_file(r->io->ctx, r->paths->script, &previous, &plen) == 0;
    struct ffn_json *markers = NULL;
    if (r->io->write_file(r->io->ctx, r->paths->script, text, strlen(text))) { fail(r, 1, "cannot stage SDK recipe"); goto out; }
    struct ffn_json *req = ffn_json_object();
    if (!req || ffn_json_set(req, "op", ffn_json_string("cint.run")) || ffn_json_set(req, "script", ffn_json_string(basename_of(r->paths->script))) ||
        ffn_json_set(req, "timeout", ffn_json_int(PORTCTL_CINT_TIMEOUT))) { ffn_json_free(req); fail(r, 1, "out of memory"); goto restore; }
    struct ffn_json *reply = bcm(r, req);
    if (!reply) goto restore;
    bool completed = false, truncated = false;
    ffn_json_get_bool(reply, "completed", &completed); ffn_json_get_bool(reply, "truncated", &truncated);
    if (!completed || truncated) { ffn_json_free(reply); fail(r, 1, incomplete); goto restore; }
    const struct ffn_json *m = ffn_json_get(reply, "markers");
    markers = m && m->type == FFN_JSON_ARRAY ? ffn_json_copy(m) : ffn_json_array();
    ffn_json_free(reply);
restore:
    if (existed) r->io->write_file(r->io->ctx, r->paths->script, previous, plen);
    else r->io->unlink(r->io->ctx, r->paths->script);
out:
    free(previous);
    r->io->unlock(r->io->ctx, fd);
    return markers;
}

/* The redirect observation/change for one front port: mode 0 observe, 1 start, 2 stop. */
static struct ffn_json *redirect(struct run *r, int bcm_port, int mode)
{
    char *text = substitute(REDIRECT_RECIPE, "@PORT@", bcm_port);
    char *full = text ? substitute(text, "@MODE@", mode) : NULL;
    free(text);
    if (!full) { fail(r, 1, "out of memory"); return NULL; }
    struct ffn_json *markers = recipe(r, full, "WAN SDK operation incomplete");
    free(full);
    if (!markers) return NULL;
    struct ffn_json *hardware = NULL;
    char perr[128];
    if (portctl_parse_redirect(markers, &hardware, perr, sizeof perr)) { ffn_json_free(markers); fail(r, 1, perr); return NULL; }
    ffn_json_free(markers);
    return hardware;
}

/* The fabric observation/change for one BCM port: op 0 observe, 1 queues, 2 trunk header. */
static struct ffn_json *fabric(struct run *r, int bcm_port, int op)
{
    char *text = substitute(FABRIC_RECIPE, "@PORT@", bcm_port);
    char *full = text ? substitute(text, "@OP@", op) : NULL;
    free(text);
    if (!full) { fail(r, 1, "out of memory"); return NULL; }
    struct ffn_json *markers = recipe(r, full, "Packet fabric operation incomplete; inspect allocation journal and last-result report");
    free(full);
    if (!markers) return NULL;
    if (op) {
        /* The last-result report the operators read after an incomplete allocation. */
        char path[512];
        struct ffn_json *report = ffn_json_object();
        if (report && !path_join(path, sizeof path, r->paths->etc, "packet-fabric-last-result.json")) {
            ffn_json_set(report, "port", ffn_json_int(bcm_port)); ffn_json_set(report, "operation", ffn_json_int(op));
            ffn_json_set(report, "markers", ffn_json_copy(markers));
            char werr[64];
            r->io->write_json(r->io->ctx, path, report, werr, sizeof werr);
        }
        ffn_json_free(report);
    }
    struct ffn_json *row = NULL;
    char perr[128];
    if (portctl_parse_fabric(markers, bcm_port, &row, perr, sizeof perr)) { ffn_json_free(markers); fail(r, 1, perr); return NULL; }
    ffn_json_free(markers);
    return row;
}

static int epoch_now(struct run *r, char *out, size_t outsz)
{
    char eerr[160] = "";
    if (r->io->epoch(r->io->ctx, out, outsz, eerr, sizeof eerr)) return fail(r, 1, eerr[0] ? eerr : "one BCM owner process required");
    return 0;
}

static int fence(struct run *r, const char *expected, const char *message)
{
    char now[160];
    if (epoch_now(r, now, sizeof now)) return -1;
    if (strcmp(now, expected) != 0) return fail(r, 1, message);
    return 0;
}

static long long row_int(const struct ffn_json *row, const char *key)
{
    long long v = -1; ffn_json_get_int(row, key, &v); return v;
}

/* Journaled queue preparation for the trunk and one front port (ffn_packet_fabric.ensure). */
static int ensure_fabric(struct run *r, int bcm_port, const char *expected)
{
    char lockpath[512], statepath[512];
    if (path_join(lockpath, sizeof lockpath, r->paths->run, "ffn-packet-fabric.lock") || path_join(statepath, sizeof statepath, r->paths->etc, "packet-fabric.json"))
        return fail(r, 1, "path too long");
    char lerr[128] = "";
    int fd = r->io->lock(r->io->ctx, lockpath, PORTCTL_LOCK_SECONDS, lerr, sizeof lerr);
    if (fd < 0) return fail(r, 1, lerr[0] ? lerr : "Aggregate hardware lock busy");
    int rc = -1;
    struct ffn_json *saved = NULL, *observed[2] = { NULL, NULL };
    const int required[2] = { PORTCTL_TRUNK, bcm_port };
    char werr[64];
    if (fence(r, expected, "BCM lifetime changed during fabric preparation")) goto out;
    saved = r->io->read_json(r->io->ctx, statepath);
    if (!saved || saved->type != FFN_JSON_OBJECT) { ffn_json_free(saved); saved = ffn_json_object(); }
    const char *saved_epoch = ffn_json_get_string(saved, "epoch");
    if (!saved_epoch || strcmp(saved_epoch, expected) != 0) {
        ffn_json_free(saved); saved = ffn_json_object();
        if (!saved || ffn_json_set(saved, "epoch", ffn_json_string(expected)) || ffn_json_set(saved, "ports", ffn_json_object())) { fail(r, 1, "out of memory"); goto out; }
    }
    if (ffn_json_get(saved, "pending")) { fail(r, 1, "Uncertain packet fabric allocation; automatic retry withheld"); goto out; }
    for (int i = 0; i < 2; i++) {
        observed[i] = fabric(r, required[i], 0);
        if (!observed[i]) goto out;
        long long q = row_int(observed[i], "queues");
        if (q != 0 && q != PORTCTL_QUEUES) { fail(r, 1, "Conflicting packet fabric queue layout"); goto out; }
    }
    if (row_int(observed[0], "header") != PORTCTL_HEADER_TM_SSP) {
        /* Native CP images may initialise the empty internal trunk as RAW (2) rather than ETH (1). */
        long long header = row_int(observed[0], "header");
        if ((header != 1 && header != 2) || row_int(observed[0], "bundles") != 0) { fail(r, 1, "Cannot change an occupied packet trunk header"); goto out; }
        if (fence(r, expected, "BCM lifetime changed during fabric preparation")) goto out;
        struct ffn_json *pending = ffn_json_object();
        ffn_json_set(pending, "operation", ffn_json_string("header"));
        ffn_json_set(saved, "pending", pending);
        if (r->io->write_json(r->io->ctx, statepath, saved, werr, sizeof werr)) { fail(r, 1, "cannot journal fabric intent"); goto out; }
        struct ffn_json *after = fabric(r, PORTCTL_TRUNK, 2);
        if (!after) goto out;
        if (fence(r, expected, "BCM lifetime changed during fabric preparation")) { ffn_json_free(after); goto out; }
        if (row_int(after, "header") != PORTCTL_HEADER_TM_SSP) { ffn_json_free(after); fail(r, 1, "Packet trunk header not verified"); goto out; }
        ffn_json_remove(saved, "pending");
        ffn_json_set(saved, "header", ffn_json_int(row_int(after, "header")));
        ffn_json_free(after);
        if (r->io->write_json(r->io->ctx, statepath, saved, werr, sizeof werr)) { fail(r, 1, "cannot journal fabric state"); goto out; }
    }
    for (int i = 0; i < 2; i++) {
        if (fence(r, expected, "BCM lifetime changed during fabric preparation")) goto out;
        if (row_int(observed[i], "queues") == PORTCTL_QUEUES) continue;
        struct ffn_json *pending = ffn_json_object();
        ffn_json_set(pending, "operation", ffn_json_string("queues")); ffn_json_set(pending, "port", ffn_json_int(required[i]));
        ffn_json_set(saved, "pending", pending);
        if (r->io->write_json(r->io->ctx, statepath, saved, werr, sizeof werr)) { fail(r, 1, "cannot journal fabric intent"); goto out; }
        struct ffn_json *after = fabric(r, required[i], 1);
        if (!after) goto out;
        if (fence(r, expected, "BCM lifetime changed during fabric preparation")) { ffn_json_free(after); goto out; }
        if (row_int(after, "queues") != PORTCTL_QUEUES) { ffn_json_free(after); fail(r, 1, "Packet queues not verified"); goto out; }
        char key[16]; snprintf(key, sizeof key, "%d", required[i]);
        struct ffn_json *ports = ffn_json_get(saved, "ports");
        if (!ports || ports->type != FFN_JSON_OBJECT) { ffn_json_set(saved, "ports", ffn_json_object()); ports = ffn_json_get(saved, "ports"); }
        ffn_json_set(ports, key, after);
        ffn_json_remove(saved, "pending");
        if (r->io->write_json(r->io->ctx, statepath, saved, werr, sizeof werr)) { fail(r, 1, "cannot journal fabric state"); goto out; }
    }
    for (int i = 0; i < 2; i++) {
        struct ffn_json *verified = fabric(r, required[i], 0);
        if (!verified) goto out;
        long long q = row_int(verified, "queues"), h = row_int(verified, "header");
        ffn_json_free(verified);
        if (q != PORTCTL_QUEUES || h != PORTCTL_HEADER_TM_SSP) { fail(r, 1, "Packet fabric final readback failed"); goto out; }
    }
    if (fence(r, expected, "BCM lifetime changed during fabric preparation")) goto out;
    rc = 0;
out:
    ffn_json_free(saved); ffn_json_free(observed[0]); ffn_json_free(observed[1]);
    r->io->unlock(r->io->ctx, fd);
    return rc;
}

/* ---- the attach state machine ---- */

static bool aggregate_owns(const struct ffn_json *aggregates, int front)
{
    const struct ffn_json *groups = ffn_json_get(aggregates, "groups");
    if (!groups || groups->type != FFN_JSON_OBJECT) return false;
    for (size_t g = 0; g < groups->count; g++) {
        const struct ffn_json *group = groups->items[g];
        const char *phase = ffn_json_get_string(group, "phase");
        if (phase && strcmp(phase, "stopped") == 0) continue;
        const struct ffn_json *ports = ffn_json_get(group, "ports");
        if (!ports || ports->type != FFN_JSON_ARRAY) continue;
        for (size_t i = 0; i < ports->count; i++)
            if (ports->items[i]->type == FFN_JSON_INT && ports->items[i]->integer == front) return true;
    }
    return false;
}

static struct ffn_json *new_state(int front, const char *epoch, const char *dp_boot_id, bool enabled, const char *pending)
{
    struct ffn_json *s = ffn_json_object();
    if (!s) return NULL;
    ffn_json_set(s, "port", ffn_json_int(front)); ffn_json_set(s, "epoch", ffn_json_string(epoch));
    ffn_json_set(s, "dp_boot_id", ffn_json_string(dp_boot_id)); ffn_json_set(s, "enabled", ffn_json_bool(enabled));
    ffn_json_set(s, "pending", pending ? ffn_json_string(pending) : ffn_json_null());
    return s;
}

static bool hardware_ready(const struct ffn_json *hw)
{
    return row_int(hw, "header") == PORTCTL_HEADER_TM_SSP && row_int(hw, "wan_queues") == PORTCTL_QUEUES &&
           row_int(hw, "trunk_queues") == PORTCTL_QUEUES && row_int(hw, "destination") == PORTCTL_TRUNK && row_int(hw, "enabled") == 1;
}

int portctl_execute(const char *action, const char *payload, size_t length, const struct portctl_paths *paths,
                    const struct portctl_io *io, char **out, int *status, char *err, size_t errsz)
{
    struct run r = { paths, io, err, errsz, 1 };
    *out = NULL; *status = 0;
    enum { STATUS, START, STOP } op;
    if (!strcmp(action, "status")) op = STATUS; else if (!strcmp(action, "start")) op = START; else if (!strcmp(action, "stop")) op = STOP;
    else { fail(&r, 2, "Invalid physical operation"); *status = r.status; return -1; }

    char perr[128];
    struct ffn_json *request = ffn_json_parse(payload, length, perr, sizeof perr);
    if (!request || request->type != FFN_JSON_OBJECT) { ffn_json_free(request); fail(&r, 2, "Invalid physical request"); *status = r.status; return -1; }
    long long front = 0;
    const struct ffn_json *pv = ffn_json_get(request, "port");
    if (!pv || pv->type != FFN_JSON_INT || !portctl_bcm_port((int)pv->integer) || pv->integer == 1) {
        ffn_json_free(request); fail(&r, 2, "Independent physical port required; port 1 retains its WAN owner"); *status = r.status; return -1;
    }
    front = pv->integer;
    size_t expected_fields = op == STATUS ? 1 : 3;
    const char *dp_boot_id = ffn_json_get_string(request, "dp_boot_id");
    const char *claimed_epoch = ffn_json_get_string(request, "epoch");
    if (request->count != expected_fields || (op != STATUS && (!dp_boot_id || !claimed_epoch))) {
        ffn_json_free(request); fail(&r, 2, "Invalid physical operation"); *status = r.status; return -1;
    }
    if (op != STATUS && portctl_canonical_uuid(dp_boot_id)) { ffn_json_free(request); fail(&r, 2, "DP boot identity required"); *status = r.status; return -1; }
    int bcm_port = portctl_bcm_port((int)front);

    char portlock[512], facelock[512], statepath[512], aggpath[512], name[48];
    snprintf(name, sizeof name, "ffn-physical-%lld.lock", front);
    if (path_join(portlock, sizeof portlock, paths->run, name) || path_join(facelock, sizeof facelock, paths->run, "ffn-faceplate.lock")) { ffn_json_free(request); fail(&r, 1, "path too long"); *status = 1; return -1; }
    snprintf(name, sizeof name, "physical-%lld.json", front);
    if (path_join(statepath, sizeof statepath, paths->etc, name) || path_join(aggpath, sizeof aggpath, paths->etc, "aggregate-hardware.json")) { ffn_json_free(request); fail(&r, 1, "path too long"); *status = 1; return -1; }

    int rc = -1, pfd = -1, ffd = -1;
    struct ffn_json *state = NULL, *observed = NULL, *result = NULL;
    char lerr[128] = "", current[160] = "", werr[64];
    pfd = io->lock(io->ctx, portlock, PORTCTL_LOCK_SECONDS, lerr, sizeof lerr);
    if (pfd < 0) { fail(&r, 1, lerr[0] ? lerr : "Aggregate hardware lock busy"); goto out; }
    ffd = io->lock(io->ctx, facelock, PORTCTL_LOCK_SECONDS, lerr, sizeof lerr);
    if (ffd < 0) { fail(&r, 1, lerr[0] ? lerr : "Aggregate hardware lock busy"); goto out; }
    if (epoch_now(&r, current, sizeof current)) goto out;
    state = io->read_json(io->ctx, statepath);
    if (!state || state->type != FFN_JSON_OBJECT) { ffn_json_free(state); state = ffn_json_object(); }
    observed = redirect(&r, bcm_port, 0);
    if (!observed) goto out;

    if (op != STATUS) {
        if (strcmp(claimed_epoch, current) != 0) { fail(&r, 2, "BCM lifetime changed"); goto out; }
        struct ffn_json *aggregates = io->read_json(io->ctx, aggpath);
        bool owned_by_aggregate = aggregate_owns(aggregates, (int)front);
        ffn_json_free(aggregates);
        if (owned_by_aggregate) { fail(&r, 2, "Physical port belongs to an aggregate owner"); goto out; }
        const char *state_epoch = ffn_json_get_string(state, "epoch");
        bool state_enabled = false; ffn_json_get_bool(state, "enabled", &state_enabled);
        const struct ffn_json *pending = ffn_json_get(state, "pending");
        bool owned = state_epoch && strcmp(state_epoch, current) == 0 && state_enabled && (!pending || pending->type == FFN_JSON_NULL);
        bool redirected = row_int(observed, "enabled") != 0;
        if (redirected && !owned) { fail(&r, 2, "Unowned or uncertain physical redirect; explicit recovery required"); goto out; }
        if (op == START) {
            int before = -1;
            if (!redirected) {
                before = admin_state(&r, bcm_port);
                if (before == -2) goto out;
                if (before < 0) { fail(&r, 2, "Physical administrative state unavailable"); goto out; }
                if (before == 1) {
                    if (admin_set(&r, bcm_port, false)) goto out;
                    if (admin_state(&r, bcm_port) != 0) { fail(&r, 1, "Physical withdrawal unverified"); goto out; }
                }
                /* An ambiguous allocation stays down with the fabric's durable pending journal. */
                if (ensure_fabric(&r, bcm_port, current)) goto out;
                if (fence(&r, current, "BCM lifetime changed during queue preparation")) goto out;
            }
            ffn_json_free(state);
            state = new_state((int)front, current, dp_boot_id, false, "start");
            if (!state || io->write_json(io->ctx, statepath, state, werr, sizeof werr)) { fail(&r, 1, "cannot journal physical intent"); goto out; }
            ffn_json_free(observed);
            observed = redirect(&r, bcm_port, 1);
            if (!observed) goto out;
            if (before == 1) {
                if (admin_set(&r, bcm_port, true)) goto out;
                if (admin_state(&r, bcm_port) != 1) { fail(&r, 1, "Physical administrative restoration unverified"); goto out; }
            }
        } else {
            const char *state_owner = ffn_json_get_string(state, "dp_boot_id");
            if (redirected && (!state_owner || strcmp(state_owner, dp_boot_id) != 0)) { fail(&r, 2, "Refusing stale DP owner cleanup"); goto out; }
            ffn_json_free(state);
            state = new_state((int)front, current, dp_boot_id, redirected, "stop");
            if (!state || io->write_json(io->ctx, statepath, state, werr, sizeof werr)) { fail(&r, 1, "cannot journal physical intent"); goto out; }
            if (admin_set(&r, bcm_port, false)) goto out;
            if (admin_state(&r, bcm_port) != 0) { fail(&r, 1, "Physical withdrawal unverified"); goto out; }
            ffn_json_free(observed);
            observed = redirect(&r, bcm_port, 2);
            if (!observed) goto out;
        }
        if (fence(&r, current, "BCM lifetime changed during physical attachment")) goto out;
        ffn_json_set(state, "enabled", ffn_json_bool(op == START));
        ffn_json_set(state, "pending", ffn_json_null());
        if (io->write_json(io->ctx, statepath, state, werr, sizeof werr)) { fail(&r, 1, "cannot journal physical state"); goto out; }
    }

    {
        const char *state_epoch = ffn_json_get_string(state, "epoch");
        bool state_enabled = false; ffn_json_get_bool(state, "enabled", &state_enabled);
        const struct ffn_json *pending = ffn_json_get(state, "pending");
        bool ready = state_epoch && strcmp(state_epoch, current) == 0 && state_enabled && (!pending || pending->type == FFN_JSON_NULL) && hardware_ready(observed);
        result = ffn_json_object();
        if (!result || ffn_json_set(result, "port", ffn_json_int(front)) || ffn_json_set(result, "epoch", ffn_json_string(current)) ||
            ffn_json_set(result, "ready", ffn_json_bool(ready)) || ffn_json_set(result, "state", ffn_json_copy(state)) ||
            ffn_json_set(result, "hardware", ffn_json_copy(observed))) { fail(&r, 1, "out of memory"); goto out; }
        *out = ffn_json_dump(result);
        if (!*out) { fail(&r, 1, "out of memory"); goto out; }
        rc = 0;
    }
out:
    ffn_json_free(request); ffn_json_free(state); ffn_json_free(observed); ffn_json_free(result);
    if (ffd >= 0) io->unlock(io->ctx, ffd);
    if (pfd >= 0) io->unlock(io->ctx, pfd);
    *status = rc ? r.status : 0;
    return rc;
}

/* ---- production I/O ---- */

struct system_ctx { char host[64]; int port; char proc[256]; };

static int connect_bounded(const char *host, int port, int seconds)
{
    struct addrinfo hints = { 0 }, *list = NULL;
    char service[8];
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    snprintf(service, sizeof service, "%d", port);
    if (getaddrinfo(host, service, &hints, &list)) return -1;
    int fd = -1;
    for (struct addrinfo *a = list; a; a = a->ai_next) {
        fd = socket(a->ai_family, a->ai_socktype | SOCK_NONBLOCK, a->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, a->ai_addr, a->ai_addrlen) == 0) break;
        if (errno == EINPROGRESS) {
            struct pollfd p = { fd, POLLOUT, 0 };
            int soerr = 0; socklen_t len = sizeof soerr;
            if (poll(&p, 1, seconds * 1000) == 1 && getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len) == 0 && soerr == 0) break;
        }
        close(fd); fd = -1;
    }
    freeaddrinfo(list);
    if (fd >= 0) {
        int flags = fcntl(fd, F_GETFL, 0);
        fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
        struct timeval tv = { 70, 0 };
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv); setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    }
    return fd;
}

static struct ffn_json *system_bcm_call(void *ctx, const struct ffn_json *request, char *err, size_t errsz)
{
    struct system_ctx *s = ctx;
    char *line = ffn_json_dump(request);
    if (!line) { seterr(err, errsz, "out of memory"); return NULL; }
    int fd = connect_bounded(s->host, s->port, 5);
    if (fd < 0) { free(line); seterr(err, errsz, "BCM service unreachable"); return NULL; }
    size_t len = strlen(line);
    line[len] = '\n';  /* dump() leaves room: replace the terminator and send len+1 bytes */
    ssize_t sent = 0;
    for (size_t off = 0; off < len + 1; off += (size_t)sent) {
        sent = write(fd, line + off, len + 1 - off);
        if (sent <= 0) { free(line); close(fd); seterr(err, errsz, "BCM service write failed"); return NULL; }
    }
    free(line);
    size_t cap = 65536, used = 0;
    char *buf = malloc(cap);
    struct ffn_json *reply = NULL;
    while (buf) {
        if (used + 1 >= cap) {
            if (cap >= 1024 * 1024 + 2) { seterr(err, errsz, "BCM response too large"); break; }
            char *n = realloc(buf, cap * 2); if (!n) break; buf = n; cap *= 2;
        }
        ssize_t got = read(fd, buf + used, cap - used - 1);
        if (got <= 0) { seterr(err, errsz, "BCM service read failed"); break; }
        used += (size_t)got;
        char *nl = memchr(buf, '\n', used);
        if (nl) {
            char perr[96];
            reply = ffn_json_parse(buf, (size_t)(nl - buf), perr, sizeof perr);
            if (!reply) seterr(err, errsz, "BCM response is not valid JSON");
            break;
        }
    }
    free(buf); close(fd);
    return reply;
}

static int system_epoch(void *ctx, char *out, size_t outsz, char *err, size_t errsz)
{
    struct system_ctx *s = ctx;
    DIR *dir = opendir(s->proc);
    if (!dir) { seterr(err, errsz, "cannot read process table"); return -1; }
    char owner[320] = ""; int owners = 0;
    struct dirent *e;
    while ((e = readdir(dir))) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
        char path[640];
        if (snprintf(path, sizeof path, "%s/%s/cmdline", s->proc, e->d_name) >= (int)sizeof path) continue;
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        char cmd[4096];
        size_t n = fread(cmd, 1, sizeof cmd - 1, f);
        fclose(f);
        cmd[n] = 0;
        bool match = false;
        for (size_t i = 0; i < n;) {
            const char *arg = cmd + i;
            size_t alen = strlen(arg);
            if (alen && strcmp(basename_of(arg), "ffn_bcmd.py") == 0) match = true;
            i += alen + 1;
        }
        if (!match) continue;
        if (snprintf(path, sizeof path, "%s/%s/stat", s->proc, e->d_name) >= (int)sizeof path) continue;
        f = fopen(path, "r");
        if (!f) continue;
        char stat[1024];
        n = fread(stat, 1, sizeof stat - 1, f);
        fclose(f);
        stat[n] = 0;
        const char *paren = strrchr(stat, ')');
        if (!paren) continue;
        /* fields after ") ": state(3) ... starttime is field 22, i.e. index 19 of this split */
        const char *p = paren + 2; char start[32] = "";
        for (int field = 0; field < 20 && *p; field++) {
            const char *q = p; while (*q && *q != ' ') q++;
            if (field == 19) { size_t l = (size_t)(q - p) < sizeof start - 1 ? (size_t)(q - p) : sizeof start - 1; memcpy(start, p, l); start[l] = 0; }
            p = *q ? q + 1 : q;
        }
        if (!start[0]) continue;
        if (snprintf(owner, sizeof owner, "%s:%s", e->d_name, start) >= (int)sizeof owner) continue;
        owners++;
    }
    closedir(dir);
    if (owners != 1) { seterr(err, errsz, "one BCM owner process required"); return -1; }
    char path[640], boot[80] = "";
    if (snprintf(path, sizeof path, "%s/sys/kernel/random/boot_id", s->proc) >= (int)sizeof path) { seterr(err, errsz, "proc path too long"); return -1; }
    FILE *f = fopen(path, "r");
    if (!f || !fgets(boot, sizeof boot, f)) { if (f) fclose(f); seterr(err, errsz, "cannot read boot identity"); return -1; }
    fclose(f);
    boot[strcspn(boot, "\r\n")] = 0;
    if (snprintf(out, outsz, "%s:%s", boot, owner) >= (int)outsz) { seterr(err, errsz, "epoch too long"); return -1; }
    return 0;
}

static int system_lock(void *ctx, const char *path, int seconds, char *err, size_t errsz)
{
    (void)ctx;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0) { seterr(err, errsz, "cannot open lock file"); return -1; }
    struct timespec deadline; clock_gettime(CLOCK_MONOTONIC, &deadline); deadline.tv_sec += seconds;
    for (;;) {
        if (flock(fd, LOCK_EX | LOCK_NB) == 0) return fd;
        if (errno != EWOULDBLOCK && errno != EINTR) break;
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec > deadline.tv_sec || (now.tv_sec == deadline.tv_sec && now.tv_nsec >= deadline.tv_nsec)) break;
        struct timespec nap = { 0, 20 * 1000 * 1000 };
        nanosleep(&nap, NULL);
    }
    close(fd);
    seterr(err, errsz, "Aggregate hardware lock busy");
    return -1;
}

static void system_unlock(void *ctx, int fd) { (void)ctx; if (fd >= 0) close(fd); }

static int system_read_file(void *ctx, const char *path, char **data, size_t *len)
{
    (void)ctx;
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    size_t cap = 4096, used = 0;
    char *buf = malloc(cap);
    while (buf) {
        size_t got = fread(buf + used, 1, cap - used, f);
        used += got;
        if (got == 0) break;
        if (used == cap) { char *n = realloc(buf, cap * 2); if (!n) { free(buf); buf = NULL; break; } buf = n; cap *= 2; }
    }
    fclose(f);
    if (!buf) return -1;
    *data = buf; *len = used;
    return 0;
}

static int sync_dir(const char *path)
{
    char dir[512];
    snprintf(dir, sizeof dir, "%s", path);
    char *slash = strrchr(dir, '/');
    if (slash) *slash = 0; else strcpy(dir, ".");
    int fd = open(dir, O_RDONLY | O_DIRECTORY);
    if (fd < 0) return -1;
    int rc = fsync(fd);
    close(fd);
    return rc;
}

static int write_atomic(const char *path, const char *data, size_t len)
{
    /* Python's with_suffix('.tmp'): physical-2.json -> physical-2.tmp, in the same directory. */
    char temp[512];
    if (snprintf(temp, sizeof temp, "%s", path) >= (int)sizeof temp - 5) return -1;
    char *slash = strrchr(temp, '/'), *dot = strrchr(temp, '.');
    if (dot && (!slash || dot > slash)) *dot = 0;
    strcat(temp, ".tmp");
    int fd = open(temp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    for (size_t off = 0; off < len;) {
        ssize_t w = write(fd, data + off, len - off);
        if (w <= 0) { close(fd); unlink(temp); return -1; }
        off += (size_t)w;
    }
    if (fsync(fd) || close(fd)) { unlink(temp); return -1; }
    if (rename(temp, path)) { unlink(temp); return -1; }
    return sync_dir(path);
}

static struct ffn_json *system_read_json(void *ctx, const char *path)
{
    char *data; size_t len;
    if (system_read_file(ctx, path, &data, &len)) return NULL;
    struct ffn_json *v = ffn_json_parse(data, len, NULL, 0);
    free(data);
    return v;
}

static int system_write_json(void *ctx, const char *path, const struct ffn_json *value, char *err, size_t errsz)
{
    (void)ctx;
    char *text = ffn_json_dump(value);
    if (!text) { seterr(err, errsz, "out of memory"); return -1; }
    int rc = write_atomic(path, text, strlen(text));
    free(text);
    if (rc) seterr(err, errsz, "cannot write state file");
    return rc;
}

static int system_write_file(void *ctx, const char *path, const char *data, size_t len)
{
    (void)ctx;
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    int rc = fwrite(data, 1, len, f) == len ? 0 : -1;
    if (fclose(f)) rc = -1;
    return rc;
}

static int system_unlink(void *ctx, const char *path) { (void)ctx; return unlink(path) == 0 || errno == ENOENT ? 0 : -1; }

const struct portctl_io *portctl_system_io(const char *bcm_host, int bcm_port, const char *proc_root)
{
    static struct system_ctx ctx;
    static struct portctl_io io;
    snprintf(ctx.host, sizeof ctx.host, "%s", bcm_host);
    ctx.port = bcm_port;
    snprintf(ctx.proc, sizeof ctx.proc, "%s", proc_root);
    io.ctx = &ctx;
    io.bcm_call = system_bcm_call; io.epoch = system_epoch; io.lock = system_lock; io.unlock = system_unlock;
    io.read_json = system_read_json; io.write_json = system_write_json; io.read_file = system_read_file;
    io.write_file = system_write_file; io.unlink = system_unlink;
    return &io;
}

#ifndef PORTCTL_NO_MAIN
int main(int argc, char **argv)
{
    if (argc != 2) { fprintf(stderr, "usage: ffn-portctl status|start|stop  (JSON payload on stdin)\n"); return 2; }
    size_t cap = 4096, used = 0;
    char *payload = malloc(cap);
    while (payload) {
        size_t got = fread(payload + used, 1, cap - used, stdin);
        used += got;
        if (got == 0) break;
        if (used == cap) { char *n = realloc(payload, cap * 2); if (!n) { free(payload); payload = NULL; break; } payload = n; cap *= 2; }
    }
    if (!payload) { fprintf(stderr, "out of memory\n"); return 1; }
    const char *etc = getenv("FFN_PORTCTL_ETC"), *run = getenv("FFN_PORTCTL_RUN"), *script = getenv("FFN_PORTCTL_SCRIPT");
    const char *host = getenv("FFN_PORTCTL_BCM_HOST"), *port = getenv("FFN_PORTCTL_BCM_PORT"), *proc = getenv("FFN_PORTCTL_PROC");
    struct portctl_paths paths = { etc ? etc : "/etc/ffn", run ? run : "/run", script ? script : "/usr/share/broadcom/ffn_bcm_forward_test.c" };
    const struct portctl_io *io = portctl_system_io(host ? host : "127.1.1.2", port ? atoi(port) : 8104, proc ? proc : "/proc");
    char *out = NULL, err[256] = "";
    int status = 0;
    int rc = portctl_execute(argv[1], payload, used, &paths, io, &out, &status, err, sizeof err);
    free(payload);
    if (rc) { fprintf(stderr, "%s\n", err); return status ? status : 1; }
    printf("%s\n", out);
    free(out);
    return 0;
}
#endif
