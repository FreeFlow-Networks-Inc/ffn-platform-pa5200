/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ffn-portctl against a fake BCM service, a fake BCM lifetime and in-memory
 * files: the attach state machine, its journal, the fabric preparation, every
 * rejection, and the readback parsers. */
#define PORTCTL_NO_MAIN
#include "ffn_portctl.c"
#include "ffn_json.c"
#include <assert.h>

/* ---- fake world ---- */

#define MAX_FILES 32
#define MAX_OPS 64

struct fake {
    /* files */
    char *names[MAX_FILES]; char *data[MAX_FILES]; size_t lens[MAX_FILES]; int files;
    /* hardware */
    int admin[64];            /* BCM port admin state: 0/1, -1 unknown */
    int redirect[64];         /* force-forward enabled per BCM port */
    int redirect_dst[64];
    int queues[64];           /* queues per BCM port */
    int trunk_header;
    int bundles;
    bool fail_queue_alloc;    /* op 1 leaves queues unchanged */
    bool cint_incomplete;
    bool service_down;
    /* lifetime */
    char epoch[80];
    /* lock behaviour */
    const char *busy_lock;    /* path suffix that reports busy */
    /* trace */
    char ops[MAX_OPS][96]; int nops;
};

static struct fake F;

static int find_file(const char *path) { for (int i = 0; i < F.files; i++) if (strcmp(F.names[i], path) == 0) return i; return -1; }

static int fake_read_file(void *ctx, const char *path, char **data, size_t *len)
{
    (void)ctx; int i = find_file(path);
    if (i < 0) return -1;
    *data = malloc(F.lens[i] + 1); memcpy(*data, F.data[i], F.lens[i]); (*data)[F.lens[i]] = 0; *len = F.lens[i];
    return 0;
}

static int fake_write_file(void *ctx, const char *path, const char *data, size_t len)
{
    (void)ctx; int i = find_file(path);
    if (i < 0) { assert(F.files < MAX_FILES); i = F.files++; F.names[i] = strdup(path); F.data[i] = NULL; }
    free(F.data[i]); F.data[i] = malloc(len + 1); memcpy(F.data[i], data, len); F.data[i][len] = 0; F.lens[i] = len;
    return 0;
}

static int fake_unlink(void *ctx, const char *path)
{
    (void)ctx; int i = find_file(path);
    if (i < 0) return 0;
    free(F.names[i]); free(F.data[i]);
    for (int j = i; j + 1 < F.files; j++) { F.names[j] = F.names[j + 1]; F.data[j] = F.data[j + 1]; F.lens[j] = F.lens[j + 1]; }
    F.files--;
    return 0;
}

static struct ffn_json *fake_read_json(void *ctx, const char *path)
{
    char *data; size_t len;
    if (fake_read_file(ctx, path, &data, &len)) return NULL;
    struct ffn_json *v = ffn_json_parse(data, len, NULL, 0); free(data); return v;
}

static int fake_write_json(void *ctx, const char *path, const struct ffn_json *value, char *err, size_t errsz)
{
    (void)err; (void)errsz;
    char *text = ffn_json_dump(value);
    int rc = fake_write_file(ctx, path, text, strlen(text));
    free(text);
    snprintf(F.ops[F.nops++ % MAX_OPS], 96, "write %s", strrchr(path, '/') + 1);
    return rc;
}

static int fake_epoch(void *ctx, char *out, size_t outsz, char *err, size_t errsz)
{
    (void)ctx; (void)err; (void)errsz;
    snprintf(out, outsz, "%s", F.epoch); return 0;
}

static int fake_lock(void *ctx, const char *path, int seconds, char *err, size_t errsz)
{
    (void)ctx;
    assert(seconds == PORTCTL_LOCK_SECONDS);
    if (F.busy_lock && strstr(path, F.busy_lock)) { snprintf(err, errsz, "Aggregate hardware lock busy"); return -1; }
    return 7;
}

static void fake_unlock(void *ctx, int fd) { (void)ctx; assert(fd == 7); }

static void trace(const char *fmt, int a, int b)
{
    snprintf(F.ops[F.nops++ % MAX_OPS], 96, fmt, a, b);
}

static struct ffn_json *markers_from(const char *first, const char *done)
{
    struct ffn_json *m = ffn_json_array();
    if (first) ffn_json_push(m, ffn_json_string(first));
    if (done) ffn_json_push(m, ffn_json_string(done));
    return m;
}

