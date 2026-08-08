import sys, os, psutil, time, warnings, subprocess, json, socket, ssl, threading, re, http.server, urllib.parse, io, secrets, logging
from datetime import datetime, timedelta

try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QFrame, QGridLayout, QSlider, QPushButton, QCheckBox,
        QDialog, QScrollArea, QSystemTrayIcon, QMenu, QMessageBox, QStyle
    )
    from PySide6.QtCore import QTimer, Qt, QSettings, Signal, QObject, QRunnable, QThreadPool
    from PySide6.QtGui import QColor, QPainter, QBrush, QPen, QLinearGradient, QFont, QIcon, QAction, QPixmap
except ImportError:
    raise SystemExit("PySide6 gerekli")

try:
    import wmi
    WMI = True
except Exception:
    WMI = False

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

GPU = None
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import pynvml
        pynvml.nvmlInit()
        if pynvml.nvmlDeviceGetCount() > 0:
            GPU = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    GPU = None

PHONE_SERVER_PORT = 8080
PHONE_SESSION_TTL = 24 * 3600
PHONE_PAIR_TTL = 60
PHONE_MAX_CONNECTIONS = 50
PHONE_RATE_LIMIT_PER_MIN = 60
PHONE_PAIR_RATE_LIMIT_PER_MIN = 10
PHONE_RATE_WINDOW = 60
PHONE_SOCKET_TIMEOUT = 5.0
PHONE_BODY_MAX = 4096
PHONE_CLEANUP_INTERVAL = 60
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# Endpoint bazlı rate limit kovaları. Pairing daha sıkıdır.
PHONE_RATE_BUCKETS = {
    "pairing": PHONE_PAIR_RATE_LIMIT_PER_MIN,
    "page": PHONE_RATE_LIMIT_PER_MIN,
    "metrics": PHONE_RATE_LIMIT_PER_MIN,
    "events": PHONE_RATE_LIMIT_PER_MIN,
}

_phone_server = None
_phone_server_thread = None
_phone_stop_event = threading.Event()
_phone_server_lock = threading.RLock()
_phone_logger = logging.getLogger("sistemmonitor.phone")
if not _phone_logger.handlers:
    _phone_logger.setLevel(logging.INFO)
    _lh = logging.StreamHandler()
    _lh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _phone_logger.addHandler(_lh)

def _safe_log(msg):
    try:
        _phone_logger.info(msg)
    except Exception:
        pass

class PhoneMetricsProvider:
    def __init__(self, monitor):
        self.monitor = monitor
    
    def get_metrics(self):
        m = getattr(self.monitor, '_phone_metrics', None)
        if m is not None:
            return m
        return {
            "cpu": {"percent": 0.0, "temp": None},
            "gpu": {"percent": None, "temp": None},
            "ram": {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0},
            "disk": {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0},
            "network": {"down": 0, "up": 0},
            "uptime": "",
            "errors": {"count": "0", "msg": None},
            "timestamp": ""
        }

_sessions = {}
_sessions_lock = threading.Lock()
_pairing_code = None
_pairing_expiry = 0.0
_pairing_lock = threading.Lock()
# Rate store anahtarı: (ip, bucket)
_rate_store = {}
_rate_lock = threading.Lock()
_cleanup_stop = threading.Event()
_cleanup_thread = None

def _create_session():
    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[sid] = time.time() + PHONE_SESSION_TTL
    return sid

def _valid_session(sid):
    if not sid:
        return False
    with _sessions_lock:
        exp = _sessions.get(sid)
        if exp is None:
            return False
        if time.time() > exp:
            del _sessions[sid]
            return False
        return True

def _revoke_all_sessions():
    with _sessions_lock:
        _sessions.clear()

def _cleanup_expired():
    """Expired session ve rate kayitlarini siler. Thread-safe. Gecen sureyi dondurmez."""
    now = time.time()
    removed_sessions = 0
    with _sessions_lock:
        expired = [sid for sid, exp in list(_sessions.items()) if exp <= now]
        for sid in expired:
            del _sessions[sid]
        removed_sessions = len(expired)
    with _rate_lock:
        expired_keys = [k for k, ts in list(_rate_store.items())
                        if not any(now - t < PHONE_RATE_WINDOW for t in ts)]
        for k in expired_keys:
            del _rate_store[k]
    return removed_sessions

def _start_cleanup_thread():
    global _cleanup_thread
    with _phone_server_lock:
        if _cleanup_thread and _cleanup_thread.is_alive():
            return
        _cleanup_stop.clear()
        def _loop():
            while not _cleanup_stop.wait(PHONE_CLEANUP_INTERVAL):
                try:
                    _cleanup_expired()
                except Exception:
                    pass
        _cleanup_thread = threading.Thread(target=_loop, daemon=True)
        _cleanup_thread.start()

def _stop_cleanup_thread():
    global _cleanup_thread
    _cleanup_stop.set()
    if _cleanup_thread:
        try:
            _cleanup_thread.join(timeout=5)
        except Exception:
            pass
        _cleanup_thread = None

def _rate_limited(ip, bucket="page"):
    limit = PHONE_RATE_BUCKETS.get(bucket, PHONE_RATE_LIMIT_PER_MIN)
    key = (ip, bucket)
    now = time.time()
    with _rate_lock:
        ts = _rate_store.setdefault(key, [])
        ts = [t for t in ts if now - t < PHONE_RATE_WINDOW]
        if len(ts) >= limit:
            _rate_store[key] = ts
            return True
        ts.append(now)
        _rate_store[key] = ts
        return False

_phone_tls_active = False

def _validate_tls_paths(cert, key):
    """TLS cert/key yapilandirmasini dogrular. (ok, hata_msg, scheme) dondurur.
    Her ikisi de yoksa HTTP modu. Tek tarafli veya eksik dosya -> hata (sessiz dusme yok)."""
    if not cert and not key:
        return True, None, "http"
    if not cert or not key:
        return False, "TLS: sertifika ve private key birlikte verilmelidir.", "http"
    if not os.path.isfile(cert):
        return False, f"TLS: sertifika dosyasi bulunamadi: {cert}", "http"
    if not os.path.isfile(key):
        return False, f"TLS: private key dosyasi bulunamadi: {key}", "http"
    return True, None, "https"

