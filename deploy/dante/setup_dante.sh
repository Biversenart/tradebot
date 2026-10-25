#!/usr/bin/env bash
# Dante SOCKS5 kurulumu (WireGuard sunucusu kurulduktan SONRA çalıştırın).
# Kullanım:  sudo bash setup_dante.sh [PROXY_USER]
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "root olarak çalıştırın (sudo)." >&2; exit 1; fi
PROXY_USER="${1:-botproxy}"
SERVER_TUNNEL_IP="10.13.13.1"
TUNNEL_SUBNET="10.13.13.0/24"
WAN_IF="$(ip -4 route show default | awk '{print $5; exit}')"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! ip -4 addr show wg0 >/dev/null 2>&1; then
  echo "wg0 arayüzü yok. Önce setup_wireguard_server.sh çalıştırın." >&2; exit 1
fi

DEBIAN_FRONTEND=noninteractive apt-get install -y dante-server

if ! id "$PROXY_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$PROXY_USER"
fi
PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
echo "$PROXY_USER:$PASS" | chpasswd

sed -e "s|__SERVER_TUNNEL_IP__|$SERVER_TUNNEL_IP|g" \
    -e "s|__WAN_IF__|$WAN_IF|g" \
    -e "s|__TUNNEL_SUBNET__|$TUNNEL_SUBNET|g" \
    -e "s|__PROXY_USER__|$PROXY_USER|g" \
    "$HERE/danted.conf.template" > /etc/danted.conf

# danted wg0 adresi hazır olmadan başlamasın
mkdir -p /etc/systemd/system/danted.service.d
cat > /etc/systemd/system/danted.service.d/override.conf <<UNIT
[Unit]
After=wg-quick@wg0.service
Requires=wg-quick@wg0.service
UNIT
systemctl daemon-reload
systemctl enable --now danted
systemctl restart danted

echo
echo "Dante çalışıyor: $SERVER_TUNNEL_IP:1080 (yalnızca tünel içinden)."
echo "Bot .env satırı (bu parolayı yalnızca .env'e yazın, başka yere kaydetmeyin):"
echo "EGRESS_PROXY_URL=socks5h://$PROXY_USER:$PASS@$SERVER_TUNNEL_IP:1080"
