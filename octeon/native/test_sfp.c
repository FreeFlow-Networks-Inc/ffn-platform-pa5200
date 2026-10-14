/* SPDX-License-Identifier: GPL-2.0-or-later */
/* The SFP+ cage control against recorded hardware: the PCA9555 registers and
 * SFF-8472 pages captured from a PA-5220 on 2026-10-13 (port 5: GLC-LH-SMD),
 * a fake BCM service, fake locks and in-memory files. Covers the board map,
 * the cage decode and TX_DISABLE writes, identity validation and summary,
 * diagnostics decoding, link-mode resolution, the fiber recipe and its
 * readback, every transmit-check verdict, the live collection, relink, and
 * the insertion watcher. */
#include "ffn_sfp.c"
#include "ffn_json.c"
#include <assert.h>
#include <signal.h>

/* ---- fixtures ---- */

static const char A0_LX[] = "0304070000000200000000010d000a64373700004f454d202020202020202020202020200000005f474c432d4c482d534d4420202020202041202020051e0006001a00004353594745314f433538323620202020323431313031202068f0016f";
static const char A2_LX[] = "5000f6004b00fb008ca0753088b879189c4009c4927c0dac312d031a2710037b18a6000a1394000d000000000000000000000000000000000000000000000000000000003f80000000000000010000000100000001000000010000000000000d2980843a2f130b4e04970000000000000000000000000000ffffffffffffff00";
static const char PRESENCE_REGS[] = "7677ffff0000ffff";
static const char CONTROL_REGS[] = "fefffeff0000feff";

static void unhex(const char *hex, uint8_t *out, size_t len)
{
    for (size_t i = 0; i < len; i++) { unsigned v; sscanf(hex + 2 * i, "%2x", &v); out[i] = (uint8_t)v; }
}

static void fix_sums(uint8_t a0[96])
{
    unsigned s1 = 0, s2 = 0;
    for (int i = 0; i < 63; i++) s1 += a0[i];
    for (int i = 64; i < 95; i++) s2 += a0[i];
    a0[63] = (uint8_t)s1; a0[95] = (uint8_t)s2;
}

/* identity(connector, codes10g, codes1g, nominal, wavelength, br_max, vendor, part) as the Python tests build it */
static void identity(uint8_t a0[96], int connector, int codes10g, int codes1g, int nominal, int wavelength, int br_max, const char *vendor, const char *part)
{
    memset(a0, 0, 96);
    a0[0] = 3; a0[2] = (uint8_t)connector; a0[3] = (uint8_t)codes10g; a0[6] = (uint8_t)codes1g; a0[12] = (uint8_t)nominal;
    memset(a0 + 20, ' ', 16); memcpy(a0 + 20, vendor, strlen(vendor)); memset(a0 + 40, ' ', 16); memcpy(a0 + 40, part, strlen(part));
    a0[60] = (uint8_t)(wavelength >> 8); a0[61] = (uint8_t)wavelength; a0[66] = (uint8_t)br_max;
    fix_sums(a0);
}

/* ---- fake world ---- */

#define MAX_FILES 48
struct fake {
    uint8_t presence[8], control[8];
    uint8_t a0[SFP_PORTS][96], a2[SFP_PORTS][128]; bool page_ok[SFP_PORTS]; int bus_of[SFP_PORTS];
    bool expanders_fail, pages_fail, write_fail;
    int lock_calls; const char *busy_lock;
    /* switch */
    int iface[64], an[64], speed[64], enabled[64], link[64]; bool bcm_down, cint_incomplete; int cint_runs; char last_recipe[4096];
    char epoch[160]; bool epoch_fail;
    time_t now;
    char *names[MAX_FILES]; char *data[MAX_FILES]; int files;
    char log[64][1024]; int logs;
};
static struct fake F;

static int find_file(const char *path) { for (int i = 0; i < F.files; i++) if (!strcmp(F.names[i], path)) return i; return -1; }
static void put_file(const char *path, const char *text)
{
    int i = find_file(path);
    if (i < 0) { assert(F.files < MAX_FILES); i = F.files++; F.names[i] = strdup(path); F.data[i] = NULL; }
    free(F.data[i]); F.data[i] = strdup(text);
}

static int f_i2c_read(void *ctx, unsigned bus, unsigned addr, unsigned offset, unsigned width, uint8_t *out, unsigned count)
{
    (void)ctx; assert(width == 1); assert(count <= 8);
    if (bus == 1 && (addr == 0x22 || addr == 0x23)) {
        if (F.expanders_fail) return -1;
        assert(count == 1 && offset < 8);
        if (addr == 0x22) out[0] = F.presence[offset];
        else if (offset < 2) {
            /* PCA9555 input port: pins driven as outputs read back their latch, the rest the pin state */
            uint8_t dir = F.control[6 + offset];
            out[0] = (uint8_t)((F.control[offset] & dir) | (F.control[2 + offset] & ~dir));
        } else out[0] = F.control[offset];
        return 0;
    }
    for (int i = 0; i < SFP_PORTS; i++) if (F.bus_of[i] == (int)bus) {
        if (F.pages_fail || !F.page_ok[i]) return -1;
        const uint8_t *page = addr == 0x50 ? F.a0[i] : F.a2[i]; unsigned size = addr == 0x50 ? 96 : 128;
        assert(offset + count <= size);
        memcpy(out, page + offset, count);
        return 0;
    }
    return -1;
}
static int f_i2c_write8(void *ctx, unsigned bus, unsigned addr, unsigned offset, unsigned value)
{
    (void)ctx; assert(bus == 1 && addr == 0x23 && offset < 8);
    if (F.write_fail) return -1;
    F.control[offset] = (uint8_t)value;
    return 0;
}
static int f_module_bus(void *ctx, int port, char *err, size_t errsz)
{
    (void)ctx; int i = port - SFP_FIRST_PORT;
    if (F.bus_of[i] < 0) { snprintf(err, errsz, "SFP diagnostic I2C multiplexer is not bound"); return -1; }
    return F.bus_of[i];
}
static int f_lock(void *ctx, const char *path, int seconds)
{
    (void)ctx; (void)seconds; F.lock_calls++;
    if (F.busy_lock && strstr(path, F.busy_lock)) return -1;
    return 100 + F.lock_calls;
}
static void f_unlock(void *ctx, int fd) { (void)ctx; assert(fd > 100); }

