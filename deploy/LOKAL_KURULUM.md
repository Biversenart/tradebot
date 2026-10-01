# Ev Bilgisayarında Kurulum (Windows + Docker Desktop / Linux)

Bot sizin bilgisayarınızda çalışır; borsa trafiği WireGuard tüneliyle VPS'in sabit IP'sinden çıkar. Bilgisayarınızın **diğer internet trafiği etkilenmez** (tünel yalnızca bot konteynerleri içindir).

Ön koşul: VPS hazır ve `deploy/local/wg_confs/wg0.conf` dosyası elinizde (bkz. `ORACLE_KURULUM.md`).

---

## A. Windows 10/11 (Docker Desktop + WSL2)

### 1. Kurulumlar
1. **WSL2:** PowerShell (yönetici) → `wsl --install` → yeniden başlatın.
2. **Docker Desktop:** <https://www.docker.com/products/docker-desktop/> → kurulumda "Use WSL 2 based engine" işaretli olsun.
3. Docker Desktop → **Settings → General → "Start Docker Desktop when you sign in"** işaretleyin.
4. **Git for Windows:** <https://git-scm.com/download/win>.

### 2. Repo ve ayarlar
PowerShell:
```powershell
git clone https://github.com/Biversenart/tradebot.git
cd tradebot
copy .env.example .env
copy config\config.example.yaml config\config.yaml
notepad .env
```
`.env` içinde en az:
```
TRADING_MODE=paper            # testnet/live için config.yaml'daki mode ile aynı olmalı
EGRESS_EXPECTED_IP=<VPS_RESERVED_IP>
POSTGRES_PASSWORD=<uzun-rastgele-parola>
```
`config/config.yaml` → `egress.mode: wireguard` (varsayılan).

VPS'ten aldığınız istemci dosyasını `deploy\local\wg_confs\wg0.conf` olarak koyun.

> **Satır sonları:** `deploy/local/*.sh` dosyaları LF olmalı. Git'i `git config --global core.autocrlf input` ile ayarlayın veya klonlamadan önce bunu yapın; CRLF olursa kill-switch betiği çalışmaz ve WireGuard konteyneri sağlıksız kalır (bot başlamaz — güvenli taraf).

### 3. Başlatma
```powershell
docker compose up -d --build
docker compose ps          # wireguard: healthy, bot: running
docker compose logs -f bot
```

### 4. Uyku modunu kapatma (ÖNEMLİ)
Bilgisayar uyursa bot durur (pozisyonların stop emirleri borsada bekler, ama yeni işlem/izleme olmaz).
- **Ayarlar → Sistem → Güç ve pil → Ekran ve uyku → "Prize takılıyken, bilgisayarı uyku moduna geçir: Hiçbir zaman"**.
- PowerShell (yönetici): `powercfg /change standby-timeout-ac 0` ve `powercfg /change hibernate-timeout-ac 0`.
- **Windows Update → Gelişmiş seçenekler → Etkin saatler**: botun çalıştığı saatleri ayarlayın; güncelleme sonrası yeniden başlatmada Docker Desktop otomatik açılır, konteynerler `restart: always` ile kalkar.

### 5. Otomatik başlatma
- Docker Desktop oturum açılınca başlar (adım 1.3). Konteynerler `restart: always` ile kendiliğinden kalkar.
- Bilgisayar elektrik kesintisinden sonra kendiliğinden açılsın istiyorsanız BIOS'ta **"Restore on AC Power Loss = Power On"** ayarını yapın ve Windows'ta otomatik oturum açmayı değerlendirin (Docker Desktop oturum ister).

---

## B. Linux (Ubuntu/Debian vb.)
```bash
# Docker Engine + compose eklentisi
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
sudo systemctl enable --now docker

git clone https://github.com/Biversenart/tradebot.git && cd tradebot
cp .env.example .env && cp config/config.example.yaml config/config.yaml
nano .env                                   # EGRESS_EXPECTED_IP, POSTGRES_PASSWORD, ...
cp ~/wg0.conf deploy/local/wg_confs/wg0.conf && chmod 600 deploy/local/wg_confs/wg0.conf
docker compose up -d --build
```
Uyku: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
Otomatik başlatma: Docker servisi `enable` edildi; konteynerler `restart: always`.

---

## C. Doğrulama (her iki sistemde)

### 1. Çıkış IP'si = VPS IP'si
```bash
docker compose exec bot python -c "import urllib.request as u; print(u.urlopen('https://api.ipify.org', timeout=10).read().decode())"
```
Çıktı `EGRESS_EXPECTED_IP` ile aynı olmalı.

### 2. Botun kendi kontrolü
```bash
docker compose exec bot bot net-check --config /app/config/config.yaml
```
`Dış IP durumu: ok` ve `Emirlere izin: EVET` görmelisiniz (paper modda IP kontrolü atlanır).

### 3. Kill-switch testi (tünel düşünce internet YOK)
```bash
docker compose exec wireguard wg-quick down wg0
docker compose exec bot python -c "import urllib.request as u; u.urlopen('https://api.ipify.org', timeout=5)"
# BEKLENEN: hata (timeout / network unreachable). Yanıt gelirse kill-switch çalışmıyor demektir: BOTU ÇALIŞTIRMAYIN.
docker compose restart wireguard bot        # tüneli geri getir
```

### 4. Heartbeat (önerilir)
1. <https://healthchecks.io> (ücretsiz) → yeni check → periyot 1 dk, grace 3 dk → **Integrations → Telegram** bağlayın.
2. Ping URL'sini `.env` → `HEARTBEAT_URL=` satırına yazın; `config.yaml` → `heartbeat.enabled: true`.
3. Bot yalnızca sağlıklıyken ping atar. Bilgisayar kapanır, tünel düşer veya IP doğrulanamazsa ping kesilir ve Telegram'a uyarı gelir.

---

## Riskler ve önlemler
| Risk | Önlem |
|---|---|
| Elektrik/internet kesintisi, uyku, Windows güncellemesi | Her pozisyonun **stop-loss'u borsada emir olarak** bekler; `restart: always`; açılışta reconciliation (Aşama 5) |
| Tünel düşmesi | Kill-switch: bot internete çıkamaz; IP doğrulanamaz → emir yok; heartbeat kesilir → Telegram |
| Ev IP'sinin değişmesi | Etkisi yok: Binance yalnızca VPS IP'sini görür; WireGuard yeniden bağlanır |
| Gecikme | Swing/gün içi için yeterli. Arbitraj ciddi kullanılacaksa botu borsaya yakın bir VPS'te çalıştırın |

## Sorun giderme
- **wireguard `unhealthy`:** `docker compose logs wireguard`. `[killswitch] Endpoint IPv4 olmalı` → `wg0.conf`'ta `Endpoint` alan adı değil IP olmalı. El sıkışma yok → VPS'te `sudo wg show`, OCI Security List'te UDP 51820 açık mı?
- **bot başlamıyor:** WireGuard sağlıklı olmadan bot başlamaz (tasarım gereği).
- **Postgres'e bağlanamıyor:** `docker-compose.yml`'deki `botnet` alt ağı (172.28.0.0/24) başka bir ağla çakışıyorsa hem `subnet`, hem IP adreslerini, hem de `KILLSWITCH_LOCAL_NET`'i birlikte değiştirin.
