#!/bin/sh
# Son WireGuard el sıkışması 180 sn'den eskiyse sağlıksız.
set -eu
last="$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2; exit}')"
[ -n "${last:-}" ] && [ "$last" -gt 0 ] || exit 1
now="$(date +%s)"
[ $((now - last)) -lt 180 ]