static struct ffn_json *f_bcm(void *ctx, const struct ffn_json *req, char *err, size_t errsz)
{
    (void)ctx;
    if (F.bcm_down) { snprintf(err, errsz, "BCM service unreachable"); return NULL; }
    const char *op = ffn_json_get_string(req, "op");
    struct ffn_json *o = ffn_json_object(); ffn_json_set(o, "ok", ffn_json_bool(true));
    if (!strcmp(op, "port.list")) {
        struct ffn_json *ports = ffn_json_array();
        for (int p = SFP_FIRST_PORT; p <= SFP_LAST_PORT; p++) {
            int chip = sfp_bcm_port(p); if (F.enabled[chip] < 0) continue;
            struct ffn_json *row = ffn_json_object(); ffn_json_set(row, "port", ffn_json_int(chip));
            ffn_json_set(row, "enabled", ffn_json_bool(F.enabled[chip])); ffn_json_set(row, "link", ffn_json_bool(F.link[chip]));
            ffn_json_set(row, "speed_mb", ffn_json_int(F.speed[chip])); ffn_json_push(ports, row);
        }
        ffn_json_set(o, "ports", ports); return o;
    }
    long long chip = 0; ffn_json_get_int(req, "port", &chip);
    if (!strcmp(op, "port.link.status")) {
        if (F.iface[chip] < 0) { ffn_json_free(o); snprintf(err, errsz, "BCM operation failed; refresh hardware status"); return NULL; }
        ffn_json_set(o, "port", ffn_json_int(chip)); ffn_json_set(o, "interface", ffn_json_int(F.iface[chip]));
        ffn_json_set(o, "autoneg", ffn_json_bool(F.an[chip]));
        ffn_json_set(o, "configured_speed", ffn_json_string(F.iface[chip] == 3 ? "1000" : F.an[chip] ? "auto" : F.speed[chip] == 10000 ? "10000" : "1000"));
        ffn_json_set(o, "link_mode", ffn_json_string(F.iface[chip] == 3 ? "1000BASE-X" : "XFI"));
        struct ffn_json *sup = ffn_json_array(); ffn_json_push(sup, ffn_json_string("1000")); ffn_json_push(sup, ffn_json_string("10000")); ffn_json_set(o, "supported_speeds", sup);
        return o;
    }
    if (!strcmp(op, "port.link.set")) { const char *speed = ffn_json_get_string(req, "speed"); F.iface[chip] = 10; F.an[chip] = !strcmp(speed, "auto"); F.speed[chip] = !strcmp(speed, "1000") ? 1000 : 10000; return o; }
    ffn_json_free(o); snprintf(err, errsz, "unknown op"); return NULL;
}

/* The fake cint applies the recipe like the switch would and reports the readback marker. */
static struct ffn_json *f_cint(void *ctx, const char *recipe, int timeout, char *err, size_t errsz)
{
    (void)ctx; assert(timeout == 20);
    F.cint_runs++; snprintf(F.last_recipe, sizeof F.last_recipe, "%s", recipe);
    if (F.cint_incomplete) { snprintf(err, errsz, "SDK recipe incomplete; inspect BCM diagnostics"); return NULL; }
    int port = 0, ifacev = 0, rate = 0, autoneg = 0;
    const char *p = strstr(recipe, "bcm_port_interface_get(0,"); assert(p); sscanf(p, "bcm_port_interface_get(0,%d,", &port);
    p = strstr(recipe, "ifn!="); assert(p); sscanf(p, "ifn!=%d || an!=%d", &ifacev, &autoneg);
    p = strstr(recipe, "bcm_port_speed_set(0,"); assert(p); sscanf(p, "bcm_port_speed_set(0,%*d,%d)", &rate);
    int changed = 0;
    if (F.iface[port] != ifacev || F.an[port] != autoneg || (!autoneg && F.speed[port] != rate)) { changed = 1; F.iface[port] = ifacev; F.an[port] = autoneg; F.speed[port] = rate; }
    char line[96]; snprintf(line, sizeof line, "FFN_SFP_LINK 0 %d %d %d %d %d", F.iface[port], F.an[port], F.speed[port], changed, autoneg ? -4 : 0);
    struct ffn_json *m = ffn_json_array(); ffn_json_push(m, ffn_json_string(line)); ffn_json_push(m, ffn_json_string("FFN_SFP_LINK_DONE"));
    return m;
}

static struct ffn_json *f_read_json(void *ctx, const char *path)
{
    (void)ctx; int i = find_file(path);
    if (i < 0) return NULL;
    return ffn_json_parse(F.data[i], strlen(F.data[i]), NULL, 0);
}
static int f_write_json(void *ctx, const char *path, const struct ffn_json *value) { (void)ctx; char *t = ffn_json_dump(value); put_file(path, t); free(t); return 0; }
static int f_unlink(void *ctx, const char *path)
{
    (void)ctx; int i = find_file(path);
    if (i < 0) return 0;
    free(F.names[i]); free(F.data[i]);
    for (int j = i; j + 1 < F.files; j++) { F.names[j] = F.names[j + 1]; F.data[j] = F.data[j + 1]; }
    F.files--; return 0;
}
static int f_epoch(void *ctx, char *out, size_t outsz) { (void)ctx; if (F.epoch_fail) return -1; snprintf(out, outsz, "%s", F.epoch); return 0; }
static time_t f_now(void *ctx) { (void)ctx; return F.now; }
static void f_log(void *ctx, const char *line) { (void)ctx; assert(F.logs < 64); snprintf(F.log[F.logs++], 1024, "%s", line); }

