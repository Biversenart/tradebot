#!/usr/bin/env bash
# VPS (Ubuntu 24.04) üzerinde WireGuard sunucusu kurar ve bot için bir istemci (peer) üretir.
# Kullanım:  sudo bash setup_wireguard_server.sh <VPS_PUBLIC_IP> [WAN_IF] [WG_PORT]
# Çıktı: /etc/wireguard/wg0.conf ve /root/wg-client/wg0.conf (ev bilgisayarına kopyalanacak).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "root olarak çalıştırın (sudo)." >&2; exit 1; fi
VPS_IP="${1:?Kullanım: $0 <VPS_PUBLIC_IP> [WAN_IF] [WG_PORT]}"
WAN_IF="${2:-$(ip -4 route show default | awk '{print $5; exit}')}"
WG_PORT="${3:-51820}"
SERVER_TUNNEL_IP="10.13.13.1"
CLIENT_TUNNEL_IP="10.13.13.2"
TUNNEL_SUBNET="10.13.13.0/24"
HERE="$(cd "$(dirname "$0")" && pwd)"
CLIENT_TEMPLATE="$HERE/../client/wg0.conf.template"

apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard iptables

# IP yönlendirme (kalıcı)
cat > /etc/sysctl.d/99-wireguard.conf <<SYSCTL
net.ipv4.ip_forward=1
SYSCTL
sysctl --system >/dev/null

umask 077
mkdir -p /etc/wireguard /root/wg-client
if [[ -f /etc/wireguard/wg0.conf ]]; then
  echo "/etc/wireguard/wg0.conf zaten var; üzerine yazılmadı." >&2
  exit 1
fi
SERVER_PRIV=$(wg genkey); SERVER_PUB=$(echo "$SERVER_PRIV" | wg pubkey)
CLIENT_PRIV=$(wg genkey); CLIENT_PUB=$(echo "$CLIENT_PRIV" | wg pubkey)
PSK=$(wg genpsk)

sed -e "s|__SERVER_TUNNEL_IP__|$SERVER_TUNNEL_IP|" \
    -e "s|__WG_PORT__|$WG_PORT|" \
    -e "s|__SERVER_PRIVATE_KEY__|$SERVER_PRIV|" \
    -e "s|__TUNNEL_SUBNET__|$TUNNEL_SUBNET|g" \
    -e "s|__WAN_IF__|$WAN_IF|g" \
    -e "s|__CLIENT_PUBLIC_KEY__|$CLIENT_PUB|" \
    -e "s|__PRESHARED_KEY__|$PSK|" \
    -e "s|__CLIENT_TUNNEL_IP__|$CLIENT_TUNNEL_IP|" \
    "$HERE/wg0.conf.template" > /etc/wireguard/wg0.conf

sed -e "s|__CLIENT_TUNNEL_IP__|$CLIENT_TUNNEL_IP|" \
    -e "s|__CLIENT_PRIVATE_KEY__|$CLIENT_PRIV|" \
    -e "s|__SERVER_PUBLIC_KEY__|$SERVER_PUB|" \
    -e "s|__PRESHARED_KEY__|$PSK|" \
    -e "s|__VPS_PUBLIC_IP__|$VPS_IP|" \
    -e "s|__WG_PORT__|$WG_PORT|" \
    "$CLIENT_TEMPLATE" > /root/wg-client/wg0.conf

systemctl enable --now wg-quick@wg0
echo
echo "WireGuard sunucusu çalışıyor (arayüz: wg0, port: $WG_PORT/udp, WAN: $WAN_IF)."
echo "İstemci dosyası: /root/wg-client/wg0.conf"
echo "Bu dosyayı GÜVENLİ şekilde (scp) ev bilgisayarına deploy/local/wg_confs/wg0.conf olarak kopyalayın,"
echo "sonra VPS'ten silin:  shred -u /root/wg-client/wg0.conf"
