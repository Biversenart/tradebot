# CLAUDE.md — Kripto Trade & Arbitraj Botu

Bu dosya, bu repoda çalışan Claude Code için kalıcı talimatlardır. Her oturumun başında oku.

## Proje özeti
Seçilen kripto paraları analiz eden, sinyal üreten, işlem açıp kapatan ve borsalar arası/üçgen arbitraj fırsatlarını yakalayan modüler bir bot. Tam şartname: `docs/PROJE_SPEC.md`. Aşama planı: `docs/YOL_HARITASI.md`.

## Değişmez kurallar (asla çiğneme)
1. **Varsayılan mod `paper`dır.** `live` mod yalnızca `.env` içinde `TRADING_MODE=live` VE `config.yaml` içinde `live_trading_confirmed: true` birlikte olduğunda açılır. Kodda bu kontrolü asla atlama veya kısaltma.
2. **API anahtarları asla koda, loga, teste veya commit'e girmez.** Sadece `.env` üzerinden okunur. `.env` her zaman `.gitignore` içindedir. Loglarda anahtarlar maskelenir.
3. **Para çekme (withdraw) fonksiyonu yazma.** Bot yalnızca işlem yapar; borsalar arası transfer manuel kalır.
4. **Her emir RiskManager'dan geçer.** Strateji katmanı borsaya doğrudan emir gönderemez; yalnızca `OrderIntent` üretir.
5. **Kill switch her zaman çalışır durumda olmalı:** günlük zarar limiti, maksimum drawdown, art arda hata sayısı aşılınca tüm yeni emirler durur ve açık pozisyonlar config'e göre kapatılır.
6. Para hesaplarında `float` değil `Decimal` kullan. Borsa hassasiyetine (tick size, lot size, min notional) göre yuvarla.
7. Her yeni modül için test yaz. `pytest` yeşil olmadan aşamayı tamamlanmış sayma.
8. **Sabit çıkış IP zorunlu (testnet/live).** Borsalara giden TÜM trafik (REST + WebSocket) `egress` ayarındaki proxy/tünel üzerinden çıkar. Başlangıçta ve periyodik olarak dış IP doğrulanır; `expected_ip` ile eşleşmezse bot emir göndermez (fail-closed). Proxy düşerse doğrudan bağlantıya **asla** geri dönme (fallback yok). Ayrıntı: `docs/PROJE_SPEC.md` §5.11.

9. **Her açık pozisyonun stop-loss'u borsada emir olarak bulunur.** Bot lokal bilgisayarda çalışabilir; kesinti olursa koruma borsada kalmalı. Stop emri konamayan pozisyon hemen kapatılır.

## Teknoloji
- Python 3.12, `asyncio`
- Borsa erişimi: `ccxt` (REST) + `ccxt.pro` veya borsanın native websocket'i
- Veri: `pandas`, `numpy`; indikatörler: `pandas-ta` (veya kendi implementasyonu)
- Depolama: PostgreSQL (prod) / SQLite (geliştirme), `SQLAlchemy 2` + `alembic`
- Config: `pydantic-settings` + YAML
- Log: `structlog` (JSON)
- Bildirim: Telegram bot
- Panel: FastAPI + basit web arayüzü
- Test: `pytest`, `pytest-asyncio`; lint: `ruff`, tip: `mypy`
- Dağıtım: Docker + docker-compose; CI: GitHub Actions

## Kod stili
- Tip ipuçları zorunlu, `mypy --strict` hedefi.
- Kod, değişken ve fonksiyon isimleri İngilizce; kullanıcıya dönük mesajlar ve dokümanlar Türkçe.
- Her borsa bağlayıcısı `ExchangeAdapter` arayüzünü uygular; strateji kodu hiçbir zaman ccxt'yi doğrudan import etmez.
- Küçük, odaklı commit'ler. Commit mesajları: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`.

## Çalışma şekli
- Bir aşamaya başlamadan önce `docs/YOL_HARITASI.md` içindeki ilgili bölümü oku, kısa bir plan çıkar, sonra uygula.
- Aşama bitince: testleri çalıştır, `README.md`'yi güncelle, `docs/YOL_HARITASI.md`'de aşamayı işaretle.
- Belirsiz bir finansal karar (ör. pozisyon büyüklüğü formülü) varsa varsayım yapıp `docs/KARARLAR.md`'ye yaz.