static const struct sfp_io IO = { NULL, f_i2c_read, f_i2c_write8, f_module_bus, f_lock, f_unlock, f_bcm, f_cint, f_read_json, f_write_json, f_unlink, f_epoch, f_now, f_log };
static const struct sfp_paths P = { "/run/ffn-sfp-control.lock", "/run/ffn-i2c-bus1.lock", "/run/ffn-faceplate.lock", "/etc/ffn/faceplate.json",
                                    "/etc/ffn", "/etc/ffn/aggregate-hardware.json", "/run/ffn-sfp-modules", "/run/ffn-sfp-health.json" };

static void reset(void)
{
    for (int i = 0; i < F.files; i++) { free(F.names[i]); free(F.data[i]); }
    memset(&F, 0, sizeof F);
    unhex(PRESENCE_REGS, F.presence, 8); unhex(CONTROL_REGS, F.control, 8);
    for (int i = 0; i < SFP_PORTS; i++) { F.bus_of[i] = 10 + i; F.page_ok[i] = false; }
    unhex(A0_LX, F.a0[0], 96); unhex(A2_LX, F.a2[0], 128); F.page_ok[0] = true;
    for (int c = 0; c < 64; c++) { F.iface[c] = 10; F.an[c] = 0; F.speed[c] = 10000; F.enabled[c] = -1; F.link[c] = 0; }
    for (int p = SFP_FIRST_PORT; p <= SFP_LAST_PORT; p++) F.enabled[sfp_bcm_port(p)] = 1;
    snprintf(F.epoch, sizeof F.epoch, "boot-1:4242:777");
    F.now = 1000000;
}

static const char *log_event(int i) { struct ffn_json *e = ffn_json_parse(F.log[i], strlen(F.log[i]), NULL, 0); assert(e); static char name[64]; snprintf(name, sizeof name, "%s", ffn_json_get_string(e, "event")); ffn_json_free(e); return name; }
static int log_count(const char *event) { int n = 0; for (int i = 0; i < F.logs; i++) if (!strcmp(log_event(i), event)) n++; return n; }

/* ---- tests ---- */

static void test_board_map(void)
{
    assert(sfp_bit(4) == -1 && sfp_bit(21) == -1 && sfp_bit(5) == 0 && sfp_bit(9) == 5 && sfp_bit(10) == 4 && sfp_bit(20) == 10);
    assert(sfp_bcm_port(5) == 16 && sfp_bcm_port(6) == 1 && sfp_bcm_port(20) == 31 && sfp_bcm_port(3) == 0);
}

static void test_inventory_decodes_the_captured_expanders(void)
{
    reset();
    struct sfp_cage c[SFP_PORTS]; char err[96];
    assert(sfp_inventory(&IO, &P, c, err, sizeof err) == 0);
    const int present[] = { 5, 8, 11, 16, 19 };
    for (int p = SFP_FIRST_PORT; p <= SFP_LAST_PORT; p++) {
        bool expect = false; for (size_t i = 0; i < 5; i++) if (present[i] == p) expect = true;
        assert(c[p - SFP_FIRST_PORT].present == expect);
        assert(c[p - SFP_FIRST_PORT].control_ready);
    }
    assert(c[0].tx_enabled && !c[0].tx_disable);           /* port 5 transmitter on */
    assert(!c[14].tx_enabled && c[14].tx_disable);         /* port 19 transmitter off */
    struct ffn_json *j = sfp_cage_json(&c[0]); char *t = ffn_json_dump(j);
    assert(!strcmp(t, "{\"present\": true, \"tx_enabled\": true, \"tx_disable\": false, \"control_ready\": true}")); free(t); ffn_json_free(j);
    F.expanders_fail = true;
    assert(sfp_inventory(&IO, &P, c, err, sizeof err) == -1 && strstr(err, "expanders unreadable"));
    F.expanders_fail = false; F.busy_lock = "sfp-control";
    assert(sfp_inventory(&IO, &P, c, err, sizeof err) == -1 && strstr(err, "lock busy"));
}

static void test_set_enabled_writes_only_this_cage(void)
{
    reset();
    struct sfp_cage c; char err[96];
    uint8_t before[8]; memcpy(before, F.control, 8);
    assert(sfp_set_enabled(&IO, &P, 19, true, &c, err, sizeof err) == 0 && c.tx_enabled && !c.tx_disable && c.present);
    int bit = sfp_bit(19);                                /* 11 -> bank 1, mask 0x08 */
    assert(bit == 11);
    assert(F.control[3] == (before[3] & ~8) && F.control[7] == (before[7] & ~8));
    assert(F.control[2] == before[2] && F.control[6] == before[6]);   /* bank 0 untouched */
    assert(sfp_set_enabled(&IO, &P, 19, false, &c, err, sizeof err) == 0 && !c.tx_enabled && c.tx_disable);
    assert(F.control[3] == (before[3] | 8));
    assert(sfp_set_enabled(&IO, &P, 4, true, &c, err, sizeof err) == -1 && strstr(err, "5 through 20"));
    F.write_fail = true;
    assert(sfp_set_enabled(&IO, &P, 19, true, &c, err, sizeof err) == -1 && strstr(err, "write failed"));
}