def _create_tls_context(cert, key):
    """Dogrulanmis cert/key icin TLS server context uretir."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx

def _build_session_cookie(sid, secure=False):
    """Session cookie. HTTPS modunda Secure flag eklenir."""
    parts = ["HttpOnly", "SameSite=Strict", "Path=/"]
    if secure:
        parts.append("Secure")
    return f"sm_session={sid}; " + "; ".join(parts)

class _PhoneHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    active_connections = 0
    conn_lock = threading.Lock()
    tls_active = False

    def process_request(self, request, client_address):
        with self.conn_lock:
            if self.active_connections >= PHONE_MAX_CONNECTIONS:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                    request.close()
                except OSError:
                    pass
                _safe_log("HTTP: max baglanti sinirina ulasildi, istemci reddedildi")
                return
            self.active_connections += 1
        try:
            super().process_request(request, client_address)
        finally:
            with self.conn_lock:
                self.active_connections -= 1

class PhoneRequestHandler(http.server.BaseHTTPRequestHandler):
    provider = None
    clients = []
    clients_lock = threading.Lock()
    server_version = "SistemMonitor"
    timeout = PHONE_SOCKET_TIMEOUT
    protocol_version = "HTTP/1.0"

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _server_host(self):
        return getattr(self.server, "server_address", ("127.0.0.1", PHONE_SERVER_PORT))[0]

    def _server_port(self):
        return getattr(self.server, "server_address", ("127.0.0.1", PHONE_SERVER_PORT))[1]

    def _server_tls(self):
        return getattr(self.server, "tls_active", False) or _phone_tls_active

    def _host_netloc(self, host_header):
        """Host/Origin degerini (hostname, port) olarak guvenli ayristirir.
        IPv6 literal ([::1]:port), port varyasyonlari ve enjeksiyon guvenli. Basarisizsa None."""
        host_header = (host_header or "").strip()
        if not host_header:
            return None
        if host_header.startswith("["):
            end = host_header.find("]")
            if end == -1:
                return None
            hostname = host_header[1:end]
            rest = host_header[end + 1:]
            if not rest:
                return (hostname, None)
            if not rest.startswith(":"):
                return None
            try:
                return (hostname, int(rest[1:]))
            except ValueError:
                return None
        if host_header.count(":") > 1:
            # Kosesiz IPv6 veya enjeksiyon girisimi -> guvenli red
            return None
        if ":" in host_header:
            hostname, _, port_str = host_header.rpartition(":")
            try:
                return (hostname, int(port_str))
            except ValueError:
                return None
        return (host_header, None)

    def _host_ok(self):
        parsed = self._host_netloc(self.headers.get("Host"))
        if parsed is None:
            return False
        hostname, port = parsed
        bind_ip = self._server_host()
        if port is not None and port != self._server_port():
            return False
        return hostname == bind_ip or hostname in ("127.0.0.1", "localhost", "::1")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            # Tarayici disi istemciler Origin gondermez; mevcut politika kabul eder.
            return True
        scheme, sep, rest = origin.partition("://")
        if not sep or not rest or scheme not in ("http", "https"):
            return False
        if scheme == "https" and not self._server_tls():
            return False
        if scheme == "http" and self._server_tls():
            return False
        parsed = self._host_netloc(rest)
        if parsed is None:
            return False
        hostname, port = parsed
        bind_ip = self._server_host()
        if port is None:
            port = 443 if scheme == "https" else 80
        if port != self._server_port():
            return False
        return hostname == bind_ip or hostname in ("127.0.0.1", "localhost", "::1")

    def _request_valid(self):
        """Host/Origin dogrulamasini tek noktada uygular. Gecersizse 403 doner, False dondurur."""
        if not self._host_ok():
            _safe_log(f"HTTP: gecersiz Host header {self.headers.get('Host')!r}")
            self._deny(403, "Gecersiz host.")
            return False
        if not self._origin_ok():
            _safe_log(f"HTTP: izin verilmeyen Origin {self.headers.get('Origin')!r}")
            self._deny(403, "Gecersiz origin.")
            return False
        return True

    def _session_from_cookie(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("sm_session="):
                return part[len("sm_session="):]
        return None

    def _deny(self, code=403, msg=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        body = (msg or "Yetkisiz erisim.").encode("utf-8")
        self.wfile.write(body)

    def _read_body(self):
        """Request body'yi sinirli okur. (body_bytes, hata_kodu_or_None) dondurur.
        413: cok buyuk, 400: gecersiz Content-Length."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, 400
        if length < 0:
            return None, 400
        if length > PHONE_BODY_MAX:
            return None, 413
        if length == 0:
            return b"", None
        try:
            return self.rfile.read(length), None
        except OSError:
            return None, 400

    def _redact_path(self, path):
        """Query'deki pairing kodu (c) dahil gizli anahtarlari maskele."""
        try:
            parsed = urllib.parse.urlparse(path)
            if not parsed.query:
                return path
            q = urllib.parse.parse_qs(parsed.query)
            q = {k: (["***"] if k == "c" else v) for k, v in q.items()}
            new_query = urllib.parse.urlencode(q, doseq=True)
            return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                                            parsed.params, new_query, parsed.fragment))
        except Exception:
            return path

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def log_message(self, fmt, *args):
        # fmt % args içindeki requestline/path'te ?c= (pairing kodu) maskele
        msg = fmt % args
        msg = re.sub(r"([?&]c=)[^&\s]*", r"\1***", msg)
        token = ""
        if "c=" in self.path:
            token = " [pair]"
        _safe_log(f"HTTP {self.command} {self._redact_path(self.path)} -> {msg}{token} ip={self._client_ip()}")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/pair":
            self._deny(404, "Bulunamadi.")
            return
        if not self._request_valid():
            return
        self._handle_pair()

    def _handle_pair(self):
        global _pairing_code, _pairing_expiry
        ip = self._client_ip()
        if _rate_limited(ip, "pairing"):
            _safe_log("PAIR: rate limit asildi")
            self._deny(429, "Cok fazla istek.")
            return
        body, err = self._read_body()
        if err == 413:
            _safe_log("PAIR: body limit asildi")
            self._deny(413, "Istek cok buyuk.")
            return
        if err == 400 or body is None:
            _safe_log("PAIR: gecersiz Content-Length")
            self._deny(400, "Gecersiz istek.")
            return
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict):
                raise ValueError("body dict degil")
            code = str(data.get("code", ""))
        except (ValueError, AttributeError):
            _safe_log("PAIR: JSON cozulemedi")
            self._deny(400, "Gecersiz istek.")
            return
        with _pairing_lock:
            ok = (_pairing_code is not None
                  and code
                  and secrets.compare_digest(_pairing_code.encode(), code.encode())
                  and time.time() <= _pairing_expiry)
            if not ok:
                _safe_log("PAIR: gecersiz/suresi dolmus/tekrar kullanilan kod reddedildi")
                self._deny(403, "Gecersiz veya suresi dolmus baglanti kodu.")
                return
            _pairing_code = None
            _pairing_expiry = 0.0
        sid = _create_session()
        _safe_log("PAIR: basarili, session olusturuldu")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", _build_session_cookie(sid, secure=self._server_tls()))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def do_GET(self):
        ip = self._client_ip()
        parsed = urllib.parse.urlparse(self.path)
        if not self._request_valid():
            return
        if parsed.path == "/":
            if _rate_limited(ip, "page"):
                self._deny(429, "Cok fazla istek.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_security_headers()
            if not self._server_tls():
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
                                 "style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
            self.end_headers()
            html = PHONE_HTML.replace("<!--PHONE_TLS_WARN-->",
                                      "" if self._server_tls() else
                                      '<div class="tls-warn">Trafik sifresiz (HTTP). Guvenli baglanti icin TLS yapilandirin.</div>')
            self.wfile.write(html.encode("utf-8"))
            return
        sid = self._session_from_cookie()
        if not _valid_session(sid):
            _safe_log(f"GET: oturum yok/gecersiz -> 403 {parsed.path}")
            self._deny(403, "Gecersiz oturum.")
            return
        bucket = "metrics" if parsed.path == "/metrics" else "events" if parsed.path == "/events" else "page"
        if _rate_limited(ip, bucket):
            self._deny(429, "Cok fazla istek.")
            return
        if parsed.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_security_headers()
            self.end_headers()
            if self.provider:
                data = json.dumps(self.provider.get_metrics())
                self.wfile.write(data.encode("utf-8"))
            else:
                self.wfile.write(b"{}")
        elif parsed.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._send_security_headers()
            self.end_headers()
            with self.clients_lock:
                self.clients.append(self)
            try:
                while not _phone_stop_event.is_set():
                    if _phone_stop_event.wait(2):
                        break
                    if self.provider:
                        data = json.dumps(self.provider.get_metrics())
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass
                with self.clients_lock:
                    if self in self.clients:
                        self.clients.remove(self)
        else:
            self._deny(404, "Bulunamadi.")

PHONE_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Sistem Monitoru - Telefon</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #cdd6f4; min-height: 100vh; padding: 16px; }
        .container { max-width: 480px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #313244; }
        .title { font-size: 18px; font-weight: 600; color: #cba6f7; }
        .status { font-size: 12px; padding: 4px 8px; border-radius: 12px; background: #a6e3a1; color: #1e1e2e; font-weight: 500; }
        .status.disconnected { background: #f38ba8; color: #1e1e2e; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .card { background: #181825; border: 1px solid #313244; border-radius: 12px; padding: 16px; }
        .card.full { grid-column: span 2; }
        .card-title { font-size: 11px; color: #a6adc8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
        .card-value { font-size: 24px; font-weight: 700; line-height: 1.2; }
        .card-sub { font-size: 11px; color: #6c7086; margin-top: 4px; }
        .bar { height: 6px; background: #313244; border-radius: 3px; margin-top: 10px; overflow: hidden; }
        .bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
        .temp-normal { color: #a6e3a1; }
        .temp-warm { color: #f9e2af; }
        .temp-hot { color: #fab387; }
        .temp-critical { color: #f38ba8; }
        .metric-row { display: flex; justify-content: space-between; align-items: baseline; }
        .metric-label { font-size: 11px; color: #6c7086; }
        .metric-value { font-size: 14px; font-weight: 600; }
        .errors-card { border-color: #f38ba8; }
        .errors-card .card-title { color: #f38ba8; }
        .timestamp { text-align: center; font-size: 11px; color: #6c7086; margin-top: 16px; }
        .tls-warn { background: #3b2b1f; border: 1px solid #f9e2af; color: #f9e2af; border-radius: 8px; font-size: 11px; padding: 8px; margin-bottom: 12px; text-align: center; }
        @media (max-width: 360px) {
            .card-value { font-size: 20px; }
            .grid { gap: 8px; }
            .card { padding: 12px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <!--PHONE_TLS_WARN-->
        <div class="header">
            <div class="title">Sistem Monitoru</div>
            <div class="status" id="status">Bağlanıyor...</div>
        </div>
        <div class="grid">
            <div class="card">
                <div class="card-title">CPU Kullanım</div>
                <div class="card-value" id="cpu-val">--%</div>
                <div class="bar"><div class="bar-fill" id="cpu-bar" style="width: 0%; background: #a6e3a1;"></div></div>
            </div>
            <div class="card">
                <div class="card-title">CPU Sıcaklık</div>
                <div class="card-value temp-normal" id="cpu-temp">--°C</div>
                <div class="bar"><div class="bar-fill" id="cpu-temp-bar" style="width: 0%;"></div></div>
            </div>
            <div class="card">
                <div class="card-title">GPU Kullanım</div>
                <div class="card-value" id="gpu-val">--%</div>
                <div class="bar"><div class="bar-fill" id="gpu-bar" style="width: 0%; background: #cba6f7;"></div></div>
            </div>
            <div class="card">
                <div class="card-title">GPU Sıcaklık</div>
                <div class="card-value temp-normal" id="gpu-temp">--°C</div>
                <div class="bar"><div class="bar-fill" id="gpu-temp-bar" style="width: 0%;"></div></div>
            </div>
            <div class="card full">
                <div class="card-title">RAM</div>
                <div class="metric-row">
                    <span class="metric-value" id="ram-val">--/-- GB</span>
                    <span class="metric-label">%<span id="ram-pct">--</span></span>
                </div>
                <div class="bar"><div class="bar-fill" id="ram-bar" style="width: 0%; background: #89b4fa;"></div></div>
            </div>
            <div class="card full">
                <div class="card-title">Disk</div>
                <div class="metric-row">
                    <span class="metric-value" id="disk-val">--/-- GB</span>
                    <span class="metric-label">%<span id="disk-pct">--</span></span>
                </div>
                <div class="bar"><div class="bar-fill" id="disk-bar" style="width: 0%; background: #f9e2af;"></div></div>
            </div>
            <div class="card full">
                <div class="card-title">Ağ</div>
                <div class="metric-row">
                    <span class="metric-value" id="net-down">↓ --</span>
                    <span class="metric-value" id="net-up">↑ --</span>
                </div>
            </div>
            <div class="card full">
                <div class="card-title">Aktif Süre</div>
                <div class="card-value" id="uptime" style="font-size: 16px;">--</div>
            </div>
            <div class="card full errors-card" id="errors-card" style="display: none;">
                <div class="card-title">Sistem Hataları</div>
                <div class="card-value" style="font-size: 14px; color: #f38ba8;" id="errors-count">-- Hata</div>
                <div class="card-sub" id="errors-msg"></div>
            </div>
        </div>
        <div class="timestamp">Son güncelleme: <span id="timestamp">--:--:--</span></div>
    </div>
    <script>
        const statusEl = document.getElementById('status');
        let eventSource = null;
        let sseTimer = null;
        let lastNet = { down: 0, up: 0, time: Date.now() };
        let netInit = false;
        
        function fmtBytes(bytes) {
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let i = 0;
            while (bytes >= 1024 && i < units.length - 1) {
                bytes /= 1024;
                i++;
            }
            return bytes.toFixed(1) + units[i];
        }
        
        function fmtSpeed(bytesPerSec) {
            return fmtBytes(bytesPerSec) + '/s';
        }
        
        function tempClass(temp) {
            if (temp === null || temp === undefined) return 'temp-normal';
            if (temp < 60) return 'temp-normal';
            if (temp < 75) return 'temp-warm';
            if (temp < 85) return 'temp-hot';
            return 'temp-critical';
        }
        
        function updateUI(data) {
            document.getElementById('cpu-val').textContent = data.cpu.percent.toFixed(1) + '%';
            document.getElementById('cpu-bar').style.width = data.cpu.percent + '%';
            
            if (data.cpu.temp !== null) {
                const cpuTempEl = document.getElementById('cpu-temp');
                cpuTempEl.textContent = data.cpu.temp.toFixed(1) + '°C';
                cpuTempEl.className = 'card-value ' + tempClass(data.cpu.temp);
                document.getElementById('cpu-temp-bar').style.width = Math.min(100, data.cpu.temp / 0.9) + '%';
                document.getElementById('cpu-temp-bar').style.background = 
                    data.cpu.temp < 60 ? '#a6e3a1' : data.cpu.temp < 75 ? '#f9e2af' : data.cpu.temp < 85 ? '#fab387' : '#f38ba8';
            } else {
                document.getElementById('cpu-temp').textContent = 'N/A';
                document.getElementById('cpu-temp').className = 'card-value temp-normal';
            }
            
            if (data.gpu.percent !== null) {
                document.getElementById('gpu-val').textContent = data.gpu.percent + '%';
                document.getElementById('gpu-bar').style.width = data.gpu.percent + '%';
            } else {
                document.getElementById('gpu-val').textContent = 'N/A';
            }
            
            if (data.gpu.temp !== null) {
                const gpuTempEl = document.getElementById('gpu-temp');
                gpuTempEl.textContent = data.gpu.temp.toFixed(0) + '°C';
                gpuTempEl.className = 'card-value ' + tempClass(data.gpu.temp);
                document.getElementById('gpu-temp-bar').style.width = Math.min(100, data.gpu.temp / 0.9) + '%';
                document.getElementById('gpu-temp-bar').style.background = 
                    data.gpu.temp < 60 ? '#a6e3a1' : data.gpu.temp < 75 ? '#f9e2af' : data.gpu.temp < 85 ? '#fab387' : '#f38ba8';
            } else {
                document.getElementById('gpu-temp').textContent = 'N/A';
                document.getElementById('gpu-temp').className = 'card-value temp-normal';
            }
            
            document.getElementById('ram-val').textContent = data.ram.used_gb + '/' + data.ram.total_gb + ' GB';
            document.getElementById('ram-pct').textContent = data.ram.percent.toFixed(1);
            document.getElementById('ram-bar').style.width = data.ram.percent + '%';
            
            document.getElementById('disk-val').textContent = data.disk.used_gb + '/' + data.disk.total_gb + ' GB';
            document.getElementById('disk-pct').textContent = data.disk.percent.toFixed(1);
            document.getElementById('disk-bar').style.width = data.disk.percent + '%';
            
            const now = Date.now();
            if (!netInit) {
                lastNet = { down: data.network.down, up: data.network.up, time: now };
                netInit = true;
                document.getElementById('net-down').textContent = '↓ --';
                document.getElementById('net-up').textContent = '↑ --';
            } else {
                const elapsed = Math.max(1000, now - lastNet.time);
                const downSpeed = Math.max(0, data.network.down - lastNet.down) / (elapsed / 1000);
                const upSpeed = Math.max(0, data.network.up - lastNet.up) / (elapsed / 1000);
                document.getElementById('net-down').textContent = '↓ ' + fmtSpeed(downSpeed);
                document.getElementById('net-up').textContent = '↑ ' + fmtSpeed(upSpeed);
                lastNet = { down: data.network.down, up: data.network.up, time: now };
            }
            
            document.getElementById('uptime').textContent = data.uptime;
            
            if (data.errors.count && data.errors.count !== "0") {
                document.getElementById('errors-card').style.display = 'block';
                document.getElementById('errors-count').textContent = data.errors.count + ' Kritik Hata';
                document.getElementById('errors-msg').textContent = data.errors.msg ? data.errors.msg + '...' : '';
            } else {
                document.getElementById('errors-card').style.display = 'none';
            }
            
            document.getElementById('timestamp').textContent = data.timestamp;
            statusEl.textContent = 'Bağlı';
            statusEl.className = 'status';
        }
        
        function connectSSE() {
            if (sseTimer) { clearTimeout(sseTimer); sseTimer = null; }
            if (eventSource) { eventSource.close(); eventSource = null; }
            const es = new EventSource('/events');
            eventSource = es;
            es.onmessage = function(e) {
                try {
                    const data = JSON.parse(e.data);
                    updateUI(data);
                } catch (err) {
                    console.error('Parse error:', err);
                }
            };
            es.onerror = function() {
                if (eventSource === es) {
                    es.close();
                    eventSource = null;
                    statusEl.textContent = 'Bağlantı koptu, yeniden deneniyor...';
                    statusEl.className = 'status disconnected';
                    sseTimer = setTimeout(connectSSE, 3000);
                }
            };
        }
        
        async function fetchMetrics() {
            try {
                const res = await fetch('/metrics', { credentials: 'same-origin' });
                if (res.status === 403) {
                    statusEl.textContent = 'Oturum süresi doldu';
                    statusEl.className = 'status disconnected';
                    if (eventSource) { eventSource.close(); eventSource = null; }
                    return;
                }
                const data = await res.json();
                updateUI(data);
            } catch (err) {
                statusEl.textContent = 'Hata, yeniden deneniyor...';
                statusEl.className = 'status disconnected';
            }
        }
        
        async function startMonitoring() {
            if (typeof EventSource !== 'undefined') {
                connectSSE();
            } else {
                setInterval(fetchMetrics, 2000);
                fetchMetrics();
            }
        }
        
        async function tryPair(code) {
            if (!code) return false;
            try {
                const res = await fetch('/api/pair', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ code: code })
                });
                if (res.ok) {
                    history.replaceState(null, '', window.location.pathname);
                    startMonitoring();
                    return true;
                }
                statusEl.textContent = 'Bağlantı kodu hatalı';
                statusEl.className = 'status disconnected';
            } catch (err) {
                statusEl.textContent = 'Bağlantı hatası';
                statusEl.className = 'status disconnected';
            }
            return false;
        }
        
        window.addEventListener('load', function() {
            const params = new URLSearchParams(window.location.search);
            const code = params.get('c');
            tryPair(code);
        });
        
        document.addEventListener('visibilitychange', function() {
            if (document.visibilityState === 'visible' && eventSource) {
                eventSource.close();
                eventSource = null;
                connectSSE();
            }
        });
    </script>
</body>
</html>
"""

def _list_adapters():
    """Güvenilir yerel ağ adaptörlerini listeler. VPN/public/sanal adaptörleri hariç tutar."""
    results = []
    skip_names = ("virtual", "vpn", "tun", "tap", "docker", "vmware", "virtualbox",
                  "hyper-v", "vethernet", "wsl", "loopback", "bluetooth", "hamachi",
                  "zerotier", "tailscale", "wireguard", "openvpn", "vEthernet",
                  "Default Switch", "Ethernet 2", "isatap", "teredo")
    try:
        ifaces = psutil.net_if_addrs()
    except Exception:
        return results
    for name, addrs in ifaces.items():
        low = name.lower()
        if any(k in low for k in skip_names):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and a.address:
                ip = a.address
                if ip.startswith("169.254."):
                    continue
                if not ip.startswith(("10.", "192.168.", "172.")):
                    continue
                try:
                    if ip.startswith("172."):
                        second = int(ip.split(".")[1])
                        if not (16 <= second <= 31):
                            continue
                except ValueError:
                    continue
                results.append((name, ip))
    return results

def get_local_ip():
    adapters = _list_adapters()
    if adapters:
        return adapters[0][1]
    return "127.0.0.1"

def start_phone_server(monitor, tls_cert=None, tls_key=None):
    global _phone_server, _phone_server_thread, _pairing_code, _pairing_expiry, _phone_tls_active
    ok_cfg, tls_err, scheme = _validate_tls_paths(tls_cert, tls_key)
    if not ok_cfg:
        _safe_log(f"PHONE: {tls_err}")
        return False, tls_err, None
    with _phone_server_lock:
        if _phone_server:
            return True, get_local_ip(), _pairing_code
        adapters = _list_adapters()
        if not adapters:
            _safe_log("PHONE: guvenli adaptor bulunamadi, sunucu acilmadi")
            return False, "Güvenli yerel ağ adaptörü bulunamadı (VPN/sanal/public ağlar desteklenmez).", None
        bind_ip = adapters[0][1]
        bind_name = adapters[0][0]
        try:
            _phone_stop_event.clear()
            PhoneRequestHandler.provider = PhoneMetricsProvider(monitor)
            _pairing_code = secrets.token_urlsafe(6)
            _pairing_expiry = time.time() + PHONE_PAIR_TTL
            server = _PhoneHTTPServer((bind_ip, PHONE_SERVER_PORT), PhoneRequestHandler)
            if scheme == "https":
                ctx = _create_tls_context(tls_cert, tls_key)
                server.socket = ctx.wrap_socket(server.socket, server_side=True)
                server.tls_active = True
            _phone_server = server
            _phone_tls_active = server.tls_active
            _start_cleanup_thread()
            _phone_server_thread = threading.Thread(target=_phone_server.serve_forever, daemon=True)
            _phone_server_thread.start()
            _safe_log(f"PHONE: sunucu {scheme}://{bind_ip}:{PHONE_SERVER_PORT} ({bind_name}) üzerinde baslatildi")
            return True, bind_ip, _pairing_code
        except Exception as e:
            _phone_server = None
            _phone_server_thread = None
            _phone_tls_active = False
            _stop_cleanup_thread()
            _safe_log(f"PHONE: sunucu baslatilamadi: {e}")
            return False, f"Telefon sunucusu başlatılamadı: {e}", None

def stop_phone_server():
    global _phone_server, _phone_server_thread, _pairing_code, _pairing_expiry, _phone_tls_active
    with _phone_server_lock:
        if _phone_server:
            _phone_stop_event.set()
            with PhoneRequestHandler.clients_lock:
                for c in list(PhoneRequestHandler.clients):
                    try:
                        c.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        c.connection.close()
                    except OSError:
                        pass
                PhoneRequestHandler.clients.clear()
            PhoneRequestHandler.provider = None
            with _pairing_lock:
                _pairing_code = None
                _pairing_expiry = 0.0
            _revoke_all_sessions()
            with _rate_lock:
                _rate_store.clear()
            _stop_cleanup_thread()
            try:
                _phone_server.shutdown()
            except Exception:
                pass
            try:
                _phone_server.server_close()
            except Exception:
                pass
            _phone_server = None
            _phone_server_thread = None
            _phone_tls_active = False
            _safe_log("PHONE: sunucu durduruldu")
    return True

def tcolor(t):
    if t is None: return "#cdd6f4"
    if t < 60: return "#a6e3a1"
    if t < 75: return "#f9e2af"
    if t < 85: return "#fab387"
    return "#f38ba8"

class Bar(QFrame):
    def __init__(self, c="#00c853"):
        super().__init__()
        self.v = 0; self.c = c
        self.setMinimumHeight(4); self.setMaximumHeight(4)

    def set(self, v, c=None):
        self.v = max(0, min(100, v))
        if c: self.c = c
        self.update()

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width(); h = self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(25,25,30,180)))
        p.drawRoundedRect(0,0,w,h,2,2)
        fw = int(w * self.v / 100)
        if fw > 0:
            g = QLinearGradient(0,0,fw,0)
            c = QColor(self.c)
            g.setColorAt(0,c); g.setColorAt(1,c.lighter(130))
            p.setBrush(QBrush(g)); p.drawRoundedRect(0,0,fw,h,2,2)
        p.end()

class Card(QFrame):
    def __init__(self, title, color="#fff"):
        super().__init__()
        self.c = color; self.ca = 180; self._handler = None
        lo = QVBoxLayout(self); lo.setContentsMargins(8,5,8,5); lo.setSpacing(2)
        self.t = QLabel(title)
        self.t.setStyleSheet("color:#a6adc8; font-size:10px; background:transparent;")
        lo.addWidget(self.t)
        self.v = QLabel("---")
        self.v.setStyleSheet(f"color:{color}; font-size:18px; font-weight:bold; background:transparent;")
        lo.addWidget(self.v)
        self.b = Bar(color); lo.addWidget(self.b)

    def set_clickable(self, handler):
        self._handler = handler
        self.v.mousePressEvent = lambda e: handler() if e.button() == Qt.MouseButton.LeftButton else None

    def set_compact(self):
        self.t.setStyleSheet("color:#a6adc8; font-size:9px; background:transparent;")
        self.v.setStyleSheet(f"color:{self.c}; font-size:15px; font-weight:bold; background:transparent;")

    def set_normal(self):
        self.t.setStyleSheet("color:#a6adc8; font-size:10px; background:transparent;")
        self.v.setStyleSheet(f"color:{self.c}; font-size:18px; font-weight:bold; background:transparent;")

    def upd(self, txt, pv=None, pc=None):
        self.v.setText(txt)
        if pv is not None: self.b.set(pv, pc or self.c); self.b.show()
        else: self.b.hide()

    def paintEvent(self, e):
        if self.ca > 0:
            p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = self.rect()
            p.setBrush(QBrush(QColor(14,14,24,self.ca)))
            p.setPen(QPen(QColor(49,50,68,self.ca), 1))
            p.drawRoundedRect(r.adjusted(0,0,-1,-1), 6, 6)
            p.end()
        super().paintEvent(e)

class Monitor(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cmp = False; self._wa = 200; self._sz = 0
        self._phone_active = False
        self._phone_dialog = None
        self.setWindowTitle("Sistem Monitoru")
        icon_path = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(__file__) if '__file__' in dir() else '.'), "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setAttribute(Qt.WA_TranslucentBackground)

        cw = QWidget(); self.setCentralWidget(cw)
        self.ml = QVBoxLayout(cw); self.ml.setContentsMargins(8,6,8,6); self.ml.setSpacing(5)

        tb = QHBoxLayout(); tb.setSpacing(5)
        self.hdr = QLabel("Sistem Monitoru")
        self.hdr.setStyleSheet("color:#cba6f7; font-size:13px; font-weight:bold; background:transparent;")
        tb.addWidget(self.hdr)
        self.chdr = QLabel("SM")
        self.chdr.setStyleSheet("color:#cba6f7; font-size:11px; font-weight:bold; background:transparent;")
        self.chdr.setAlignment(Qt.AlignCenter)
        tb.addWidget(self.chdr)
        tb.addStretch()
        self._phone_btn = QPushButton("📱")
        self._phone_btn.setFixedSize(28,28)
        self._phone_btn.setToolTip("Telefonla Bağla (F2)")
        self._phone_btn.setStyleSheet("QPushButton{background:rgba(60,60,80,200);border-radius:14px;color:#cdd6f4;font-size:14px;border:none;} QPushButton:hover{background:rgba(80,80,100,220);} QPushButton:checked{background:#cba6f7;color:#1e1e2e;}")
        self._phone_btn.setCheckable(True)
        self._phone_btn.clicked.connect(self._toggle_phone)
        tb.addWidget(self._phone_btn)
        self._help_btn = QPushButton("?")
        self._help_btn.setFixedSize(18,18)
        self._help_btn.setToolTip("Kısayollar (F1)")
        self._help_btn.setStyleSheet("QPushButton{background:rgba(60,60,80,200);border-radius:9px;color:#cdd6f4;font-weight:bold;border:none;} QPushButton:hover{background:rgba(80,80,100,220);}")
        self._help_btn.clicked.connect(self._show_help)
        tb.addWidget(self._help_btn)
        self._bm = QPushButton("-"); self._bm.setFixedSize(18,18)
        self._bm.setStyleSheet("QPushButton{background:rgba(60,60,80,200);border-radius:9px;color:#cdd6f4;font-weight:bold;border:none;} QPushButton:hover{background:rgba(80,80,100,220);}")
        self._bm.clicked.connect(lambda: self._rsz(-1))
        tb.addWidget(self._bm)
        self._bp = QPushButton("+"); self._bp.setFixedSize(18,18)
        self._bp.setStyleSheet("QPushButton{background:rgba(60,60,80,200);border-radius:9px;color:#cdd6f4;font-weight:bold;border:none;} QPushButton:hover{background:rgba(80,80,100,220);}")
        self._bp.clicked.connect(lambda: self._rsz(1))
        tb.addWidget(self._bp)
        self.sl = QSlider(Qt.Horizontal)
        self.sl.setRange(0,100); self.sl.setValue(80); self.sl.setFixedWidth(60)
        self.sl.setStyleSheet("QSlider::groove:horizontal{background:rgba(60,60,80,180);height:4px;border-radius:2px;} QSlider::handle:horizontal{background:#cba6f7;width:14px;height:14px;margin:-5px 0;border-radius:7px;} QSlider::handle:horizontal:hover{background:#d8b9ff;} QSlider::sub-page:horizontal{background:#cba6f7;border-radius:2px;}")
        self.sl.valueChanged.connect(self._opa)
        tb.addWidget(self.sl)
        self.ml.addLayout(tb)

        self.gr = QGridLayout(); self.gr.setSpacing(5); self.ml.addLayout(self.gr)
        self._cards = []
        def ac(t, c): cd = Card(t, c); self._cards.append(cd); return cd
        self.gr.addWidget(ac("CPU Kullanim","#a6e3a1"),0,0)
        self.gr.addWidget(ac("CPU Sicaklik","#fab387"),0,1)
        self.gr.addWidget(ac("GPU Kullanim","#cba6f7"),1,0)
        self.gr.addWidget(ac("GPU Sicaklik","#f38ba8"),1,1)
        self.gr.addWidget(ac("RAM","#89b4fa"),2,0)
        self.gr.addWidget(ac("Disk","#f9e2af"),2,1)
        self.gr.addWidget(ac("Ag","#94e2d5"),3,0,1,2)
        self.gr.addWidget(ac("Aktif Sure","#f5c2e7"),4,0,1,2)
        self._elc = ac("Windows Hata (tikla ac)","#f38ba8")
        self.gr.addWidget(self._elc,5,0,1,2)
        self._elc.v.setWordWrap(True)
        self._elc.set_clickable(self._open_event_log)

        self._np = psutil.net_io_counters(); self._nt = time.time()
        self._last_ev = 0
        self._phone_metrics = None
        self.tmr = QTimer(); self.tmr.timeout.connect(self._tick); self.tmr.start(2000)
        self._tick()
        
        self._setup_tray()
        self._set_mode(False)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F1: self._show_help()
        elif e.key() == Qt.Key_F4: self._set_mode(not self._cmp)
        elif e.key() == Qt.Key_Escape and self._cmp: self._set_mode(False)
        elif e.key() == Qt.Key_F5: self._tick()
        elif e.key() == Qt.Key_F2: self._toggle_phone()
        elif e.modifiers() & Qt.ControlModifier:
            if e.key() in (Qt.Key_B, 98): self._rsz(1)
            elif e.key() in (Qt.Key_S, 115): self._rsz(-1)
        super().keyPressEvent(e)

    def _show_help(self):
        d = WelcomeDialog()
        d.exec()

    def _rsz(self, d):
        fsizes = [8,9,10,11,12,13,14,15,16]
        wsizes = [(320,340),(360,380),(400,420),(440,460),(480,500),(520,540),(560,580),(600,620),(640,660)]
        osizes = [(240,260),(270,290),(300,320),(330,350),(360,380),(390,410),(420,440),(450,470),(480,500)]
        self._sz = max(0, min(len(fsizes)-1, self._sz + d))
        if self._cmp: self.resize(*osizes[self._sz])
        else: self.resize(*wsizes[self._sz])

    def _opa(self, v):
        self._wa = int(v * 2.3)
        ca = 50 + int(v * 1.7)
        for c in self._cards: c.ca = ca
        self.update()

    def _set_mode(self, comp):
        self._cmp = comp; self.hide()
        if comp:
            self.hdr.hide(); self.chdr.show(); self.sl.hide(); self._bm.show(); self._bp.show()
            for c in self._cards: c.set_compact()
            self.gr.setSpacing(4); self.ml.setContentsMargins(6,4,6,4); self.ml.setSpacing(3)
            self.setMinimumSize(240,240); self.resize(270,290)
            self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        else:
            self.hdr.show(); self.chdr.hide(); self.sl.show(); self._bm.show(); self._bp.show()
            for c in self._cards: c.set_normal()
            self.gr.setSpacing(5); self.ml.setContentsMargins(8,6,8,6); self.ml.setSpacing(5)
            self.setMinimumSize(320,320); self.resize(400,420)
            self.setWindowFlags(Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        v = self.sl.value(); self._wa = int(v * 2.3); ca = 50 + int(v * 1.7)
        for c in self._cards: c.ca = ca
        self._rsz(0); self.show(); self.raise_(); self.setFocus(); self.activateWindow()

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        icon_path = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(__file__) if '__file__' in dir() else '.'), "icon.png")
        if os.path.exists(icon_path):
            self._tray.setIcon(QIcon(icon_path))
        else:
            self._tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self._tray.setToolTip("Sistem Monitoru")
        
        tray_menu = QMenu()
        show_action = QAction("Göster", self)
        show_action.triggered.connect(self._show_from_tray)
        tray_menu.addAction(show_action)
        
        self._tray_phone_action = QAction("Telefon Bağlantısı: Kapalı", self)
        self._tray_phone_action.triggered.connect(self._toggle_phone)
        tray_menu.addAction(self._tray_phone_action)
        
        help_action = QAction("Kısayollar", self)
        help_action.triggered.connect(self._show_help)
        tray_menu.addAction(help_action)
        
        tray_menu.addSeparator()
        quit_action = QAction("Çıkış", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)
        
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self):
        self.show()
        self.raise_()
        self.setFocus()
        self.activateWindow()
        if self._cmp:
            self._set_mode(False)

    def _quit_app(self):
        stop_phone_server()
        self._tray.hide()
        QThreadPool.globalInstance().waitForDone(3000)
        QApplication.quit()

    def closeEvent(self, e):
        if self._phone_active:
            e.ignore()
            self.hide()
            self._tray.showMessage("Sistem Monitoru", "Uygulama arka planda çalışıyor. Telefon bağlantısı aktif.", QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            stop_phone_server()
            self._tray.hide()
            super().closeEvent(e)

    def _toggle_phone(self):
        if self._phone_active:
            self._stop_phone()
        else:
            self._start_phone()

    def _start_phone(self):
        ok, result, pair = start_phone_server(self)
        if ok:
            self._phone_active = True
            self._phone_btn.setChecked(True)
            self._phone_btn.setToolTip("Telefon Bağlantısı Aktif (F2)")
            self._tray_phone_action.setText("Telefon Bağlantısı: Açık")
            ip = result
            port = PHONE_SERVER_PORT
            scheme = "https" if _phone_tls_active else "http"
            url = f"{scheme}://{ip}:{port}/?c={pair}"
            self._phone_dialog = PhoneConnectDialog(self, ip, port, pair, _pairing_expiry, tls=_phone_tls_active)
            result_code = self._phone_dialog.exec()
            if result_code == 1:
                self.hide()
                self._tray.showMessage("Sistem Monitoru", f"Telefon bağlantısı aktif: {url}", QSystemTrayIcon.MessageIcon.Information, 3000)
            elif result_code == 2:
                self._stop_phone()
        else:
            QMessageBox.critical(self, "Hata", f"Telefon sunucusu başlatılamadı:\n{result}")

    def _stop_phone(self):
        stop_phone_server()
        self._phone_active = False
        self._phone_btn.setChecked(False)
        self._phone_btn.setToolTip("Telefonla Bağla (F2)")
        self._tray_phone_action.setText("Telefon Bağlantısı: Kapalı")
        if self._phone_dialog:
            self._phone_dialog.close()
            self._phone_dialog = None

    def _gt(self):
        if not GPU: return None
        try:
            return pynvml.nvmlDeviceGetTemperature(GPU, pynvml.NVML_TEMPERATURE_GPU)
        except (pynvml.NVMLError, OSError):
            return None

    def _gu(self):
        if not GPU: return None
        try:
            return pynvml.nvmlDeviceGetUtilizationRates(GPU).gpu
        except (pynvml.NVMLError, OSError):
            return None

    def _ct(self):
        # WMI termal sorgusu GUI thread'i bloklamamasi icin worker'dan doldurulan cache
        return getattr(self, '_ct_cache', None)

    def _ensure_ct_refresh(self):
        if not WMI:
            return
        now = time.time()
        if getattr(self, '_ct_running', False):
            return
        if now - getattr(self, '_ct_last', 0.0) < 30:
            return
        self._ct_last = now
        self._ct_running = True
        worker = SignalWorker(_query_cpu_temp)
        worker.signals.done.connect(self._on_ct_result)
        worker.signals.failed.connect(self._on_ct_failed)
        _start_worker(worker)

    def _on_ct_result(self, value):
        self._ct_running = False
        self._ct_cache = value

    def _on_ct_failed(self, err):
        self._ct_running = False
        self._ct_cache = None

    def _fb(self, b):
        for u in ['B','KB','MB','GB','TB']:
            if b < 1024: return f"{b:.1f}{u}"
            b /= 1024
        return f"{b:.1f}PB"

    def _ensure_events_check(self):
        """Ağır olay sayım sorgusunu worker thread'e taşır. Devam eden aynı sorguyu tekrarlamaz."""
        now = time.time()
        if getattr(self, '_events_running', False):
            return
        if now - self._last_ev < 60:
            return
        self._last_ev = now
        self._events_running = True
        worker = SignalWorker(_query_event_count)
        worker.signals.done.connect(self._on_event_count)
        worker.signals.failed.connect(self._on_event_count_failed)
        _start_worker(worker)

    def _on_event_count(self, result):
        self._events_running = False
        self._ev_cache = result
        self._render_event_card(*result)
        if self._phone_metrics:
            ec, em = result
            self._phone_metrics["errors"] = {"count": ec, "msg": em} if ec and ec != "0" else {"count": "0", "msg": None}

    def _on_event_count_failed(self, err):
        self._events_running = False
        _safe_log(f"EVENT: worker hatasi: {err}")
        self._ev_cache = (None, None)
        self._render_event_card(None, None)

    def _render_event_card(self, ec, em):
        elc = self._elc
        if ec is None:
            elc.v.setStyleSheet(f"color:#6c7086; font-size:{9 if self._cmp else 11}px; font-weight:bold; background:transparent;")
            elc.upd("Kontrol edilemedi")
        elif ec == "0":
            elc.v.setStyleSheet(f"color:#a6e3a1; font-size:{10 if self._cmp else 13}px; font-weight:bold; background:transparent;")
            elc.v.setWordWrap(False)
            elc.upd("Sistem Temiz \u2713")
        else:
            elc.v.setStyleSheet(f"color:#f38ba8; font-size:{9 if self._cmp else 11}px; font-weight:bold; background:transparent;")
            elc.v.setWordWrap(True)
            txt = f"{ec} Kritik Hata"
            if em: txt += f"\n{em}..."
            elc.upd(txt)

    def _get_events(self):
        """Event log sorgusu (GUI thread disinda calistirilmali).
        Sonuc EventQueryResult; failure asla 'temiz' olarak gosterilmez."""
        try:
            since = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
            r = subprocess.run(
                [POWERSHELL_EXE, "-NoProfile", "-Command",
                 f"$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-WinEvent -FilterHashtable @{{LogName='System'; Level=1,2; StartTime='{since}'}} -MaxEvents 30 -ErrorAction SilentlyContinue | Select-Object @{{N='T';E={{$_.TimeCreated.ToString('yyyy-MM-dd HH:mm')}}}},Id,LevelDisplayName,ProviderName,Message | ConvertTo-Json"],
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            _safe_log("EVENT: liste zamansaimi")
            return EventQueryResult(ok=False, error="Zaman aşımı")
        except (subprocess.SubprocessError, OSError) as e:
            _safe_log(f"EVENT: calistirma hatasi: {e}")
            return EventQueryResult(ok=False, error="PowerShell çalıştırılamadı")
        if r.returncode != 0:
            _safe_log(f"EVENT: PowerShell hata kodu {r.returncode}")
            return EventQueryResult(ok=False, error=f"PowerShell hata kodu {r.returncode}")
        raw = r.stdout.decode('utf-8', errors='replace')
        data = _parse_event_json(raw)
        if data is None:
            _safe_log("EVENT: JSON cozulemedi")
            return EventQueryResult(ok=False, error="JSON ayrıştırma hatası")
        return EventQueryResult(ok=True, events=data)

    def _open_event_log(self):
        try:
            d = EventLogDialog(self)
            d.exec()
        except (RuntimeError, OSError) as ex:
            _safe_log(f"EVENT: dialog acilamadi: {ex}")

    def _tick(self):
        cp = self._cards
        cpu_pct = psutil.cpu_percent(0)
        cp[0].upd(f"%{cpu_pct:.1f}", cpu_pct)
        self._ensure_ct_refresh()
        ct = self._ct()
        if ct: c = tcolor(ct); cp[1].v.setStyleSheet(f"color:{c}; font-size:{15 if self._cmp else 18}px; font-weight:bold; background:transparent;"); cp[1].upd(f"{ct:.1f}C", min(100,ct/.9), c)
        else: cp[1].upd("N/A")
        gu = self._gu()
        if gu is not None: cp[2].upd(f"%{gu}", gu)
        else: cp[2].upd("N/A")
        gt = self._gt()
        if gt is not None: c = tcolor(gt); cp[3].v.setStyleSheet(f"color:{c}; font-size:{15 if self._cmp else 18}px; font-weight:bold; background:transparent;"); cp[3].upd(f"{gt:.0f}C", min(100,gt/.9), c)
        else: cp[3].upd("N/A")
        m = psutil.virtual_memory(); cp[4].upd(f"{m.used/1024**3:.1f}/{m.total/1024**3:.1f}GB", m.percent)
        d = psutil.disk_usage('/'); cp[5].upd(f"{d.used/1024**3:.1f}/{d.total/1024**3:.1f}GB", d.percent)
        n = psutil.net_io_counters(); nw = time.time(); el = nw - self._nt
        dn = _compute_net_speed(self._np.bytes_recv, n.bytes_recv, el)
        up = _compute_net_speed(self._np.bytes_sent, n.bytes_sent, el)
        cp[6].upd(f"D:{self._fb(dn)}/s Y:{self._fb(up)}/s")
        self._np = n; self._nt = nw
        bt = datetime.fromtimestamp(psutil.boot_time())
        td = datetime.now() - bt; s = int(td.total_seconds())
        dd, r = divmod(s,86400); hh, r = divmod(r,3600); mm = r//60
        p = []
        if dd: p.append(f"{dd}g")
        p.append(f"{hh}s"); p.append(f"{mm}d")
        uptime = " ".join(p)
        cp[7].upd(uptime)
        self._ensure_events_check()
        ec, em = getattr(self, '_ev_cache', (None, None))
        self._render_event_card(ec, em)

        self._phone_metrics = {
            "cpu": {"percent": round(cpu_pct, 1), "temp": ct},
            "gpu": {"percent": gu, "temp": gt},
            "ram": {"used_gb": round(m.used/1024**3, 1), "total_gb": round(m.total/1024**3, 1), "percent": round(m.percent, 1)},
            "disk": {"used_gb": round(d.used/1024**3, 1), "total_gb": round(d.total/1024**3, 1), "percent": round(d.percent, 1)},
            "network": {"down": n.bytes_recv, "up": n.bytes_sent},
            "uptime": uptime,
            "errors": {"count": ec, "msg": em} if ec and ec != "0" else {"count": "0", "msg": None},
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }

    def paintEvent(self, e):
        if self._wa > 0:
            p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = self.rect()
            p.setBrush(QBrush(QColor(10,10,16,self._wa)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r.adjusted(1,1,-1,-1) if self._cmp else r, 8, 8 if self._cmp else 0)
            p.end()
        super().paintEvent(e)

_EXP = {
    41: "Beklenmeyen kapanma (Kernel-Power). Bilgisayar aniden kapandi veya guc kesintisi yasandi.",
    55: "NTFS dosya sistemi bozulmasi tespit edildi. Disk hatasi veya dosya sistemi hasari olabilir.",
    51: "Sayfalama sirasinda hata. Disk veya bellek sorunu olabilir.",
    57: "Veri okuma/yazma hatasi. Disk sagligini kontrol edin.",
    116: "Disk hatasi. Disk bozuk olabilir, yedekleme yapin.",
    134: "Bellek hatasi. RAM modullerinde fiziksel sorun olabilir.",
    137: "Bellek yetmezligi. Uygulamalar yeterli RAM bulamadi.",
    153: "Disk hatasi tespit edildi. Disk degisimi gerekebilir.",
    1000: "Uygulama hatasi. Bir program beklenmeyen sekilde coktu.",
    1001: "Windows Hata Raporlama. Bir uygulama veya sistem bileseni coktu.",
    1002: "Uygulama yanit vermiyor. Bir program dondu.",
    1014: "DNS cozumleme hatasi. Internet baglantisi veya DNS ayarlarini kontrol edin.",
    7000: "Servis baslatilamadi. Bir Windows servisi calismayi durdurdu.",
    7001: "Servis bagimlilik hatasi. Gerekli bir baska servis calismiyor.",
    7023: "Servis hata ile sonlandi. Servis durumunu kontrol edin.",
    7024: "Servis ozel bir hata ile sonlandi.",
    7030: "Servis guvenlik turu hatasi. Servis izinlerini kontrol edin.",
    7031: "Servis beklenmeyen sekilde sonlandi. Otomatik yeniden baslatma ayarlanabilir.",
    7034: "Servis kilitlendi veya coktu.",
    10005: "DCOM hatasi. Bir bilesen kaydedilmemis veya kayip.",
    10010: "DCOM sunucu bulunamadi. Kayit defteri veya servis sorunu.",
    10016: "DCOM izin hatasi. Bir uygulamanin yeterli izni yok.",
    6008: "Windows onceki acilista beklenmeyen sekilde kapandi.",
    1074: "Kullanici veya sistem yeniden baslatma/kapatma baslatti.",
    1076: "Bilgisayar basarisiz acilista yeniden baslatildi.",
    7: "Aygit surucusu hatasi. Bir donanim bileseni sorun yasiyor.",
    11: "Surucu hatasi. Donanim surucusu calismayi durdurdu.",
    12: "Aygit surucusu kilitlenmesi.",
    26: "Uygulama acilirken hata. Sistem uyumlulugunu kontrol edin.",
}

def _exp(eid):
    try:
        eid = int(eid)
    except (ValueError, TypeError):
        return "Bilinmeyen hata kodu."
    return _EXP.get(eid, f"Hata kodu {eid}. Windows olay goruntuleyiciden detayli inceleyin.")

def _compute_net_speed(prev_bytes, cur_bytes, elapsed_s):
    """Ag hizi (byte/s). Counter reset/wrap negatif uretmez. Ilk sample baseline olarak kabul edilir."""
    if prev_bytes is None or cur_bytes is None or elapsed_s <= 0:
        return 0.0
    delta = cur_bytes - prev_bytes
    if delta < 0:
        delta = 0
    return delta / elapsed_s

class EventQueryResult:
    """Event log sorgu sonucu. Failure 'temiz' durumla karistirilmaz."""
    def __init__(self, ok, events=None, error=None):
        self.ok = ok
        self.events = events if events is not None else []
        self.error = error

    @property
    def empty(self):
        return self.ok and not self.events

def _parse_event_json(raw):
    """PowerShell ConvertTo-Json ciktisini normalize eder.
    object -> [object], array -> array, empty -> [], invalid JSON -> None."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []

def _query_event_count():
    """GUI thread disinda calistirilir. (count, msg) veya hata halinde (None, None)."""
    try:
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        r = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command",
             f"$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-WinEvent -FilterHashtable @{{LogName='System'; Level=1,2; StartTime='{since}'}} -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        cnt = r.stdout.decode('utf-8', errors='replace').strip()
        if not cnt or cnt == "0":
            return ("0", None)
        r2 = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command",
             f"$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-WinEvent -FilterHashtable @{{LogName='System'; Level=1; StartTime='{since}'}} -MaxEvents 1 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Message"],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        msg = r2.stdout.decode('utf-8', errors='replace').strip()[:120] if r2.stdout else None
        return (cnt, msg)
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        _safe_log(f"EVENT: sayim hatasi: {e}")
        return (None, None)

def _query_cpu_temp():
    """WMI termal sorgusu; GUI thread disinda calistirilir. Hata halinde None."""
    if not WMI:
        return None
    try:
        import wmi as _w
        t = _w.WMI(namespace="root\\CIMv2").Win32_PerfFormattedData_Counters_ThermalZoneInformation()
        if t:
            v = int(t[0].Temperature) / 10
            return v if 0 < v < 120 else None
    except Exception:
        _safe_log("CT: WMI termal sorgusu basarisiz")
        return None
    return None

_running_workers = set()
_workers_lock = threading.Lock()

def _track_worker(worker):
    with _workers_lock:
        _running_workers.add(worker)

def _untrack_worker(worker):
    with _workers_lock:
        _running_workers.discard(worker)

def _start_worker(worker):
    """Worker'i canli tutarak thread havuzunda baslatir."""
    _track_worker(worker)
    QThreadPool.globalInstance().start(worker)

class SignalWorker(QRunnable):
    """GUI thread disinda calisan worker. Sonucu signal ile ana thread'e tasir."""
    class _Signals(QObject):
        done = Signal(object)
        failed = Signal(object)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = SignalWorker._Signals()

    def run(self):
        try:
            self.signals.done.emit(self._fn(*self._args, **self._kwargs))
        except Exception as e:
            self.signals.failed.emit(e)
        finally:
            _untrack_worker(self)

class PhoneConnectDialog(QDialog):
    def __init__(self, parent, ip, port, pair=None, expiry=None, tls=False):
        super().__init__(parent)
        self._expiry = expiry or (time.time() + PHONE_PAIR_TTL)
        self._countdown_timer = None
        self.setWindowTitle("Telefon Bağlantısı")
        self.setFixedSize(380, 580)
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog { background: #1e1e2e; border: 1px solid #45475a; border-radius: 8px; }
            QLabel { color: #cdd6f4; background: transparent; }
            QPushButton { background: #cba6f7; color: #1e1e2e; border: none; border-radius: 6px; padding: 8px 24px; font-weight: bold; font-size:12px; }
            QPushButton:hover { background: #d8b9ff; }
            QPushButton#stopBtn { background: #f38ba8; }
            QPushButton#stopBtn:hover { background: #f5a9b8; }
        """)
        lo = QVBoxLayout(self); lo.setContentsMargins(20,16,20,16); lo.setSpacing(10)
        
        title = QLabel("<b>Telefonla Bağlantı Aktif</b>")
        title.setStyleSheet("color:#cba6f7; font-size:14px; background:transparent;")
        title.setAlignment(Qt.AlignCenter)
        lo.addWidget(title)
        
        addr = QLabel(f"<b>{ip}</b>:<b>{port}</b>")
        addr.setStyleSheet("color:#89b4fa; font-size:12px; background:transparent;")
        addr.setAlignment(Qt.AlignCenter)
        lo.addWidget(addr)

        scheme = "https" if tls else "http"
        url = f"{scheme}://{ip}:{port}/?c={pair}" if pair else f"{scheme}://{ip}:{port}"
        url_label = QLabel(f'<a href="{url}" style="color:#89b4fa;">{url}</a>')
        url_label.setStyleSheet("font-size:11px; background:transparent;")
        url_label.setAlignment(Qt.AlignCenter)
        url_label.setOpenExternalLinks(True)
        url_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        url_label.setWordWrap(True)
        lo.addWidget(url_label)
        
        if QRCODE_AVAILABLE:
            try:
                qr = qrcode.QRCode(version=1, box_size=6, border=2)
                qr.add_data(url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="#cdd6f4", back_color="#1e1e2e")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                pixmap = QPixmap()
                pixmap.loadFromData(buf.read())
                qr_label = QLabel()
                qr_label.setPixmap(pixmap)
                qr_label.setAlignment(Qt.AlignCenter)
                lo.addWidget(qr_label)
                hint = QLabel("QR kodu telefonla tarayın")
                hint.setStyleSheet("color:#6c7086; font-size:11px; background:transparent;")
                hint.setAlignment(Qt.AlignCenter)
                lo.addWidget(hint)
            except Exception:
                _safe_log("QR olusturulamadi")
        else:
            hint = QLabel("QR kod için: pip install qrcode[pil]")
            hint.setStyleSheet("color:#6c7086; font-size:11px; background:transparent;")
            hint.setAlignment(Qt.AlignCenter)
            lo.addWidget(hint)

        secure = QLabel("Bağlantı kodu 60 saniye geçerli ve tek kullanımlıktır. "
                        "Kod kullanıldıktan sonra oturum çereziyle erişim sağlanır.")
        secure.setStyleSheet("color:#a6e3a1; font-size:10px; background:transparent;")
        secure.setAlignment(Qt.AlignCenter)
        secure.setWordWrap(True)
        lo.addWidget(secure)
        
        local_only = QLabel("⚠ Yalnızca bu yerel ağ IP adresine bağlanılabilir.\n"
                            "VPN, sanal veya public ağlarda sunucu açılmaz.")
        local_only.setStyleSheet("color:#f9e2af; font-size:10px; background:transparent;")
        local_only.setAlignment(Qt.AlignCenter)
        local_only.setWordWrap(True)
        lo.addWidget(local_only)

        if tls:
            tls_note = QLabel("🔒 HTTPS (TLS) aktif. Trafik şifreleniyor.")
            tls_note.setStyleSheet("color:#a6e3a1; font-size:10px; background:transparent;")
            tls_note.setAlignment(Qt.AlignCenter)
            tls_note.setWordWrap(True)
            lo.addWidget(tls_note)
        else:
            tls_note = QLabel("⚠ HTTP modunda trafik şifreli değildir.\n"
                              "Sertifika/private key verilirse HTTPS kullanılabilir.")
            tls_note.setStyleSheet("color:#f38ba8; font-size:10px; background:transparent;")
            tls_note.setAlignment(Qt.AlignCenter)
            tls_note.setWordWrap(True)
            lo.addWidget(tls_note)
        
        info = QLabel("Uygulama arka planda çalışmaya devam edecek.\nTelefondan değerleri görüntüleyebilirsiniz.")
        info.setStyleSheet("color:#a6adc8; font-size:11px; background:transparent;")
        info.setAlignment(Qt.AlignCenter)
        info.setWordWrap(True)
        lo.addWidget(info)
        
        lo.addStretch()

        self.countdown_label = QLabel("")
        self.countdown_label.setStyleSheet("color:#f9e2af; font-size:15px; font-weight:bold; background:transparent;")
        self.countdown_label.setAlignment(Qt.AlignCenter)
        lo.addWidget(self.countdown_label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        self.minimize_btn = QPushButton("Simgeye Küçült")
        self.minimize_btn.clicked.connect(lambda: self.done(1))
        btn_layout.addWidget(self.minimize_btn)
        
        self.stop_btn = QPushButton("Bağlantıyı Kes")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(lambda: self.done(2))
        btn_layout.addWidget(self.stop_btn)
        
        lo.addLayout(btn_layout)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._countdown_timer.start(500)
        self._update_countdown()

    def _update_countdown(self):
        left = max(0, int(self._expiry - time.time()))
        if left > 0:
            self.countdown_label.setText(f"Bağlantı süresi: {left} sn")
        else:
            self.countdown_label.setText("Bağlantı kodu süresi doldu")
            if self._countdown_timer:
                self._countdown_timer.stop()

    def done(self, result):
        if self._countdown_timer:
            self._countdown_timer.stop()
        super().done(result)

class EventLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mon = parent
        self.setWindowTitle("Windows Hata Kayitlari")
        self.setStyleSheet("QDialog{background:#1e1e2e;} QLabel{color:#cdd6f4;background:transparent;}")
        self.resize(620, 520)
        self._lo = QVBoxLayout(self); self._lo.setContentsMargins(12,10,12,10); self._lo.setSpacing(8)

        h = QLabel("<b>Son 48 Saatteki Sistem Hatalari</b>")
        h.setStyleSheet("color:#cba6f7; font-size:14px; background:transparent;")
        self._lo.addWidget(h)

        self._loading = QLabel("Hatalar kontrol ediliyor...")
        self._loading.setStyleSheet("color:#f9e2af; font-size:12px; background:transparent;")
        self._loading.setAlignment(Qt.AlignCenter)
        self._lo.addWidget(self._loading)

        self._sc = QScrollArea(); self._sc.setWidgetResizable(True)
        self._sc.setStyleSheet("QScrollArea{border:none;background:transparent;} QScrollBar:vertical{width:6px;background:#313244;border-radius:3px;} QScrollBar::handle:vertical{background:#585b70;border-radius:3px;} QScrollBar::add-line:vertical{height:0} QScrollBar::sub-line:vertical{height:0}")
        self._lo.addWidget(self._sc)

        b = QPushButton("Kapat")
        b.setStyleSheet("QPushButton{background:#cba6f7;color:#1e1e2e;border:none;border-radius:6px;padding:7px 24px;font-weight:bold;} QPushButton:hover{background:#d8b9ff;}")
        b.clicked.connect(self.accept)
        self._lo.addWidget(b, 0, Qt.AlignCenter)

        QTimer.singleShot(50, self._load)

    def _load(self):
        self._loading.show()
        if self._mon:
            worker = SignalWorker(self._mon._get_events)
            worker.signals.done.connect(self._on_result)
            worker.signals.failed.connect(self._on_error)
            _start_worker(worker)
        else:
            self._on_result(EventQueryResult(ok=True, events=[]))

    def _on_result(self, result):
        self._loading.hide()
        self._render_result(result)

    def _on_error(self, err):
        self._loading.hide()
        _safe_log(f"EVENT: dialog worker hatasi: {err}")
        self._render_result(EventQueryResult(ok=False, error="Beklenmeyen hata"))

    def _render_result(self, result):
        if not result.ok:
            lb = QLabel("Windows olay kayıtları kontrol edilemedi.")
            if result.error:
                lb.setText(f"{lb.text()}\n({result.error})")
            lb.setStyleSheet("color:#f38ba8; font-size:12px; background:transparent;")
            lb.setAlignment(Qt.AlignCenter)
            lb.setWordWrap(True)
            self._lo.insertWidget(1, lb)
            return
        if result.empty:
            lb = QLabel("Belirtilen zaman aralığında olay kaydı bulunamadı.")
            lb.setStyleSheet("color:#a6e3a1; font-size:12px; background:transparent;")
            lb.setAlignment(Qt.AlignCenter)
            self._lo.insertWidget(1, lb)
            return
        events = result.events
        cw = QWidget(); cw.setStyleSheet("background:transparent;")
        cl = QVBoxLayout(cw); cl.setSpacing(6); cl.setContentsMargins(0,0,0,0)
        for ev in events:
            tid = str(ev.get("T","?"))
            eid = str(ev.get("Id","?"))
            lvl = str(ev.get("LevelDisplayName","?"))
            src = str(ev.get("ProviderName","?"))
            msg = str(ev.get("Message") or "")[:200]
            fb = QFrame()
            fb.setStyleSheet("QFrame{background:#181825;border-radius:6px;border:1px solid #313244;}")
            fl = QVBoxLayout(fb); fl.setContentsMargins(10,8,10,8); fl.setSpacing(3)
            hb = QHBoxLayout(); hb.setSpacing(6)
            el = QLabel(f"[{eid}] {lvl}")
            el.setTextFormat(Qt.PlainText)
            el.setStyleSheet("color:#f38ba8;font-size:11px;font-weight:bold;background:transparent;")
            hb.addWidget(el)
            tl = QLabel(tid)
            tl.setTextFormat(Qt.PlainText)
            tl.setStyleSheet("color:#6c7086;font-size:10px;background:transparent;")
            hb.addWidget(tl)
            hb.addStretch()
            sl = QLabel(src)
            sl.setTextFormat(Qt.PlainText)
            sl.setStyleSheet("color:#89b4fa;font-size:10px;background:transparent;")
            hb.addWidget(sl)
            fl.addLayout(hb)
            if msg:
                ml = QLabel(msg)
                ml.setTextFormat(Qt.PlainText)
                ml.setWordWrap(True)
                ml.setStyleSheet("color:#bac2de; font-size:10px; background:transparent; padding:2px 0;")
                fl.addWidget(ml)
            xl = QLabel(f"{_exp(eid)}")
            xl.setTextFormat(Qt.PlainText)
            xl.setWordWrap(True)
            xl.setStyleSheet("color:#a6e3a1; font-size:10px; font-style:italic; background:transparent; padding:2px 0;")
            fl.addWidget(xl)
            cl.addWidget(fb)
        cl.addStretch()
        self._sc.setWidget(cw)
        self._loading.setStyleSheet("color:#a6e3a1; font-size:12px; background:transparent;")

class WelcomeDialog(QDialog):
    def __init__(self, first_run=False):
        super().__init__()
        self.setWindowTitle("Sistem Monitoru - Kisa Yollar")
        self.setFixedSize(380, 360)
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        if first_run:
            self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog { background: #1e1e2e; border: 1px solid #45475a; border-radius: 8px; }
            QLabel { color: #cdd6f4; background: transparent; }
            QCheckBox { color: #a6adc8; spacing: 8px; background: transparent; }
            QCheckBox::indicator { width: 16px; height: 16px; border-radius: 3px; border: 1px solid #585b70; background: #313244; }
            QCheckBox::indicator:checked { background: #cba6f7; border-color: #cba6f7; }
            QPushButton { background: #cba6f7; color: #1e1e2e; border: none; border-radius: 6px; padding: 8px 28px; font-weight: bold; font-size:12px; }
            QPushButton:hover { background: #d8b9ff; }
        """)
        lo = QVBoxLayout(self); lo.setContentsMargins(20,16,20,16); lo.setSpacing(6)

        t = QLabel("<b>Sistem Monitoru - Kisa Yollar</b>")
        t.setStyleSheet("color:#cba6f7; font-size:13px; background:transparent;")
        t.setAlignment(Qt.AlignCenter)
        lo.addWidget(t)

        lines = [
            "<b>F1</b>  - Bu kısayolları göster",
            "<b>F2</b>  - Telefon bağlantısını aç/kapat",
            "<b>F4</b>  - Gizli simgelere kucult / geri getir",
            "<b>Esc</b> - Overlay modundan gizli simgelere kucult",
            "<b>Ctrl+B</b> - Pencereyi buyut",
            "<b>Ctrl+S</b> - Pencereyi kucult",
            "<b>F5</b>  - Verileri yenile",
            "",
            "<i>Slider: Saydamlik ayari (solda seffaf, sagda opak)</i>",
            "<i>+ / - : Pencere boyutu (tiklanabilir)</i>",
        ]
        for l in lines:
            lb = QLabel(l); lb.setStyleSheet("color:#bac2de; font-size:11px; background:transparent;")
            lo.addWidget(lb)

        lo.addStretch()
        self.cb = QCheckBox("Bir daha gosterme")
        lo.addWidget(self.cb)
        b = QPushButton("Kapat")
        b.clicked.connect(self.accept)
        lo.addWidget(b, 0, Qt.AlignCenter)

def main():
    a = QApplication(sys.argv); a.setStyle("Fusion"); a.setFont(QFont("Segoe UI", 9))
    a.setQuitOnLastWindowClosed(False)
    s = QSettings("SistemMonitor", "SistemMonitor")
    if not s.value("nofirstrun", False, type=bool):
        d = WelcomeDialog(first_run=True)
        d.show(); d.raise_(); d.activateWindow()
        if d.exec() == QDialog.Accepted and d.cb.isChecked():
            s.setValue("nofirstrun", True)
    w = Monitor(); w.show(); sys.exit(a.exec())

if __name__ == "__main__":
    main()
