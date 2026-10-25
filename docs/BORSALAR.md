# Desteklenen borsalar

Bot 21 borsaya bağlanabilir. Binance ve BTCTurk'ün kendi bağlayıcıları var; diğerleri
profil tabanlı ortak bağlayıcıyla (`bot/exchanges/generic.py`, ccxt) çalışır. Yeni borsalarda
**yalnızca spot** desteklenir (futures yalnızca Binance).

| Borsa | config adı | Ana kotasyon | Borsada stop emri | Testnet | WebSocket | Passphrase | Not |
|---|---|---|---|---|---|---|---|
| Binance | `binance` | USDT | ✅ | ✅ | ✅ |  | Ana borsa; spot + USDⓈ-M futures (demo). |
| BingX | `bingx` | USDT | ✅ | ✅ | ✅ |  |  |
| Bitfinex | `bitfinex` | USD | ✅ | — (yalnızca veri) | ✅ |  |  |
| Bitget | `bitget` | USDT | ✅ | — (yalnızca veri) | ✅ | ✅ |  |
| Bithumb | `bithumb` | KRW | ❌ yalnızca veri/arbitraj | — (yalnızca veri) | ✅ |  | KRW piyasası; Kore'de yerleşik hesap gerekir. Borsada stop yok. |
| Bitstamp | `bitstamp` | USD | ❌ yalnızca veri/arbitraj | — (yalnızca veri) | ✅ |  | Borsada stop emri yok. |
| Bitvavo | `bitvavo` | EUR | ✅ | — (yalnızca veri) | ✅ |  |  |
| BTCTurk | `btcturk` | TRY | ❌ yalnızca veri/arbitraj | — (yalnızca veri) | — (REST yoklama) |  | TRY pariteleri; arbitrajda USDT/TRY köprüsü. Borsada stop emri yok. |
| Bybit | `bybit` | USDT | ✅ | ✅ | ✅ |  |  |
| Coinbase (Advanced Trade) | `coinbase` | USDC | ✅ | — (yalnızca veri) | ✅ |  | API anahtarı: CDP key name + private key (secret). |
| Crypto.com Exchange | `cryptocom` | USDT | ✅ | ✅ | ✅ |  |  |
| Gate | `gate` | USDT | ✅ | ✅ | ✅ |  |  |
| Gemini | `gemini` | USD | ❌ yalnızca veri/arbitraj | ✅ | ✅ |  | Borsada stop emri yok (ccxt). |
| HTX | `htx` | USDT | ✅ | — (yalnızca veri) | ✅ |  |  |
| Kraken | `kraken` | USD | ✅ | — (yalnızca veri) | ✅ |  |  |
| KuCoin | `kucoin` | USDT | ✅ | — (yalnızca veri) | ✅ | ✅ |  |
| MEXC | `mexc` | USDT | ✅ | — (yalnızca veri) | ✅ |  |  |
| OKX | `okx` | USDT | ✅ | ✅ | ✅ | ✅ | Demo trading anahtarı ayrıca oluşturulur (testnet). |
| Poloniex | `poloniex` | USDT | ✅ | ✅ | ✅ |  |  |
| Upbit | `upbit` | KRW | ❌ yalnızca veri/arbitraj | — (yalnızca veri) | ✅ |  | KRW piyasası; Kore'de yerleşik hesap gerekir. Borsada stop yok. |
| WhiteBIT | `whitebit` | USDT | ✅ | — (yalnızca veri) | ✅ |  |  |

## Önemli kurallar
- **Borsada stop emri olmayan borsalarda strateji pozisyonu açılmaz** (CLAUDE.md kural 9):
  Upbit, Bithumb, Bitstamp, Gemini, BTCTurk. Bu borsalar piyasa verisi, analiz ve arbitraj
  taraması için kullanılır. Paper modda da aynı kural uygulanır (sonuçlar gerçekçi kalsın).
- **Testnet'i olmayan borsalar** testnet modunda anahtarsız bağlanır ve emir göndermez
  (yalnızca veri). Testnet'i olanlar: Binance, OKX (demo), Bybit, Gate, Crypto.com, Gemini,
  BingX (VST), Poloniex.
- Tüm trafik egress (sabit IP) üzerinden çıkar; doğrudan bağlantıya dönülmez.
- Bazı borsalar bazı ülkeleri engeller (HTTP 451/403). `bot net-check` başlangıçta bunu test eder.
- Upbit ve Bithumb KRW piyasalarıdır; genelde Kore'de yerleşik hesap gerekir.

## Bağlama
1. `config/config.yaml`:
   ```yaml
   exchanges:
     okx:    { enabled: true, market_type: spot, testnet: true }
     kraken: { enabled: true, testnet: true, symbols: ["BTC/USD", "ETH/USD"] }
   ```
   `symbols` boşsa `universe.symbols` kullanılır. Borsada listelenmeyen pariteler
   başlangıçta atlanır ve loglanır (`symbols_not_listed`). USD/EUR/KRW/TRY kotasyonlu
   borsalarda `symbols` vermeniz gerekir.
2. `.env` (yalnızca burada; koda/config'e asla):
   ```
   OKX_API_KEY=...          OKX_API_SECRET=...          OKX_API_PASSWORD=...
   OKX_TESTNET_API_KEY=...  OKX_TESTNET_API_SECRET=...  OKX_TESTNET_API_PASSWORD=...
   ```
   Desen: `<AD>_API_KEY`, `<AD>_API_SECRET`, `<AD>_API_PASSWORD` (passphrase isteyenler) ve
   testnet için `<AD>_TESTNET_...`. `<AD>` = config adının büyük harfi (ör. `CRYPTOCOM`).
3. Borsada API anahtarı oluştururken: **yalnızca okuma + spot işlem** izni, **para çekme
   kapalı**, IP kısıtlaması = `EGRESS_EXPECTED_IP`.
4. Panel → Ayarlar → **Borsalar** tablosunda etkinlik, yetenekler ve anahtarın tanımlı olup
   olmadığı (yalnızca evet/hayır) görünür.

## Doğrulama durumu
- Profiller kurulu ccxt sürümünün yetenek bayraklarıyla otomatik testte karşılaştırılır;
  bağlayıcı, proxy, anahtar ve testnet bağlantıları çevrimdışı test edilir.
- Gerçek uç noktalara erişen testler: `pytest -m integration tests/integration/test_all_exchanges_public.py`
  (herkese açık veri, anahtarsız). Bu testler geliştirme ortamının ağ politikası nedeniyle
  henüz çalıştırılamadı.
- **Emir ve stop emri yerleşimi yeni borsaların hiçbirinde gerçek ortamda denenmedi.** Her
  borsada önce testnet'te (varsa), yoksa çok küçük bir live emirle stop emrinin borsada
  göründüğünü doğrulayın. Müşteri emir kimliği biçimleri (Gate `t-…`, Kraken/Bitvavo UUID,
  Bitfinex sayısal) borsaların API kurallarına göre seçildi; ilk emirde kontrol edin.
