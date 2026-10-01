# Ek Özellik Önerileri (v1.0 sonrası)

Öncelik: ⭐⭐⭐ yüksek fayda / ⭐⭐ orta / ⭐ deneysel

## Güvenilirlik ve operasyon
- ⭐⭐⭐ **Sağlık izleme (heartbeat):** Bot her dakika "yaşıyorum" sinyali gönderir; gelmezse harici izleyici (UptimeRobot / Healthchecks.io) Telegram'a haber verir. Bot çökse de haberin olur.
- ⭐⭐⭐ **Borsa tarafı koruyucu stop:** Her pozisyon açılınca stop-loss emri borsaya da konur. Bot/VPS/proxy çökse bile pozisyon korumasız kalmaz.
- ⭐⭐⭐ **Otomatik yeniden başlatma + durum eşitleme:** Docker `restart: always`, açılışta reconciliation.
- ⭐⭐ **Yedekleme:** Veritabanının günlük şifreli yedeği.
- ⭐⭐ **Prometheus + Grafana:** gecikme, hata oranı, PnL, IP durumu metrikleri.

## Analiz
- ⭐⭐⭐ **Piyasa geneli filtresi:** BTC sert düşüşteyse altcoin long'ları otomatik engelle.
- ⭐⭐ **Duyarlılık (sentiment):** Fear & Greed endeksi, funding rate aşırılıkları, likidasyon verileri.
- ⭐⭐ **On-chain (opsiyonel):** borsaya büyük giriş/çıkışlar, whale hareketleri.
- ⭐⭐ **Haber/olay takvimi:** FOMC, CPI, token unlock, listeleme/delisting duyuruları öncesi risk azaltma.
- ⭐ **ML sinyal filtresi:** Kural tabanlı sinyalleri reddetmek/onaylamak için basit model (gradient boosting). Yalnızca filtre, karar verici değil; sıkı out-of-sample testli.

## İşlem ve yürütme
- ⭐⭐⭐ **Akıllı emir yerleştirme:** Mümkünse maker (limit) emir → komisyon düşer, kayma azalır. Büyük emirleri TWAP ile parçalama.
- ⭐⭐ **Komisyon optimizasyonu:** BNB ile komisyon ödeme, VIP kademe takibi.
- ⭐⭐ **Short desteği:** Futures ile düşüşten de kazanma (kaldıraç 1x, sıkı limitlerle).
- ⭐ **Çoklu hesap/alt hesap:** Strateji başına ayrı Binance alt hesabı → riskler birbirine karışmaz.

## Raporlama
- ⭐⭐⭐ **İşlem günlüğü (journal):** Her işlem için grafik ekran görüntüsü, gerekçe, sonuç, "neden kazandı/kaybetti" etiketi. Stratejiyi geliştirmenin en iyi yolu.
- ⭐⭐⭐ **Vergi/muhasebe dökümü:** Tüm alım-satımların TL karşılığıyla CSV/Excel dökümü (Türkiye'deki kripto vergi düzenlemesine hazırlık).
- ⭐⭐ **Haftalık performans raporu:** PDF/HTML, strateji ve coin bazında.

## Kullanım kolaylığı
- ⭐⭐ **Mobil uyumlu panel** veya Flutter ile küçük kontrol uygulaması.
- ⭐⭐ **Telegram'dan onaylı mod (yarı otomatik):** Bot sinyali gönderir, sen "✅ Aç" dersen işlem açılır. Güven oluşana kadar ideal ara adım.
- ⭐ **Strateji oluşturucu:** Config'den kod yazmadan kural tanımlama (ör. "RSI<30 VE fiyat destek bölgesinde").

## Güvenlik
- ⭐⭐⭐ **Panel 2FA** ve yalnızca VPN/WireGuard üzerinden erişim.
- ⭐⭐ **Anahtar şifreleme:** API anahtarları diskte şifreli (age/sops), açılışta çözülür.
- ⭐⭐ **Anomali freni:** Fiyat birkaç saniyede %X'ten fazla oynarsa (flash crash / hatalı veri) işlemleri duraklat.
