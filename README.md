<p align="center">
  <img src="assets/Banner.png" alt="SistemMonitor Banner">
</p>

<h1 align="center">SistemMonitor</h1>

<p align="center">
  <b>Windows için gerçek zamanlı sistem izleme uygulaması</b><br>
  CPU, GPU, RAM, Disk, Ağ ve Windows hata kayıtları tek ekranda.
</p>

<p align="center">
  <a href="README.en.md">English</a> · Türkçe
</p>

---

## ✨ Özellikler

- **Gerçek zamanlı CPU kullanımı** ve sıcaklık takibi
- **Gerçek zamanlı GPU kullanımı** ve sıcaklık takibi (NVIDIA)
- **RAM** kullanım izleme
- **Disk** kullanım izleme
- **Ağ** hız izleme (indirme / yükleme)
- **Windows hata kayıtları** görüntüleyici (son 48 saat)
- **📱 Telefonla uzaktan izleme** - QR kod ile (aynı Wi-Fi / LAN ağı üzerinde çalışır)
- **Sistem tepsisi** desteği - arka planda çalışmaya devam eder
- **🌐 Türkçe / İngilizce** dil desteği (anlık geçiş)

## 📱 Telefonla Bağlanma

En sevdiğin özellik: **QR kodu telefonunla okut, bilgisayarının anlık durumunu telefondan izle.**

1. `F2` tuşuna basın veya 📱 butonuna tıklayın
2. QR kodu telefonunuzla tarayın (veya ekrandaki URL'yi girin)
3. Değerleri telefonunuzdan canlı olarak izleyin
4. Uygulama arka planda çalışmaya devam eder

> Bağlantı kodu **60 saniye** geçerli ve **tek kullanımlıktır**. Kod, ilk başarılı
> bağlantıda imha edilir; sonrasında oturum çerezi ile erişim sağlanır.

Telefon bağlantısı yalnızca **yerel ağ (LAN/Wi-Fi)** üzerinde çalışır; VPN, sanal veya
public ağlarda sunucu açılmaz.

## 🔒 Güvenlik

Telefon bağlantısı **varsayılan olarak kapalıdır**; açıldığında güvenlik temel tasarımın bir parçasıdır:

- Sunucu yalnızca **güvenli yerel ağ adaptörüne** bağlanır (private `10.x`, `172.16-31.x`, `192.168.x`). VPN, sanal ve public ağlar **reddedilir**.
- **Tek kullanımlık pairing kodu**: 60 saniye geçerli, ilk bağlantıda imha edilir.
- Pairing sonrası `HttpOnly; SameSite=Strict` **oturum çerezi** verilir; kalıcı token URL'de taşınmaz. Token karşılaştırması `secrets.compare_digest()` ile yapılır.
- Kimlik doğrulama **fail-closed**: token/session yoksa her istek 403 alır; Host ve Origin doğrulaması GET ve POST'ta aynıdır.
- **Endpoint bazlı rate limit**, **4 KB request body limiti**, güvenlik header'ları (`nosniff`, `no-referrer`, `no-store`) ve log'larda pairing kodu **maskelenmesi**.
- Süresi dolan oturumlar ve rate-limit kayıtları periyodik temizlenir.

### Taşıma güvenliği (TLS)

Telefon bağlantısı LAN içindir; varsayılan HTTP modunda trafik şifreli değildir. Opsiyonel **HTTPS** için sertifika + private key verilirse sunucu TLS ile başlar ve session cookie'ye `Secure` flag'i eklenir:

```powershell
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes
```

```python
start_phone_server(monitor, tls_cert="cert.pem", tls_key="key.pem")
```

Sertifika/key yalnızca biri verilirse veya dosya bulunamazsa sunucu HTTPS'e sessizce düşmez; kullanıcıya açık hata gösterilir.

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

## 🌐 Dil Değiştirme

- Pencerenin üst çubuğundaki **TR/EN** butonuna tıklayın veya
- Sistem tepsisi menüsü → **Dil / Language** seçin.

Arayüz ve telefon sayfası anında güncellenir; tercih kaydedilir.

## 🧪 Testler

```powershell
python kaynak/test_security.py
```

Güvenlik testleri: token doğrulama (yanlış/süresi dolmuş/tekrar kullanım), rate limit,
brute-force pairing, request body limiti, 100 eşzamanlı bağlantı, SSE kopma/cleanup,
VPN/public adaptöre bind edilmeme, CORS/Host/IPv6 doğrulaması, session auth, Event Log
JSON normalizasyonu, ağ hızı hesabı, TLS cookie/validasyon, log redaction ve dil/çeviri.

## 🔍 Sürekli Güvenlik (GitHub Actions)

- `security.yml`: Bandit, Ruff, pip-audit (CVE) ve Semgrep taramaları + **security/regression testleri**
  her push/PR'de çalışır (test hatası pipeline'ı durdurur).
- `release.yml`: `v*` tag'lerinde otomatik build alır; EXE, kaynak, SHA-256 ve SBOM'u release'e yükler.

## 🖥️ Gereksinimler

- Windows 10 / 11 (64-bit)
- NVIDIA GPU'da sıcaklık izleme için NVIDIA sürücüsü (opsiyonel)