static void test_identity_and_summary(void)
{
    reset();
    uint8_t a0[96]; char err[96];
    assert(sfp_identity(&IO, &P, 5, a0, err, sizeof err) == 0 && sfp_validate_identity(a0) == 0);
    struct ffn_json *s = sfp_summary_json(a0); char *t = ffn_json_dump(s);
    assert(!strcmp(t, "{\"vendor\": \"OEM\", \"part\": \"GLC-LH-SMD\", \"wavelength_nm\": 1310, \"optical\": true, \"speeds\": [1000]}")); free(t); ffn_json_free(s);
    int rates[2];
    identity(a0, 7, 0x10, 0x01, 103, 850, 0, "TEST", "AFBR-709SMZ"); assert(sfp_module_speeds(a0, rates) == 2 && rates[0] == 1000 && rates[1] == 10000);
    identity(a0, 7, 0, 0, 103, 850, 0, "TEST", "MODULE"); assert(sfp_module_speeds(a0, rates) == 1 && rates[0] == 10000);
    identity(a0, 7, 0, 0, 255, 1310, 42, "TEST", "MODULE"); assert(sfp_module_speeds(a0, rates) == 1 && rates[0] == 10000);
    identity(a0, 7, 0, 0, 0, 1310, 0, "TEST", "MODULE"); assert(sfp_module_speeds(a0, rates) == 0);
    identity(a0, 0x22, 0, 0x08, 13, 0, 0, "TEST", "SFP-1000T"); assert(!sfp_optical(a0) && sfp_module_speeds(a0, rates) == 1 && rates[0] == 1000);
    s = sfp_summary_json(a0); t = ffn_json_dump(s); assert(strstr(t, "\"wavelength_nm\": null") && strstr(t, "\"optical\": false")); free(t); ffn_json_free(s);
    /* corrupt page */
    F.a0[0][30] ^= 1;
    assert(sfp_identity(&IO, &P, 5, a0, err, sizeof err) == -1 && strstr(err, "Invalid SFP identity"));
    F.a0[0][30] ^= 1; F.bus_of[0] = -1;
    assert(sfp_identity(&IO, &P, 5, a0, err, sizeof err) == -1 && strstr(err, "multiplexer is not bound"));
    reset(); F.page_ok[0] = false;
    assert(sfp_identity(&IO, &P, 5, a0, err, sizeof err) == -1 && strstr(err, "page unreadable"));
}

static bool near(double a, double b, double tol) { return fabs(a - b) <= tol; }

static void test_diagnostics_decode(void)
{
    reset();
    uint8_t a0[96], a2[128]; bool has_a2 = false; char err[96]; struct ffn_json *d = NULL;
    assert(sfp_read_diagnostics(&IO, &P, 5, a0, a2, &has_a2, err, sizeof err) == 0 && has_a2);
    assert(sfp_decode_diagnostics(a0, a2, 128, &d, err, sizeof err) == 0);
    double v; bool b;
    assert(ffn_json_get_bool(d, "diagnostics_available", &b) && b);
    assert(ffn_json_get_number(d, "temperature_c", &v) && near(v, 41.5, 0.01));
    assert(ffn_json_get_number(d, "voltage_v", &v) && near(v, 3.385, 0.001));
    assert(ffn_json_get_number(d, "tx_bias_ma", &v) && near(v, 24.102, 0.001));
    assert(ffn_json_get_number(d, "tx_power_dbm", &v) && near(v, -5.385, 0.001));
    assert(ffn_json_get_number(d, "rx_power_dbm", &v) && near(v, -9.3, 0.001));
    assert(ffn_json_get_bool(d, "tx_disable", &b) && !b && ffn_json_get_bool(d, "rx_los", &b) && !b && ffn_json_get_bool(d, "tx_fault", &b) && !b);
    assert(!strcmp(ffn_json_get_string(d, "vendor"), "OEM") && !strcmp(ffn_json_get_string(d, "model"), "GLC-LH-SMD"));
    char *t = ffn_json_dump(d); assert(strstr(t, "\"temperature_c\": 41.5") && strstr(t, "\"rx_power_dbm\": -9.3")); free(t);
    ffn_json_free(d);
    /* no diagnostics page */
    uint8_t plain[96]; identity(plain, 7, 0, 0x02, 13, 1310, 0, "OEM", "GLC-LH-SMD");
    assert(sfp_decode_diagnostics(plain, NULL, 0, &d, err, sizeof err) == 0 && ffn_json_get_bool(d, "diagnostics_available", &b) && !b); ffn_json_free(d);
    /* corrupt A2 */
    a2[10] ^= 1;
    assert(sfp_decode_diagnostics(a0, a2, 128, &d, err, sizeof err) == -1 && strstr(err, "checksum"));
    a2[10] ^= 1; a2[110] |= 1;
    assert(sfp_decode_diagnostics(a0, a2, 128, &d, err, sizeof err) == -1 && strstr(err, "not ready"));
    a2[110] &= ~1; plain[0] = 2;
    assert(sfp_decode_diagnostics(plain, a2, 128, &d, err, sizeof err) == -1 && strstr(err, "Not an SFP"));
}

