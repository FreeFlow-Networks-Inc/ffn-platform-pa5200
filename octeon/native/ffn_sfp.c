/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_sfp.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* PA-5200 version 11 port map: expander bit per faceplate port 5..20, and the BCM port. */
static const int BITS[SFP_PORTS] = { 0, 1, 2, 3, 5, 4, 7, 6, 12, 13, 14, 15, 9, 8, 11, 10 };
static const int BCM[SFP_PORTS] = { 16, 1, 18, 19, 6, 21, 22, 23, 7, 11, 36, 27, 10, 29, 30, 31 };

int sfp_bit(int port) { return port >= SFP_FIRST_PORT && port <= SFP_LAST_PORT ? BITS[port - SFP_FIRST_PORT] : -1; }
int sfp_bcm_port(int port) { return port >= SFP_FIRST_PORT && port <= SFP_LAST_PORT ? BCM[port - SFP_FIRST_PORT] : 0; }

static void seterr(char *err, size_t errsz, const char *message) { if (err && errsz) snprintf(err, errsz, "%s", message); }

static int read_byte(const struct sfp_io *io, unsigned addr, unsigned reg, uint8_t *out)
{
    return io->i2c_read(io->ctx, SFP_BUS, addr, reg, 1, out, 1);
}

/* ---- cage controls ---- */

static int inventory_locked(const struct sfp_io *io, struct sfp_cage out[SFP_PORTS], char *err, size_t errsz)
{
    /* PCA9555 sequential reads toggle within a register pair; explicit offsets for every register. */
    uint8_t presence[8], control[8];
    for (unsigned r = 0; r < 8; r++) {
        if (read_byte(io, SFP_PRESENCE_EXPANDER, r, &presence[r]) || read_byte(io, SFP_CONTROL_EXPANDER, r, &control[r])) {
            seterr(err, errsz, "SFP cage expanders unreadable"); return -1;
        }
    }
    for (int port = SFP_FIRST_PORT; port <= SFP_LAST_PORT; port++) {
        int bit = sfp_bit(port), bank = bit / 8; unsigned mask = 1u << (bit % 8);
        struct sfp_cage *c = &out[port - SFP_FIRST_PORT];
        /* input registers already include the polarity register XOR */
        c->present = !((presence[bank] ^ presence[4 + bank]) & mask);
        bool driven = !(control[6 + bank] & mask);
        c->tx_disable = ((control[bank] ^ control[4 + bank]) & mask) != 0;
        c->tx_enabled = driven && !c->tx_disable && !(control[2 + bank] & mask);
        c->control_ready = true;
    }
    return 0;
}

int sfp_inventory(const struct sfp_io *io, const struct sfp_paths *paths, struct sfp_cage out[SFP_PORTS], char *err, size_t errsz)
{
    int lock = io->lock(io->ctx, paths->control_lock, SFP_LOCK_SECONDS);
    if (lock < 0) { seterr(err, errsz, "SFP control lock busy"); return -1; }
    int rc = inventory_locked(io, out, err, errsz);
    io->unlock(io->ctx, lock);
    return rc;
}

static int write_bit(const struct sfp_io *io, unsigned reg, unsigned mask, bool value, char *err, size_t errsz)
{
    uint8_t old;
    if (read_byte(io, SFP_CONTROL_EXPANDER, reg, &old)) { seterr(err, errsz, "SFP control register unreadable"); return -1; }
    uint8_t fresh = value ? (uint8_t)(old | mask) : (uint8_t)(old & ~mask);
    if (fresh != old && io->i2c_write8(io->ctx, SFP_BUS, SFP_CONTROL_EXPANDER, reg, fresh)) { seterr(err, errsz, "SFP control register write failed"); return -1; }
    uint8_t back;
    if (read_byte(io, SFP_CONTROL_EXPANDER, reg, &back) || back != fresh) { seterr(err, errsz, "SFP control register readback mismatch"); return -1; }
    return 0;
}

int sfp_set_enabled(const struct sfp_io *io, const struct sfp_paths *paths, int port, bool enabled, struct sfp_cage *out, char *err, size_t errsz)
{
    int bit = sfp_bit(port);
    if (bit < 0) { seterr(err, errsz, "SFP control requires a faceplate port from 5 through 20"); return -1; }
    int bank = bit / 8; unsigned mask = 1u << (bit % 8);
    int lock = io->lock(io->ctx, paths->control_lock, SFP_LOCK_SECONDS);
    if (lock < 0) { seterr(err, errsz, "SFP control lock busy"); return -1; }
    int rc = -1;
    struct sfp_cage cages[SFP_PORTS];
    /* Preload the latch before enabling output; never alter another cage. */
    if (write_bit(io, 2 + (unsigned)bank, mask, !enabled, err, errsz)) goto out;
    if (write_bit(io, 6 + (unsigned)bank, mask, false, err, errsz)) goto out;
    if (inventory_locked(io, cages, err, errsz)) goto out;
    *out = cages[port - SFP_FIRST_PORT];
    if (out->tx_disable != !enabled || out->tx_enabled != enabled) { seterr(err, errsz, "SFP transmitter control readback mismatch"); goto out; }
    rc = 0;
out:
    io->unlock(io->ctx, lock);
    return rc;
}

struct ffn_json *sfp_cage_json(const struct sfp_cage *c)
{
    struct ffn_json *o = ffn_json_object();
    ffn_json_set(o, "present", ffn_json_bool(c->present)); ffn_json_set(o, "tx_enabled", ffn_json_bool(c->tx_enabled));
    ffn_json_set(o, "tx_disable", ffn_json_bool(c->tx_disable)); ffn_json_set(o, "control_ready", ffn_json_bool(c->control_ready));
    return o;
}

/* ---- module pages ---- */

static int read_page(const struct sfp_io *io, int bus, unsigned addr, unsigned offset, unsigned count, uint8_t *out)
{
    for (unsigned start = offset; start < offset + count; start += SFP_CHUNK) {
        unsigned n = offset + count - start < SFP_CHUNK ? offset + count - start : SFP_CHUNK;
        if (io->i2c_read(io->ctx, (unsigned)bus, addr, start, 1, out + (start - offset), n)) return -1;
    }
    return 0;
}

