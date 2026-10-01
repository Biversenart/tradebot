# Backtest Özeti — Spec §9 Karşılaştırması (Aşama 4)

> ⚠️ **BU SONUÇLAR SENTETİK VERİYLE ÜRETİLMİŞTİR — gerçek piyasa performansı DEĞİLDİR.**
> Geliştirme ortamının ağ politikası borsa uç noktalarını (api.binance.com, data.binance.vision vb.)
> engellediği için gerçek BTC/ETH/SOL 1h verisi indirilemedi. Pipeline'ın uçtan uca çalıştığını
> göstermek için `bot data synthetic` ile rejim değiştiren (trend/yatay/volatil) sentetik seriler
> üretildi (2022-01-01 → 2026-09-30, sembol başına 41.616 mum). **Bu tablo hiçbir strateji için
> live kararı dayanağı olamaz.**

## Gerçek veriyle çalıştırma (lokalde)
```bash
bot data download -s BTC/USDT -s ETH/USDT -s SOL/USDT --tf 1h --since 2022-01-01
bot backtest report -s BTC/USDT -s ETH/USDT -s SOL/USDT --since 2022-01-01
# -> reports/backtest/<tarih>/OZET.md + her sembol x strateji için HTML rapor
```

## Yöntem
- Walk-forward: 6 ay (4320 mum) eğitim → 2 ay (1440 mum) test, kayan pencere; her fold'da
  parametre ızgarası eğitimde taranır, en iyisi (R-beklentisi) görülmemiş testte çalıştırılır.
