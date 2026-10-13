#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# CP side of the management plane's service routes (core docs/service-routes.md).
# Idempotent: rerunning leaves the same rule, table and sysctls in place.
set -eu
MP=127.1.1.1
DP=127.1.2.2
UP=ffnnet0
DOWN=ffndp0
TABLE=100
sysctl -q -w net.ipv4.ip_forward=1
for dev in "$UP" "$DOWN"; do
    sysctl -q -w "net.ipv4.conf.$dev.route_localnet=1"
    sysctl -q -w "net.ipv4.conf.$dev.rp_filter=0"
done
sysctl -q -w net.ipv4.conf.all.rp_filter=0
ip route replace default via "$DP" dev "$DOWN" table "$TABLE"
if ! ip rule show | grep -q "from $MP iif $UP lookup $TABLE"; then
    ip rule add pref "$TABLE" from "$MP" iif "$UP" lookup "$TABLE"
fi
echo "service-route forwarding: $MP via $UP -> $DP via $DOWN (table $TABLE)"