static int pages_locked(const struct sfp_io *io, const struct sfp_paths *paths, int port, uint8_t a0[96], uint8_t *a2, bool *has_a2, char *err, size_t errsz)
{
    char berr[96];
    int bus = io->module_bus(io->ctx, port, berr, sizeof berr);
    if (bus < 0) { seterr(err, errsz, berr[0] ? berr : "SFP diagnostic I2C multiplexer is not bound"); return -1; }
    int hold = io->lock(io->ctx, paths->bus_lock, SFP_LOCK_SECONDS);
    if (hold < 0) { seterr(err, errsz, "I2C bus 1 lock busy"); return -1; }
    int rc = read_page(io, bus, 0x50, 0, 96, a0);
    if (rc == 0 && a2) {
        *has_a2 = (a0[92] & 0x40) != 0;
        if (*has_a2) rc = read_page(io, bus, 0x51, 0, 128, a2);
    }
    io->unlock(io->ctx, hold);
    if (rc) { seterr(err, errsz, "SFP module page unreadable"); return -1; }
    return 0;
}

int sfp_validate_identity(const uint8_t a0[96])
{
    unsigned s1 = 0, s2 = 0;
    for (int i = 0; i < 63; i++) s1 += a0[i];
    for (int i = 64; i < 95; i++) s2 += a0[i];
    return a0[0] == 3 && (s1 & 255) == a0[63] && (s2 & 255) == a0[95] ? 0 : -1;
}

int sfp_identity(const struct sfp_io *io, const struct sfp_paths *paths, int port, uint8_t a0[96], char *err, size_t errsz)
{
    if (sfp_bit(port) < 0) { seterr(err, errsz, "SFP control requires a faceplate port from 5 through 20"); return -1; }
    int lock = io->lock(io->ctx, paths->control_lock, SFP_LOCK_SECONDS);
    if (lock < 0) { seterr(err, errsz, "SFP control lock busy"); return -1; }
    int rc = pages_locked(io, paths, port, a0, NULL, NULL, err, errsz);
    io->unlock(io->ctx, lock);
    if (rc) return -1;
    if (sfp_validate_identity(a0)) { seterr(err, errsz, "Invalid SFP identity or checksum"); return -1; }
    return 0;
}

int sfp_read_diagnostics(const struct sfp_io *io, const struct sfp_paths *paths, int port, uint8_t a0[96], uint8_t a2[128], bool *has_a2, char *err, size_t errsz)
{
    if (sfp_bit(port) < 0) { seterr(err, errsz, "SFP control requires a faceplate port from 5 through 20"); return -1; }
    int lock = io->lock(io->ctx, paths->control_lock, SFP_LOCK_SECONDS);
    if (lock < 0) { seterr(err, errsz, "SFP control lock busy"); return -1; }
    *has_a2 = false;
    int rc = pages_locked(io, paths, port, a0, a2, has_a2, err, errsz);
    io->unlock(io->ctx, lock);
    return rc;
}

/* ---- SFF-8472 decoding ---- */

static void text_field(const uint8_t *src, size_t len, char *out, size_t outsz)
{
    size_t n = len < outsz - 1 ? len : outsz - 1;
    for (size_t i = 0; i < n; i++) out[i] = (src[i] >= 0x20 && src[i] < 0x7f) ? (char)src[i] : '?';
    out[n] = 0;
    while (n && out[n - 1] == ' ') out[--n] = 0;
    size_t lead = 0; while (out[lead] == ' ') lead++;
    if (lead) memmove(out, out + lead, n - lead + 1);
}

bool sfp_optical(const uint8_t a0[96]) { return (a0[2] == 1 || a0[2] == 7) && ((a0[60] << 8) | a0[61]) > 0; }

unsigned sfp_module_speeds(const uint8_t a0[96], int rates[2])
{
    bool has1g = (a0[6] & 0x0f) != 0, has10g = (a0[3] & 0xf0) != 0;
    unsigned nominal = a0[12];
    if (nominal >= 10 && nominal <= 15) has1g = true;
    else if (nominal >= 100 && nominal <= 110) has10g = true;
    else if (nominal == 255 && a0[66] >= 40) has10g = true;
    unsigned n = 0;
    if (has1g) rates[n++] = 1000;
    if (has10g) rates[n++] = 10000;
    return n;
}

struct ffn_json *sfp_summary_json(const uint8_t a0[96])
{
    char vendor[20], part[20];
    text_field(a0 + 20, 16, vendor, sizeof vendor); text_field(a0 + 40, 16, part, sizeof part);
    unsigned wavelength = (unsigned)((a0[60] << 8) | a0[61]);
    struct ffn_json *o = ffn_json_object(), *speeds = ffn_json_array();
    int rates[2]; unsigned n = sfp_module_speeds(a0, rates);
    for (unsigned i = 0; i < n; i++) ffn_json_push(speeds, ffn_json_int(rates[i]));
    ffn_json_set(o, "vendor", ffn_json_string(vendor)); ffn_json_set(o, "part", ffn_json_string(part));
    ffn_json_set(o, "wavelength_nm", wavelength ? ffn_json_int(wavelength) : ffn_json_null());
    ffn_json_set(o, "optical", ffn_json_bool(sfp_optical(a0))); ffn_json_set(o, "speeds", speeds);
    return o;
}

static double be_float(const uint8_t *p)
{
    uint32_t bits = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
    float f; memcpy(&f, &bits, 4); return (double)f;
}

