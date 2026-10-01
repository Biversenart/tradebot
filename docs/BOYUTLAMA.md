> ⚠️ SENTETİK VERİ (bkz. docs/BACKTEST_OZET.md). Gerçek veriyle: `bot backtest sizing -s BTC/USDT -s ETH/USDT -s SOL/USDT --since 2022-01-01`

# Boyutlama Karşılaştırması — Sabit %1 risk vs Büyüme odaklı

_2026-10-01 23:16 UTC_ · Veri: synthetic 1h

Aynı sinyaller, aynı komisyon/kayma; yalnızca pozisyon boyutu farklı. Örneklem içi (tam dönem) karşılaştırmadır; formüller `docs/KARARLAR.md`.

| Sembol | Strateji | İşlem | Getiri % (sabit → büyüme) | Maks DD % | Sharpe | PF |
|---|---|---:|---|---|---|---|
| BTC/USDT | ema_crossover | 36/36 | 17.45 → 18.70 | 2.73 → 2.84 | 1.14 → 1.12 | 2.53 → 2.56 |
| BTC/USDT | rsi_reversion | 134/124 | -21.42 → -7.81 | 23.67 → 8.49 | -0.82 → -1.03 | 0.70 → 0.62 |
| BTC/USDT | breakout | 765/653 | -29.09 → -16.04 | 41.66 → 29.13 | -0.45 → -0.41 | 0.92 → 0.92 |
| BTC/USDT | grid | 2330/1564 | -97.35 → -11.98 | 98.57 → 60.37 | -1.64 → -0.01 | 0.81 → 0.97 |
| BTC/USDT | dca | 1386/1152 | 41.77 → 66.79 | 61.34 → 13.97 | 0.60 → 2.27 | 1.10 → 1.64 |
| ETH/USDT | ema_crossover | 24/24 | 11.04 → 11.65 | 4.40 → 4.50 | 0.97 → 1.03 | 2.22 → 2.44 |
| ETH/USDT | rsi_reversion | 141/125 | -20.64 → -8.53 | 22.07 → 9.00 | -0.81 → -1.17 | 0.71 → 0.57 |
| ETH/USDT | breakout | 740/602 | -58.18 → -33.00 | 63.53 → 41.46 | -1.24 → -1.00 | 0.80 → 0.81 |
| ETH/USDT | grid | 2382/1688 | -37.69 → 99.00 | 82.34 → 34.36 | 0.01 → 0.67 | 0.92 → 1.19 |
| ETH/USDT | dca | 1465/1215 | 208.06 → 109.30 | 32.53 → 7.33 | 1.90 → 3.30 | 1.51 → 2.12 |
| SOL/USDT | ema_crossover | 28/28 | 8.20 → 8.72 | 4.30 → 3.74 | 0.68 → 0.79 | 1.78 → 1.91 |
| SOL/USDT | rsi_reversion | 147/131 | -12.00 → -6.05 | 21.73 → 8.33 | -0.40 → -0.65 | 0.84 → 0.75 |
| SOL/USDT | breakout | 740/633 | -13.74 → 8.73 | 46.27 → 25.12 | -0.16 → 0.25 | 0.97 → 1.04 |
| SOL/USDT | grid | 2196/1482 | -90.10 → -36.46 | 91.17 → 42.92 | -1.00 → -0.33 | 0.73 → 0.89 |
| SOL/USDT | dca | 1372/1157 | 182.95 → 90.38 | 27.34 → 5.69 | 1.75 → 2.76 | 1.36 → 1.79 |

Büyüme boyutlaması drawdown'u 13/15 kombinasyonda azalttı veya eşit tuttu.
