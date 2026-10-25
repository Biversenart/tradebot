# Web paneli

Tarayıcıdan botu izlemek ve yönetmek için tek sayfalık arayüz. Bot çalışırken (`bot run`)
açılır; bot olmadan denemek için `bot panel-demo` kullanın.

## Açma
1. `.env` → `API_AUTH_TOKEN=` (en az 16 karakter):
   `python -c "import secrets;print(secrets.token_urlsafe(32))"`
2. `config/config.yaml` → `api.enabled: true` (Docker'da `api.host: 0.0.0.0`).
3. `bot run` → tarayıcıda <http://127.0.0.1:8080> → token ile giriş.

**Denemek için (borsa/anahtar gerekmez):**
```bash
bot panel-demo            # sentetik veri; adres ve token ekrana yazılır
bot panel-demo --port 8090 --token "kendi-uzun-tokeniniz"
```
Demo her şeyi geçici bir klasörde tutar ve kapanınca siler; gerçek hesaba dokunmaz.

## Sayfalar
| Sayfa | İçerik |
|---|---|
| Genel bakış | Bakiye, bugünkü/toplam PnL, açık PnL, isabet, profit factor, maruziyet, drawdown; bakiye + drawdown grafiği, günlük PnL, açık pozisyonlar, son olaylar |
| Pozisyonlar | Açık pozisyonlar (canlı fiyat, açık PnL, R, borsada stop var mı 🛡️), kapanmış işlemler (sembol/strateji filtresi, CSV indirme) |
| Grafik | Mum grafiği + hacim; giriş ▲▼ / çıkış ✕ işaretleri; açık pozisyonun giriş, stop ve hedef çizgileri |
| Stratejiler | Strateji kartları (aç/kapa, parametreler, işlem sayısı, isabet, PF, beklenti), strateji başına PnL, gölge mod onayı |
| Risk | Kill switch nedenleri, günlük zarar / drawdown / maruziyet limit çubukları, API hata sayacı, parite maruziyeti, son boyutlama dökümü, operasyonel güvenlik (depeg, saat, delisting), kanarya tavanı |
| Arbitraj | Fırsat sayısı, net spread dağılımı, son fırsatlar |
| Backtest & veri | Panelden walk-forward backtest başlatma, geçmiş veri indirme (egress üzerinden), iş durumu, raporları panel içinde görüntüleme, indirilmiş veri listesi |
| Analiz | Coin analizi başlatma, rapor listesi, grafik + metin rapor |
| Olaylar | Risk olayları (seviye filtresi), config değişiklik günlüğü |
| Ayarlar | Çalışma modu, geçerli yapılandırma (salt okunur, ağaç görünümü), tema |

Üst çubukta her sayfada: mod (PAPER/TESTNET/LIVE), kill switch ve IP durumu, **⛔ KILL SWITCH**
ve (aktifken) **▶ Devam** butonları. Pozisyon açılış/kapanışları ve uyarılar anlık bildirim
olarak gelir (sunucu olay akışı, `/api/stream`).

## Neler panelden değiştirilemez (bilinçli)
Risk ve strateji parametreleri panelden düzenlenmez; `config/config.yaml` değiştirilip bot
yeniden başlatılır (değişiklik config günlüğüne önceki değeriyle yazılır, testnet/live'da
değişen strateji parametreleri önce gölgede çalışır). Panel; kill switch/devam, strateji
aç-kapa (yeniden başlatmada config'e döner), kanarya tavanı, gölge onayı, analiz, backtest ve
veri indirme yapabilir. Para çekme yoktur.

## Güvenlik
- Token veya HttpOnly oturum çerezi + CSRF; 60 sn'de 10 hatalı girişte kilit.
- Sıkı CSP: yalnızca panelin kendi scriptleri (satır içi script, `eval`, CDN yok). plotly.js
  kurulu Python paketinden sunulur; panel internetsiz de çalışır.
- Rapor sayfaları (analiz/backtest HTML) ayrı, sandbox'lı bir CSP ile sunulur: panel
  çerezlerine ve API'ye erişemezler.
- Varsayılan adres `127.0.0.1`; paneli internete açmayın (uzaktan erişim: SSH tüneli / WireGuard).
- Yapılandırma görünümünde API anahtarları/tokenlar yoktur (bunlar yalnızca `.env`'de).