int sfp_decode_diagnostics(const uint8_t a0[96], const uint8_t *a2, size_t a2len, struct ffn_json **out, char *err, size_t errsz)
{
    if (a0[0] != 3) { seterr(err, errsz, "Not an SFP identity page"); return -1; }
    if (sfp_validate_identity(a0)) { seterr(err, errsz, "SFP identity checksum mismatch"); return -1; }
    char vendor[20], model[20];
    text_field(a0 + 20, 16, vendor, sizeof vendor); text_field(a0 + 40, 16, model, sizeof model);
    struct ffn_json *o = ffn_json_object();
    ffn_json_set(o, "vendor", ffn_json_string(vendor)); ffn_json_set(o, "model", ffn_json_string(model));
    ffn_json_set(o, "wavelength_nm", ffn_json_int((a0[60] << 8) | a0[61]));
    if (!(a0[92] & 0x40)) { ffn_json_set(o, "diagnostics_available", ffn_json_bool(false)); *out = o; return 0; }
    unsigned sum = 0;
    for (int i = 0; i < 95; i++) sum += a2 && a2len > 95 ? a2[i] : 0;
    if (!a2 || a2len < 118 || (sum & 255) != a2[95]) { ffn_json_free(o); seterr(err, errsz, "SFP diagnostic checksum mismatch"); return -1; }
    if (a2[110] & 1) { ffn_json_free(o); seterr(err, errsz, "SFP diagnostic data is not ready"); return -1; }
    bool external = (a0[92] & 0x10) != 0, internal = (a0[92] & 0x20) != 0;
    if (external == internal) { ffn_json_free(o); seterr(err, errsz, "SFP diagnostic calibration is unspecified or ambiguous"); return -1; }
    double values[5];
    values[0] = (double)(int16_t)((a2[96] << 8) | a2[97]);          /* temperature, signed */
    for (int i = 1; i < 5; i++) values[i] = (double)((a2[96 + 2 * i] << 8) | a2[97 + 2 * i]);
    if (external) {
        const int offsets[4] = { 84, 88, 76, 80 };
        for (int index = 0; index < 4; index++) {
            int off = offsets[index];
            double slope = ((a2[off] << 8) | a2[off + 1]) / 256.0;
            double intercept = (double)(int16_t)((a2[off + 2] << 8) | a2[off + 3]);
            values[index] = values[index] * slope + intercept;
        }
        double rx = 0.0;
        for (int c = 0; c < 5; c++) rx = rx * values[4] + be_float(a2 + 56 + 4 * c);
        values[4] = rx;
    }
    for (int i = 0; i < 5; i++) if (!isfinite(values[i])) { ffn_json_free(o); seterr(err, errsz, "Invalid SFP calibration values"); return -1; }
    ffn_json_set(o, "diagnostics_available", ffn_json_bool(true));
    ffn_json_set(o, "temperature_c", ffn_json_number(values[0] / 256.0));
    ffn_json_set(o, "voltage_v", ffn_json_number(values[1] / 10000.0));
    ffn_json_set(o, "tx_bias_ma", ffn_json_number(values[2] * 0.002));
    ffn_json_set(o, "tx_power_dbm", values[3] > 0 ? ffn_json_number(round(10000.0 * log10(values[3] / 10000.0)) / 1000.0) : ffn_json_null());
    ffn_json_set(o, "rx_power_dbm", values[4] > 0 ? ffn_json_number(round(10000.0 * log10(values[4] / 10000.0)) / 1000.0) : ffn_json_null());
    ffn_json_set(o, "tx_disable", ffn_json_bool(a2[110] & 0x80));
    ffn_json_set(o, "rx_los", (a0[93] & 0x10) ? ffn_json_bool(a2[110] & 2) : ffn_json_null());
    ffn_json_set(o, "tx_fault", (a0[93] & 0x20) ? ffn_json_bool(a2[110] & 4) : ffn_json_null());
    *out = o;
    return 0;
}

int sfp_link_mode(const uint8_t a0[96], const char *speed, int *rate, bool *autoneg, char *err, size_t errsz)
{
    if (!sfp_optical(a0)) return 0;
    int rates[2]; unsigned n = sfp_module_speeds(a0, rates);
    if (!n) return 0;
    int wanted;
    if (!strcmp(speed, "auto")) wanted = rates[n - 1];
    else {
        wanted = atoi(speed);
        bool declared = false;
        for (unsigned i = 0; i < n; i++) if (rates[i] == wanted) declared = true;
        if (!declared) {
            char list[32] = ""; for (unsigned i = 0; i < n; i++) snprintf(list + strlen(list), sizeof list - strlen(list), "%s%d", i ? "/" : "", rates[i]);
            if (err && errsz) snprintf(err, errsz, "Detected SFP supports %s; %s cannot be selected", list, speed);
            return -1;
        }
    }
    *rate = wanted; *autoneg = wanted == 1000;
    return 1;
}

/* ---- the fiber recipe ---- */

static const char FIBER_RECIPE[] =
"{\n"
"int rv=0; int an=0; int rate=0; int changed=0; int ifn=0; int en=0; int mrv=0; bcm_port_if_t iface;\n"
"rv=bcm_port_interface_get(0,@PORT@,&iface); ifn=iface;\n"
"if(rv==0) rv=bcm_port_autoneg_get(0,@PORT@,&an);\n"
"if(rv==0) rv=bcm_port_speed_get(0,@PORT@,&rate);\n"
"if(rv==0 && (ifn!=@IFACEV@ || an!=@AUTO@ || (!@AUTO@ && rate!=@RATE@))) {\n"
" changed=1;\n"
" rv=bcm_port_enable_get(0,@PORT@,&en);\n"
" if(rv==0 && en) rv=bcm_port_enable_set(0,@PORT@,0);\n"
" if(rv==0) rv=bcm_port_autoneg_set(0,@PORT@,0);\n"
" if(rv==0) rv=bcm_port_interface_set(0,@PORT@,@IFACE@);\n"
" if(rv==0) rv=bcm_port_speed_set(0,@PORT@,@RATE@);\n"
" if(rv==0 && @AUTO@) { mrv=bcm_port_phy_control_set(0,@PORT@,BCM_PORT_PHY_CONTROL_AUTONEG_MODE,1); if(mrv!=-4 && mrv!=-16) rv=mrv; }\n"
" if(rv==0 && @AUTO@) rv=bcm_port_autoneg_set(0,@PORT@,1);\n"
" if(rv==0 && en) rv=bcm_port_enable_set(0,@PORT@,1);\n"
"}\n"
"if(rv==0) rv=bcm_port_interface_get(0,@PORT@,&iface); ifn=iface;\n"
"if(rv==0) rv=bcm_port_autoneg_get(0,@PORT@,&an);\n"
"if(rv==0) rv=bcm_port_speed_get(0,@PORT@,&rate);\n"
"printf(\"FFN_SFP_LINK %d %d %d %d %d %d\\n\",rv,ifn,an,rate,changed,mrv);\n"
"printf(\"FFN_SFP_LINK_DONE\\n\");\n"
"}";

static char *substitute(const char *text, const char *marker, const char *value)
{
    size_t mlen = strlen(marker), vlen = strlen(value), count = 0;
    for (const char *p = text; (p = strstr(p, marker)); p += mlen) count++;
    char *out = malloc(strlen(text) + count * vlen + 1);
    if (!out) return NULL;
    char *w = out;
    for (const char *p = text;;) {
        const char *hit = strstr(p, marker);
        if (!hit) { strcpy(w, p); break; }
        memcpy(w, p, (size_t)(hit - p)); w += hit - p; memcpy(w, value, vlen); w += vlen; p = hit + mlen;
    }
    return out;
}