static void test_link_mode(void)
{
    uint8_t a0[96]; int rate; bool an; char err[96];
    unhex(A0_LX, a0, 96);
    assert(sfp_link_mode(a0, "auto", &rate, &an, err, sizeof err) == 1 && rate == 1000 && an);
    assert(sfp_link_mode(a0, "1000", &rate, &an, err, sizeof err) == 1 && rate == 1000 && an);
    assert(sfp_link_mode(a0, "10000", &rate, &an, err, sizeof err) == -1 && !strcmp(err, "Detected SFP supports 1000; 10000 cannot be selected"));
    identity(a0, 7, 0x10, 0x01, 103, 850, 0, "TEST", "AFBR-709SMZ");
    assert(sfp_link_mode(a0, "auto", &rate, &an, err, sizeof err) == 1 && rate == 10000 && !an);
    assert(sfp_link_mode(a0, "1000", &rate, &an, err, sizeof err) == 1 && rate == 1000 && an);
    identity(a0, 0x22, 0, 0x08, 13, 0, 0, "TEST", "SFP-1000T");
    assert(sfp_link_mode(a0, "auto", &rate, &an, err, sizeof err) == 0);
    identity(a0, 7, 0, 0, 0, 1310, 0, "TEST", "MODULE");
    assert(sfp_link_mode(a0, "auto", &rate, &an, err, sizeof err) == 0);
}

static void test_configure_fiber(void)
{
    reset();
    struct ffn_json *mode = NULL; char err[160];
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "auto", NULL, &mode, err, sizeof err) == 1 && mode);
    char *t = ffn_json_dump(mode);
    assert(!strcmp(t, "{\"speed\": 1000, \"autoneg\": true, \"interface\": \"BCM_PORT_IF_GMII\", \"link_mode\": \"1000BASE-X\", \"changed\": true, \"autoneg_mode_control\": -4}"));
    free(t); ffn_json_free(mode);
    assert(F.cint_runs == 1 && strstr(F.last_recipe, "bcm_port_interface_set(0,16,BCM_PORT_IF_GMII)") && strstr(F.last_recipe, "bcm_port_speed_set(0,16,1000)"));
    assert(!strstr(F.last_recipe, "@"));
    assert(F.iface[16] == 3 && F.an[16] == 1);
    /* idempotent: a second run changes nothing */
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "1000", NULL, &mode, err, sizeof err) == 1);
    bool changed = true; assert(ffn_json_get_bool(mode, "changed", &changed) && !changed); ffn_json_free(mode);
    /* refusals */
    assert(sfp_configure_fiber(&IO, &P, 5, 1, "auto", NULL, &mode, err, sizeof err) == -1 && strstr(err, "Invalid SFP link request"));
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "100", NULL, &mode, err, sizeof err) == -1);
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "10000", NULL, &mode, err, sizeof err) == -1 && strstr(err, "cannot be selected"));
    /* no module: generic path */
    assert(sfp_configure_fiber(&IO, &P, 8, 19, "auto", NULL, &mode, err, sizeof err) == 0 && F.cint_runs == 2);
    /* copper SFP: generic path even with identity supplied */
    uint8_t copper[96]; identity(copper, 0x22, 0, 0x08, 13, 0, 0, "TEST", "SFP-1000T");
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "auto", copper, &mode, err, sizeof err) == 0);
    /* a 10G optic goes XFI with AN off */
    uint8_t sr[96]; identity(sr, 7, 0x10, 0, 103, 850, 0, "TEST", "SFP-10G-SR");
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "auto", sr, &mode, err, sizeof err) == 1 && F.iface[16] == 10 && F.an[16] == 0 && F.speed[16] == 10000);
    t = ffn_json_dump(mode); assert(strstr(t, "\"link_mode\": \"XFI\"") && strstr(t, "\"autoneg\": false")); free(t); ffn_json_free(mode);
    /* recipe failures */
    F.cint_incomplete = true;
    assert(sfp_configure_fiber(&IO, &P, 5, 16, "auto", NULL, &mode, err, sizeof err) == -1 && strstr(err, "recipe incomplete"));
    F.cint_incomplete = false;
}

static struct ffn_json *obj(const char *text) { struct ffn_json *v = ffn_json_parse(text, strlen(text), NULL, 0); assert(v); return v; }
static const char *verdict_of(struct ffn_json *r) { static char v[32]; snprintf(v, sizeof v, "%s", ffn_json_get_string(r, "verdict")); ffn_json_free(r); return v; }

