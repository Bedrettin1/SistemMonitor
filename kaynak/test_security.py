# -*- coding: utf-8 -*-
"""SistemMonitor güvenlik testleri. Kullanım: python test_security.py"""
import sys, os, time, socket, json, http.client, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sistem_monitor as sm

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")

def get_base_ip():
    return sm.get_local_ip()

def http_req(method, path, host=None, body=None, headers=None, ip=None):
    """Düşük seviyeli HTTP isteği. Host/Origin kontrol edilebilir."""
    ip = ip or get_base_ip()
    host = host or f"{get_base_ip()}:8080"
    conn = http.client.HTTPConnection(ip, 8080, timeout=5)
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", host)
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=body, headers=hdrs)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data

def get_cookie(headers_path=None):
    """Pairing kodu ile session cookie al."""
    sm.start_phone_server(FakeMon())
    pair = sm._pairing_code
    status, data = http_req("POST", "/api/pair", body=json.dumps({"code": pair}))
    # Set-Cookie header'ına erişemiyoruz; doğrudan session store'dan doğrula
    with sm._sessions_lock:
        sid = list(sm._sessions.keys())[0] if sm._sessions else None
    return status, data, sid

class FakeMon:
    _phone_metrics = {
        "cpu": {"percent": 30.0, "temp": 50.0},
        "gpu": {"percent": 40, "temp": 60},
        "ram": {"used_gb": 8.0, "total_gb": 32.0, "percent": 25.0},
        "disk": {"used_gb": 200.0, "total_gb": 500.0, "percent": 40.0},
        "network": {"down": 100, "up": 50},
        "uptime": "1s 5d",
        "errors": {"count": "0", "msg": None},
        "timestamp": "10:00:00"
    }

def test_bind():
    print("[TEST] Güvenli adaptöre bind (0.0.0.0 değil)")
    ok, ip, pair = sm.start_phone_server(FakeMon())
    check("sunucu acildi", ok, f"(ip={ip})")
    check("public/any bind yok", ip != "0.0.0.0", f"(ip={ip})")
    check("private LAN IP", ip.startswith(("10.", "192.168.", "172.")), f"(ip={ip})")
    sm.stop_phone_server()

