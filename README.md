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
bot data download --symbol BTC/USDT --tf 1h --since 2022-01-01   # geçmiş veri → data/ohlcv/*.parquet
bot analyze BTC/USDT                       # MTF Türkçe rapor + plotly grafik → reports/analysis/
bot backtest report -s BTC/USDT -s ETH/USDT -s SOL/USDT --since 2022-01-01   # walk-forward + §9 özeti
bot backtest walkforward -s BTC/USDT --strategy breakout                     # tek strateji HTML rapor
bot data synthetic -s BTC/USDT --price 47000                                 # ağsız demo için SENTETİK veri
bot run                                    # servisleri başlatır
```

Kalite kontrolleri:
```bash
ruff check . && ruff format --check .
mypy src tests
pytest                  # `integration` işaretli testler varsayılan olarak atlanır
pytest -m integration   # gerçek borsa uç noktalarına bağlanan testler
```

Veritabanı migration (postgres): `docker compose run --rm bot alembic upgrade head` (SQLite'ta tablolar otomatik oluşur).

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
- `docs/BACKTEST_OZET.md` — Aşama 4 backtest özeti (sentetik veri; gerçek veriyle yeniden çalıştırılmalı)

## Durum
- ✅ Aşama 0 — İskelet (config + çift onay, JSON log + maskeleme, Decimal çekirdek modeller, EventBus, CI, Docker)
- ✅ Aşama 1 — Sabit IP: egress (proxy/WireGuard), fail-closed IP doğrulama, restricted-location testi, heartbeat, kill-switch'li compose, VPS/lokal kurulum rehberleri
- ✅ Aşama 2 — ExchangeAdapter, Binance (spot + futures testnet/demo), BTCTurk, PaperExchange (derinliğe göre dolum), market data (WS akışları, mum oluşturucu, warm-up, boşluk doldurma), `bot data download` (Parquet)
- ✅ Aşama 3 — Analiz motoru: göstergeler, rejim, yapı (BOS/CHoCH), bölgeler, hacim profili, uyumsuzluk, formasyonlar, confluence, TradePlan, `bot analyze`
- ✅ Aşama 4 — 5 strateji + sinyal birleştirici (canlı/backtest aynı sınıflar), olay güdümlü backtest, metrikler, walk-forward + parametre taraması, HTML rapor, çıkış yönetimi
- ✅ Aşama 5 — RiskManager (ApprovedIntent), büyüme odaklı boyutlama, kill switch, portföy riski; emir yürütme (borsa tarafı zorunlu stop, idempotent id, kısmi dolum), reconciliation, SQLAlchemy + alembic, `bot run` ile uçtan uca paper/testnet akışı
- ⏭️ Sıradaki: Aşama 6 — Arbitraj