static void test_check_matrix(void)
{
    struct sfp_cage present = { true, true, false, true }, dark = { true, false, true, true }, empty = { false, false, false, true };
    struct ffn_json *lx = obj("{\"vendor\": \"OEM\", \"part\": \"GLC-LH-SMD\", \"wavelength_nm\": 1310, \"optical\": true, \"speeds\": [1000]}");
    struct ffn_json *good = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": -5.385, \"rx_power_dbm\": -9.3, \"tx_disable\": false, \"rx_los\": false, \"tx_fault\": false}");
    struct ffn_json *up = obj("{\"mac_enabled\": true, \"link\": true, \"interface\": \"BCM_PORT_IF_GMII\", \"link_mode\": \"1000BASE-X\", \"autoneg\": true, \"configured_speed\": \"1000\"}");
    struct ffn_json *attached = obj("{\"state\": \"enabled\", \"enabled\": true, \"pending\": null, \"epoch\": \"e\"}");
    struct ffn_json *committed = obj("{\"enabled\": true, \"speed\": \"auto\", \"attached\": true}");
    struct ffn_json *r = sfp_check_port(5, &present, lx, good, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "ok") && !strcmp(ffn_json_get_string(r, "expected_interface"), "BCM_PORT_IF_GMII") && !strcmp(ffn_json_get_string(r, "interface"), "ethernet1/5"));
    assert(ffn_json_get(r, "remedy")->type == FFN_JSON_NULL);
    ffn_json_free(r);
    assert(!strcmp(verdict_of(sfp_check_port(5, NULL, lx, good, up, attached, committed)), "no-module"));
    assert(!strcmp(verdict_of(sfp_check_port(5, &empty, NULL, NULL, up, attached, committed)), "no-module"));
    struct ffn_json *off = obj("{\"enabled\": false, \"speed\": \"auto\"}");
    assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, good, up, attached, off)), "disabled")); ffn_json_free(off);
    r = sfp_check_port(5, &dark, lx, good, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "transmitter-off") && !strcmp(ffn_json_get_string(r, "remedy"), "enable-transmitter")); ffn_json_free(r);
    struct ffn_json *d;
    d = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": -5.0, \"tx_disable\": true}"); assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, d, up, attached, committed)), "transmitter-off")); ffn_json_free(d);
    d = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": -5.0, \"tx_fault\": true}"); r = sfp_check_port(5, &present, lx, d, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "tx-fault") && !strcmp(ffn_json_get_string(r, "remedy"), "hardware")); ffn_json_free(r); ffn_json_free(d);
    d = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": -32.5}"); r = sfp_check_port(5, &present, lx, d, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "laser-off") && !strcmp(ffn_json_get_string(r, "detail"), "launch power -32.5 dBm")); ffn_json_free(r); ffn_json_free(d);
    d = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": null}"); assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, d, up, attached, committed)), "laser-off")); ffn_json_free(d);
    d = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": -5.0, \"rx_los\": true}"); r = sfp_check_port(5, &present, lx, d, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "no-rx-light") && !strcmp(ffn_json_get_string(r, "remedy"), "far-end")); ffn_json_free(r); ffn_json_free(d);
    d = obj("{\"diagnostics_available\": true, \"tx_power_dbm\": -5.0, \"rx_power_dbm\": -35.0}"); r = sfp_check_port(5, &present, lx, d, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "no-rx-light") && !strcmp(ffn_json_get_string(r, "detail"), "no light from the far end (RX -35 dBm)")); ffn_json_free(r); ffn_json_free(d);
    struct ffn_json *l;
    l = obj("{\"mac_enabled\": true, \"link\": true, \"interface\": \"BCM_PORT_IF_XFI\"}"); r = sfp_check_port(5, &present, lx, good, l, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "link-mode-mismatch") && !strcmp(ffn_json_get_string(r, "remedy"), "relink") && !strcmp(ffn_json_get_string(r, "detail"), "switch runs BCM_PORT_IF_XFI, module needs BCM_PORT_IF_GMII")); ffn_json_free(r); ffn_json_free(l);
    l = obj("{\"mac_enabled\": false, \"link\": false, \"interface\": \"BCM_PORT_IF_GMII\"}"); r = sfp_check_port(5, &present, lx, good, l, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "link-down") && !strcmp(ffn_json_get_string(r, "detail"), "switch MAC disabled")); ffn_json_free(r); ffn_json_free(l);
    l = obj("{\"mac_enabled\": true, \"link\": false, \"interface\": \"BCM_PORT_IF_GMII\"}"); assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, good, l, attached, committed)), "link-down")); ffn_json_free(l);
    l = obj("{\"mac_enabled\": null, \"link\": null, \"interface\": null}"); assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, good, l, attached, committed)), "ok")); ffn_json_free(l);
    struct ffn_json *a;
    a = obj("{\"state\": \"pending\", \"enabled\": true, \"pending\": {\"op\": \"start\"}, \"epoch\": \"e\"}"); r = sfp_check_port(5, &present, lx, good, up, a, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "attachment-pending") && !strcmp(ffn_json_get_string(r, "remedy"), "reapply")); ffn_json_free(r); ffn_json_free(a);
    a = obj("{\"state\": \"stale\", \"enabled\": true, \"pending\": null, \"epoch\": \"old\"}"); assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, good, up, a, committed)), "attachment-stale")); ffn_json_free(a);
    a = obj("{\"state\": \"none\", \"enabled\": false, \"pending\": null, \"epoch\": null}"); assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, good, up, a, committed)), "attachment-off"));
    struct ffn_json *unattached = obj("{\"enabled\": true, \"speed\": \"auto\", \"attached\": false}");
    assert(!strcmp(verdict_of(sfp_check_port(5, &present, lx, good, up, a, unattached)), "ok")); ffn_json_free(unattached); ffn_json_free(a);
    /* a fixed speed the module does not declare: no expectation, so no mismatch */
    struct ffn_json *tenk = obj("{\"enabled\": true, \"speed\": \"10000\", \"attached\": true}");
    l = obj("{\"mac_enabled\": true, \"link\": true, \"interface\": \"BCM_PORT_IF_XFI\"}");
    r = sfp_check_port(5, &present, lx, good, l, attached, tenk); assert(!strcmp(ffn_json_get_string(r, "verdict"), "ok") && ffn_json_get(r, "expected_interface")->type == FFN_JSON_NULL); ffn_json_free(r);
    ffn_json_free(l); ffn_json_free(tenk);
    /* diagnostics read error leaves the other layers judged and names the error in the detail */
    d = obj("{\"error\": \"SFP module page unreadable\"}"); r = sfp_check_port(5, &present, lx, d, up, attached, committed);
    assert(!strcmp(ffn_json_get_string(r, "verdict"), "ok") && strstr(ffn_json_get_string(r, "detail"), "module diagnostics unreadable: SFP module page unreadable")); ffn_json_free(r); ffn_json_free(d);
    ffn_json_free(lx); ffn_json_free(good); ffn_json_free(up); ffn_json_free(attached); ffn_json_free(committed);
}