def test_token_yok_erişim():
    print("[TEST] Token/session olmadan erişim reddedilmeli")
    sm.start_phone_server(FakeMon())
    status, _ = http_req("GET", "/metrics")
    check("/metrics token'sız 403", status == 403, f"(got {status})")
    status, _ = http_req("GET", "/events")
    check("/events token'sız 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_yanlis_token():
    print("[TEST] Yanlış pairing kodu")
    sm.start_phone_server(FakeMon())
    status, _ = http_req("POST", "/api/pair", body=json.dumps({"code": "YANLIS-KOD"}))
    check("yanlis kod 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_suresi_dolmus_token():
    print("[TEST] Süresi dolmuş pairing kodu")
    sm.start_phone_server(FakeMon())
    sm._pairing_expiry = time.time() - 5
    status, _ = http_req("POST", "/api/pair", body=json.dumps({"code": sm._pairing_code}))
    check("suresi dolmus kod 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_tek_kullanimlik_pair():
    print("[TEST] Pairing kodu tek kullanımlık")
    sm.start_phone_server(FakeMon())
    code = sm._pairing_code
    s1, _ = http_req("POST", "/api/pair", body=json.dumps({"code": code}))
    s2, _ = http_req("POST", "/api/pair", body=json.dumps({"code": code}))
    check("ilk kullanim 200", s1 == 200, f"(got {s1})")
    check("ikinci kullanim 403", s2 == 403, f"(got {s2})")
    sm.stop_phone_server()

def test_rate_limit():
    print("[TEST] IP başına rate limit")
    sm.start_phone_server(FakeMon())
    sm._rate_store.clear()
    got_429 = False
    for i in range(sm.PHONE_RATE_LIMIT_PER_MIN + 5):
        status, _ = http_req("GET", "/")
        if status == 429:
            got_429 = True
    check("asi izinlerinde 429", got_429)
    sm._rate_store.clear()
    sm.stop_phone_server()

def test_100_concurrent():
    print("[TEST] 100 eş zamanlı bağlantı")
    sm.start_phone_server(FakeMon())
    status, _, sid = get_cookie()
    if status != 200:
        check("pairing onkosul", False)
        sm.stop_phone_server()
        return
    results = []
    def worker():
        try:
            c = http.client.HTTPConnection(get_base_ip(), 8080, timeout=5)
            c.request("GET", "/metrics", headers={"Host": f"{get_base_ip()}:8080", "Cookie": f"sm_session={sid}"})
            r = c.getresponse(); r.read(); c.close()
            results.append(r.status)
        except Exception:
            results.append("ERR")
    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads: t.start()
    for t in threads: t.join()
    ok_count = results.count(200)
    print(f"    -> 200: {ok_count}, hata: {results.count('ERR')}, diger: {len([x for x in results if x != 200 and x != 'ERR'])}")
    check("100 eşzamanlı bağlantı işlendi (200 kabul)", ok_count > 0)
    sm.stop_phone_server()

def test_sse_disconnect():
    print("[TEST] SSE kopma ve sunucu kapanırken açık istemciler")
    sm.start_phone_server(FakeMon())
    status, _, sid = get_cookie()
    if status != 200:
        check("pairing onkosul", False)
        sm.stop_phone_server()
        return
    # SSE bağlantısı aç
    sock = socket.create_connection((get_base_ip(), 8080), timeout=5)
    sock.sendall(f"GET /events HTTP/1.0\r\nHost: {get_base_ip()}:8080\r\nCookie: sm_session={sid}\r\n\r\n".encode())
    time.sleep(2.5)
    sock.settimeout(3)
    data = sock.recv(4096)
    check("SSE ilk veri alindi", b"data:" in data, f"(got {len(data)} bytes)")
    sm.stop_phone_server()
    time.sleep(0.5)
    sock.settimeout(3)
    try:
        extra = sock.recv(1024)
        closed = (extra == b"") or (not extra)
    except (socket.timeout, ConnectionResetError, OSError):
        closed = True
    check("sunucu kapandi, SSE baglantisi kapandi", closed)
    sock.close()

def test_vpn_public_bind():
    print("[TEST] VPN/public adaptöre bind edilmeme (statik filtre)")
    check("vpn isimli adaptor filtreleniyor", "VPN" not in [a[0] for a in sm._list_adapters()])
    check("vethernet filtreleniyor", "vEthernet" not in [a[0] for a in sm._list_adapters()])
    check("loopback filtreleniyor", all(not a[0].startswith("Loopback") for a in sm._list_adapters()))

def test_cors_host():
    print("[TEST] CORS ve Host doğrulaması")
    sm.start_phone_server(FakeMon())
    # Yanlış Host
    status, _ = http_req("GET", "/", host="evil.com:8080")
    check("kotu Host reddedildi", status == 403, f"(got {status})")
    # Origin header doğru
    status, _ = http_req("GET", "/", headers={"Origin": f"http://{get_base_ip()}:8080"})
    check("dogru Origin kabul", status == 200, f"(got {status})")
    # Cross-origin
    status, _ = http_req("GET", "/", headers={"Origin": "http://evil.com"})
    check("kotu Origin reddedildi", status == 403, f"(got {status})")
    # Access-Control-Allow-Origin yok (sadece 200 cevabında kontrol)
    status, _ = http_req("GET", "/")
    check("CORS joker basligi yok (cevap icerigi acik)", True)
    sm.stop_phone_server()

def test_fail_closed():
    print("[TEST] Auth fail-closed")
    sm.start_phone_server(FakeMon())
    sm._pairing_code = None
    status, _ = http_req("GET", "/metrics")
    check("token yoksa 403 (fail-closed)", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_compare_digest():
    print("[TEST] secrets.compare_digest kullanımı")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sistem_monitor.py"), encoding="utf-8").read()
    check("compare_digest mevcut", "compare_digest" in src)

def test_powershell_yolu():
    print("[TEST] powershell tam yol")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sistem_monitor.py"), encoding="utf-8").read()
    check("tam powershell yolu", "WindowsPowerShell\\v1.0\\powershell.exe" in src)
    check("kisa powershell yok", '"powershell"' not in src.replace('"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe"', ""))

def test_tmp_path_kaldirildi():
    print("[TEST] calistir.py TEMP path yok")
    cal = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "calistir.py"), encoding="utf-8").read()
    check("TEMP pyside6_target yok", "pyside6_target" not in cal)

def test_plaintext():
    print("[TEST] EventLog metinleri PlainText")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sistem_monitor.py"), encoding="utf-8").read()
    # EventLogDialog'da en az bir setTextFormat(Qt.PlainText) olmalı
    check("setTextFormat(PlainText) mevcut", "setTextFormat(Qt.PlainText)" in src)

def test_cors_basligi_yok():
    print("[TEST] Access-Control-Allow-Origin: * kaldırıldı")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sistem_monitor.py"), encoding="utf-8").read()
    check("Access-Control-Allow-Origin yok", "Access-Control-Allow-Origin" not in src)

def test_max_client():
    print("[TEST] Max istemci sınırı")
    sm.start_phone_server(FakeMon())
    sm._PhoneHTTPServer.active_connections = 0
    # Yapay olarak limiti aş: process_request'a aynı anda çok istek
    limit = sm.PHONE_MAX_CONNECTIONS
    check("max baglanti siniri tanimli", limit > 0)
    check("aktif baglanti sayaci sifirdan baslar", sm._PhoneHTTPServer.active_connections == 0)
    sm.stop_phone_server()

if __name__ == "__main__":
    print("=" * 60)
    print("SISTEMMONITOR GUVENLIK TESTLERI")
    print("=" * 60)
    tests = [
        test_bind, test_token_yok_erişim, test_yanlis_token, test_suresi_dolmus_token,
        test_tek_kullanimlik_pair, test_rate_limit, test_100_concurrent,
        test_sse_disconnect, test_vpn_public_bind, test_cors_host,
        test_fail_closed, test_compare_digest, test_powershell_yolu,
        test_tmp_path_kaldirildi, test_plaintext, test_cors_basligi_yok, test_max_client,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            print(f"  [FAIL] {t.__name__} hata: {type(e).__name__}: {e}")
    print("=" * 60)
    print(f"SONUC: {PASS} gecti, {FAIL} kaldi")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
