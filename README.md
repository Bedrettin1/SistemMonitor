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
- **Host ve Origin doğrulaması**: yanlış Host header veya yabancı Origin reddedilir.
- `Access-Control-Allow-Origin` **yoktur**; API çapraz kaynaklı isteklere açık değildir.
- IP başına **rate limit** (60 istek/dakika), socket timeout, header limit ve **maksimum 50 eşzamanlı bağlantı**.
- SSE istemci listesi `threading.Lock()` ile korunur; her istemci için tek EventSource bağlantısı.
- Windows Event Log metinleri yalnızca **PlainText** olarak gösterilir (HTML render yok).
- `powershell.exe` yalnızca tam sistem yolundan çağrılır: `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`.
- `calistir.py` `%TEMP%` dizinini Python path'ine eklemez.

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

Güvenlik testleri: yanlış/süresi dolmuş/tekrar kullanılan token, rate limit,
100 eşzamanlı bağlantı, SSE kopma, sunucu kapanırken açık istemciler,
VPN/public adaptöre bind edilmeme, token'sız erişim, CORS/Host doğrulaması.

## 🔍 Sürekli Güvenlik (GitHub Actions)

- `security.yml`: Bandit, Ruff, pip-audit (CVE) ve Semgrep taramaları her push/PR'de çalışır.
- `release.yml`: `v*` tag'lerinde otomatik build alır; EXE, kaynak, SHA-256 ve SBOM'u release'e yükler.

## 🖥️ Gereksinimler

- Windows 10 / 11 (64-bit)
- NVIDIA GPU'da sıcaklık izleme için NVIDIA sürücüsü (opsiyonel)

> Arayüz şu anda yalnızca Türkçe olarak sunulmaktadır.
