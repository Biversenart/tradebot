# Proje Şartnamesi — Kripto Trade & Arbitraj Botu

## 1. Amaç
Kullanıcının seçtiği kripto paralar için:
- Piyasa verisini gerçek zamanlı toplamak,
- Teknik analizle sinyal üretmek,
- Risk kurallarına uyarak işlem açıp kapatmak,
- Borsalar arası ve üçgen arbitraj fırsatlarını tespit edip (isteğe bağlı) değerlendirmek,
- Tüm bunları geriye dönük test (backtest), kâğıt üzerinde işlem (paper) ve canlı (live) modlarda çalıştırmak.

## 2. Çalışma modları
| Mod | Açıklama |
|---|---|
| `backtest` | Geçmiş OHLCV/orderbook verisiyle strateji simülasyonu, komisyon ve kayma (slippage) dahil |
| `paper` | Canlı veri, sahte bakiye; emirler simüle edilir (varsayılan mod) |
| `testnet` | Borsanın test ağı (Binance Testnet vb.) üzerinde gerçek API çağrıları |
| `live` | Gerçek para. Çift onay gerektirir (bkz. CLAUDE.md kural 1) |

## 3. Mimari

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ MarketData   │──▶│ Strategy     │──▶│ RiskManager  │
│ (WS + REST)  │   │ Engine       │   │              │
└──────┬───────┘   └──────────────┘   └──────┬───────┘
       │           ┌──────────────┐          │ onaylı OrderIntent
       └──────────▶│ Arbitrage    │──────────┤
                   │ Scanner      │          ▼
                   └──────────────┘   ┌──────────────┐
                                      │ Execution    │──▶ ExchangeAdapter(ler)
                                      │ Engine       │
                                      └──────┬───────┘
                                             ▼
             ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
             │ Portfolio /  │   │ Storage (DB) │   │ Notifier     │
             │ Position Mgr │   │              │   │ (Telegram)   │
             └──────────────┘   └──────────────┘   └──────────────┘
                                 ▲
                         FastAPI Panel + Kill Switch
```

Bileşenler bir iç olay yolu (`asyncio.Queue` tabanlı EventBus) üzerinden haberleşir: `TickerEvent`, `CandleEvent`, `OrderBookEvent`, `SignalEvent`, `OrderIntent`, `OrderUpdate`, `FillEvent`, `RiskAlert`.

## 4. Klasör yapısı
```
kripto-bot/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── config/
│   └── config.example.yaml
├── src/bot/
│   ├── main.py                 # CLI giriş noktası (typer)
│   ├── core/                   # EventBus, modeller, enum'lar, saat
│   ├── config/                 # pydantic ayar modelleri
│   ├── exchanges/              # ExchangeAdapter arayüzü + binance, btcturk, okx, bybit, paper
│   ├── marketdata/             # WS akışları, mum oluşturucu, orderbook önbelleği
│   ├── indicators/             # RSI, EMA, MACD, Bollinger, ATR, VWAP, hacim profili
│   ├── analysis/               # MTF, rejim, yapı, bölgeler, formasyonlar, confluence, TradePlan, rapor
│   ├── net/                    # egress proxy, IP doğrulama
│   ├── strategies/             # BaseStrategy + somut stratejiler
│   ├── arbitrage/              # cross-exchange, triangular, funding-rate tarayıcıları
│   ├── risk/                   # RiskManager, risk analizi, portföy riski, büyüme odaklı boyutlama, kill switch
│   ├── execution/              # emir yönetimi, retry, kısmi dolum, OCO/SL/TP
│   ├── portfolio/              # pozisyon, bakiye, PnL hesapları
│   ├── storage/                # SQLAlchemy modelleri, repository'ler, alembic
│   ├── backtest/               # olay güdümlü backtest motoru + raporlama
│   ├── notify/                 # Telegram
│   └── api/                    # FastAPI panel
├── tests/
│   ├── unit/
│   ├── integration/            # testnet'e karşı, CI'da varsayılan olarak atlanır
│   └── fixtures/               # örnek OHLCV ve orderbook verileri
└── docs/
    ├── PROJE_SPEC.md
    ├── YOL_HARITASI.md
    └── KARARLAR.md
