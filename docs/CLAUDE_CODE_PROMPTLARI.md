# Claude Code Promptları (sırayla yapıştır)

Repo'yu GitHub'da oluştur, bu dosyaları yükle, Claude Code'u repoya bağla. Her aşama için aşağıdaki promptu ayrı bir oturumda/PR'da ver. Bir PR merge edilmeden sonrakine geçme.

---

### Prompt 0 — İskelet
> CLAUDE.md, docs/PROJE_SPEC.md ve docs/YOL_HARITASI.md dosyalarını oku. Yol haritasındaki **Aşama 0**'ı uygula. Önce kısa bir plan yaz, sonra kodla. Bitince `ruff`, `mypy`, `pytest` çalıştır, yeşil olmadan bitirme. Yol haritasında aşamayı işaretle ve `feat/asama-0` dalında PR aç.

### Prompt 1 — Sabit IP
> Aşama 1'i uygula (spec §5.11). Borsalara giden tüm REST ve WebSocket trafiği egress proxy'den geçmeli; dış IP doğrulaması fail-closed olmalı, proxy düşünce asla doğrudan bağlantıya dönmemeli. `deploy/` altına WireGuard ve Dante SOCKS5 kurulum şablonlarını ve adım adım Türkçe README yaz (VPS: Ubuntu 24.04; bot: kullanıcının ev bilgisayarı, Windows + Docker Desktop veya Linux). Bot konteyneri trafiğini yalnızca WireGuard tünelinden çıkarmalı, tünel düşerse internete erişimi olmamalı. Heartbeat'i de ekle. VPS olarak Oracle Cloud Always Free için `deploy/ORACLE_KURULUM.md` yaz (spec §5.11.2): reserved IP, WireGuard sunucu, OCI Security List + ufw, idle reclamation uyarısı, Frankfurt bölgesi ve VPS'ten Binance erişim testi (451/403 restricted location kontrolü). IP uyuşmazlığı ve proxy kopması için testler yaz.

### Prompt 2 — Borsalar ve veri
> Aşama 2'yi uygula. ExchangeAdapter arayüzü, Binance (spot+futures testnet), BTCTurk ve PaperExchange. Strateji kodu ccxt'yi import etmemeli. Geçmiş veri indirme komutunu ekle. Gerçek ağa bağlanan testleri `integration` işaretiyle ayır.

### Prompt 3 — Analiz motoru
> Aşama 3'ü uygula (spec §5.3, tüm alt maddeler). Her gösterge ve tespit fonksiyonu için bilinen veriyle birim testi yaz (tests/fixtures altına örnek OHLCV koy). `bot analyze BTC/USDT` komutu Türkçe MTF raporu ve bölgeler, yapı, giriş/stop/hedef çizgileri işaretli plotly HTML grafiği üretmeli. Bu aşamayı küçük PR'lara bölebilirsin.

### Prompt 4 — Stratejiler ve backtest
> Aşama 4'ü uygula. Backtest canlı kodla aynı strateji sınıflarını kullanmalı. Walk-forward ve out-of-sample raporu zorunlu. BTC/USDT, ETH/USDT, SOL/USDT için 2022'den bugüne 1h veriyle örnek rapor üret ve sonuçları spec §9 eşikleriyle karşılaştıran bir özet yaz.

### Prompt 5 — Risk ve yürütme
> Aşama 5'i uygula (spec §5.6 risk analizi ve büyüme odaklı boyutlama dahil). Her OrderIntent RiskManager'dan geçmeli. Boyutlama formüllerini docs/KARARLAR.md'ye yaz ve backtest'te sabit boyutla karşılaştır. Kill switch'in tüm tetikleyicileri için test yaz. Reconciliation'ı testnet senaryosuyla doğrula.

### Prompt 6 — Arbitraj
> Aşama 6'yı uygula. Net spread hesabında komisyon, kayma ve derinliği kullan. Leg risk senaryolarını test et. İlk sürümde arbitraj yalnızca fırsatları loglasın ve paper modda işlem yapsın.

### Prompt 7 — Bildirim ve panel
> Aşama 7'yi uygula. Telegram komutları: /durum /pozisyonlar /analiz <coin> /durdur /devam. Panel token korumalı, kill switch butonu ve analiz raporları sayfası olmalı.

### Prompt 7.5 — Operasyonel güvenlik ağları
> Aşama 7.5'i uygula (spec §5.13). Her madde için test yaz; kaos testlerini ayrı bir `tests/chaos` klasöründe topla.

### Prompt 8 — Değerlendirme
> Son 2 haftalık paper/testnet loglarından ve veritabanından spec §9'a göre performans raporu üret. Backtest ile paper sonuçlarını karşılaştır, overfitting belirtilerini işaretle, live'a geçiş için önerini gerekçeleriyle yaz.