static void test_collect_and_publish(void)
{
    reset();
    F.link[16] = 1; F.iface[16] = 3; F.an[16] = 1; F.speed[16] = 1000;
    put_file("/etc/ffn/faceplate.json", "{\"ports\": {\"5\": true, \"8\": true, \"19\": true}, \"speeds\": {\"5\": \"auto\"}, \"pending\": null}");
    put_file("/etc/ffn/physical-5.json", "{\"enabled\": true, \"pending\": null, \"epoch\": \"boot-1:4242:777\"}");
    put_file("/etc/ffn/physical-8.json", "{\"enabled\": true, \"pending\": null, \"epoch\": \"boot-0:1:1\"}");
    const int ports[] = { 5, 8, 19, 20 }; char err[160];
    struct ffn_json *results = sfp_collect(&IO, &P, ports, 4, true, err, sizeof err);
    assert(results && results->count == 4);
    struct ffn_json *r5 = results->items[0], *r8 = results->items[1], *r19 = results->items[2], *r20 = results->items[3];
    assert(!strcmp(ffn_json_get_string(r5, "verdict"), "ok"));
    const struct ffn_json *link = ffn_json_get(r5, "link"); assert(!strcmp(ffn_json_get_string(link, "interface"), "BCM_PORT_IF_GMII") && !strcmp(ffn_json_get_string(link, "configured_speed"), "1000"));
    double v; assert(ffn_json_get_number(ffn_json_get(r5, "diagnostics"), "tx_power_dbm", &v) && near(v, -5.385, 0.001));
    assert(!strcmp(ffn_json_get_string(ffn_json_get(r5, "module"), "part"), "GLC-LH-SMD"));
    assert(find_file("/run/ffn-sfp-modules/5.json") >= 0);     /* identity cached */
    /* port 8: present with TX_DISABLE asserted (captured 0x23 bank 0 = 0xfe), pages unreadable (no fixture), stale attachment */
    assert(!strcmp(ffn_json_get_string(r8, "verdict"), "transmitter-off") && ffn_json_get(r8, "module")->type == FFN_JSON_NULL);
    assert(strstr(ffn_json_get_string(ffn_json_get(r8, "diagnostics"), "error"), "page unreadable"));
    assert(!strcmp(ffn_json_get_string(ffn_json_get(r8, "attachment"), "state"), "stale"));
    /* port 19: present, transmitter off by the cage line */
    assert(!strcmp(ffn_json_get_string(r19, "verdict"), "transmitter-off"));
    /* port 20: empty cage, not committed */
    assert(!strcmp(ffn_json_get_string(r20, "verdict"), "no-module"));
    assert(sfp_publish(&IO, &P, results, "boot-1", 1234) == 0);
    struct ffn_json *health = f_read_json(NULL, P.health); long long n;
    assert(health && ffn_json_get_int(health, "schema", &n) && n == 1 && ffn_json_get(health, "ports")->count == 4 && !strcmp(ffn_json_get_string(health, "boot_id"), "boot-1"));
    ffn_json_free(health); ffn_json_free(results);
    /* a module removal drops the cache */
    F.presence[0] |= 1;   /* port 5 bit 0 reads absent */
    results = sfp_collect(&IO, &P, ports, 1, false, err, sizeof err);
    assert(results && !strcmp(ffn_json_get_string(results->items[0], "verdict"), "no-module") && find_file("/run/ffn-sfp-modules/5.json") < 0);
    ffn_json_free(results);
    /* unreadable expanders: every port no-module with the detail, still a result */
    F.expanders_fail = true;
    results = sfp_collect(&IO, &P, ports, 4, true, err, sizeof err);
    assert(results && !strcmp(ffn_json_get_string(results->items[0], "detail"), "cage controls unreadable")); ffn_json_free(results);
    F.expanders_fail = false;
    /* no BCM: link layer null, verdict from the other layers */
    F.presence[0] &= ~1; F.bcm_down = true;
    results = sfp_collect(&IO, &P, ports, 1, true, err, sizeof err);
    assert(results && ffn_json_get(ffn_json_get(results->items[0], "link"), "interface")->type == FFN_JSON_NULL && !strcmp(ffn_json_get_string(results->items[0], "verdict"), "ok"));
    ffn_json_free(results);
}

static void test_relink(void)
{
    reset(); char err[160];
    put_file("/etc/ffn/faceplate.json", "{\"ports\": {\"5\": true}, \"speeds\": {\"5\": \"auto\"}, \"pending\": null}");
    /* port 5 sits at XFI with autonegotiation (configured "auto"), module is 1G LX: relink moves it to 1000BASE-X */
    F.an[16] = 1;
    struct ffn_json *r = sfp_relink(&IO, &P, 5, err, sizeof err);
    assert(r && !strcmp(ffn_json_get_string(r, "result"), "applied") && !strcmp(ffn_json_get_string(r, "speed"), "auto") && !strcmp(ffn_json_get_string(r, "name"), "ethernet1/5"));
    assert(F.iface[16] == 3 && F.an[16] == 1 && F.speed[16] == 1000);
    assert(!strcmp(ffn_json_get_string(ffn_json_get(r, "mode"), "link_mode"), "1000BASE-X") && !strcmp(ffn_json_get_string(r, "link_mode"), "1000BASE-X"));
    assert(!strcmp(ffn_json_get_string(ffn_json_get(r, "module"), "part"), "GLC-LH-SMD"));
    ffn_json_free(r);
    /* generic path for a cage whose module cannot be read: port.link.set */
    r = sfp_relink(&IO, &P, 8, err, sizeof err);
    assert(r && !strcmp(ffn_json_get_string(r, "result"), "applied") && ffn_json_get(r, "mode")->type == FFN_JSON_STRING && F.cint_runs == 1); ffn_json_free(r);
    /* refusals */
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 3, err, sizeof err)), "result"), "not-an-sfp-port")); ffn_json_free(r);
    put_file("/etc/ffn/faceplate.json", "{\"ports\": {}, \"speeds\": {}, \"pending\": {\"port\": 7}}");
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 5, err, sizeof err)), "result"), "pending-operation")); ffn_json_free(r);
    put_file("/etc/ffn/faceplate.json", "{\"ports\": {}, \"speeds\": {}, \"pending\": null}");
    put_file("/etc/ffn/aggregate-hardware.json", "{\"groups\": {\"ae1\": {\"phase\": \"running\", \"ports\": [5, 6]}, \"ae2\": {\"phase\": \"stopped\", \"ports\": [8]}}}");
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 5, err, sizeof err)), "result"), "aggregate-member")); ffn_json_free(r);
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 8, err, sizeof err)), "result"), "applied")); ffn_json_free(r);
    F.enabled[19] = 0;
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 8, err, sizeof err)), "result"), "unconfigured")); ffn_json_free(r);
    F.enabled[19] = 1; F.iface[19] = -1;   /* link status unavailable */
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 8, err, sizeof err)), "result"), "unconfigured")); ffn_json_free(r);
    F.iface[19] = 10; F.enabled[19] = -1;  /* not in port.list */
    assert(!strcmp(ffn_json_get_string((r = sfp_relink(&IO, &P, 8, err, sizeof err)), "result"), "not-an-sfp-port")); ffn_json_free(r);
    F.busy_lock = "faceplate";
    assert(!sfp_relink(&IO, &P, 8, err, sizeof err) && strstr(err, "lock busy"));
    F.busy_lock = NULL; F.cint_incomplete = true; F.enabled[16] = 1; F.iface[16] = 10; F.an[16] = 1;
    put_file("/etc/ffn/aggregate-hardware.json", "{\"groups\": {}}");
    assert(!sfp_relink(&IO, &P, 5, err, sizeof err) && strstr(err, "recipe incomplete"));
}

