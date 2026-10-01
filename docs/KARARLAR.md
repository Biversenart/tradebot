# Kararlar Günlüğü

Belirsiz finansal/teknik kararlar ve varsayımlar burada kayıt altına alınır.

| Tarih | Konu | Karar | Gerekçe |
|---|---|---|---|
| 2026-10-02 | Varsayılan mod | `paper` | Gerçek para riski yalnızca çift onayla |
| 2026-10-02 | Sabit IP | Trafik sabit IP'li VPS üzerinden, fail-closed | Binance IP whitelist; sızıntı olursa anahtar reddedilir |
| 2026-10-02 | Başarı ölçütü | Expectancy + profit factor + drawdown, out-of-sample | Win rate tek başına yanıltıcı |
| 2026-10-02 | İşlem başı risk | Hesabın %1'i | Yaygın muhafazakâr başlangıç |
| 2026-10-02 | VPS | Geliştirme/paper: Oracle Always Free (reserved IP); live: ücretli VPS | Ücretsiz seçenekte idle reclamation riski |
| 2026-10-02 | Çalışma yeri | Bot kullanıcının lokal bilgisayarında, trafik WireGuard ile VPS'ten çıkar | Kullanıcı tercihi |
| 2026-10-02 | Borsa hesabı | Binance global (Binance TR kapsam dışı) | Kullanıcının hesabı global'de |
| 2026-10-02 | VPS bölgesi | Frankfurt; ABD bölgeleri yasak | Binance global ABD IP'lerini reddeder |
| 2026-10-02 | Mod çözümleme | `.env` TRADING_MODE ve config `mode` ikisi de verilmişse aynı olmalı; aksi halde bot başlamaz. `live` için ikisi de `live` + `live_trading_confirmed: true` | Belirsiz mod, yanlışlıkla gerçek parayla işleme yol açabilir |
| 2026-10-02 | Testnet/live tutarlılığı | `testnet` modunda `testnet: false` borsa, `live` modunda `testnet: true` borsa reddedilir | Testnet sanılan modda gerçek emir gönderilmesini engeller |
| 2026-10-02 | EGRESS_EXPECTED_IP | Yalnızca genel (global) IP kabul edilir; `0.0.0.0`, özel ağ, loopback reddedilir | `.env.example`'daki yer tutucu ile testnet/live açılmasın |
| 2026-10-02 | TradePlan R:R | `risk_reward` = giriş bölgesi ortasından **son** TP'ye R katı; her TP için ayrıca `reward_risk_ratios` | `take_profits: [1,2,3]` R katlarıyla `min_risk_reward: 2.0` filtresi ancak son hedefe göre anlamlı |
| 2026-10-02 | Config sınırları | Kaldıraç ≤ 3x, Kelly ≤ ¼ ve ≥ 100 işlem, kalite çarpanı ≤ 1.5, `fail_closed` yalnızca `true` | Spec §5.6/§5.11 tavanları şema seviyesinde zorlanır |
| 2026-10-02 | Paket adı | Kaynak `src/bot/` (spec §4), dağıtım adı `tradebot`, CLI komutu `bot` | Spec ile uyum |
| 2026-10-02 | Varsayılan egress | `egress.mode: wireguard` | Birincil senaryo: bot evde, trafik WireGuard ile VPS'ten |
| 2026-10-02 | IP doğrulama kuralı | Tüm servisler (≥2) beklenen IP'yi döndürmeli; biri ulaşılamazsa/ farklıysa emir yok; kontrol 2×aralıktan eskiyse bayat → emir yok | Fail-closed; tek servis yanıltabilir |
| 2026-10-02 | Heartbeat | Yalnızca sağlıklıyken ping (push modeli); URL `.env`'de | Uyarı mantığı harici serviste; bot ölürse de çalışır |
| 2026-10-02 | Kill-switch | iptables OUTPUT DROP, wg-quick'ten bağımsız; yalnızca wg0, compose alt ağı ve VPS UDP portu | Tünel düşerse doğrudan çıkış imkânsız |
| 2026-10-02 | Postgres erişimi | Kill-switch ağında sabit IP (172.28.0.10) | DNS tünelden gider; compose servis adı çözülmez |
| 2026-10-02 | Dante | Yalnızca tünel adresinde dinler (internete kapalı) | SOCKS parolası düz metin; tünel içinde şifreli kalır |
| 2026-10-02 | Binance futures testnet | Spot testnet: `testnet.binance.vision` (sandbox); futures "testnet": Binance **demo trading** (`demo-fapi.binance.com`), anahtarlar `BINANCE_TESTNET_*` | ccxt eski futures sandbox'ını kaldırdı |
| 2026-10-02 | BTCTurk | Test ağı yok → testnet modunda anahtar bağlanmaz, emir reddedilir; ccxt.pro WS yok → REST yoklama (2 sn) | Gerçek parayla yanlışlıkla işlem olmasın |
| 2026-10-02 | Emir tipi ayrımı | Strateji `OrderIntent`, adapter yalnızca `OrderRequest` (risk onaylı) kabul eder | Kural 4'ün tip seviyesinde ayrımı |
| 2026-10-02 | Stop emirleri | `STOP_MARKET/STOP_LIMIT` → ccxt `stopLossPrice` (spot STOP_LOSS(_LIMIT), futures STOP_MARKET) | Kural 9: borsa tarafı stop |
| 2026-10-02 | PaperExchange | Spot; market emri derinliği tüketir (IOC), limit kalan kısım maker, stop bid/ask ile tetiklenir; komisyon quote cinsinden; rezervasyon free→used | Gerçekçi kayma + basit muhasebe |
| 2026-10-02 | Geçmiş veri | Parquet `data/ohlcv/<borsa>/<BASE-QUOTE>/<tf>.parquet`, float64 kolonlar; Candle'a `Decimal(str(x))` | ccxt OHLCV'yi float verir; para hesapları Decimal kalır |
| 2026-10-02 | Paper veri kaynağı | Paper modda gerçek (live) public uç noktalar, anahtarsız | Testnet fiyatları gerçek piyasayı yansıtmaz |
| 2026-10-02 | Gösterge hesapları | Kendi implementasyonumuz (pandas-ta değil); EMA/RMA SMA ile tohumlanır (TradingView/TA-Lib uyumlu); göstergeler float, para hesapları Decimal | pandas-ta bakımı zayıf; RSI StockCharts referansıyla birebir doğrulandı |
| 2026-10-02 | Volatil rejim | ATR% ≥ 2 × son 500 mumun medyanı | Kısa pencereli yüzdelik, uzun süren kaosu "normal" sayıyordu |
