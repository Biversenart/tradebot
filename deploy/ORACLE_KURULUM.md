# Oracle Cloud Always Free — WireGuard Çıkış Sunucusu Kurulumu

Bu rehber, botun borsaya **hep aynı IP'den** bağlanması için ücretsiz bir Oracle Cloud sunucusunu WireGuard çıkış kapısı olarak kurar. Bot bu sunucuda **çalışmaz**; sunucu yalnızca tünel ucudur.

> ⚠️ **Live (gerçek para) için** ücretli bir VPS (Hetzner, Contabo vb., Avrupa) önerilir: Always Free sunucular boşta kalırsa geri alınabilir (aşağıya bakın). Geliştirme / paper / testnet için Oracle Free yeterlidir.

---

## 1. Hesap açma ve bölge seçimi
1. <https://www.oracle.com/cloud/free/> adresinden hesap açın. Kredi kartı **doğrulama** için istenir (Always Free kaynaklar ücretlendirilmez).
2. **Home Region = Germany Central (Frankfurt)** seçin. Ana bölge sonradan **değiştirilemez** ve Always Free sunucular yalnızca ana bölgede açılabilir.
   - **ABD bölgelerini seçmeyin:** Binance global, ABD IP'lerinden gelen API isteklerini "restricted location" (HTTP 451/403) ile reddeder.

## 2. Sunucu (instance) oluşturma
1. Menü → **Compute → Instances → Create instance**.
2. **Image:** *Canonical Ubuntu 24.04* (Minimal de olur).
3. **Shape:** `VM.Standard.E2.1.Micro` (Always Free eligible). Tünel için 1 OCPU / 1 GB fazlasıyla yeterli.
4. **Networking:** Yeni VCN + **public subnet**; "Assign a public IPv4 address" işaretli kalsın (sonra reserved IP ile değiştireceğiz).
5. **SSH keys:** Kendi açık anahtarınızı yükleyin (`ssh-keygen -t ed25519` ile üretebilirsiniz). Parola ile giriş kullanmayın.
6. **Create**.

## 3. Reserved (sabit) public IP — ZORUNLU
Ephemeral IP, sunucu yeniden oluşturulursa değişir ve Binance whitelist'i bozulur. Reserved IP hesapta kalır.
1. **Networking → IP Management → Reserved public IPs → Reserve public IP address** → ad verin, oluşturun.
2. **Compute → Instances → (sunucu) → Attached VNICs → (VNIC) → IPv4 Addresses → (primary private IP) → Edit**.
3. "Public IP type" = **No public IP** → kaydedin (ephemeral IP serbest kalır).
4. Tekrar **Edit** → **Reserved public IP** → az önce ayırdığınız IP'yi seçin → kaydedin.
5. Bu IP'yi not edin: `.env` → `EGRESS_EXPECTED_IP=<RESERVED_IP>` ve Binance API anahtarı whitelist'i bu IP olacak.

