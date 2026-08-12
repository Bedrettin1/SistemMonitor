<p align="center">
  <img src="assets/Banner.png" alt="SistemMonitor Banner">
</p>

<h1 align="center">SistemMonitor</h1>

<p align="center">
  <b>Real-time system monitoring app for Windows</b><br>
  CPU, GPU, RAM, Disk, Network and Windows error logs on a single screen.
</p>

<p align="center">
  English · <a href="README.md">Türkçe</a>
</p>

---

## ✨ Features

- **Real-time CPU usage** and temperature monitoring
- **Real-time GPU usage** and temperature monitoring (NVIDIA)
- **RAM** usage monitoring
- **Disk** usage monitoring
- **Network** speed monitoring (download / upload)
- **Windows error log** viewer (last 48 hours)
- **📱 Remote monitoring from your phone** - via QR code (works on the same Wi-Fi / LAN)
- **System tray** support - keeps running in the background
- **🌐 Turkish / English** language support (instant switching)

## 📱 Connect from Your Phone

The best part: **scan the QR code with your phone and watch your PC's live status remotely.**

1. Press `F2` or click the 📱 button
2. Scan the QR code with your phone (or type the URL shown on screen)
3. Watch the values live on your phone
4. The app keeps running in the background

> The connection code is valid for **60 seconds** and is **single-use**. It is destroyed
> after the first successful connection; afterwards access is granted via the session cookie.

The phone connection only works on your **local network (LAN/Wi-Fi)**; it will not start on
VPN, virtual or public networks.

## 🔒 Security

The phone connection is **off by default**; when enabled, security is part of the core design:

- The server only binds to a **secure local network adapter** (private `10.x`, `172.16-31.x`, `192.168.x`). VPN, virtual and public networks are **rejected**.
- **Single-use pairing code**: valid for 60 seconds, destroyed on first successful connection.
- After pairing, an `HttpOnly; SameSite=Strict` **session cookie** is issued; no permanent token is carried in the URL. Token comparison uses `secrets.compare_digest()`.
- Authentication is **fail-closed**: any request without a token/session gets a 403; Host and Origin validation is identical for GET and POST.
- **Endpoint-based rate limiting**, **4 KB request body limit**, security headers (`nosniff`, `no-referrer`, `no-store`) and **masking** of the pairing code in logs.
- Expired sessions and rate-limit entries are cleaned up periodically.

### Transport security (TLS)

The phone connection is meant for your LAN; by default HTTP mode traffic is **not encrypted**. For optional **HTTPS**, provide a certificate + private key and the server will start with TLS, adding the `Secure` flag to the session cookie:

```powershell
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes
```

```python
start_phone_server(monitor, tls_cert="cert.pem", tls_key="key.pem")
```

If only one of the certificate/key is provided or a file is missing, the server will **not silently fall back** to HTTP; a clear error is shown.

## 🖥️ Installation

1. Download `SistemMonitor.exe` from the [Releases](/Bedrettin1/SistemMonitor/releases) page
2. Run it - no Python or other dependencies required

### Run from source

```powershell
pip install -r kaynak/requirements.txt
python kaynak/sistem_monitor.py
```

## 🎮 Shortcuts

| Key | Action |
|-----|-------|
| `F1` | Show shortcuts page |
| `F2` | Toggle phone connection |
| `F4` | Minimize to / restore from tray |
| `Esc` | Exit overlay mode |
| `Ctrl+B` | Enlarge window |
| `Ctrl+S` | Shrink window |
| `F5` | Refresh data |

## 🌐 Changing the Language

- Click the **TR/EN** button on the top bar, or
- System tray menu → **Language**.

The interface and the phone page update instantly; the preference is saved.

## 🧪 Tests

```powershell
python kaynak/test_security.py
```

Security tests: token validation (wrong/expired/replay), rate limiting, brute-force pairing,
request body limit, 100 concurrent connections, SSE disconnect/cleanup, no binding to
VPN/public adapters, CORS/Host/IPv6 validation, session auth, Event Log JSON normalization,
network speed calculation, TLS cookie/validation, log redaction and i18n/translation.

## 🔍 Continuous Security (GitHub Actions)

- `security.yml`: Bandit, Ruff, pip-audit (CVE) and Semgrep scans + **security/regression tests**
  run on every push/PR (a test failure stops the pipeline).
- `release.yml`: automatic build on `v*` tags; uploads EXE, source, SHA-256 and SBOM to the release.

## 🖥️ Requirements

- Windows 10 / 11 (64-bit)
- NVIDIA driver for GPU temperature monitoring (optional)