```

## 5. Modüller

### 5.1 ExchangeAdapter
Ortak arayüz:
- `fetch_ohlcv(symbol, timeframe, since, limit)`
- `watch_ticker / watch_order_book / watch_trades` (async generator)
- `fetch_balance()`, `fetch_open_orders()`, `fetch_my_trades()`
- `create_order(intent) -> Order`, `cancel_order(id)`, `fetch_order(id)`
- `market_info(symbol)` → tick size, lot size, min notional, maker/taker komisyonu
- Rate-limit yönetimi, otomatik yeniden bağlanma, saat senkronizasyonu (server time farkı)

İlk desteklenecek borsalar: **Binance (spot + USDⓈ-M futures testnet)**, **BTCTurk** (TRY pariteleri için), ardından OKX ve Bybit.
`PaperExchange`: canlı orderbook'a göre dolum simüle eder (piyasa emirlerinde derinliği tüketerek gerçekçi kayma).

### 5.2 Market Data
- Websocket ile ticker, orderbook (L2, ilk 20 kademe) ve trade akışı
- Trade'lerden mum oluşturma (1m, 5m, 15m, 1h, 4h, 1d)
- Başlangıçta REST ile geçmiş mumları doldurma (warm-up)
- Kopma/boşluk tespiti ve doldurma

### 5.3 Analiz Motoru (grafik okuma)
Botun kalbi. Her seçili coin için kısa (scalp/gün içi), orta (swing) ve uzun vade analizini birlikte yapar.

**a) Çoklu zaman dilimi (MTF)**
- Uzun vade: 1d / 1w → ana trend ve büyük bölgeler
- Orta vade: 4h / 1h → setup ve yapı
- Kısa vade: 15m / 5m → giriş zamanlaması
- Kural: alt zaman dilimi sinyali, üst zaman dilimi yönüne ters ise puanı düşer veya reddedilir.

**b) Piyasa rejimi tespiti:** trend (yukarı/aşağı), yatay (range), yüksek volatilite/kaos. ADX, ATR yüzdeliği, Bollinger genişliği, EMA eğimleriyle. Her strateji yalnızca uygun rejimde çalışır (ör. grid yalnızca range'de, breakout yalnızca sıkışma sonrası).

**c) Piyasa yapısı:** swing high/low tespiti (fraktal / ZigZag), HH-HL / LH-LL sınıflandırması, yapı kırılımı (BOS) ve karakter değişimi (CHoCH).

**d) Bölgeler:** 
- Destek/direnç bölgeleri (swing noktalarının kümelenmesi, dokunma sayısı ve hacimle güç puanı)
- Arz/talep bölgeleri, likidite havuzları (eşit tepeler/dipler)
- Fibonacci geri çekilme/uzatma (son belirgin swing'e göre)
- Hacim profili: POC, VAH, VAL; anchored VWAP

**e) Göstergeler & teyitler:** EMA 9/21/50/200, RSI, MACD, Stochastic RSI, Bollinger, ATR, OBV, hacim ortalaması; **RSI/MACD uyumsuzlukları (divergence)**.

**f) Formasyonlar:**
- Mum: yutan, pin bar, inside bar, doji, sabah/akşam yıldızı (yalnızca önemli bölgede anlamlı sayılır)
- Grafik: üçgen, bayrak/flama, çift tepe/dip, OBO/TOBO, kanal kırılımı (geometrik tespit + tolerans parametreleri)

**g) Piyasa ortamı verisi (opsiyonel modüller):** funding rate, open interest değişimi, orderbook dengesizliği, BTC dominansı / BTC trendi (altcoinler için filtre).

**h) Uyum (confluence) puanı:** Her potansiyel işlem 0–100 arası puanlanır. Ağırlıklar config'de. Örnek bileşenler: üst TF trend uyumu, bölgeye yakınlık, yapı teyidi, formasyon, divergence, hacim teyidi, rejim uygunluğu. Eşik altı (varsayılan 70) işlem açılmaz.

**i) İşlem planı (TradePlan):** Her sinyal şu yapıda çıkar:
```
symbol, yön, vade (scalp/swing/position), giriş bölgesi (min–max),
stop-loss (yapısal seviye + ATR tamponu), TP1/TP2/TP3,
risk/ödül oranı (min 1:2, config), confluence puanı, gerekçe listesi, geçersizlik koşulu
```
Çıkış yönetimi: TP1'de kısmi kâr + stop'u başabaşa çekme, sonrası trailing (ATR veya yapı bazlı), zaman bazlı çıkış (setup X mum içinde çalışmazsa kapat).

**j) Analiz raporu:** `bot analyze BTC/USDT` komutu ve panelde her coin için okunabilir Türkçe rapor + işaretli grafik (bölgeler, yapı, giriş/stop/hedef çizgileri; plotly HTML).

**k) (Opsiyonel) LLM yorumu:** Claude API ile teknik verinin Türkçe özetlenmesi. Sadece açıklama üretir; **karar vermez**, emir oluşturamaz.

**Başarı hedefi hakkında gerçekçi not:** Hiçbir bot yüksek kazanma oranını garanti edemez; kazanma oranı tek başına yanıltıcıdır (yüksek win rate + kötü R:R zarar ettirir). Bu yüzden başarı ölçütü **beklenti (expectancy)**, profit factor ve drawdown'dur ve yalnızca **örneklem dışı (out-of-sample) / walk-forward** sonuçlar kabul edilir. Hedefler §8'de.

### 5.4 Stratejiler
`BaseStrategy` arayüzü: `on_candle`, `on_ticker`, `on_fill`, `generate_signals() -> list[Signal]`. Her strateji parametrelerini config'den alır.

Stratejiler Analiz Motorunun ürettiği verileri (rejim, yapı, bölgeler, puan) kullanır ve `TradePlan` döndürür.

Başlangıç stratejileri:
1. **EMA Crossover + trend filtresi** (EMA 9/21, üst zaman diliminde EMA 200 filtresi)
2. **RSI ortalamaya dönüş** (RSI < 30 al, > 70 sat, Bollinger teyidi)
3. **Breakout** (Donchian kanalı + hacim artışı teyidi, ATR tabanlı stop)
4. **Grid** (belirli fiyat aralığında kademeli al/sat; yatay piyasa için)
5. **DCA** (zamanlanmış veya düşüşte kademeli alım)

Sinyal birleştirici: birden fazla stratejinin sinyallerini ağırlıklı oylamayla birleştirebilir (config ile açılır).

Coin seçimi: config'deki sabit liste VEYA tarayıcı modu (24s hacim, volatilite, spread filtreleriyle en uygun N parite).

### 5.5 Arbitraj
1. **Borsalar arası (cross-exchange):** Aynı parite iki borsada; `net_spread = (bid_B - ask_A)/ask_A - komisyon_A - komisyon_B - tahmini_kayma`. Net spread eşiği aşınca iki bacak eşzamanlı gönderilir. Transfer yapılmaz; her iki borsada önceden bakiye tutulur ve periyodik rebalans uyarısı verilir.
2. **Üçgen (triangular):** Tek borsada üç parite döngüsü (ör. USDT→BTC→ETH→USDT). Orderbook derinliğine göre gerçekleştirilebilir miktar hesaplanır.
3. **Funding rate (ileri aşama):** Spot long + perpetual short ile fonlama oranı toplama.
4. **TRY köprüsü (opsiyonel):** BTCTurk TRY fiyatları ile Binance USDT fiyatları arasındaki fark, anlık USD/TRY kuru üzerinden hesaplanır.

Arbitraj riskleri koda gömülü olmalı: bacaklardan biri dolmazsa (leg risk) diğer bacağı hedge etme/kapatma mantığı, gecikme ölçümü, minimum derinlik şartı.

### 5.6 Risk Yönetimi
Her `OrderIntent` şu kontrollerden geçer:
- İşlem başı risk: hesap değerinin `%X`'i (varsayılan %1), stop mesafesine göre pozisyon boyutu
- Parite başı ve toplam maksimum maruziyet
- Maksimum eşzamanlı açık pozisyon
- Günlük zarar limiti (varsayılan %3) → aşılırsa gün sonuna kadar yeni işlem yok
- Maksimum drawdown (varsayılan %10) → kill switch
- Art arda N API hatası → duraklat
- Kaldıraç üst sınırı (futures; varsayılan 1x, en fazla 3x)
- Her pozisyonda zorunlu stop-loss; **stop borsaya da emir olarak konur** (bot kapalıyken koruma için); isteğe bağlı take-profit ve trailing stop
- Spread / likidite kontrolü (spread çok genişse emir yok)


**Risk analizi (işlem öncesi):** Her TradePlan için risk raporu üretilir ve kaydedilir:
- Stop'a kadar olası zarar (tutar ve hesap %'si), beklenen kayma ve komisyon
- Volatilite (ATR %, son 30 günlük gerçekleşen volatilite), likidite/derinlik puanı
- Mevcut portföyle korelasyon (ör. BTC'ye 0.8+ korele 3. altcoin pozisyonu → boyut küçültülür veya red)
- Haber/olay penceresi (opsiyonel takvim: FOMC, CPI, büyük token unlock) → boyut küçültme
- Sonuç: `risk_score` (düşük/orta/yüksek) + onay/ret/boyut düzeltmesi, gerekçesiyle

**Portföy risk analizi (sürekli):** toplam maruziyet, korelasyon matrisi, tarihsel VaR/CVaR (%95), stablecoin oranı, açık pozisyonların toplam stop riski ("risk-at-stop"). Panelde ve günlük Telegram özetinde gösterilir.

**Bakiye büyütme odaklı pozisyon yönetimi:** Amaç tek işlemde büyük kazanç değil, bakiyenin istikrarlı bileşik büyümesidir.
- Bileşik büyüme: pozisyon boyutu sabit tutar değil, **güncel bakiyenin yüzdesi** üzerinden hesaplanır; bakiye büyüdükçe işlemler de büyür.
- Volatiliteye göre boyut: ATR yüksekse aynı risk % için daha küçük pozisyon.
- Kalite çarpanı: confluence puanı yüksek setup'larda risk %1 → en fazla %1.5; düşük puanlıda %0.5 (config).
- Kesirli Kelly (opsiyonel): yeterli işlem geçmişi (≥100) oluşunca, strateji başına gerçekleşen win rate ve ortalama R'den Kelly hesaplanır; **en fazla ¼ Kelly** ve risk_per_trade tavanıyla sınırlanır.
- Equity eğrisi koruması: drawdown %5'i geçince risk yarıya iner; yeni zirve yapılınca kademeli olarak normale döner. Art arda 3 kayıpta bir sonraki işlem riski düşer.
- Kâr kilitleme: bakiye belirlenen eşikleri aştıkça (ör. her +%20'de) kârın bir kısmı aynı hesap içinde "rezerv" olarak işaretlenir ve işlem sermayesine dahil edilmez (para çekme yok, sadece iç muhasebe).
- Strateji performans takibi: her strateji/coin çiftinin gerçekleşen beklentisi izlenir; son N işlemde beklentisi negatife dönen kombinasyon otomatik duraklatılır, iyi gideni sermaye payı artar (tavanlı).

Not: Büyüme odaklı olmak daha fazla risk almak demek değildir; uzun vadede bakiyeyi en hızlı büyüten şey, büyük kayıplardan kaçınarak pozitif beklentili işlemleri bileşik şekilde sürdürmektir. Kaldıraç ve risk tavanları bu modüller tarafından asla aşılamaz.

### 5.7 Emir Yürütme
- Limit, market, stop-limit, OCO desteği
- Kısmi dolum takibi, zaman aşımında iptal veya fiyat güncelleme
- Idempotent `client_order_id`
- Yeniden başlatmada borsadaki açık emir/pozisyonlarla iç durumu eşitleme (reconciliation)

### 5.8 Depolama
Tablolar: `orders`, `trades`, `positions`, `signals`, `risk_reports`, `balances_snapshot`, `arbitrage_opportunities`, `risk_events`, `equity_curve`. Ham mum verisi ayrı tabloda veya Parquet dosyalarında.

### 5.9 Backtest
- Olay güdümlü motor (canlı kodla aynı strateji sınıfları kullanılır)
- Komisyon, kayma, gecikme modeli
- Çıktılar: toplam getiri, CAGR, Sharpe, Sortino, maksimum drawdown, kazanma oranı, profit factor, işlem listesi, equity eğrisi (HTML rapor)
- Walk-forward ve parametre taraması (overfitting uyarısıyla)

### 5.10 Bildirim & Panel
- Telegram: işlem açıldı/kapandı, günlük özet, risk uyarıları; komutlar `/durum`, `/pozisyonlar`, `/durdur`, `/devam`
- FastAPI panel: bakiye, açık pozisyonlar, son işlemler, equity grafiği, strateji aç/kapa, **büyük kırmızı kill switch butonu**. Panel parola/token korumalı.

### 5.11 Sabit çıkış IP (egress)
Binance vb. borsalar API anahtarını IP whitelist ile kısıtlar. Bot nerede çalışırsa çalışsın borsaya hep aynı IP'den bağlanmalı. (IP adresi "taklit" edilemez; bunun yerine trafik sabit IP'li bir çıkış noktasından geçirilir.)

Desteklenen yöntemler (config `egress.mode`):
1. `direct_vps` — Bot zaten sabit IP'li bir VPS'te çalışıyor (en basit, önerilen). Ek proxy yok, sadece IP doğrulama.
2. `wireguard` — Sabit IP'li küçük bir VPS'te WireGuard sunucusu; bot konteyneri tüm trafiği bu tünelden çıkarır (docker-compose'da `wireguard` servisi + `network_mode: service:wireguard`).
3. `proxy` — Sabit IP'li VPS'te kendi kurduğun SOCKS5/HTTP proxy (ör. 3proxy/Dante, kullanıcı adı+parola veya kaynak IP kısıtlı). ccxt'ye `httpsProxy` / `socksProxy` / `wsSocksProxy` olarak verilir; WebSocket akışları da aynı proxy'den geçer.
4. `managed_proxy` — Statik IP sunan ücretli proxy servisi (URL `.env`'de).

Zorunlu davranış:
- Başlangıçta proxy üzerinden dış IP sorgulanır (en az 2 bağımsız servis, ör. api.ipify.org + ifconfig.me); `EGRESS_EXPECTED_IP` ile eşleşmezse testnet/live modda bot emir göndermez.
- Her N dakikada bir tekrar kontrol; IP değişirse → `RiskAlert`, yeni emirler durur, Telegram uyarısı.
- Proxy erişilemezse doğrudan bağlantıya **geri dönülmez** (fail-closed).
- Borsa `-2015 / invalid IP` türü hataları ayrı sınıflandırılıp kill switch sayacına eklenir.
- `deploy/` klasörüne VPS kurulum betikleri: WireGuard sunucu/istemci şablonu, Dante/3proxy örneği, firewall (ufw) kuralları.
- Not: Çıkış IP'sinin ülkesi borsanın hizmet verdiği bir yer olmalı; bu yapı coğrafi kısıtlamaları aşmak için değil, IP whitelist için kullanılır.

#### 5.11.1 Lokal bilgisayarda çalıştırma (birincil senaryo)
Ev internetinin IP'si genellikle dinamiktir (modem yeniden başlayınca değişir). Çözüm:

```
[Ev bilgisayarı: bot + Docker] ──WireGuard tüneli (şifreli)──▶ [VPS: sabit IP] ──▶ Binance / BTCTurk
                                                          Binance yalnızca VPS IP'sini görür
