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
