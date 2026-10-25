# Operasyonel güvenlik ağları (spec §5.13)

Bu belge Aşama 7.5'te eklenen güvenlik ağlarının nasıl çalıştığını ve günlük kullanımda ne
yapmanız gerektiğini anlatır. Tüm ayarlar `config/config.yaml` → `operations:` altındadır.

## 1. Kademeli sermaye (kanarya)
- **Yalnızca `live` modda** uygulanır. Boyutlama bakiyenin `live_capital_cap_pct` kadarını
  (varsayılan %10) "görür": 10 000 USDT bakiyeyle işlem başı risk 1 000 USDT üzerinden hesaplanır.
- Tavan veritabanında saklanır. **Artış yalnızca manuel onayla:**
  - Panel: `POST /api/capital-cap?pct=20` (panelde "Operasyonel güvenlik" kartı durumu gösterir)
  - CLI (bot dururken): `bot capital show`, `bot capital raise 20 --by "adınız"`
- Kurallar: canlıya geçişten veya son artıştan sonra en az `canary_days` (14) gün; tek seferde en
  fazla `max_cap_step_factor` (x2). Azaltma her zaman serbesttir.
- Config'e daha yüksek bir değer yazmak tavanı **artırmaz** (uyarı loglanır); daha düşük değer
  hemen geçerli olur.
- Öneri: artırmadan önce `bot shadow report` / panel ve paper sonuçlarıyla canlı sonuçları
  karşılaştırın (spec: "performans paper ile tutarlıysa").

## 2. Gölge mod
- `testnet`/`live` modda, `shadow_mode: true` iken her stratejinin parametreleri son **onaylı**
  parametrelerle karşılaştırılır.
  - Değişmemiş → canlıda çalışır.
  - Değişmiş → canlıda **onaylı (eski)** parametreler çalışmaya devam eder, yeni parametreler
    gölgede sanal işlem yapar (asla emir göndermez).
  - Yeni eklenen strateji → yalnızca gölgede.
  - Bir stratejiyi kapatmak (`enabled: false`) hemen geçerlidir; kapalı bir stratejiyi açmak ise
    değişiklik sayılır (önce gölgede çalışır, onay gerekir).
- İlk testnet/live başlangıcında mevcut parametreler temel olarak onaylanır.
- Karşılaştırma: `bot shadow report` (veya panel / Telegram `/guvenlik`). Her iki sürüm aynı
  sanal kuralla puanlanır (beklenti R, isabet oranı, işlem sayısı). `shadow_min_trades` (20)
  işlemden önce "yetersiz veri" denir.
- Onay: `bot shadow approve <strateji> --by "adınız"` veya panel `POST /api/shadow/<ad>/approve`.
  Yeni parametreler **bir sonraki yeniden başlatmada** canlıya alınır.

## 3. Borsa duyuru takibi
- Binance duyuruları 15 dakikada bir egress üzerinden okunur (delisting ve bakım katalogları).
- Ayrıştırılamayan veya başka borsalara ait duyurular için `config/announcements.yaml` kullanın
  (şablon: `config/announcements.example.yaml`).
- Etkiler:
  - **Delisting:** o varlığı içeren paritelerde yeni işlem açılmaz; `on_delisting: close` ise açık
    pozisyonlar kapatılır (`keep` ise bırakılır, uyarı gelir).
  - **Bakım:** `effective_at`'ten `maintenance_block_before_minutes` (60) önce başlayıp `until`'e
    kadar o borsada yeni işlem açılmaz.
  - **Cüzdan askısı:** yalnızca uyarı.

## 4. Stablecoin sağlık kontrolü
- `stablecoin_pairs` (USDC/USDT, FDUSD/USDT) fiyatı 1'den `stablecoin_depeg_threshold_pct`
  (%0.5) fazla saparsa CRITICAL uyarı gelir ve **tüm yeni işlemler durur**. Sapma eşiğin yarısına
  inince otomatik kalkar. Çıkış emirleri (stop, TP, kapatma) her zaman serbesttir.

## 5. Borsa başına bakiye tavanı
- Bir borsadaki toplam değer `max_balance_per_exchange_usdt`'yi aşınca 6 saatte bir uyarı gelir.
  Bot transfer yapmaz (CLAUDE.md kural 3); fazlayı manuel olarak taşıyın.

## 6. Hafta sonu / düşük likidite modu
- Cumartesi-Pazar (UTC) ve `holidays` listesindeki günlerde risk çarpanı uygulanır (×0.5).
- `min_depth_quote > 0` ise orderbook ±%0.5 derinliği bu tutarın altında kaldığında da uygulanır.
- `action: halt` → bu dönemlerde yeni işlem açılmaz.

## 7. Saat kayması
- Borsa saatiyle fark `max_clock_skew_ms` (1000 ms) üstündeyse o borsada yeni emir açılmaz.
  Çözüm: işletim sisteminde NTP senkronizasyonunu açın (Windows: "Saati otomatik ayarla";
  Linux: `timedatectl set-ntp true`).

## 8. Config değişiklik günlüğü
- Bot her başlangıçta `config.yaml`'ı (sırlar hariç) bir önceki çalıştırmayla karşılaştırır ve
  her değişen ayarı önceki değeriyle kaydeder.
- Panel/Telegram/CLI işlemleri (kill switch, strateji aç/kapa, tavan, gölge onayı) kaynak ve
  kişi bilgisiyle kaydedilir.
- Görüntüleme: panel "Config değişiklik günlüğü" kartı veya `bot config-log -n 50`.

## 9. Backtest önyargı kontrolleri
Her backtest raporunda "Önyargı kontrolleri" bölümü bulunur:
- Lookahead testi (kısaltılmış geçmişle yeniden hesaplama aynı olmalı) — başarısızsa rapora güvenmeyin.
- Gerçekçi maliyet: komisyon ≥ `backtest.min_commission_pct`, kayma ≥ `min_slippage_bps`, gecikme ≥ 1 mum.
- Survivorship: delist olmuş coin verisi `backtest.delisted_symbols` ile dahil edilmediyse UYARI.
- Veri bütünlüğü: %1'den fazla eksik mum UYARI.

## 10. Kaos testleri
`tests/chaos/` klasöründe: WebSocket kopması (yeniden bağlanma, akış gözetmeni, bayat veri,
WS yokken borsa stop'unun korumaya devam etmesi), proxy düşmesi (fail-closed, doğrudan bağlantı
yok, geri gelince otomatik devam), kısmi dolum (giriş ve çıkışta stop miktarının senkronu),
borsa 5xx hataları (idempotent yeniden deneme, kill switch, stop konamazsa kapatma) ve saat
kayması. Çalıştırma: `pytest tests/chaos`.

> Not: CLI komutları (`bot capital raise`, `bot shadow approve`) veritabanını doğrudan değiştirir;
> bot çalışırken panel/Telegram'ı kullanın — çalışan bot kendi kopyasını periyodik olarak yazar.
