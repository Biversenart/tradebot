# Kripto Trade & Arbitraj Botu

Seçilen kripto paraları çoklu zaman diliminde analiz eden, giriş/çıkış planı çıkaran, risk kontrollü işlem açıp kapatan ve arbitraj fırsatlarını tarayan modüler bot.

> ⚠️ Varsayılan mod **paper**'dır. Gerçek para ile işlem yalnızca çift onayla açılır. Kripto ticareti yüksek risk içerir; geçmiş performans gelecekteki sonuçları garanti etmez.

## Kurulum (geliştirme)
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                       # sırlar yalnızca burada; ASLA commit etme
cp config/config.example.yaml config/config.yaml
bot config-check                           # config + .env doğrulama, etkin modu gösterir
bot net-check                              # dış IP + borsa erişim testi
bot run                                    # servisleri başlatır
```

Kalite kontrolleri:
```bash
ruff check . && ruff format --check .
mypy src tests
pytest            # `integration` işaretli testler varsayılan olarak atlanır
```

Docker (wireguard + bot + postgres; `.env` içinde `POSTGRES_PASSWORD` ve `deploy/local/wg_confs/wg0.conf` gerekli):
```bash
docker compose up -d --build
docker compose exec bot bot net-check --config /app/config/config.yaml
```
Sabit IP kurulumu: `deploy/README.md`, `deploy/ORACLE_KURULUM.md`, `deploy/LOKAL_KURULUM.md`.

## Güvenlik kuralları (özet)
- `live` için `.env`'de `TRADING_MODE=live` **ve** `config.yaml`'da `mode: live` + `live_trading_confirmed: true` gerekir.
- Testnet/live modda `EGRESS_EXPECTED_IP` (genel IP) zorunlu; proxy modlarında `EGRESS_PROXY_URL` de zorunlu. `egress.fail_closed` kapatılamaz.
- Loglar JSON'dur; anahtar, token, parola ve URL içindeki kimlik bilgileri maskelenir.
- Para hesapları `Decimal` ile yapılır.

## Proje yapısı
`src/bot/` altında spec §4'teki modüller: `core` (modeller, EventBus, saat, hassasiyet), `config`, `log`, ve sonraki aşamalarda doldurulacak `exchanges`, `marketdata`, `indicators`, `analysis`, `net`, `strategies`, `arbitrage`, `risk`, `execution`, `portfolio`, `storage`, `backtest`, `notify`, `api`.

## Dokümanlar
- `CLAUDE.md` — Claude Code için kalıcı kurallar
- `docs/PROJE_SPEC.md` — teknik şartname
- `docs/YOL_HARITASI.md` — aşama planı
- `docs/CLAUDE_CODE_PROMPTLARI.md` — her aşama için hazır promptlar
- `docs/EK_OZELLIKLER.md` — ileri özellik önerileri
- `docs/KARARLAR.md` — karar günlüğü

## Durum
- ✅ Aşama 0 — İskelet (config + çift onay, JSON log + maskeleme, Decimal çekirdek modeller, EventBus, CI, Docker)
- ✅ Aşama 1 — Sabit IP: egress (proxy/WireGuard), fail-closed IP doğrulama, restricted-location testi, heartbeat, kill-switch'li compose, VPS/lokal kurulum rehberleri
- ⏭️ Sıradaki: Aşama 2 — Borsa bağlayıcıları ve veri