static const char *interface_name(int iface) { return iface == 3 ? "BCM_PORT_IF_GMII" : iface == 4 ? "BCM_PORT_IF_SGMII" : iface == 9 ? "BCM_PORT_IF_SFI" : iface == 10 ? "BCM_PORT_IF_XFI" : "unknown"; }
static const char *link_mode_name(int iface) { return iface == 3 ? "1000BASE-X" : iface == 4 ? "SGMII" : iface == 9 ? "SFI" : iface == 10 ? "XFI" : "unknown"; }

int sfp_configure_fiber(const struct sfp_io *io, const struct sfp_paths *paths, int port, int chip, const char *speed, const uint8_t *a0_in,
                        struct ffn_json **mode, char *err, size_t errsz)
{
    *mode = NULL;
    if (sfp_bit(port) < 0 || chip != sfp_bcm_port(port) || (strcmp(speed, "auto") && strcmp(speed, "1000") && strcmp(speed, "10000"))) {
        seterr(err, errsz, "Invalid SFP link request"); return -1;
    }
    uint8_t a0[96];
    if (a0_in) memcpy(a0, a0_in, 96);
    else if (sfp_identity(io, paths, port, a0, err, errsz)) return 0;   /* no classifiable module: generic path */
    int rate; bool autoneg;
    int resolved = sfp_link_mode(a0, speed, &rate, &autoneg, err, errsz);
    if (resolved <= 0) return resolved;
    int ifacev = rate == 1000 ? 3 : 10;
    const char *iface = rate == 1000 ? "BCM_PORT_IF_GMII" : "BCM_PORT_IF_XFI";
    char num[16];
    char *body = substitute(FIBER_RECIPE, "@IFACEV@", (snprintf(num, sizeof num, "%d", ifacev), num));
    char *b2 = body ? substitute(body, "@IFACE@", iface) : NULL; free(body);
    char *b3 = b2 ? substitute(b2, "@PORT@", (snprintf(num, sizeof num, "%d", chip), num)) : NULL; free(b2);
    char *b4 = b3 ? substitute(b3, "@RATE@", (snprintf(num, sizeof num, "%d", rate), num)) : NULL; free(b3);
    char *b5 = b4 ? substitute(b4, "@AUTO@", autoneg ? "1" : "0") : NULL; free(b4);
    if (!b5) { seterr(err, errsz, "out of memory"); return -1; }
    struct ffn_json *markers = io->cint(io->ctx, b5, 20, err, errsz);
    free(b5);
    if (!markers) return -1;
    long long rv = -1, ifn = -1, an = -1, got = -1, changed = 0, mrv = 0; bool found = false;
    for (size_t i = 0; i < markers->count; i++) {
        const char *m = markers->items[i]->type == FFN_JSON_STRING ? markers->items[i]->string : "";
        if (sscanf(m, "FFN_SFP_LINK %lld %lld %lld %lld %lld %lld", &rv, &ifn, &an, &got, &changed, &mrv) == 6) { found = true; break; }
    }
    ffn_json_free(markers);
    if (!found) { seterr(err, errsz, "SFP link recipe incomplete; inspect BCM diagnostics"); return -1; }
    if (rv != 0 || ifn != ifacev || an != (autoneg ? 1 : 0) || (got != rate && !(autoneg && got == 0))) {
        if (err && errsz) snprintf(err, errsz, "SFP link readback mismatch: FFN_SFP_LINK %lld %lld %lld %lld %lld %lld", rv, ifn, an, got, changed, mrv);
        return -1;
    }
    struct ffn_json *o = ffn_json_object();
    ffn_json_set(o, "speed", ffn_json_int(rate)); ffn_json_set(o, "autoneg", ffn_json_bool(autoneg));
    ffn_json_set(o, "interface", ffn_json_string(iface)); ffn_json_set(o, "link_mode", ffn_json_string(link_mode_name(ifacev)));
    ffn_json_set(o, "changed", ffn_json_bool(changed != 0)); ffn_json_set(o, "autoneg_mode_control", ffn_json_int(mrv));
    *mode = o;
    return 1;
}

/* ---- the transmit check ---- */

static const char *REMEDY[][2] = {
    { "transmitter-off", "enable-transmitter" }, { "link-mode-mismatch", "relink" }, { "attachment-pending", "reapply" },
    { "attachment-stale", "reapply" }, { "attachment-off", "reapply" }, { "tx-fault", "hardware" }, { "laser-off", "hardware" },
    { "no-rx-light", "far-end" }, { "link-down", "far-end" },
};

static double number_or(const struct ffn_json *o, const char *key, double missing, bool *present)
{
    double d;
    *present = ffn_json_get_number(o, key, &d);
    return *present ? d : missing;
}

static const char *expected_interface(const struct ffn_json *module, const char *configured_speed)
{
    const struct ffn_json *speeds = module ? ffn_json_get(module, "speeds") : NULL;
    if (!speeds || speeds->type != FFN_JSON_ARRAY || !speeds->count) return NULL;
    long long best = 0, wanted = 0; bool declared = false;
    for (size_t i = 0; i < speeds->count; i++) if (speeds->items[i]->type == FFN_JSON_INT && speeds->items[i]->integer > best) best = speeds->items[i]->integer;
    if (configured_speed && strcmp(configured_speed, "auto")) {
        wanted = atoll(configured_speed);
        for (size_t i = 0; i < speeds->count; i++) if (speeds->items[i]->type == FFN_JSON_INT && speeds->items[i]->integer == wanted) declared = true;
        if (!declared) return NULL;
    } else wanted = best;
    return wanted == 1000 ? "BCM_PORT_IF_GMII" : wanted == 10000 ? "BCM_PORT_IF_XFI" : NULL;
}