static struct ffn_json *fake_bcm(void *ctx, const struct ffn_json *request, char *err, size_t errsz)
{
    (void)ctx;
    if (F.service_down) { snprintf(err, errsz, "BCM service unreachable"); return NULL; }
    const char *op = ffn_json_get_string(request, "op");
    struct ffn_json *reply = ffn_json_object();
    ffn_json_set(reply, "ok", ffn_json_bool(true));
    if (!strcmp(op, "port.list")) {
        trace("port.list %d %d", 0, 0);
        struct ffn_json *ports = ffn_json_array();
        for (int p = 1; p < 64; p++) {
            if (F.admin[p] < 0) continue;
            struct ffn_json *row = ffn_json_object();
            ffn_json_set(row, "port", ffn_json_int(p)); ffn_json_set(row, "enabled", ffn_json_bool(F.admin[p] == 1));
            ffn_json_push(ports, row);
        }
        ffn_json_set(reply, "ports", ports);
        return reply;
    }
    if (!strcmp(op, "port.set")) {
        long long p; bool enable;
        assert(ffn_json_get_int(request, "port", &p) && ffn_json_get_bool(request, "enable", &enable));
        trace("port.set %d %d", (int)p, enable);
        F.admin[p] = enable;
        return reply;
    }
    assert(!strcmp(op, "cint.run"));
    assert(!strcmp(ffn_json_get_string(request, "script"), "ffn_bcm_forward_test.c"));
    char *script; size_t len;
    assert(fake_read_file(NULL, "/share/ffn_bcm_forward_test.c", &script, &len) == 0);
    ffn_json_set(reply, "completed", ffn_json_bool(!F.cint_incomplete)); ffn_json_set(reply, "truncated", ffn_json_bool(false));
    char line[160];
    if (strstr(script, "FFN_WAN_STATE")) {
        int mode, p;
        assert(sscanf(strstr(script, "int mode="), "int mode=%d; int p=%d;", &mode, &p) == 2);
        trace("redirect %d mode %d", p, mode);
        int rv = 0;
        if (mode == 1 && (F.trunk_header != 11 || F.queues[p] != 8 || F.queues[24] != 8)) rv = -1;
        if (mode && rv == 0) { F.redirect[p] = mode == 1; F.redirect_dst[p] = 24; }
        snprintf(line, sizeof line, "FFN_WAN_STATE header=%d wanq=%d trunkq=%d dst=%d enabled=%d rv=%d", F.trunk_header, F.queues[p], F.queues[24], F.redirect_dst[p], F.redirect[p], rv);
        ffn_json_set(reply, "markers", markers_from(line, rv == 0 ? "FFN_WAN_DONE" : NULL));
    } else {
        int p, opn;
        assert(strstr(script, "FFN_FABRIC_STATE"));
        assert(sscanf(strstr(script, "int p="), "int p=%d;int op=%d;", &p, &opn) == 2);
        trace("fabric %d op %d", p, opn);
        if (opn == 1 && !F.fail_queue_alloc) F.queues[p] = 8;
        if (opn == 2) F.trunk_header = 11;
        snprintf(line, sizeof line, "FFN_FABRIC_STATE port=%d queues=%d bundles=%d header=%d rv=0", p, F.queues[p], F.bundles, F.trunk_header);
        struct ffn_json *m = ffn_json_array();
        if (opn == 1) ffn_json_push(m, ffn_json_string("FFN_FABRIC_VOQ port=13 id=0x1 rv=0"));
        ffn_json_push(m, ffn_json_string(line)); ffn_json_push(m, ffn_json_string("FFN_FABRIC_DONE"));
        ffn_json_set(reply, "markers", m);
    }
    free(script);
    return reply;
}

static const struct portctl_io IO = { NULL, fake_bcm, fake_epoch, fake_lock, fake_unlock, fake_read_json, fake_write_json, fake_read_file, fake_write_file, fake_unlink };
static const struct portctl_paths PATHS = { "/etc", "/run", "/share/ffn_bcm_forward_test.c" };

static void put_text(const char *path, const char *text) { fake_write_file(NULL, path, text, strlen(text)); }

static void reset(void)
{
    for (int i = 0; i < F.files; i++) { free(F.names[i]); free(F.data[i]); }
    memset(&F, 0, sizeof F);
    for (int p = 0; p < 64; p++) F.admin[p] = -1;
    F.admin[13] = 1; F.admin[16] = 1; F.admin[24] = 1;
    F.trunk_header = 1;
    snprintf(F.epoch, sizeof F.epoch, "%s", "11111111-2222-3333-4444-555555555555:316:4630");
    put_text("/share/ffn_bcm_forward_test.c", "previous recipe");
}