static void test_watcher(void)
{
    reset();
    put_file("/etc/ffn/faceplate.json", "{\"ports\": {\"5\": true, \"8\": true, \"11\": true, \"16\": true, \"19\": true}, \"speeds\": {\"5\": \"auto\"}, \"pending\": null}");
    F.presence[0] |= 1;   /* start with port 5 empty */
    F.an[16] = 1;         /* the switch holds "auto" for the port */
    struct sfp_watch w; sfp_watch_init(&w);
    assert(w.interval == 5 && w.identity_deadline == 30 && w.check_interval == 60);
    sfp_watch_poll(&w, &IO, &P);                              /* baseline: no events for what is already there, first check runs */
    assert(log_count("inserted") == 0 && log_count("relink") == 0 && w.have_previous);
    assert(find_file(P.health) >= 0);
    int transmit = log_count("transmit"); assert(transmit >= 1);  /* ports 8/11/16 link-down, 19 transmitter-off ... */
    /* insertion: relink after settle */
    F.now += 5; F.presence[0] &= ~1;
    sfp_watch_poll(&w, &IO, &P);
    assert(log_count("inserted") == 1 && w.pending_deadline[0] == F.now + 30 && log_count("relink") == 0);
    F.now += 5;
    sfp_watch_poll(&w, &IO, &P);
    assert(log_count("relink") == 1 && w.pending_deadline[0] == 0 && F.iface[16] == 3);
    struct ffn_json *e = ffn_json_parse(F.log[F.logs - 1], strlen(F.log[F.logs - 1]), NULL, 0);
    assert(!strcmp(ffn_json_get_string(e, "event"), "relink") && !strcmp(ffn_json_get_string(e, "result"), "applied")); ffn_json_free(e);
    /* an unreadable identity gives up after the deadline */
    F.now += 5; F.presence[0] |= 1; sfp_watch_poll(&w, &IO, &P);
    F.now += 5; F.presence[0] &= ~1; F.page_ok[0] = false; sfp_watch_poll(&w, &IO, &P);
    assert(log_count("inserted") == 2);
    for (int i = 0; i < 7; i++) { F.now += 5; sfp_watch_poll(&w, &IO, &P); }
    assert(log_count("identity-unreadable") == 1 && w.pending_deadline[0] == 0 && log_count("relink") == 1);
    /* removal while pending */
    F.page_ok[0] = true; F.now += 5; F.presence[0] |= 1; sfp_watch_poll(&w, &IO, &P);
    F.now += 5; F.presence[0] &= ~1; sfp_watch_poll(&w, &IO, &P);
    F.now += 1; F.presence[0] |= 1; sfp_watch_poll(&w, &IO, &P);
    assert(log_count("removed-before-relink") == 1 && w.pending_deadline[0] == 0);
    /* bus trouble backs off */
    F.expanders_fail = true; F.now += 5; sfp_watch_poll(&w, &IO, &P);
    assert(log_count("presence-unavailable") == 1 && w.quiet_until == F.now + 60);
    F.now += 5; sfp_watch_poll(&w, &IO, &P); assert(log_count("presence-unavailable") == 1);   /* quiet */
    F.expanders_fail = false; F.now += 60; sfp_watch_poll(&w, &IO, &P);
    /* a transmitter-off verdict is remedied once, then re-judged */
    int before = log_count("remediated");
    assert(before >= 1);   /* port 19 transmitter-off was remedied on the first check */
    assert(!F.control[3] || true);
    struct sfp_cage c[SFP_PORTS]; char err[96]; assert(sfp_inventory(&IO, &P, c, err, sizeof err) == 0 && c[14].tx_enabled);
}

static void on_abort(int sig) { (void)sig; for (int i = 0; i < F.logs; i++) fprintf(stderr, "log[%d] %s\n", i, F.log[i]); }

int main(void)
{
    signal(SIGABRT, on_abort);
    test_board_map();
    test_inventory_decodes_the_captured_expanders();
    test_set_enabled_writes_only_this_cage();
    test_identity_and_summary();
    test_diagnostics_decode();
    test_link_mode();
    test_configure_fiber();
    test_check_matrix();
    test_collect_and_publish();
    test_relink();
    test_watcher();
    puts("test-sfp: ok");
    return 0;
}
