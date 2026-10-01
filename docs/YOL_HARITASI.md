# Yol Haritası

Her aşama bitince kutuyu işaretle, testleri çalıştır, README'yi güncelle. Bir aşama bitmeden diğerine geçme.

## Aşama 0 — İskelet
- [ ] `pyproject.toml` (Python 3.12, bağımlılıklar, ruff/mypy/pytest ayarları)
- [ ] Klasör yapısı (`docs/PROJE_SPEC.md` §4), `.gitignore`, `.env.example`, `config/config.example.yaml`
- [ ] `pydantic-settings` ile config yükleme + doğrulama; live çift onay kontrolü
- [ ] `structlog` JSON log + sır maskeleme
- [ ] Çekirdek modeller: `Candle`, `Ticker`, `OrderBook`, `OrderIntent`, `Order`, `Fill`, `Position`, `TradePlan`, `Signal` (Decimal)
- [ ] EventBus
- [ ] GitHub Actions: ruff, mypy, pytest, gitleaks, pip-audit
- [ ] Dockerfile + docker-compose (bot, postgres)

## Aşama 1 — Ağ katmanı ve sabit IP
- [ ] `net/egress.py`: proxy URL'lerinden HTTP/SOCKS/WS ayarı üretme
- [ ] `net/ip_guard.py`: 2 bağımsız servisle dış IP doğrulama, periyodik kontrol, fail-closed
- [ ] `deploy/`: WireGuard sunucu/istemci şablonu, Dante (SOCKS5) örnek config, ufw kuralları, kurulum README'si
- [ ] docker-compose'a `wireguard` istemci servisi; bot `network_mode: service:wireguard`, tünel dışı çıkış iptables ile kapalı
- [ ] `deploy/ORACLE_KURULUM.md`: Oracle Cloud Always Free + reserved IP + WireGuard sunucu + firewall (spec §5.11.2)
- [ ] `deploy/LOKAL_KURULUM.md`: Windows (Docker Desktop/WSL2) ve Linux için adım adım lokal kurulum, uyku modu kapatma, otomatik başlatma
- [ ] Heartbeat (harici izleme → Telegram)
- [ ] Testler: IP uyuşmazlığında emir engelleniyor; proxy düşünce direct'e dönülmüyor

## Aşama 2 — Borsa bağlayıcıları ve veri
- [ ] `ExchangeAdapter` arayüzü
- [ ] Binance (spot + futures testnet) adapter'ı — ccxt, proxy ayarlarıyla
- [ ] BTCTurk adapter'ı
- [ ] `PaperExchange` (orderbook derinliğine göre dolum)
- [ ] Market data: WS akışları, mum oluşturucu, warm-up, boşluk doldurma
- [ ] Geçmiş veri indirme komutu: `bot data download --symbol BTC/USDT --tf 1h --since 2022-01-01` (Parquet)

## Aşama 3 — Analiz motoru (en kritik aşama)
- [ ] Göstergeler (EMA, RSI, MACD, StochRSI, Bollinger, ATR, ADX, OBV, VWAP) + birim testleri (bilinen değerlerle)
- [ ] Rejim tespiti
- [ ] Swing noktaları, HH/HL sınıflandırma, BOS/CHoCH
- [ ] Destek/direnç ve arz/talep bölgeleri (güç puanıyla), likidite havuzları, Fibonacci
- [ ] Hacim profili (POC/VAH/VAL), anchored VWAP
- [ ] Divergence tespiti
- [ ] Mum ve grafik formasyonları
- [ ] Confluence puanlayıcı (ağırlıklar config'den)
- [ ] `TradePlan` üretici (giriş bölgesi, yapısal SL + ATR tamponu, TP1-3, R:R filtresi, geçersizlik)
- [ ] `bot analyze <SYMBOL>` → Türkçe rapor + plotly HTML grafik

## Aşama 4 — Stratejiler ve backtest
- [ ] `BaseStrategy`, 5 strateji (spec §5.4)
- [ ] Olay güdümlü backtest motoru (komisyon, kayma, gecikme)
- [ ] Metrikler + HTML rapor
- [ ] Walk-forward ve parametre taraması; out-of-sample raporu
- [ ] Çıkış yönetimi: kısmi TP, başabaşa stop, trailing, zaman bazlı çıkış

## Aşama 5 — Risk ve emir yürütme
- [ ] RiskManager (spec §5.6 tüm kontroller)
- [ ] İşlem öncesi risk raporu (risk_score, korelasyon, volatilite, likidite)
- [ ] Portföy riski: korelasyon matrisi, VaR/CVaR, risk-at-stop
- [ ] Büyüme odaklı boyutlama: bileşik %, volatilite ayarı, kalite çarpanı, ¼ Kelly, drawdown'da risk azaltma, kâr kilitleme
- [ ] Strateji/coin performans takibi ve otomatik sermaye dağılımı
- [ ] Borsa tarafı stop emirleri (her pozisyon için zorunlu)
- [ ] Kill switch (zarar limiti, drawdown, hata sayısı, IP uyuşmazlığı)
- [ ] Execution engine: limit/market/stop/OCO, kısmi dolum, timeout, idempotent client_order_id
- [ ] Reconciliation (yeniden başlatmada durum eşitleme)
- [ ] Depolama: SQLAlchemy modelleri + alembic migration

## Aşama 6 — Arbitraj
- [ ] Cross-exchange tarayıcı (net spread, derinlik, gecikme)
- [ ] Triangular tarayıcı
- [ ] Leg risk yönetimi (bir bacak dolmazsa hedge/kapat)
- [ ] Rebalans uyarıları
- [ ] (Opsiyonel) TRY köprüsü, funding rate

## Aşama 7 — Bildirim ve panel
- [ ] Telegram bildirimleri + komutlar
- [ ] FastAPI panel (token korumalı), kill switch butonu, analiz raporları sayfası

## Aşama 7.5 — Operasyonel güvenlik ağları (spec §5.13)
- [ ] Kademeli sermaye (live_capital_cap) ve gölge mod
- [ ] Borsa duyuru takibi, stablecoin depeg kontrolü, borsa başına bakiye tavanı
- [ ] Backtest önyargı kontrolleri, düşük likidite modu, config değişiklik günlüğü
- [ ] Kaos testleri

## Aşama 8 — Paper çalıştırma ve değerlendirme
- [ ] VPS'te sabit IP ile 2 hafta paper/testnet çalıştırma
- [ ] Sonuç raporu (spec §9 eşikleriyle karşılaştırma)
- [ ] Ancak eşikler tutarsa: küçük bakiye ile live, kademeli artış

## Aşama 9 — Ek özellikler (opsiyonel, öncelik sırasıyla)
Ayrıntı: `docs/EK_OZELLIKLER.md`. Öncelikli olanlar:
- [ ] Heartbeat + harici izleme
- [ ] Piyasa geneli (BTC) filtresi ve anomali freni
- [ ] Telegram yarı otomatik onay modu
- [ ] İşlem günlüğü + TL bazlı vergi dökümü