static int run(const char *action, const char *payload, char **out, char *err)
{
    int status = 0;
    *out = NULL; err[0] = 0;
    int rc = portctl_execute(action, payload, strlen(payload), &PATHS, &IO, out, &status, err, 256);
    return rc ? status : 0;
}

static bool trace_has(const char *text) { for (int i = 0; i < F.nops && i < MAX_OPS; i++) if (strstr(F.ops[i], text)) return true; return false; }

#define START2 "{\"port\": 2, \"epoch\": \"11111111-2222-3333-4444-555555555555:316:4630\", \"dp_boot_id\": \"f09519c0-fb1d-41e1-a273-eac75a2f44fc\"}"
#define STOP2 START2

static void test_status_unattached(void)
{
    reset();
    char *out, err[256];
    assert(run("status", "{\"port\": 2}", &out, err) == 0);
    struct ffn_json *o = ffn_json_parse(out, strlen(out), NULL, 0);
    bool ready = true; assert(ffn_json_get_bool(o, "ready", &ready) && !ready);
    assert(!strcmp(ffn_json_get_string(o, "epoch"), F.epoch));
    assert(ffn_json_get(o, "state")->count == 0);
    assert(row_int(ffn_json_get(o, "hardware"), "enabled") == 0);
    assert(trace_has("redirect 13 mode 0"));
    /* the staged recipe was restored */
    assert(!strcmp(F.data[find_file("/share/ffn_bcm_forward_test.c")], "previous recipe"));
    ffn_json_free(o); free(out);
}

static void test_start_prepares_fabric_and_journals(void)
{
    reset();
    char *out, err[256];
    assert(run("start", START2, &out, err) == 0);
    struct ffn_json *o = ffn_json_parse(out, strlen(out), NULL, 0);
    bool ready = false; assert(ffn_json_get_bool(o, "ready", &ready) && ready);
    const struct ffn_json *state = ffn_json_get(o, "state");
    bool enabled = false; assert(ffn_json_get_bool(state, "enabled", &enabled) && enabled);
    assert(ffn_json_get(state, "pending")->type == FFN_JSON_NULL);
    assert(!strcmp(ffn_json_get_string(state, "dp_boot_id"), "f09519c0-fb1d-41e1-a273-eac75a2f44fc"));
    /* admin withdrawn, trunk header moved, queues on trunk and port, redirect set, admin restored */
    assert(trace_has("port.set 13 0")); assert(trace_has("fabric 24 op 2")); assert(trace_has("fabric 24 op 1")); assert(trace_has("fabric 13 op 1"));
    assert(trace_has("redirect 13 mode 1")); assert(trace_has("port.set 13 1"));
    assert(F.admin[13] == 1 && F.redirect[13] == 1 && F.redirect_dst[13] == 24 && F.trunk_header == 11 && F.queues[13] == 8);
    /* the journal: intent before the hardware change, then the settled state */
    assert(trace_has("write physical-2.json")); assert(trace_has("write packet-fabric.json")); assert(trace_has("write packet-fabric-last-result.json"));
    struct ffn_json *journal = fake_read_json(NULL, "/etc/physical-2.json");
    assert(ffn_json_get(journal, "pending")->type == FFN_JSON_NULL); ffn_json_free(journal);
    struct ffn_json *fabric = fake_read_json(NULL, "/etc/packet-fabric.json");
    assert(!ffn_json_get(fabric, "pending") && row_int(fabric, "header") == 11 && ffn_json_get(ffn_json_get(fabric, "ports"), "13")); ffn_json_free(fabric);
    ffn_json_free(o); free(out);
    /* a second start is idempotent: the owned redirect is re-asserted (mode 1, as the Python
     * owner does), but no administrative withdrawal and no fabric allocation happen */
    F.nops = 0; memset(F.ops, 0, sizeof F.ops);
    assert(run("start", START2, &out, err) == 0);
    assert(!trace_has("port.set") && !trace_has("fabric") && trace_has("redirect 13 mode 1"));
    free(out);
}

