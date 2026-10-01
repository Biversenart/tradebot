#!/usr/bin/env bash
# VPS güvenlik duvarı: yalnızca WireGuard UDP portu (ve geçici olarak SSH) açık.
# Kullanım:  sudo bash setup_ufw.sh [WG_PORT] [SSH_FROM]
#   SSH_FROM: SSH'a izin verilen kaynak. Varsayılan "any" (kurulum sırasında);
#   tünel kurulduktan sonra "10.13.13.0/24" ile tekrar çalıştırarak SSH'ı yalnızca tünele açın.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "root olarak çalıştırın (sudo)." >&2; exit 1; fi
WG_PORT="${1:-51820}"
SSH_FROM="${2:-any}"
WAN_IF="$(ip -4 route show default | awk '{print $5; exit}')"

DEBIAN_FRONTEND=noninteractive apt-get install -y ufw

# Oracle Cloud Ubuntu imajlarında /etc/iptables/rules.v4 içinde önceden tanımlı REJECT
# kuralları gelir ve ufw ile çakışır. Yedekleyip devre dışı bırakıyoruz; ufw yönetecek.
if [[ -f /etc/iptables/rules.v4 ]]; then
  cp /etc/iptables/rules.v4 "/etc/iptables/rules.v4.bak.$(date +%s)"
  systemctl disable --now netfilter-persistent 2>/dev/null || true
  iptables -F INPUT; iptables -F FORWARD
  iptables -P INPUT ACCEPT; iptables -P FORWARD ACCEPT
fi

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw default deny routed

# SSH ÖNCE (kendinizi kilitlemeyin)
if [[ "$SSH_FROM" == "any" ]]; then
  ufw limit 22/tcp comment 'SSH (gecici; sonra yalnizca tunel)'
else
  ufw allow from "$SSH_FROM" to any port 22 proto tcp comment 'SSH yalnizca tunelden'
fi
ufw allow "$WG_PORT"/udp comment 'WireGuard'
# Tünelden internete yönlendirme (NAT wg0.conf PostUp ile yapılır)
ufw route allow in on wg0 out on "$WAN_IF" comment 'tunel -> internet'
# Dante kullanılıyorsa yalnızca tünel içinden erişilebilir
ufw allow in on wg0 to any port 1080 proto tcp comment 'Dante SOCKS5 (yalnizca tunel)'

ufw --force enable
ufw status verbose
