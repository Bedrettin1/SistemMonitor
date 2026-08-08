<p align="center">
  <img src="assets/Banner.png" alt="SistemMonitor Banner">
</p>

<h1 align="center">SistemMonitor</h1>

<p align="center">
  <b>Windows için gerçek zamanlı sistem izleme uygulaması</b><br>
  CPU, GPU, RAM, Disk, Ağ ve Windows hata kayıtları tek ekranda.
</p>

---

## ✨ Özellikler

- **Gerçek zamanlı CPU kullanımı** ve sıcaklık takibi
- **Gerçek zamanlı GPU kullanımı** ve sıcaklık takibi (NVIDIA)
- **RAM** kullanım izleme
- **Disk** kullanım izleme
- **Ağ** hız izleme (indirme / yükleme)
- **Windows hata kayıtları** görüntüleyici (son 48 saat)
- **Telefonla uzaktan izleme** - QR kod ile (aynı Wi-Fi / LAN ağı üzerinde çalışır)
- **Sistem tepsisi** desteği - arka planda çalışmaya devam eder

## 🔒 Güvenlik Mimarisi

Telefon bağlantısı **varsayılan olarak kapalıdır**. Açıldığında:

- Sunucu yalnızca **güvenli yerel ağ adaptörüne** bağlanır (private `10.x`, `172.16-31.x`, `192.168.x`).
  VPN, sanal (vEthernet, WSL, Docker, VirtualBox), loopback ve public ağlar **reddedilir**. `0.0.0.0` kullanılmaz.
- **Tek kullanımlık pairing kodu** üretilir: **60 saniye** geçerlidir ve ilk başarılı bağlantıda imha edilir.
- Pairing sonrası tarayıcıya `HttpOnly; SameSite=Strict` **oturum çerezi** verilir; kalıcı token URL'de taşınmaz.
- Token karşılaştırması `secrets.compare_digest()` ile yapılır.
- Kimlik doğrulama **fail-closed**: token/session yoksa her istek 403 alır.
- **Host ve Origin doğrulaması**: Host/Origin her iki HTTP metodu için de (GET ve POST) doğrulanır;
  IPv4, IPv6 loopback (`[::1]`), `localhost` desteklenir; yanlış port veya enjeksiyon reddedilir.
- `Access-Control-Allow-Origin` **yoktur**; API çapraz kaynaklı isteklere açık değildir.
- **Endpoint bazlı rate limit**: `pairing`, `page`, `metrics`, `events` ayrı kovalardadır.
  Pairing 10 istek/dk ile sıkı sınırlıdır; telemetry trafiği pairing limitini tüketmez.
- **Request body limiti**: 4 KB üzeri istekler 413, bozuk Content-Length/JSON 400 alır.
- **Security headers**: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` ve tarayıcıyı kırmayacak minimum `Content-Security-Policy`.
- **Periyodik cleanup**: süresi dolan oturumlar ve rate-limit kayıtları düzenli temizlenir (bellek büyümesi yok).
- SSE istemci listesi `threading.Lock()` ile korunur; her istemci için tek EventSource bağlantısı.
- **Log güvenliği**: pairing kodu ve oturum kimlikleri log'larda maskelenir.
- Windows Event Log metinleri yalnızca **PlainText** olarak gösterilir (HTML render yok).
- `powershell.exe` yalnızca tam sistem yolundan çağrılır: `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`.
- `calistir.py` `%TEMP%` dizinini Python path'ine eklemez.

### Taşıma güvenliği (TLS)

Telefon bağlantısı **LAN içindir**; varsayılan **HTTP** modunda trafik **şifreli değildir**. Aynı
ağdaki kötü niyetli bir düğüm verileri dinleyebilir. Public internet'e port forward **önerilmez**.

Opsiyonel **HTTPS**: sertifika ve private key dosyaları verilirse sunucu TLS ile başlar ve
session cookie'ye `Secure` flag'i eklenir. Self-signed sertifika kullanılabilir.

```powershell
# Sertifika + key üretme (OpenSSL):
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes
```

TLS, `start_phone_server(monitor, tls_cert="cert.pem", tls_key="key.pem")` ile etkinleştirilir.
Sertifika/key yalnızca biri verilirse veya dosya bulunamazsa sunucu **HTTPS'e sessizce düşmez**;
kullanıcıya açık hata gösterilir. HTTP modunda arayüz ve sayfada şifresiz bağlantı uyarısı görüntülenir.

## 🖥️ Kurulum

1. [Releases](/Bedrettin1/SistemMonitor/releases) sayfasından `SistemMonitor.exe` dosyasını indirin
2. Çalıştırın - Python veya başka bir bağımlılık gerekmez

### Kaynaktan çalıştırma

```powershell
pip install -r kaynak/requirements.txt
python kaynak/sistem_monitor.py
```

## 🎮 Kısayollar

| Tuş | İşlev |
|-----|-------|
| `F1` | Kısayollar sayfasını göster |
| `F2` | Telefon bağlantısını aç/kapat |
| `F4` | Gizli simgelere küçült / geri getir |
| `Esc` | Overlay modundan çık |
| `Ctrl+B` | Pencereyi büyüt |
| `Ctrl+S` | Pencereyi küçült |
| `F5` | Verileri yenile |

## 📱 Telefonla Bağlanma

1. `F2` tuşuna basın veya 📱 butonuna tıklayın
2. QR kodu telefonunuzla tarayın (veya ekrandaki URL'yi girin)
3. Değerleri telefonunuzdan canlı olarak izleyin
4. Uygulama arka planda çalışmaya devam eder

> Bağlantı kodu **60 saniye** geçerli ve **tek kullanımlıktır**. Kod, ilk başarılı
> bağlantıda imha edilir; sonrasında oturum çerezi ile erişim sağlanır.

## 🧪 Testler

```powershell
python kaynak/test_security.py
```

Güvenlik testleri: yanlış/süresi dolmuş/tekrar kullanılan token, rate limit (endpoint bazlı),
brute-force pairing, request body limiti, 100 eşzamanlı bağlantı, SSE kopma/cleanup,
VPN/public adaptöre bind edilmeme, token'sız erişim, CORS/Host doğrulaması, Host IPv6/port
varyasyonları, session auth (yanlış/süresi dolmuş/revoke), expired cleanup, Event Log JSON
normalizasyonu, ağ hızı hesabı, TLS cookie/validasyon ve log redaction.

## 🔍 Sürekli Güvenlik (GitHub Actions)

- `security.yml`: Bandit, Ruff, pip-audit (CVE) ve Semgrep taramaları + **security/regression testleri**
  her push/PR'de çalışır (test hatası pipeline'ı durdurur).
- `release.yml`: `v*` tag'lerinde otomatik build alır; EXE, kaynak, SHA-256 ve SBOM'u release'e yükler.

## 🖥️ Gereksinimler

- Windows 10 / 11 (64-bit)
- NVIDIA GPU'da sıcaklık izleme için NVIDIA sürücüsü (opsiyonel)

> Arayüz şu anda yalnızca Türkçe olarak sunulmaktadır.