static void test_stop_withdraws(void)
{
    reset();
    char *out, err[256];
    assert(run("start", START2, &out, err) == 0); free(out);
    F.nops = 0;
    assert(run("stop", STOP2, &out, err) == 0);
    struct ffn_json *o = ffn_json_parse(out, strlen(out), NULL, 0);
    bool ready = true; assert(ffn_json_get_bool(o, "ready", &ready) && !ready);
    bool enabled = true; assert(ffn_json_get_bool(ffn_json_get(o, "state"), "enabled", &enabled) && !enabled);
    assert(trace_has("port.set 13 0") && trace_has("redirect 13 mode 2") && F.redirect[13] == 0 && F.admin[13] == 0);
    ffn_json_free(o); free(out);
}

static void test_rejections(void)
{
    char *out, err[256];
    reset();
    assert(run("start", "{\"port\": 1, \"epoch\": \"x\", \"dp_boot_id\": \"f09519c0-fb1d-41e1-a273-eac75a2f44fc\"}", &out, err) == 2 && strstr(err, "port 1 retains its WAN owner"));
    assert(run("status", "{\"port\": 25}", &out, err) == 2);
    assert(run("status", "{\"port\": 2, \"extra\": 1}", &out, err) == 2 && strstr(err, "Invalid physical operation"));
    assert(run("start", "{\"port\": 2, \"epoch\": \"x\", \"dp_boot_id\": \"F09519C0-FB1D-41E1-A273-EAC75A2F44FC\"}", &out, err) == 2 && strstr(err, "DP boot identity required"));
    assert(run("start", "{\"port\": 2, \"epoch\": \"stale:1:2\", \"dp_boot_id\": \"f09519c0-fb1d-41e1-a273-eac75a2f44fc\"}", &out, err) == 2 && strstr(err, "BCM lifetime changed"));
    assert(run("restart", "{\"port\": 2}", &out, err) == 2);
    assert(run("status", "not json", &out, err) == 2 && strstr(err, "Invalid physical request"));
    /* aggregate owner */
    reset();
    put_text("/etc/aggregate-hardware.json", "{\"groups\": {\"ae1\": {\"phase\": \"running\", \"ports\": [2, 5]}}}");
    assert(run("start", START2, &out, err) == 2 && strstr(err, "belongs to an aggregate owner"));
    put_text("/etc/aggregate-hardware.json", "{\"groups\": {\"ae1\": {\"phase\": \"stopped\", \"ports\": [2]}}}");
    assert(run("start", START2, &out, err) == 0); free(out);
    /* an unowned live redirect is never adopted */
    reset();
    F.redirect[13] = 1; F.redirect_dst[13] = 24;
    assert(run("start", START2, &out, err) == 2 && strstr(err, "Unowned or uncertain physical redirect"));
    /* stale DP owner cleanup */
    reset();
    assert(run("start", START2, &out, err) == 0); free(out);
    assert(run("stop", "{\"port\": 2, \"epoch\": \"11111111-2222-3333-4444-555555555555:316:4630\", \"dp_boot_id\": \"00000000-0000-4000-8000-000000000000\"}", &out, err) == 2 && strstr(err, "Refusing stale DP owner cleanup"));
    /* admin state unknown */
    reset(); F.admin[13] = -1;
    assert(run("start", START2, &out, err) == 2 && strstr(err, "Physical administrative state unavailable"));
}

static void test_lock_and_service_failures(void)
{
    char *out, err[256];
    reset(); F.busy_lock = "ffn-faceplate.lock";
    assert(run("status", "{\"port\": 2}", &out, err) == 1 && strstr(err, "Aggregate hardware lock busy"));
    reset(); F.busy_lock = "ffn-packet-fabric.lock";
    assert(run("start", START2, &out, err) == 1 && strstr(err, "Aggregate hardware lock busy"));
    reset(); F.service_down = true;
    assert(run("status", "{\"port\": 2}", &out, err) == 1 && strstr(err, "BCM service unreachable"));
    reset(); F.cint_incomplete = true;
    assert(run("status", "{\"port\": 2}", &out, err) == 1 && strstr(err, "WAN SDK operation incomplete"));
}

