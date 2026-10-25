# Dağıtım — Sabit Çıkış IP

Bot **ev bilgisayarında** çalışır; borsalara giden tüm trafik **WireGuard tüneliyle sabit IP'li VPS'ten** çıkar. Binance yalnızca VPS'in IP'sini görür ve API anahtarı bu IP'ye kilitlenir (IP whitelist).

```
[Ev bilgisayarı: Docker]                                  [VPS: Ubuntu 24.04, sabit IP]
 ┌────────────┐  ağ ad alanı   ┌────────────────┐  UDP/51820  ┌──────────┐
 │ bot        │ ─────────────▶ │ wireguard      │ ══tünel═══▶ │ wg0 + NAT│ ──▶ Binance / BTCTurk
 └────────────┘  paylaşımlı    │ + kill-switch  │             └──────────┘
                               └────────────────┘
```

## Dosyalar
| Yol | Ne işe yarar |
|---|---|
| `ORACLE_KURULUM.md` | Oracle Cloud Always Free VPS kurulumu (reserved IP, Frankfurt, Security List, ufw) |
| `LOKAL_KURULUM.md` | Ev bilgisayarı (Windows + Docker Desktop / Linux) kurulumu |
| `wireguard/server/setup_wireguard_server.sh` | VPS'te WireGuard sunucusu + bot için istemci dosyası üretir |
| `wireguard/server/wg0.conf.template` | Sunucu şablonu |
| `wireguard/client/wg0.conf.template` | İstemci şablonu (tüm trafik + DNS tünelden) |
| `ufw/setup_ufw.sh` | VPS güvenlik duvarı (yalnızca WireGuard UDP + SSH) |
| `dante/` | (Seçenek B) Yalnızca tünel içinden erişilen SOCKS5 proxy |
| `vps/check_exchange_access.sh` | VPS'ten Binance/BTCTurk erişim testi (451/403 kontrolü) |
| `local/killswitch.sh` | WireGuard konteynerinde iptables kill-switch |
| `local/wg_healthcheck.sh` | Tünel sağlık kontrolü (son el sıkışma < 180 sn) |
| `local/wg_confs/` | İstemci `wg0.conf` buraya konur (gitignore'da) |

## Seçenekler (`config.yaml` → `egress.mode`)
- **`wireguard` (önerilen, varsayılan):** docker-compose'daki `wireguard` servisi. Bot konteyneri `network_mode: service:wireguard` ile onun ağını kullanır; kill-switch tünel dışı çıkışı engeller. `.env`'de yalnızca `EGRESS_EXPECTED_IP` gerekir.
- **`proxy` (Seçenek B):** Bot Docker'sız Linux'ta çalışıyorsa. Host WireGuard ile VPS'e bağlanır (yalnızca `10.13.13.0/24` rotası), bot `EGRESS_PROXY_URL=socks5h://...@10.13.13.1:1080` ile Dante üzerinden çıkar. Proxy yalnızca tünel içinde dinler.
- **`direct_vps`:** Bot doğrudan sabit IP'li VPS'te çalışıyorsa (veya ISS'den sabit IP alındıysa). Yalnızca IP doğrulaması yapılır.
- **`managed_proxy`:** Ücretli statik IP proxy servisi (`EGRESS_PROXY_URL`).

## Güvenlik garantileri (kodda ve testlerde)
1. Testnet/live modda bot açılışta ve her `check_interval_minutes` dakikada bir dış IP'yi **en az 2 bağımsız servisten** sorgular. Hepsi `EGRESS_EXPECTED_IP`'yi döndürmezse, servislerden biri ulaşılamazsa veya kontrol bayatlarsa **emir gönderilmez** (fail-closed) ve `RiskAlert` yayınlanır.
2. Proxy düşerse **doğrudan bağlantıya dönülmez**; istek hata verir (`EgressUnavailableError`). `HTTP(S)_PROXY` ortam değişkenleri de dikkate alınmaz.
3. ccxt'ye REST ve WebSocket için aynı proxy verilir (`socksProxy` + `wsSocksProxy` veya `httpsProxy` + `wssProxy`).
4. Açılışta borsaya erişim testi yapılır; HTTP 451/403 "restricted location" ayrı hata türü olarak raporlanır.
5. Heartbeat yalnızca bot sağlıklıyken (IP doğrulanmış) ping atar; ping kesilince harici izleme servisi (healthchecks.io, Uptime Kuma) Telegram'a uyarı gönderir.

Hızlı kontrol: `bot net-check` (dış IP + borsa erişimi, sonuç: "Emirlere izin: EVET/HAYIR").

> Not: Bu yapı coğrafi kısıtlamaları aşmak için değil, IP whitelist için kullanılır. VPS'in ülkesi borsanın hizmet verdiği bir ülke olmalıdır.