struct ffn_json *sfp_check_port(int port, const struct sfp_cage *cage, const struct ffn_json *module, const struct ffn_json *diagnostics,
                                const struct ffn_json *link, const struct ffn_json *attachment, const struct ffn_json *committed)
{
    struct ffn_json *o = ffn_json_object();
    char name[20]; snprintf(name, sizeof name, "ethernet1/%d", port);
    ffn_json_set(o, "port", ffn_json_int(port)); ffn_json_set(o, "interface", ffn_json_string(name));
    if (cage) { struct ffn_json *c = ffn_json_object(); ffn_json_set(c, "present", ffn_json_bool(cage->present)); ffn_json_set(c, "tx_disable", ffn_json_bool(cage->tx_disable)); ffn_json_set(o, "cage", c); }
    else ffn_json_set(o, "cage", ffn_json_null());
    ffn_json_set(o, "module", module ? ffn_json_copy(module) : ffn_json_null());
    ffn_json_set(o, "diagnostics", diagnostics ? ffn_json_copy(diagnostics) : ffn_json_null());
    struct ffn_json *l = ffn_json_object();
    for (const char **k = (const char *[]){ "mac_enabled", "link", "interface", "link_mode", "autoneg", "configured_speed", NULL }; *k; k++) {
        const struct ffn_json *v = link ? ffn_json_get(link, *k) : NULL;
        ffn_json_set(l, *k, v ? ffn_json_copy(v) : ffn_json_null());
    }
    ffn_json_set(o, "link", l);
    ffn_json_set(o, "attachment", attachment ? ffn_json_copy(attachment) : ffn_json_null());
    ffn_json_set(o, "committed", committed ? ffn_json_copy(committed) : ffn_json_null());
    const char *configured = committed ? ffn_json_get_string(committed, "speed") : NULL;
    const char *expected = expected_interface(module, configured);
    ffn_json_set(o, "expected_interface", expected ? ffn_json_string(expected) : ffn_json_null());
    const char *verdict = "ok"; char detail[200] = "";
    bool committed_enabled = false; if (committed) ffn_json_get_bool(committed, "enabled", &committed_enabled);
    bool diag_available = false; if (diagnostics) ffn_json_get_bool(diagnostics, "diagnostics_available", &diag_available);
    if (!cage) { verdict = "no-module"; snprintf(detail, sizeof detail, "cage controls unreadable"); }
    else if (!cage->present) { verdict = "no-module"; snprintf(detail, sizeof detail, "no transceiver in the cage"); }
    else if (!committed_enabled) { verdict = "disabled"; snprintf(detail, sizeof detail, "port disabled in the committed configuration"); }
    else if (cage->tx_disable) { verdict = "transmitter-off"; snprintf(detail, sizeof detail, "TX_DISABLE asserted by the cage control line"); }
    else if (diag_available) {
        bool txd = false, txf = false, los = false, has_tx, has_rx;
        ffn_json_get_bool(diagnostics, "tx_disable", &txd); ffn_json_get_bool(diagnostics, "tx_fault", &txf); ffn_json_get_bool(diagnostics, "rx_los", &los);
        double tx = number_or(diagnostics, "tx_power_dbm", -99, &has_tx), rx = number_or(diagnostics, "rx_power_dbm", 0, &has_rx);
        if (txd) { verdict = "transmitter-off"; snprintf(detail, sizeof detail, "module reports its transmitter disabled"); }
        else if (txf) { verdict = "tx-fault"; snprintf(detail, sizeof detail, "module reports a transmitter fault"); }
        else if (!has_tx || tx < SFP_LASER_MIN_DBM) { verdict = "laser-off"; if (has_tx) snprintf(detail, sizeof detail, "launch power %.6g dBm", tx); else snprintf(detail, sizeof detail, "launch power None dBm"); }
        else if (los || (has_rx && rx < SFP_RX_MIN_DBM)) { verdict = "no-rx-light"; if (has_rx) snprintf(detail, sizeof detail, "no light from the far end (RX %.6g dBm)", rx); else snprintf(detail, sizeof detail, "no light from the far end (RX None dBm)"); }
    }
    if (!strcmp(verdict, "ok")) {
        const char *actual = link ? ffn_json_get_string(link, "interface") : NULL;
        bool mac_enabled = true, up = true, has_mac = link && ffn_json_get_bool(link, "mac_enabled", &mac_enabled), has_link = link && ffn_json_get_bool(link, "link", &up);
        const char *state = attachment ? ffn_json_get_string(attachment, "state") : "none";
        bool attached_expected = true; if (committed) ffn_json_get_bool(committed, "attached", &attached_expected);
        if (expected && actual && strcmp(actual, expected)) { verdict = "link-mode-mismatch"; snprintf(detail, sizeof detail, "switch runs %s, module needs %s", actual, expected); }
        else if (has_mac && !mac_enabled) { verdict = "link-down"; snprintf(detail, sizeof detail, "switch MAC disabled"); }
        else if (has_link && !up) { verdict = "link-down"; snprintf(detail, sizeof detail, "no link on the switch port"); }
        else if (state && !strcmp(state, "pending")) { verdict = "attachment-pending"; snprintf(detail, sizeof detail, "dataplane attachment left pending by an interrupted apply"); }
        else if (state && !strcmp(state, "stale")) { verdict = "attachment-stale"; snprintf(detail, sizeof detail, "dataplane attachment is from another switch lifetime"); }
        else if (state && (!strcmp(state, "none") || !strcmp(state, "disabled")) && attached_expected) { verdict = "attachment-off"; snprintf(detail, sizeof detail, "no dataplane attachment for this port"); }
    }
    const char *derr = diagnostics ? ffn_json_get_string(diagnostics, "error") : NULL;
    if (derr && !strcmp(verdict, "ok")) snprintf(detail, sizeof detail, "module diagnostics unreadable: %.120s", derr);
    const char *remedy = NULL;
    for (size_t i = 0; i < sizeof REMEDY / sizeof REMEDY[0]; i++) if (!strcmp(REMEDY[i][0], verdict)) remedy = REMEDY[i][1];
    ffn_json_set(o, "verdict", ffn_json_string(verdict)); ffn_json_set(o, "detail", ffn_json_string(detail));
    ffn_json_set(o, "remedy", remedy ? ffn_json_string(remedy) : ffn_json_null());
    return o;
}

