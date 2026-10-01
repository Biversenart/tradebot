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