```
- Ucuz bir VPS (1 vCPU / 1 GB yeterli) yalnızca çıkış kapısı olarak kullanılır; bot orada çalışmaz.
- docker-compose'da `wireguard` istemci konteyneri; bot konteyneri `network_mode: service:wireguard` ile tüm trafiğini tünelden çıkarır. Bilgisayarın diğer internet trafiği etkilenmez.
- Kill-switch kuralı: tünel düşerse bot konteynerinin internete doğrudan çıkışı yoktur (iptables ile engellenir).
- Alternatif: İnternet sağlayıcısından sabit IP hizmeti almak (o zaman `egress.mode: direct_vps` gibi davranır, yalnızca IP doğrulaması yapılır).
- Windows'ta Docker Desktop (WSL2) ile, Linux/Mac'te doğrudan çalışır. `deploy/LOKAL_KURULUM.md` adım adım rehber içerir.

Lokal çalıştırmanın riskleri ve önlemleri:
- Elektrik/internet kesintisi, bilgisayarın uykuya geçmesi, Windows güncellemesiyle yeniden başlama → **her pozisyonun stop-loss'u borsada da emir olarak bekler** (bot kapalıyken de koruma sürer); güç ayarlarında uyku kapatılır; Docker `restart: always`; açılışta otomatik reconciliation.
- Bot susarsa haber almak için heartbeat (harici izleme servisi → Telegram).
- Gecikme: swing/gün içi işlemler için ev bağlantısı yeterlidir. Arbitraj gecikmeye çok duyarlıdır; arbitraj modülü ciddi kullanılacaksa botun borsa sunucularına yakın bir VPS'te çalışması önerilir.

#### 5.11.2 VPS seçimi
| Aşama | Önerilen | Not |
|---|---|---|
| Geliştirme / paper / testnet | **Oracle Cloud Always Free** (VM.Standard.E2.1.Micro, varsayılan bölge: Frankfurt) | Ücretsiz, aylık 10 TB çıkış trafiği, 50 Mbps |
| Live (gerçek para) | Ucuz ücretli VPS (Hetzner, Contabo vb., Avrupa) | Geri alınma riski yok, IP kalıcı |

Oracle Free için zorunlu önlemler:
- **Reserved public IP** kullan (ephemeral değil); sunucu yeniden oluşturulsa da IP hesapta kalır, Binance whitelist'i bu IP'ye yapılır.
- **Boşta sunucu geri alma (idle reclamation):** 7 gün boyunca CPU (95. yüzdelik) < %20, ağ < %20 (A1'de bellek < %20) olursa Oracle sunucuyu geri alabilir. Sadece tünel çalıştıran sunucu bu duruma düşer. Önlem: hesabı PAYG'ye yükseltmek (Always Free kaynaklar ücretsiz kalır) ve/veya sunucuda periyodik iş çalıştırmak (geçmiş veri indirme, backtest). Bot, IP uyuşmazlığında zaten durur ve Telegram'a uyarı gönderir.
- Kayıt kredi kartı doğrulaması ister.
- **Bölge = borsanın gördüğü ülke:** Binance global bazı ülkelerden (ör. ABD) gelen API isteklerini "restricted location" (HTTP 451/403) ile reddeder. VPS bölgesi Binance global'in hizmet verdiği bir ülkede olmalı; bu yüzden ABD bölgeli ücretsiz seçenekler (ör. Google Cloud e2-micro) uygun değildir. Kurulumdan sonra VPS üzerinden Binance'e test isteği atılarak doğrulanır.
- Bot, açılış kontrolünde IP doğrulamasına ek olarak borsaya erişim testi yapar; 451/403 "restricted location" yanıtı ayrı hata türü olarak sınıflandırılır, emir gönderilmez ve Telegram'a açıklayıcı uyarı gider.
- `deploy/ORACLE_KURULUM.md`: hesap açma, bölge seçimi, reserved IP, Ubuntu imajı, WireGuard sunucu kurulumu, OCI Security List + ufw kuralları (yalnızca WireGuard UDP portu açık; SSH anahtar ile ve tercihen yalnızca tünel üzerinden), otomatik güvenlik güncellemeleri.

### 5.12 Güvenlik
- API anahtarı izinleri: yalnızca okuma + işlem. **Para çekme izni kapalı**, mümkünse IP whitelist.
- Sırlar `.env` veya Docker secrets ile
- `pip-audit` ve `gitleaks` CI'da çalışır

### 5.13 Operasyonel güvenlik ağları (v1'e dahil)
- **Kademeli sermaye (canary):** Live'ın ilk 2 haftası config'deki `live_capital_cap` ile sınırlı (ör. toplam bakiyenin %10'u); performans paper ile tutarlıysa kademeli artış. Artış manuel onayla.
- **Gölge mod (shadow):** Strateji/parametre değişiklikleri önce canlı versiyonun yanında paper olarak çalışır; sonuçlar karşılaştırılmadan live'a alınmaz.
- **Borsa duyuru takibi:** Delisting, bakım, cüzdan askıya alma duyuruları izlenir; ilgili paritede yeni işlem açılmaz, açık pozisyon config'e göre kapatılır.
- **Stablecoin sağlık kontrolü:** USDT/USDC'nin 1$'dan sapması (depeg) izlenir; eşik aşılınca uyarı ve yeni işlemler durur.
- **Borsa başına bakiye tavanı:** Tek borsada tutulacak maksimum tutar config'de; aşılınca rebalans uyarısı (borsa riski dağıtımı).
- **Backtest önyargı kontrolleri:** lookahead (geleceği görme) testi, delist olmuş coinleri de içeren veri (survivorship bias), gerçekçi komisyon ve kayma; rapor bu kontrollerin geçtiğini belirtir.
- **Hafta sonu / düşük likidite modu:** Likidite düştüğünde (hafta sonu, tatiller) risk azaltılır veya işlem kapatılır (config).
- **Config değişiklik günlüğü:** Her ayar değişikliği zaman damgası ve önceki değerle kaydedilir; panelden yapılan değişiklikler de dahil.
- **Kaos testleri:** WS kopması, proxy düşmesi, kısmi dolum, borsa 5xx hataları, saat kayması senaryoları test paketinde simüle edilir.
- **Borsa hesabı:** Kullanıcının hesabı **Binance global**'dedir (api.binance.com, testnet: testnet.binance.vision / testnet.binancefuture.com). Binance TR adapter'ı kapsam dışı.

## 6. Konfigürasyon
Bkz. `config/config.example.yaml`. Tüm eşikler, stratejiler, pariteler ve risk limitleri buradan yönetilir; kod içinde sabit sayı olmaz.

## 7. CI/CD
GitHub Actions: `ruff`, `mypy`, `pytest` (unit), `gitleaks`, `pip-audit`. Docker imajı `main` dalına merge'de oluşturulur.

## 8. Kabul kriterleri (v1.0)
- [ ] Binance ve BTCTurk adapter'ları paper ve testnet modunda çalışıyor
- [ ] En az 3 strateji backtest'ten geçmiş ve rapor üretiyor
- [ ] Cross-exchange ve triangular arbitraj tarayıcıları fırsatları logluyor, paper modda işlem yapıyor
- [ ] Risk yöneticisi tüm limitleri uyguluyor; kill switch testleri yeşil
- [ ] Yeniden başlatmada durum eşitlemesi doğru çalışıyor
- [ ] Telegram bildirimleri ve panel çalışıyor
- [ ] Analiz motoru her coin için MTF rapor ve işaretli grafik üretiyor; her sinyal tam bir TradePlan içeriyor
- [ ] Sabit IP doğrulaması çalışıyor; IP uyuşmazlığında emir gönderilmediği testle kanıtlı
- [ ] Bot en az 2 hafta kesintisiz paper modda çalıştı ve sonuçlar raporlandı

## 9. Performans hedefleri (live'a geçiş şartı)
Bu eşikler **örneklem dışı / walk-forward backtest** ve **en az 2 hafta paper** sonuçlarında birlikte sağlanmadan `live` önerilmez:
- Profit factor ≥ 1.3, pozitif beklenti (komisyon + kayma dahil)
- Maksimum drawdown ≤ %15
- En az 100 işlemlik örneklem
- Kazanma oranı raporlanır ama tek başına kriter değildir (R:R ile birlikte değerlendirilir)
- Paper sonuçları backtest'ten belirgin kötüyse → overfitting kabul edilir, parametreler yeniden ele alınır