static void test_fabric_failure_is_journaled_and_withheld(void)
{
    char *out, err[256];
    reset(); F.fail_queue_alloc = true;
    assert(run("start", START2, &out, err) == 1 && strstr(err, "Packet queues not verified"));
    struct ffn_json *fabric = fake_read_json(NULL, "/etc/packet-fabric.json");
    assert(ffn_json_get(fabric, "pending")); ffn_json_free(fabric);
    /* the port journal was never written: nothing claims the redirect */
    assert(find_file("/etc/physical-2.json") < 0);
    F.fail_queue_alloc = false;
    assert(run("start", START2, &out, err) == 1 && strstr(err, "Uncertain packet fabric allocation; automatic retry withheld"));
    /* an occupied trunk with a foreign header is refused */
    reset(); F.trunk_header = 5; F.bundles = 3;
    assert(run("start", START2, &out, err) == 1 && strstr(err, "Cannot change an occupied packet trunk header"));
    /* a foreign queue layout is refused */
    reset(); F.queues[13] = 4;
    assert(run("start", START2, &out, err) == 1 && strstr(err, "Conflicting packet fabric queue layout"));
}

static void test_parsers(void)
{
    struct ffn_json *m = markers_from("FFN_WAN_STATE header=11 wanq=8 trunkq=8 dst=24 enabled=1 rv=0", "FFN_WAN_DONE"), *hw = NULL;
    char err[128];
    assert(portctl_parse_redirect(m, &hw, err, sizeof err) == 0 && row_int(hw, "destination") == 24 && row_int(hw, "enabled") == 1);
    ffn_json_free(hw); ffn_json_free(m);
    m = markers_from("FFN_WAN_STATE header=11 wanq=8 trunkq=8 dst=24 enabled=1 rv=-1", "FFN_WAN_DONE");
    assert(portctl_parse_redirect(m, &hw, err, sizeof err) == -1 && strstr(err, "WAN hardware readback failed")); ffn_json_free(m);
    m = markers_from("FFN_WAN_STATE header=11 wanq=8 trunkq=8 dst=24 enabled=1 rv=0", NULL);
    assert(portctl_parse_redirect(m, &hw, err, sizeof err) == -1); ffn_json_free(m);
    m = markers_from("FFN_WAN_STATE header=11 wanq=8 trunkq=8 dst=24 enabled=1 rv=01", "FFN_WAN_DONE");
    assert(portctl_parse_redirect(m, &hw, err, sizeof err) == -1); ffn_json_free(m);
    struct ffn_json *row = NULL;
    m = markers_from("FFN_FABRIC_STATE port=13 queues=8 bundles=2 header=11 rv=0", "FFN_FABRIC_DONE");
    assert(portctl_parse_fabric(m, 13, &row, err, sizeof err) == 0 && row_int(row, "queues") == 8 && ffn_json_get(row, "allocations")->count == 0);
    ffn_json_free(row); ffn_json_free(m);
    m = markers_from("FFN_FABRIC_STATE port=14 queues=8 bundles=2 header=11 rv=0", "FFN_FABRIC_DONE");
    assert(portctl_parse_fabric(m, 13, &row, err, sizeof err) == -1 && strstr(err, "port mismatch")); ffn_json_free(m);
    assert(portctl_canonical_uuid("f09519c0-fb1d-41e1-a273-eac75a2f44fc") == 0);
    assert(portctl_canonical_uuid("F09519C0-fb1d-41e1-a273-eac75a2f44fc") == -1);
    assert(portctl_canonical_uuid("f09519c0fb1d41e1a273eac75a2f44fc") == -1);
    assert(portctl_bcm_port(1) == 28 && portctl_bcm_port(2) == 13 && portctl_bcm_port(5) == 16 && portctl_bcm_port(24) == 35 && portctl_bcm_port(25) == 0);
}

static void test_json_roundtrip(void)
{
    const char *text = "{\"a\": [1, -2, true, null, \"x\\n\\u00e9\"], \"b\": {\"c\": false}}";
    struct ffn_json *v = ffn_json_parse(text, strlen(text), NULL, 0);
    assert(v);
    char *d = ffn_json_dump(v);
    assert(!strcmp(d, "{\"a\": [1, -2, true, null, \"x\\n\xc3\xa9\"], \"b\": {\"c\": false}}"));
    free(d); ffn_json_free(v);
    char err[64];
    assert(!ffn_json_parse("{\"a\": 1.5}", 10, err, sizeof err) && strstr(err, "non-integer"));
    assert(!ffn_json_parse("[1,]", 4, err, sizeof err));
    assert(!ffn_json_parse("{} x", 4, err, sizeof err) && strstr(err, "trailing"));
}

int main(void)
{
    test_json_roundtrip();
    test_parsers();
    test_status_unattached();
    test_start_prepares_fabric_and_journals();
    test_stop_withdraws();
    test_rejections();
    test_lock_and_service_failures();
    test_fabric_failure_is_journaled_and_withheld();
    puts("test_portctl: ok");
    return 0;
}