/* The switch's view of one SFP port: port.list row + port.link.status. NULL on failure. */
static struct ffn_json *switch_link(const struct sfp_io *io, int port, const struct sfp_cage *cage, char *err, size_t errsz)
{
    int chip = sfp_bcm_port(port);
    struct ffn_json *req = ffn_json_object(); ffn_json_set(req, "op", ffn_json_string("port.list"));
    struct ffn_json *list = io->bcm(io->ctx, req, err, errsz); ffn_json_free(req);
    if (!list) return NULL;
    struct ffn_json *row = ffn_json_object();
    bool enabled = false, up = false, found = false;
    const struct ffn_json *ports = ffn_json_get(list, "ports");
    for (size_t i = 0; ports && ports->type == FFN_JSON_ARRAY && i < ports->count; i++) {
        long long p; if (ffn_json_get_int(ports->items[i], "port", &p) && p == chip) { found = true; ffn_json_get_bool(ports->items[i], "enabled", &enabled); ffn_json_get_bool(ports->items[i], "link", &up); }
    }
    ffn_json_free(list);
    ffn_json_set(row, "available", ffn_json_bool(found));
    ffn_json_set(row, "mac_enabled", found ? ffn_json_bool(enabled) : ffn_json_null());
    bool tx = cage && cage->present && cage->tx_enabled;
    ffn_json_set(row, "enabled", found && cage ? ffn_json_bool(enabled && tx) : ffn_json_null());
    ffn_json_set(row, "link", found && cage ? ffn_json_bool(up && enabled && tx && cage->present) : ffn_json_null());
    req = ffn_json_object(); ffn_json_set(req, "op", ffn_json_string("port.link.status")); ffn_json_set(req, "port", ffn_json_int(chip));
    char lerr[96];
    struct ffn_json *status = io->bcm(io->ctx, req, lerr, sizeof lerr); ffn_json_free(req);
    if (status) {
        long long iface = -1; ffn_json_get_int(status, "interface", &iface);
        ffn_json_set(row, "interface", iface >= 0 ? ffn_json_string(interface_name((int)iface)) : ffn_json_null());
        for (const char **k = (const char *[]){ "configured_speed", "supported_speeds", "link_mode", "autoneg", NULL }; *k; k++) {
            const struct ffn_json *v = ffn_json_get(status, *k); ffn_json_set(row, *k, v ? ffn_json_copy(v) : ffn_json_null());
        }
        ffn_json_set(row, "speed_configuration", ffn_json_bool(true));
        ffn_json_free(status);
    } else { ffn_json_set(row, "speed_configuration", ffn_json_bool(false)); ffn_json_set(row, "speed_error", ffn_json_string("Link control unavailable; BCM link-control update may need activation")); }
    return row;
}

/* The cached module identity summary (read once per insertion), as the faceplate keeps it. */
static struct ffn_json *module_summary(const struct sfp_io *io, const struct sfp_paths *paths, int port, bool present)
{
    char path[320]; snprintf(path, sizeof path, "%s/%d.json", paths->modules_dir, port);
    if (!present) { io->unlink(io->ctx, path); return NULL; }
    struct ffn_json *cached = io->read_json(io->ctx, path);
    if (cached) return cached;
    uint8_t a0[96]; char err[96];
    if (sfp_identity(io, paths, port, a0, err, sizeof err)) return NULL;
    struct ffn_json *summary = sfp_summary_json(a0);
    io->write_json(io->ctx, path, summary);
    return summary;
}

static struct ffn_json *attachment_state(const struct sfp_io *io, const struct sfp_paths *paths, int port, const char *epoch)
{
    char path[320]; snprintf(path, sizeof path, "%s/physical-%d.json", paths->journal_dir, port);
    struct ffn_json *row = io->read_json(io->ctx, path), *o = ffn_json_object();
    const char *state = "none"; bool enabled = false;
    if (row && row->type == FFN_JSON_OBJECT) {
        const struct ffn_json *pending = ffn_json_get(row, "pending"); const char *rep = ffn_json_get_string(row, "epoch");
        ffn_json_get_bool(row, "enabled", &enabled);
        if (pending && pending->type != FFN_JSON_NULL) state = "pending";
        else if (epoch && epoch[0] && (!rep || strcmp(rep, epoch))) state = "stale";
        else state = enabled ? "enabled" : "disabled";
        ffn_json_set(o, "pending", pending ? ffn_json_copy(pending) : ffn_json_null()); ffn_json_set(o, "epoch", rep ? ffn_json_string(rep) : ffn_json_null());
    } else { ffn_json_set(o, "pending", ffn_json_null()); ffn_json_set(o, "epoch", ffn_json_null()); }
    ffn_json_set(o, "state", ffn_json_string(state)); ffn_json_set(o, "enabled", ffn_json_bool(enabled));
    ffn_json_free(row);
    return o;
}

struct ffn_json *sfp_collect(const struct sfp_io *io, const struct sfp_paths *paths, const int *ports, unsigned count, bool diagnostics, char *err, size_t errsz)
{
    struct sfp_cage cages[SFP_PORTS]; bool have_cages = sfp_inventory(io, paths, cages, err, errsz) == 0;
    struct ffn_json *saved = io->read_json(io->ctx, paths->faceplate_state);
    const struct ffn_json *saved_ports = saved ? ffn_json_get(saved, "ports") : NULL, *saved_speeds = saved ? ffn_json_get(saved, "speeds") : NULL;
    char epoch[160] = ""; io->epoch(io->ctx, epoch, sizeof epoch);
    struct ffn_json *results = ffn_json_array();
    for (unsigned i = 0; i < count; i++) {
        int port = ports[i];
        if (sfp_bit(port) < 0) continue;
        const struct sfp_cage *cage = have_cages ? &cages[port - SFP_FIRST_PORT] : NULL;
        struct ffn_json *module = cage ? module_summary(io, paths, port, cage->present) : NULL;
        struct ffn_json *record = NULL;
        if (diagnostics && cage && cage->present) {
            uint8_t a0[96], a2[128]; bool has_a2 = false; char derr[160];
            if (sfp_read_diagnostics(io, paths, port, a0, a2, &has_a2, derr, sizeof derr) || sfp_decode_diagnostics(a0, has_a2 ? a2 : NULL, has_a2 ? 128 : 0, &record, derr, sizeof derr)) {
                record = ffn_json_object(); ffn_json_set(record, "error", ffn_json_string(derr));
            }
        }
        char lerr[96];
        struct ffn_json *link = switch_link(io, port, cage, lerr, sizeof lerr);
        char key[8]; snprintf(key, sizeof key, "%d", port);
        struct ffn_json *committed = ffn_json_object();
        bool enabled = false; const struct ffn_json *sp = saved_ports ? ffn_json_get(saved_ports, key) : NULL;
        if (sp && sp->type == FFN_JSON_BOOL) enabled = sp->boolean; else if (link) ffn_json_get_bool(link, "enabled", &enabled);
        ffn_json_set(committed, "enabled", ffn_json_bool(enabled));
        const char *speed = saved_speeds ? ffn_json_get_string(saved_speeds, key) : NULL;
        ffn_json_set(committed, "speed", ffn_json_string(speed ? speed : "auto"));
        ffn_json_set(committed, "attached", ffn_json_bool(true));
        struct ffn_json *attachment = attachment_state(io, paths, port, epoch);
        ffn_json_push(results, sfp_check_port(port, cage, module, record, link, attachment, committed));
        ffn_json_free(module); ffn_json_free(record); ffn_json_free(link); ffn_json_free(committed); ffn_json_free(attachment);
    }
    ffn_json_free(saved);
    return results;
}