## 4. OCI Security List (bulut güvenlik duvarı)
**Networking → Virtual Cloud Networks → (VCN) → Security Lists → Default Security List**:
- **Ingress Rules:**
  - Var olan `0.0.0.0/0 TCP 22` kuralını kurulum bitene kadar bırakın (sonra kaldıracağız / ev IP'nizle sınırlayabilirsiniz).
  - **Add Ingress Rule:** Source CIDR `0.0.0.0/0`, IP Protocol **UDP**, Destination Port **51820** (WireGuard).
  - ICMP kurallarını varsayılan bırakabilirsiniz.
- Başka hiçbir port açmayın (Dante 1080 **açılmaz**; yalnızca tünel içinden erişilir).

## 5. Sunucuya bağlanma ve güncellemeler
```bash
ssh ubuntu@<RESERVED_IP>
sudo apt update && sudo apt -y full-upgrade
# Otomatik güvenlik güncellemeleri
sudo apt -y install unattended-upgrades git
sudo dpkg-reconfigure -plow unattended-upgrades   # "Yes"
sudo reboot
```

## 6. WireGuard sunucusu
```bash
ssh ubuntu@<RESERVED_IP>
git clone https://github.com/Biversenart/tradebot.git && cd tradebot/deploy
sudo bash wireguard/server/setup_wireguard_server.sh <RESERVED_IP>
```
Betik: WireGuard'ı kurar, IP yönlendirmeyi açar, anahtarları üretir, `/etc/wireguard/wg0.conf` (sunucu) ve `/root/wg-client/wg0.conf` (istemci) dosyalarını oluşturur, `wg-quick@wg0` servisini başlatır.

İstemci dosyasını ev bilgisayarınıza alın ve sunucudan silin:
```bash
# sunucuda:
sudo cp /root/wg-client/wg0.conf /home/ubuntu/wg0.conf && sudo chown ubuntu /home/ubuntu/wg0.conf
# ev bilgisayarında (repo klasöründe):
scp ubuntu@<RESERVED_IP>:wg0.conf deploy/local/wg_confs/wg0.conf
# sunucuda:
shred -u /home/ubuntu/wg0.conf && sudo shred -u /root/wg-client/wg0.conf
```

## 7. ufw (işletim sistemi güvenlik duvarı)
Oracle'ın Ubuntu imajı `/etc/iptables/rules.v4` içinde hazır iptables kurallarıyla gelir; betik bunları yedekleyip ufw'ye devreder.
```bash
sudo bash ufw/setup_ufw.sh 51820 any
```
Tünel çalıştıktan sonra (bkz. `LOKAL_KURULUM.md`) SSH'ı **yalnızca tünele** kısıtlayın:
```bash
# ev bilgisayarındaki WireGuard konteyneri bağlıyken, tünel üzerinden:
#   ssh ubuntu@10.13.13.1
sudo bash ufw/setup_ufw.sh 51820 10.13.13.0/24
```
Ardından OCI Security List'teki `TCP 22` kuralını kaldırabilirsiniz. (Tünel bozulursa OCI Console → Instance → **Console connection** ile erişim kalır.)

## 8. (Opsiyonel, Seçenek B) Dante SOCKS5
Yalnızca bot Docker'sız Linux'ta `egress.mode: proxy` ile çalışacaksa:
```bash
sudo bash dante/setup_dante.sh botproxy
```
Çıktıdaki `EGRESS_PROXY_URL=socks5h://...@10.13.13.1:1080` satırını **yalnızca** botun `.env` dosyasına yazın.

## 9. VPS'ten Binance erişim testi (ZORUNLU)
```bash
bash vps/check_exchange_access.sh
```
Beklenen: iki servisten aynı çıkış IP'si (= reserved IP) ve tüm satırlarda `TAMAM 200`.
- `ENGELLİ 451/403` görürseniz bu bölge Binance tarafından kısıtlanmış demektir: **bu VPS'i kullanmayın**, desteklenen bir ülkede (ör. Frankfurt) yeni sunucu açın.
- Bot da açılışta aynı testi yapar ve 451/403'ü "restricted location" olarak raporlayıp emir göndermez.

## 10. Binance API anahtarı
- Binance → API Management → anahtar oluştur → **Restrict access to trusted IPs only** → `<RESERVED_IP>`.
- İzinler: **Enable Reading** + **Enable Spot & Margin Trading** (futures için ayrıca). **Enable Withdrawals KAPALI** kalsın.
- Anahtarları yalnızca ev bilgisayarındaki `.env` dosyasına yazın.

## 11. ⚠️ Boşta sunucu geri alma (idle reclamation)
Oracle, Always Free sunucuları **7 gün boyunca** şu koşulların hepsi sağlanırsa geri alabilir:
- CPU kullanımı (95. yüzdelik) < %20,
- Ağ kullanımı < %20,
- (A1 shape'lerde) bellek kullanımı < %20.

Yalnızca tünel çalıştıran bir sunucu bu duruma kolayca düşer. Önlemler:
- **Hesabı Pay As You Go'ya yükseltin** (Always Free kaynaklar ücretsiz kalmaya devam eder, geri alma politikası uygulanmaz). En güvenilir çözüm budur.
- ve/veya sunucuda periyodik iş çalıştırın (ör. geçmiş veri indirme, backtest).
- Sunucu geri alınsa bile **reserved IP hesapta kalır**; yeni sunucuya bağlayıp 6–9. adımları tekrarlarsınız.
- Bot, IP uyuşmazlığında veya tünel koptuğunda **otomatik olarak emir göndermeyi durdurur** ve uyarı verir; heartbeat kesilince harici izleme Telegram'a haber verir.

## 12. Kontrol listesi
- [ ] Ana bölge Frankfurt (ABD değil)
- [ ] Reserved public IP bağlı, `.env` → `EGRESS_EXPECTED_IP`
- [ ] Security List: yalnızca UDP 51820 (+ geçici TCP 22)
- [ ] ufw aktif, SSH yalnızca tünelden
- [ ] `check_exchange_access.sh` → tümü `TAMAM`
- [ ] Binance API anahtarı: IP whitelist, para çekme kapalı
- [ ] PAYG yükseltmesi veya periyodik iş (idle reclamation önlemi)
- [ ] Otomatik güvenlik güncellemeleri açık