- Yalnızca **örneklem dışı** (test pencereleri zincirlenmiş) sonuçlar raporlanır.
- Komisyon %0.1/taraf, kayma 5 bps, gecikme 1 mum; aynı mumda stop+TP → önce stop.
- Boyutlama: sabit %1 risk (büyüme odaklı boyutlama Aşama 5'te karşılaştırılacak).
- Lookahead testi tüm stratejilerde GEÇTİ.

## Sonuçlar (sentetik, örneklem dışı)

| Sembol | Strateji | İşlem | PF | Beklenti | Beklenti (R) | Maks DD % | Kazanma % | Sharpe | §9 |
|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| BTC/USDT | ema_crossover | 82 | 2.15 | 41.37 | 0.411 | 6.18 | 67.1 | 1.63 | ❌ |
| BTC/USDT | rsi_reversion | 189 | 0.76 | -13.51 | -0.135 | 26.14 | 51.9 | -0.79 | ❌ |
| BTC/USDT | breakout | 374 | 1.04 | 2.33 | 0.018 | 20.41 | 53.2 | 0.15 | ❌ |
| BTC/USDT | grid | 1578 | 0.73 | -11.88 | -0.134 | 93.38 | 46.6 | -1.47 | ❌ |
| BTC/USDT | dca | 706 | 0.98 | -0.58 | -0.010 | 45.62 | 48.4 | -0.16 | ❌ |
| ETH/USDT | ema_crossover | 57 | 2.04 | 36.87 | 0.365 | 6.72 | 66.7 | 1.28 | ❌ |
| ETH/USDT | rsi_reversion | 264 | 0.71 | -15.94 | -0.176 | 38.90 | 50.0 | -1.16 | ❌ |
| ETH/USDT | breakout | 372 | 0.92 | -4.48 | -0.043 | 26.25 | 52.4 | -0.35 | ❌ |
| ETH/USDT | grid | 1706 | 1.18 | 6.42 | 0.056 | 58.85 | 55.0 | 0.63 | ❌ |
| ETH/USDT | dca | 419 | 1.92 | 14.94 | 0.141 | 14.02 | 57.8 | 2.11 | ✅ |
| SOL/USDT | ema_crossover | 64 | 1.55 | 24.03 | 0.241 | 7.25 | 60.9 | 0.87 | ❌ |
| SOL/USDT | rsi_reversion | 199 | 0.64 | -20.29 | -0.232 | 38.86 | 50.2 | -1.28 | ❌ |
| SOL/USDT | breakout | 502 | 0.96 | -2.45 | -0.031 | 39.32 | 50.6 | -0.32 | ❌ |
| SOL/USDT | grid | 1291 | 0.96 | -1.53 | -0.024 | 52.51 | 49.6 | -0.23 | ❌ |
| SOL/USDT | dca | 512 | 1.37 | 7.77 | 0.067 | 18.78 | 52.3 | 1.07 | ❌ |

**§9 eşiklerini geçen: 1/15** (ETH/USDT dca — sentetik veride; anlamsız sayılmalı).

## Gözlemler (sentetik veriye özgü, genellenemez)
- **ema_crossover:** PF 1.5–2.2, DD < %8 ama örneklem dışı işlem sayısı 57–82 (< 100) → §9'un
  örneklem büyüklüğü şartını karşılamıyor. Sentetik trendler pürüzsüz olduğu için iyimser.
- **rsi_reversion:** tüm sembollerde negatif beklenti ve "eğitimde pozitif → testte negatif"
  overfitting uyarısı. Bu haliyle reddedilmeli.
- **breakout:** başabaş civarı (PF 0.92–1.04); parametre tepe seçimi uyarıları var.
- **grid:** BTC'de %93 drawdown — çok sayıda eşzamanlı alım + rejim değişiminde stop kümelenmesi.
  Grid, gerçek veride de yalnızca sıkı range filtresi ve düşük eşzamanlı pozisyonla ele alınmalı.
- **dca:** sembole göre karışık; ETH tek geçen kombinasyon, BTC'de overfitting uyarısı.

## Overfitting uyarıları
- BTC/USDT / rsi_reversion: Örneklem dışı profit factor (0.76) eğitimdekinin (1.16) %70'inin altında: overfitting şüphesi.
- BTC/USDT / rsi_reversion: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- BTC/USDT / rsi_reversion: Fold 6: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- BTC/USDT / breakout: Fold 7: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- BTC/USDT / grid: Fold 0: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- BTC/USDT / dca: Örneklem dışı profit factor (0.98) eğitimdekinin (1.44) %70'inin altında: overfitting şüphesi.
- BTC/USDT / dca: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- BTC/USDT / dca: Fold 2: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- ETH/USDT / ema_crossover: Örneklem dışı profit factor (2.04) eğitimdekinin (4.05) %70'inin altında: overfitting şüphesi.
- ETH/USDT / rsi_reversion: Örneklem dışı profit factor (0.71) eğitimdekinin (1.25) %70'inin altında: overfitting şüphesi.
- ETH/USDT / rsi_reversion: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- ETH/USDT / rsi_reversion: Fold 4: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- ETH/USDT / breakout: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- ETH/USDT / breakout: Fold 0: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- ETH/USDT / grid: Fold 11: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- ETH/USDT / dca: Fold 7: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- SOL/USDT / rsi_reversion: Örneklem dışı profit factor (0.64) eğitimdekinin (1.22) %70'inin altında: overfitting şüphesi.
- SOL/USDT / rsi_reversion: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- SOL/USDT / rsi_reversion: Fold 20: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- SOL/USDT / breakout: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- SOL/USDT / breakout: Fold 0: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- SOL/USDT / grid: Eğitimde pozitif olan beklenti örneklem dışında negatif: overfitting.
- SOL/USDT / grid: Fold 21: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.
- SOL/USDT / dca: Örneklem dışı profit factor (1.37) eğitimdekinin (2.50) %70'inin altında: overfitting şüphesi.
- SOL/USDT / dca: Fold 5: en iyi kombinasyon medyanın 2 katından fazla — tepe seçimi (peak picking) riski.

## Değerlendirme
- Kazanma oranı tek başına kriter değildir; PF, beklenti ve drawdown birlikte değerlendirilir.
- Gerçek veriyle yeniden çalıştırılmadan hiçbir strateji live için önerilmez.
- Eşikleri geçenler bile önce ≥2 hafta paper/testnet'te doğrulanmalıdır (spec §9).