int sfp_publish(const struct sfp_io *io, const struct sfp_paths *paths, const struct ffn_json *results, const char *boot_id, long long checked_at)
{
    struct ffn_json *doc = ffn_json_object();
    ffn_json_set(doc, "schema", ffn_json_int(1)); ffn_json_set(doc, "boot_id", boot_id ? ffn_json_string(boot_id) : ffn_json_null());
    ffn_json_set(doc, "checked_at", ffn_json_int(checked_at)); ffn_json_set(doc, "ports", ffn_json_copy(results));
    int rc = io->write_json(io->ctx, paths->health, doc);
    ffn_json_free(doc);
    return rc;
}

/* ---- relink ---- */

static bool aggregate_member(const struct sfp_io *io, const struct sfp_paths *paths, int port)
{
    struct ffn_json *state = io->read_json(io->ctx, paths->aggregate_state);
    bool member = false;
    const struct ffn_json *groups = state ? ffn_json_get(state, "groups") : NULL;
    for (size_t g = 0; groups && groups->type == FFN_JSON_OBJECT && g < groups->count; g++) {
        const struct ffn_json *group = groups->items[g]; const char *phase = ffn_json_get_string(group, "phase");
        if (phase && !strcmp(phase, "stopped")) continue;
        const struct ffn_json *ports = ffn_json_get(group, "ports");
        for (size_t i = 0; ports && ports->type == FFN_JSON_ARRAY && i < ports->count; i++) if (ports->items[i]->type == FFN_JSON_INT && ports->items[i]->integer == port) member = true;
    }
    ffn_json_free(state);
    return member;
}

static struct ffn_json *result_of(int port, const char *result)
{
    struct ffn_json *o = ffn_json_object(); ffn_json_set(o, "port", ffn_json_int(port)); ffn_json_set(o, "result", ffn_json_string(result)); return o;
}

struct ffn_json *sfp_relink(const struct sfp_io *io, const struct sfp_paths *paths, int port, char *err, size_t errsz)
{
    if (sfp_bit(port) < 0) return result_of(port, "not-an-sfp-port");
    int lock = io->lock(io->ctx, paths->faceplate_lock, SFP_LOCK_SECONDS);
    if (lock < 0) { seterr(err, errsz, "Aggregate hardware lock busy"); return NULL; }
    struct ffn_json *out = NULL, *saved = io->read_json(io->ctx, paths->faceplate_state);
    const struct ffn_json *pending = saved ? ffn_json_get(saved, "pending") : NULL;
    if (pending && pending->type != FFN_JSON_NULL) { out = result_of(port, "pending-operation"); goto done; }
    if (aggregate_member(io, paths, port)) { out = result_of(port, "aggregate-member"); goto done; }
    {
        struct sfp_cage cages[SFP_PORTS]; char cerr[96];
        const struct sfp_cage *cage = sfp_inventory(io, paths, cages, cerr, sizeof cerr) == 0 ? &cages[port - SFP_FIRST_PORT] : NULL;
        char lerr[96];
        struct ffn_json *link = switch_link(io, port, cage, lerr, sizeof lerr);
        bool available = false, mac = false, configurable = false;
        const char *speed = link ? ffn_json_get_string(link, "configured_speed") : NULL;
        if (link) { ffn_json_get_bool(link, "available", &available); ffn_json_get_bool(link, "mac_enabled", &mac); ffn_json_get_bool(link, "speed_configuration", &configurable); }
        if (!link || !available) { ffn_json_free(link); out = result_of(port, "not-an-sfp-port"); goto done; }
        if (!mac || !configurable || !speed) { ffn_json_free(link); out = result_of(port, "unconfigured"); goto done; }
        struct ffn_json *mode = NULL; char merr[160];
        int applied = sfp_configure_fiber(io, paths, port, sfp_bcm_port(port), speed, NULL, &mode, merr, sizeof merr);
        if (applied < 0) { ffn_json_free(link); seterr(err, errsz, merr); goto done; }
        if (applied == 0) {
            struct ffn_json *req = ffn_json_object(); ffn_json_set(req, "op", ffn_json_string("port.link.set"));
            ffn_json_set(req, "port", ffn_json_int(sfp_bcm_port(port))); ffn_json_set(req, "speed", ffn_json_string(speed));
            struct ffn_json *reply = io->bcm(io->ctx, req, merr, sizeof merr); ffn_json_free(req);
            if (!reply) { ffn_json_free(link); seterr(err, errsz, merr); goto done; }
            ffn_json_free(reply);
        }
        struct ffn_json *after = switch_link(io, port, cage, lerr, sizeof lerr);
        out = result_of(port, "applied");
        char name[20]; snprintf(name, sizeof name, "ethernet1/%d", port);
        ffn_json_set(out, "name", ffn_json_string(name)); ffn_json_set(out, "speed", ffn_json_string(speed));
        ffn_json_set(out, "mode", mode ? mode : ffn_json_string("generic"));
        const struct ffn_json *v = after ? ffn_json_get(after, "link") : NULL; ffn_json_set(out, "link", v ? ffn_json_copy(v) : ffn_json_null());
        v = after ? ffn_json_get(after, "link_mode") : NULL; ffn_json_set(out, "link_mode", v ? ffn_json_copy(v) : ffn_json_null());
        struct ffn_json *module = cage ? module_summary(io, paths, port, cage->present) : NULL;
        ffn_json_set(out, "module", module ? module : ffn_json_null());
        ffn_json_free(link); ffn_json_free(after);
    }
done:
    ffn_json_free(saved);
    io->unlock(io->ctx, lock);
    return out;
}

/* ---- the watcher ---- */

void sfp_watch_init(struct sfp_watch *w)
{
    memset(w, 0, sizeof *w);
    w->interval = 5; w->settle = 1; w->identity_deadline = 30; w->backoff = 60; w->check_interval = 60;
}

static void emit(const struct sfp_io *io, struct ffn_json *event)
{
    char *text = ffn_json_dump(event);
    if (text) io->log(io->ctx, text);
    free(text); ffn_json_free(event);
}

static struct ffn_json *event(const char *name, int port)
{
    struct ffn_json *e = ffn_json_object(); ffn_json_set(e, "event", ffn_json_string(name));
    if (port) ffn_json_set(e, "port", ffn_json_int(port));
    return e;
}

