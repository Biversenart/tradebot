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
| 2026-10-02 | Swing tespiti | Fraktal (N=3); swing yalnızca i+N'de onaylanır, BOS/CHoCH buna göre (look-ahead yok) | Canlı ve backtest aynı davranır |
| 2026-10-02 | Bölge gücü | 0.4·dokunma + 0.3·yakınlık(recency) + 0.3·hacim (normalize, 0–100) | Basit, açıklanabilir puan |
| 2026-10-02 | Hacim profili | Mum hacmi high–low aralığına eşit dağıtılır; value area POC'tan komşu büyük bin eklenerek %70 | Standart TPO/VP yaklaşımı |
| 2026-10-02 | TF eğilimi (bias) | close>EMA200, EMA50>EMA200, yapı trendi, rejim → toplam ≥2 yükseliş, ≤−2 düşüş | Tek göstergeye bağımlı olmayan basit oylama |
| 2026-10-02 | Setup zaman dilimi | İlk mevcut orta vade TF (4h → 1h), yoksa kısa, yoksa uzun | Spec: orta vade = setup ve yapı |
| 2026-10-02 | Üst TF çelişkisi | Puan × `htf_conflict_penalty` (0.5); eşik (70) altında kalırsa plan yok | Spec §5.3.a "puanı düşer veya reddedilir" |
| 2026-10-02 | Stop seviyesi | Girişin altındaki EN YAKIN swing low (short: üstündeki en yakın swing high) − 0.5 ATR; > 4 ATR ise ret | Yapısal ama makul mesafeli stop |
| 2026-10-02 | Önündeki bölge | Güç ≥50 karşı bölge, minimum 2R hedefinden önce ise plan reddedilir | "Yetersiz alan" filtresi |
| 2026-10-02 | Analiz verisi | Önce Parquet; yoksa alt TF'den yeniden örnekleme (UTC, hafta Pazartesi); yoksa borsadan warm-up | Tek 1h dosyasıyla 4h/1d/1w analizi mümkün |
| 2026-10-02 | Grafik | plotly HTML, plotly.js CDN'den (dosya ~60 KB) | Gömülü sürüm ~4.5 MB olurdu |
| 2026-10-02 | Strateji yapısı | Her strateji nedensel `compute(df)`; canlı = son satır, backtest = tüm satırlar; `assert_causal` testi | Canlı ve backtest aynı sınıf/kod (spec §5.9), lookahead imkânsız |
| 2026-10-02 | Backtest dolumu | Sinyal t kapanışında, dolum t+1 açılışında; kayma 5 bps, komisyon %0.1/taraf | Gerçekçi gecikme; gap'te stop'un ötesindeyse işlem atlanır |
| 2026-10-02 | Aynı mumda stop + TP | Önce stop varsayılır | Muhafazakâr (iyimser sonuçları önler) |
| 2026-10-02 | Kısmi çıkış | TP1: başlangıç miktarının %40'ı + stop başabaşa; ara TP: kalanın yarısı; son TP: tamamı; TP1 sonrası ATR (2×) trailing | Spec §5.3.i |
| 2026-10-02 | Zaman bazlı çıkış | TP1'e ulaşmadan 48 mum geçerse kapanışta çık | Çalışmayan setup'ı serbest bırak |
| 2026-10-02 | Backtest boyutlama (Aşama 4) | Sabit oranlı: güncel bakiyenin %1'i risk, notional ≤ %100 | Aşama 5'te büyüme odaklı boyutlama ile karşılaştırılacak |
| 2026-10-02 | Walk-forward | 6 ay eğitim / 2 ay test, kayan; objektif R-beklentisi; min 10 işlem | §9 yalnızca örneklem dışı |
| 2026-10-02 | Overfitting uyarıları | OOS PF < 0.7×IS PF; IS beklenti>0 ama OOS≤0; parametre kararsızlığı (>%70 fold farklı); tepe seçimi (en iyi > 2×medyan); OOS işlem < min | Spec §5.9 |
| 2026-10-02 | Grid/DCA | Yalnızca long; grid sadece range rejiminde; DCA güçlü düşüşte durur; her alımda koruyucu stop | Spot uyumu ve kural 9 |
| 2026-10-02 | Sentetik veri | `bot data synthetic` → borsa adı `synthetic` | Ağsız ortamda pipeline doğrulaması; gerçek veriyle karışmaz |
| 2026-10-02 | Büyüme odaklı boyutlama | Aşağıdaki "Boyutlama formülleri" bölümü; sabit boyutla karşılaştırma `docs/BOYUTLAMA.md` | Spec §5.6, Prompt 5 |
| 2026-10-02 | Duraklatılan strateji×coin | 168 saat bekleme sonrası pencere sıfırlanıp yeniden denenir | İşlem yoksa yeni kanıt da yok; kalıcı duraklatma iyi stratejiyi de öldürüyordu |
| 2026-10-02 | Kill switch kalıcılığı | Durum `KillSwitchState.to_dict()` ile saklanır, yeniden başlatmada geri yüklenir | Yeniden başlatma kill switch'i kaldırmamalı |
| 2026-10-02 | Emir onayı | `ApprovedIntent` yalnızca RiskManager'ın özel jetonuyla oluşturulabilir; yürütme yalnızca onu kabul eder | Kural 4'ün tip seviyesinde garantisi |

## Boyutlama formülleri (Aşama 5)

```
risk_% = taban × kalite × drawdown × kayıp_serisi × olay × dağılım       (≤ taban × 1.5 tavanı, ≤ ¼ Kelly)
miktar = işlem_sermayesi × risk_% / 100 / |giriş − stop|                   (notional ≤ işlem_sermayesi × %100)
işlem_sermayesi = (bileşik ? güncel bakiye : başlangıç bakiyesi) − kilitli_kâr_rezervi
```
- **taban** = `risk.risk_per_trade_pct` (%1).
- **kalite**: confluence ≥ 85 → ×1.5; < 75 → ×0.5; arası ×1.
- **drawdown**: dd = (zirve − bakiye)/zirve. dd ≥ %5 → ×0.5; altında doğrusal toparlanma: `0.5 + 0.5 × (1 − dd/5)` (zirvede 1).
- **kayıp serisi**: art arda ≥3 kayıp → ×0.5 (bir kazanç sıfırlar).
- **olay**: config'deki olay penceresinde (FOMC, CPI, unlock) → pencerenin `risk_factor`'ü.
- **dağılım** (strateji×coin, son 30 işlem, en az 10): beklenti < 0R → 0 (168 saat duraklatma); pozitifse `1 + min(1, beklenti/0.5R) × 0.25`.
- **¼ Kelly** (isteğe bağlı, ≥100 işlem): `f* = W − (1−W)/(ortKazançR/ortKayıpR)`; risk_% ≤ `0.25 × f* × 100`; f* ≤ 0 → duraklat.
- **Kâr kilitleme**: bakiye son kilit seviyesinin %20 üstüne her çıktığında kazancın %25'i rezerve edilir (iç muhasebe; para çekme yok).
- **Volatilite**: stop ATR/yapı tabanlı olduğundan geniş stop = küçük pozisyon (aynı risk %).
- Sabit karşılaştırma tabanı: her işlemde güncel bakiyenin %1'i risk.
