#!/usr/bin/with-contenv sh
# WireGuard konteyneri için kill-switch (linuxserver/wireguard /custom-cont-init.d).
# Bot konteyneri bu konteynerin ağ ad alanını paylaşır (network_mode: service:wireguard).
# Kurallar wg-quick'ten BAĞIMSIZDIR: tünel düşse veya wg0 kaldırılsa bile varsayılan
# politika DROP kalır; internete yalnızca wg0 üzerinden çıkılabilir.
set -eu

CONF="${WG_CONF:-/config/wg_confs/wg0.conf}"
LOCAL_NET="${KILLSWITCH_LOCAL_NET:-172.28.0.0/24}"

ENDPOINT="$(awk -F'=' '/^[[:space:]]*Endpoint/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "$CONF")"
EP_HOST="${ENDPOINT%:*}"
EP_PORT="${ENDPOINT##*:}"
case "$EP_HOST" in
  *[!0-9.]*|"") echo "[killswitch] Endpoint IPv4 olmalı (alan adı değil): $ENDPOINT" >&2; exit 1 ;;
esac

# IPv4: varsayılan DROP
iptables -P OUTPUT DROP
iptables -F OUTPUT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -o wg0 -j ACCEPT
# compose ağı (postgres) - yalnızca yerel alt ağ
iptables -A OUTPUT -d "$LOCAL_NET" -j ACCEPT
# Şifreli WireGuard paketleri yalnızca VPS'e
iptables -A OUTPUT -p udp -d "$EP_HOST" --dport "$EP_PORT" -j ACCEPT

# IPv6: tamamen kapalı (sızıntı yok)
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -P OUTPUT DROP 2>/dev/null || true
  ip6tables -F OUTPUT 2>/dev/null || true
  ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
fi

echo "[killswitch] aktif: çıkış yalnızca wg0, $LOCAL_NET ve $EP_HOST:$EP_PORT/udp"