static void verify(struct sfp_watch *w, const struct sfp_io *io, const struct sfp_paths *paths, const bool present[SFP_PORTS], time_t now)
{
    int ports[SFP_PORTS]; unsigned n = 0;
    for (int p = SFP_FIRST_PORT; p <= SFP_LAST_PORT; p++) if (present[p - SFP_FIRST_PORT]) ports[n++] = p;
    char err[160] = "";
    struct ffn_json *results = sfp_collect(io, paths, ports, n, true, err, sizeof err);
    if (!results) { w->quiet_until = now + w->backoff; struct ffn_json *e = event("check-unavailable", 0); ffn_json_set(e, "error", ffn_json_string(err)); ffn_json_set(e, "backoff_seconds", ffn_json_int(w->backoff)); emit(io, e); return; }
    char boot[80] = ""; FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r"); if (f) { if (!fgets(boot, sizeof boot, f)) boot[0] = 0; fclose(f); boot[strcspn(boot, "\r\n")] = 0; }
    sfp_publish(io, paths, results, boot[0] ? boot : NULL, (long long)time(NULL));
    for (size_t i = 0; i < results->count; i++) {
        const struct ffn_json *r = results->items[i];
        long long port = 0; ffn_json_get_int(r, "port", &port);
        const char *verdict = ffn_json_get_string(r, "verdict"); if (!verdict || port < SFP_FIRST_PORT || port > SFP_LAST_PORT) continue;
        char *slot = w->verdict[port - SFP_FIRST_PORT];
        bool changed = strcmp(slot, verdict) != 0;
        snprintf(slot, 24, "%s", verdict);
        const char *remedy = ffn_json_get_string(r, "remedy");
        if (changed && strcmp(verdict, "no-module") && strcmp(verdict, "disabled")) {
            struct ffn_json *e = event("transmit", (int)port);
            ffn_json_set(e, "verdict", ffn_json_string(verdict)); ffn_json_set(e, "detail", ffn_json_string(ffn_json_get_string(r, "detail") ? ffn_json_get_string(r, "detail") : ""));
            ffn_json_set(e, "remedy", remedy ? ffn_json_string(remedy) : ffn_json_null());
            const struct ffn_json *d = ffn_json_get(r, "diagnostics");
            const struct ffn_json *tx = d ? ffn_json_get(d, "tx_power_dbm") : NULL, *rx = d ? ffn_json_get(d, "rx_power_dbm") : NULL;
            ffn_json_set(e, "tx_power_dbm", tx ? ffn_json_copy(tx) : ffn_json_null()); ffn_json_set(e, "rx_power_dbm", rx ? ffn_json_copy(rx) : ffn_json_null());
            emit(io, e);
        }
        if (changed && remedy && (!strcmp(remedy, "enable-transmitter") || !strcmp(remedy, "relink"))) {
            struct ffn_json *outcome = NULL; char rerr[160] = "";
            if (!strcmp(remedy, "enable-transmitter")) {
                struct sfp_cage cage; int lock = io->lock(io->ctx, paths->faceplate_lock, SFP_LOCK_SECONDS);
                if (lock >= 0) { if (sfp_set_enabled(io, paths, (int)port, true, &cage, rerr, sizeof rerr) == 0) outcome = result_of((int)port, "transmitter-enabled"); io->unlock(io->ctx, lock); }
                else snprintf(rerr, sizeof rerr, "Aggregate hardware lock busy");
            } else outcome = sfp_relink(io, paths, (int)port, rerr, sizeof rerr);
            struct ffn_json *e = event("remediated", (int)port); ffn_json_set(e, "remedy", ffn_json_string(remedy));
            if (outcome) { for (size_t k = 0; k < outcome->count; k++) ffn_json_set(e, outcome->keys[k], ffn_json_copy(outcome->items[k])); ffn_json_free(outcome); }
            else { ffn_json_set(e, "result", ffn_json_string("error")); ffn_json_set(e, "error", ffn_json_string(rerr)); }
            emit(io, e);
            slot[0] = 0;   /* re-judge on the next check rather than trusting the remedy */
        }
    }
    ffn_json_free(results);
}

void sfp_watch_poll(struct sfp_watch *w, const struct sfp_io *io, const struct sfp_paths *paths)
{
    time_t now = io->now(io->ctx);
    if (now < w->quiet_until) return;
    struct sfp_cage cages[SFP_PORTS]; char err[160] = "";
    if (sfp_inventory(io, paths, cages, err, sizeof err)) {
        w->quiet_until = now + w->backoff;
        struct ffn_json *e = event("presence-unavailable", 0); ffn_json_set(e, "error", ffn_json_string(err)); ffn_json_set(e, "backoff_seconds", ffn_json_int(w->backoff)); emit(io, e);
        return;
    }
    bool present[SFP_PORTS];
    for (int i = 0; i < SFP_PORTS; i++) present[i] = cages[i].present;
    if (w->have_previous)
        for (int i = 0; i < SFP_PORTS; i++)
            if (present[i] && !w->previous[i]) { w->pending_deadline[i] = now + (time_t)w->identity_deadline; emit(io, event("inserted", i + SFP_FIRST_PORT)); }
    for (int i = 0; i < SFP_PORTS; i++) {
        if (!w->pending_deadline[i]) continue;
        int port = i + SFP_FIRST_PORT;
        if (!present[i]) { w->pending_deadline[i] = 0; emit(io, event("removed-before-relink", port)); continue; }
        if (now < w->pending_deadline[i] - (time_t)w->identity_deadline + (time_t)w->settle) continue;
        uint8_t a0[96]; char ierr[160] = "";
        if (sfp_identity(io, paths, port, a0, ierr, sizeof ierr)) {
            if (now >= w->pending_deadline[i]) { w->pending_deadline[i] = 0; struct ffn_json *e = event("identity-unreadable", port); ffn_json_set(e, "error", ffn_json_string(ierr)); emit(io, e); }
            continue;
        }
        w->pending_deadline[i] = 0;
        char rerr[160] = "";
        struct ffn_json *result = sfp_relink(io, paths, port, rerr, sizeof rerr);
        struct ffn_json *e = event("relink", port);
        if (result) { for (size_t k = 0; k < result->count; k++) ffn_json_set(e, result->keys[k], ffn_json_copy(result->items[k])); ffn_json_free(result); }
        else { ffn_json_set(e, "result", ffn_json_string("error")); ffn_json_set(e, "error", ffn_json_string(rerr)); }
        emit(io, e);
    }
    memcpy(w->previous, present, sizeof present); w->have_previous = true;
    bool pending = false; for (int i = 0; i < SFP_PORTS; i++) if (w->pending_deadline[i]) pending = true;
    if (now >= w->next_check && !pending) { w->next_check = now + (time_t)w->check_interval; verify(w, io, paths, present, now); }
}
