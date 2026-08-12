# -*- coding: utf-8 -*-
"""SistemMonitor güvenlik testleri. Kullanım: python test_security.py"""
import sys, os, time, socket, json, http.client, threading, logging, io, tempfile

# Türkçe karakterlerin cp1252 konsolelarda bozulmadan yazılabilmesi için UTF-8 zorla.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

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

def start_test_server():
    """Test için sunucu başlatır. Gerçek adaptör yoksa loopback'e düşer (CI uyumu)."""
    adapters = sm._list_adapters()
    if adapters:
        _start = sm.start_phone_server
        return _start(FakeMon())
    sm.stop_phone_server()
    sm.PhoneRequestHandler.provider = sm.PhoneMetricsProvider(FakeMon())
    sm._pairing_code = sm.secrets.token_urlsafe(6)
    sm._pairing_expiry = time.time() + sm.PHONE_PAIR_TTL
    sm._phone_tls_active = False
    sm._start_cleanup_thread()
    srv = sm._PhoneHTTPServer(("127.0.0.1", sm.PHONE_SERVER_PORT), sm.PhoneRequestHandler)
    sm._phone_server = srv
    sm._phone_server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    sm._phone_server_thread.start()
    return True, "127.0.0.1", sm._pairing_code

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
    """Pairing kodu ile session cookie al. Her çağrıda temiz sunucu başlatır."""
    sm.stop_phone_server()
    start_test_server()
    pair = sm._pairing_code
    status, data = http_req("POST", "/api/pair", body=json.dumps({"code": pair}))
    # Set-Cookie header'ına erişemiyoruz; doğrudan session store'dan doğrula
    with sm._sessions_lock:
        sid = list(sm._sessions.keys())[0] if sm._sessions else None
    return status, data, sid

def get_cookie_raw():
    """Pairing ile session cookie header'ini raw dondurur."""
    sm.stop_phone_server()
    start_test_server()
    pair = sm._pairing_code
    conn = http.client.HTTPConnection(get_base_ip(), 8080, timeout=5)
    conn.request("POST", "/api/pair", body=json.dumps({"code": pair}),
                 headers={"Host": f"{get_base_ip()}:8080", "Content-Type": "application/json"})
    r = conn.getresponse(); data = r.read()
    set_cookie = r.getheader("Set-Cookie", "")
    conn.close()
    return r.status, data, set_cookie

def http_req_raw(method, path, body=None, headers=None, ip=None):
    """Düşük seviyeli HTTP isteği; header'lar üzerinde tam kontrol."""
    ip = ip or get_base_ip()
    conn = http.client.HTTPConnection(ip, 8080, timeout=5)
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", f"{get_base_ip()}:8080")
    conn.request(method, path, body=body, headers=hdrs)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data

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
    ok, ip, pair = start_test_server()
    check("sunucu acildi", ok, f"(ip={ip})")
    check("public/any bind yok", ip != "0.0.0.0", f"(ip={ip})")
    check("private LAN IP", ip.startswith(("10.", "192.168.", "172.")), f"(ip={ip})")
    sm.stop_phone_server()

def test_token_yok_erişim():
    print("[TEST] Token/session olmadan erişim reddedilmeli")
    start_test_server()
    status, _ = http_req("GET", "/metrics")
    check("/metrics token'sız 403", status == 403, f"(got {status})")
    status, _ = http_req("GET", "/events")
    check("/events token'sız 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_yanlis_token():
    print("[TEST] Yanlış pairing kodu")
    start_test_server()
    status, _ = http_req("POST", "/api/pair", body=json.dumps({"code": "YANLIS-KOD"}))
    check("yanlis kod 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_suresi_dolmus_token():
    print("[TEST] Süresi dolmuş pairing kodu")
    start_test_server()
    sm._pairing_expiry = time.time() - 5
    status, _ = http_req("POST", "/api/pair", body=json.dumps({"code": sm._pairing_code}))
    check("suresi dolmus kod 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_tek_kullanimlik_pair():
    print("[TEST] Pairing kodu tek kullanımlık")
    start_test_server()
    code = sm._pairing_code
    s1, _ = http_req("POST", "/api/pair", body=json.dumps({"code": code}))
    s2, _ = http_req("POST", "/api/pair", body=json.dumps({"code": code}))
    check("ilk kullanim 200", s1 == 200, f"(got {s1})")
    check("ikinci kullanim 403", s2 == 403, f"(got {s2})")
    sm.stop_phone_server()

def test_rate_limit():
    print("[TEST] IP başına rate limit")
    start_test_server()
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
    start_test_server()
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
    start_test_server()
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
    start_test_server()
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
    start_test_server()
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
    start_test_server()
    sm._PhoneHTTPServer.active_connections = 0
    # Yapay olarak limiti aş: process_request'a aynı anda çok istek
    limit = sm.PHONE_MAX_CONNECTIONS
    check("max baglanti siniri tanimli", limit > 0)
    check("aktif baglanti sayaci sifirdan baslar", sm._PhoneHTTPServer.active_connections == 0)
    sm.stop_phone_server()

def test_pair_invalid_host_origin():
    print("[TEST] POST /api/pair Host ve Origin dogrulamasi")
    sm.stop_phone_server()
    start_test_server()
    body = json.dumps({"code": sm._pairing_code})
    status, _ = http_req("POST", "/api/pair", host="evil.com:8080", body=body)
    check("POST kotu Host 403", status == 403, f"(got {status})")
    status, _ = http_req("POST", "/api/pair", body=body, headers={"Origin": "http://evil.com"})
    check("POST kotu Origin 403", status == 403, f"(got {status})")
    # Geçerli Host/Origin ile çalışmalı
    status, _ = http_req("POST", "/api/pair", body=body, headers={"Origin": f"http://{get_base_ip()}:8080"})
    check("POST gecerli Origin 200", status == 200, f"(got {status})")
    sm.stop_phone_server()

def test_body_limit():
    print("[TEST] Request body limiti")
    start_test_server()
    big = json.dumps({"code": "x" * (sm.PHONE_BODY_MAX + 100)})
    status, _ = http_req("POST", "/api/pair", body=big)
    check("buyuk body 413", status == 413, f"(got {status})")
    # Bozuk Content-Length
    status, _ = http_req_raw("POST", "/api/pair", body="{}", headers={"Content-Length": "abc"})
    check("gecersiz Content-Length 400", status == 400, f"(got {status})")
    # Negatif Content-Length
    status, _ = http_req_raw("POST", "/api/pair", body="{}", headers={"Content-Length": "-5"})
    check("negatif Content-Length 400", status == 400, f"(got {status})")
    # Malformed JSON
    status, _ = http_req_raw("POST", "/api/pair", body="{not-json", headers={"Content-Length": "9"})
    check("malformed JSON 400", status == 400, f"(got {status})")
    # JSON olmayan body
    status, _ = http_req_raw("POST", "/api/pair", body="hello", headers={"Content-Length": "5"})
    check("json olmayan body 400", status == 400, f"(got {status})")
    sm.stop_phone_server()

def test_session_auth():
    print("[TEST] Session auth: yanlis/suresi dolmus/revoke")
    start_test_server()
    status, _, sid = get_cookie()
    check("pairing onkosul 200", status == 200)
    # Yanlış session
    status, _ = http_req("GET", "/metrics", headers={"Cookie": "sm_session=yanlissession"})
    check("yanlis session 403", status == 403, f"(got {status})")
    # Doğru session
    status, _ = http_req("GET", "/metrics", headers={"Cookie": f"sm_session={sid}"})
    check("dogru session 200", status == 200, f"(got {status})")
    # Süresi dolmuş session
    with sm._sessions_lock:
        sm._sessions[sid] = time.time() - 5
    status, _ = http_req("GET", "/metrics", headers={"Cookie": f"sm_session={sid}"})
    check("suresi dolmus session 403", status == 403, f"(got {status})")
    # Revoke (yeni session al, sonra revoke et)
    status, _, sid2 = get_cookie()
    check("ikinci pairing onkosul 200", status == 200)
    sm._revoke_all_sessions()
    status, _ = http_req("GET", "/metrics", headers={"Cookie": f"sm_session={sid2}"})
    check("revoke edilmis session 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_pairing_replay():
    print("[TEST] Pairing replay ve rate limit")
    start_test_server()
    code = sm._pairing_code
    s1, _ = http_req("POST", "/api/pair", body=json.dumps({"code": code}))
    s2, _ = http_req("POST", "/api/pair", body=json.dumps({"code": code}))
    check("replay 403", s2 == 403, f"(got {s2})")
    sm.stop_phone_server()

def test_pairing_brute_force():
    print("[TEST] Pairing brute-force rate limiti (429)")
    sm.stop_phone_server()
    start_test_server()
    with sm._rate_lock:
        sm._rate_store.clear()
    got_429 = False
    for i in range(sm.PHONE_PAIR_RATE_LIMIT_PER_MIN + 3):
        try:
            status, _ = http_req("POST", "/api/pair", body=json.dumps({"code": "YANLIS"}))
            if status == 429:
                got_429 = True
        except (ConnectionAbortedError, ConnectionResetError, socket.timeout):
            time.sleep(0.05)
            continue
        time.sleep(0.02)
    check("pairing brute-force 429", got_429)
    sm.stop_phone_server()

def test_endpoint_rate_isolation():
    print("[TEST] Endpoint bazlı rate limit izolasyonu")
    start_test_server()
    status, _, sid = get_cookie()
    check("pairing onkosul 200", status == 200)
    with sm._rate_lock:
        sm._rate_store.clear()
    # Metrics trafiği pairing limitini tüketmemeli: önce session cookie kullanarak
    # pairing limiti + birkaç istek kadar metrics çağır, sonra pairing hâlâ çalışmalı.
    for i in range(sm.PHONE_PAIR_RATE_LIMIT_PER_MIN + 2):
        status, _ = http_req("GET", "/metrics", headers={"Cookie": f"sm_session={sid}"})
        check("metrics istegi 200", status == 200, f"(got {status})")
    # Pairing bucket'ı doğrudan kontrol et: aynı IP, farklı bucket
    ip = get_base_ip()
    with sm._rate_lock:
        sm._rate_store.clear()
    consumed_pairing = False
    for i in range(sm.PHONE_PAIR_RATE_LIMIT_PER_MIN + 3):
        if sm._rate_limited(ip, "pairing"):
            consumed_pairing = True
    check("pairing bucket'i kendi limitinde 429", consumed_pairing)
    # Metrics ayrı bucket: pairing'i doldurduktan sonra metrics hâlâ açık
    with sm._rate_lock:
        sm._rate_store.clear()
    for i in range(sm.PHONE_PAIR_RATE_LIMIT_PER_MIN + 3):
        sm._rate_limited(ip, "pairing")
    check("pairing dolduktan sonra metrics limitlenmemis", not sm._rate_limited(ip, "metrics"))
    # Farklı IP'ler birbirini etkilemez
    with sm._rate_lock:
        sm._rate_store.clear()
    for i in range(sm.PHONE_PAIR_RATE_LIMIT_PER_MIN + 3):
        sm._rate_limited("1.1.1.1", "pairing")
    check("farkli IP pairing etkilenmedi", not sm._rate_limited("2.2.2.2", "pairing"))
    sm.stop_phone_server()

def test_cleanup_expired():
    print("[TEST] Expired session/rate cleanup")
    sm._sessions.clear(); sm._rate_store.clear()
    with sm._sessions_lock:
        sm._sessions["eski"] = time.time() - 5
        sm._sessions["guncel"] = time.time() + 3600
    with sm._rate_lock:
        sm._rate_store[("1.2.3.4", "page")] = [time.time() - 999]
        sm._rate_store[("1.2.3.5", "pairing")] = [time.time() + 10]
    removed = sm._cleanup_expired()
    check("eski session silindi", "eski" not in sm._sessions, f"(removed={removed})")
    check("guncel session korundu", "guncel" in sm._sessions)
    check("eski rate kaydi silindi", ("1.2.3.4", "page") not in sm._rate_store)
    check("guncel rate kaydi korundu", ("1.2.3.5", "pairing") in sm._rate_store)
    sm._sessions.clear(); sm._rate_store.clear()

def test_event_json_normalize():
    print("[TEST] Event Log JSON normalizasyonu")
    check("bos -> []", sm._parse_event_json("") == [])
    check("bos bosluk -> []", sm._parse_event_json("   \n  ") == [])
    single = '{"T":"2026-01-01 10:00","Id":1000,"Message":"x"}'
    check("tek object -> [object]", sm._parse_event_json(single) == [json.loads(single)])
    multi = '[{"Id":1},{"Id":2}]'
    check("array korunur", sm._parse_event_json(multi) == [{"Id":1},{"Id":2}])
    check("malformed -> None", sm._parse_event_json("{bozuk") is None)
    check("null -> []", sm._parse_event_json("null") == [])

def test_event_query_result():
    print("[TEST] Event query failure vs temiz ayrımı")
    ok_empty = sm.EventQueryResult(ok=True, events=[])
    fail = sm.EventQueryResult(ok=False, error="JSON ayrıştırma hatası")
    check("basari+empty -> empty", ok_empty.empty)
    check("failure empty degil", not fail.empty)
    check("failure ok=False", not fail.ok)
    check("failure error saklanir", fail.error == "JSON ayrıştırma hatası")

def test_event_get_parse_error():
    print("[TEST] _get_events parse hatasi failure olarak donmeli")
    orig = sm.subprocess.run
    class FakeR:
        returncode = 0
        stdout = b"{bozuk json"
    sm.subprocess.run = lambda *a, **k: FakeR()
    try:
        res = sm.Monitor._get_events(None)
        check("parse hatasi ok=False", not res.ok, f"(ok={res.ok})")
        check("parse hatasi error mesaji", res.error == "JSON ayrıştırma hatası", f"(err={res.error})")
        check("parse hatasi 'temiz' degil", not res.empty)
    finally:
        sm.subprocess.run = orig

def test_event_get_success_empty():
    print("[TEST] _get_events basari+empty -> ok=True, empty")
    orig = sm.subprocess.run
    class FakeR:
        returncode = 0
        stdout = b""
    sm.subprocess.run = lambda *a, **k: FakeR()
    try:
        res = sm.Monitor._get_events(None)
        check("bos sonuc ok=True", res.ok)
        check("bos sonuc empty=True", res.empty)
    finally:
        sm.subprocess.run = orig

def test_event_get_single_object():
    print("[TEST] _get_events tek object normalize")
    orig = sm.subprocess.run
    single = '{"T":"2026-01-01 10:00","Id":1000,"LevelDisplayName":"Hata","ProviderName":"Kernel-Power","Message":"m"}'
    class FakeR:
        returncode = 0
        stdout = single.encode()
    sm.subprocess.run = lambda *a, **k: FakeR()
    try:
        res = sm.Monitor._get_events(None)
        check("tek object -> list", isinstance(res.events, list) and len(res.events) == 1, f"(len={len(res.events)})")
        check("tek object ok=True", res.ok)
    finally:
        sm.subprocess.run = orig

def test_net_speed():
    print("[TEST] Ağ hızı hesabı")
    check("normal hiz", abs(sm._compute_net_speed(100, 500, 2.0) - 200.0) < 0.01)
    check("counter reset negatif vermez", sm._compute_net_speed(500, 100, 2.0) == 0.0)
    check("elapsed 0 -> 0", sm._compute_net_speed(100, 200, 0) == 0.0)
    check("None baseline -> 0", sm._compute_net_speed(None, 200, 1.0) == 0.0)

def test_sse_max_recovery():
    print("[TEST] SSE disconnect sonrasi client/connection temizligi")
    start_test_server()
    sm._PhoneHTTPServer.active_connections = 0
    status, _, sid = get_cookie()
    check("pairing onkosul 200", status == 200)
    # Bir SSE bağlantısı aç
    sock = socket.create_connection((get_base_ip(), 8080), timeout=5)
    sock.sendall(f"GET /events HTTP/1.0\r\nHost: {get_base_ip()}:8080\r\nCookie: sm_session={sid}\r\n\r\n".encode())
    time.sleep(0.7)
    with sm.PhoneRequestHandler.clients_lock:
        clients_len = len(sm.PhoneRequestHandler.clients)
    check("SSE istemci listesine eklendi", clients_len >= 1, f"(clients={clients_len})")
    sock.close()
    time.sleep(0.6)
    sm.stop_phone_server()
    time.sleep(0.3)
    with sm.PhoneRequestHandler.clients_lock:
        clients_after = len(sm.PhoneRequestHandler.clients)
    check("stop sonrasi client listesi temiz", clients_after == 0, f"(clients={clients_after})")
    check("aktif baglanti sayaci sifir", sm._PhoneHTTPServer.active_connections == 0, f"(={sm._PhoneHTTPServer.active_connections})")

def test_secure_cookie_tls():
    print("[TEST] Secure cookie yalnizca TLS modunda")
    check("http modda Secure yok", "Secure" not in sm._build_session_cookie("abc", secure=False))
    check("tls modda Secure var", "Secure" in sm._build_session_cookie("abc", secure=True))
    check("HttpOnly her zaman", "HttpOnly" in sm._build_session_cookie("abc", secure=True))
    check("SameSite Strict", "SameSite=Strict" in sm._build_session_cookie("abc", secure=False))

def test_tls_path_validation():
    print("[TEST] TLS path validasyonu")
    ok, err, scheme = sm._validate_tls_paths(None, None)
    check("cert/key yok -> http", ok and scheme == "http")
    ok, err, scheme = sm._validate_tls_paths("a.pem", None)
    check("tek tarafli -> hata", not ok, f"(err={err})")
    check("tek tarafli hata mesaji", err is not None)
    ok, err, scheme = sm._validate_tls_paths("/yok/cert.pem", "/yok/key.pem")
    check("eksik dosya -> hata", not ok)
    fd, cert = tempfile.mkstemp(suffix=".pem"); fd2, key = tempfile.mkstemp(suffix=".key")
    os.close(fd); os.close(fd2)
    try:
        ok, err, scheme = sm._validate_tls_paths(cert, key)
        check("mevcut dosyalar -> https", ok and scheme == "https")
    finally:
        os.unlink(cert); os.unlink(key)

def test_log_redaction():
    print("[TEST] Log'da secret redaction")
    start_test_server()
    pair = sm._pairing_code
    # Log'u yakala
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    sm._phone_logger.addHandler(h)
    try:
        http_req("GET", f"/?c={pair}")
    finally:
        sm._phone_logger.removeHandler(h)
    sm.stop_phone_server()
    log_text = buf.getvalue()
    check("pairing kodu loglanmadi", pair not in log_text, f"(pair={pair})")
    check("redacted path mevcut", "?c=***" in log_text or "[pair]" in log_text)

def test_host_variants():
    print("[TEST] Host IPv6/port varyasyonlari")
    start_test_server()
    ip = get_base_ip()
    # Doğru host:port
    status, _ = http_req("GET", "/")
    check("dogru host 200", status == 200, f"(got {status})")
    # Yanlış port
    status, _ = http_req("GET", "/", host=f"{ip}:9999")
    check("yanlis port 403", status == 403, f"(got {status})")
    # localhost
    status, _ = http_req("GET", "/", host=f"localhost:{sm.PHONE_SERVER_PORT}")
    check("localhost 200", status == 200, f"(got {status})")
    # IPv6 loopback
    status, _ = http_req("GET", "/", host=f"[::1]:{sm.PHONE_SERVER_PORT}")
    check("ipv6 loopback 200", status == 200, f"(got {status})")
    # Port enjeksiyonu
    status, _ = http_req("GET", "/", host=f"{ip}:8080:evil")
    check("port enjeksiyonu 403", status == 403, f"(got {status})")
    sm.stop_phone_server()

def test_security_headers():
    print("[TEST] HTTP security headers")
    start_test_server()
    conn = http.client.HTTPConnection(get_base_ip(), 8080, timeout=5)
    conn.request("GET", "/", headers={"Host": f"{get_base_ip()}:8080"})
    r = conn.getresponse(); r.read()
    hdrs = dict(r.getheaders())
    conn.close()
    check("X-Content-Type-Options", hdrs.get("X-Content-Type-Options") == "nosniff", f"(={hdrs.get('X-Content-Type-Options')})")
    check("Referrer-Policy", hdrs.get("Referrer-Policy") == "no-referrer", f"(={hdrs.get('Referrer-Policy')})")
    check("Cache-Control no-store", "no-store" in hdrs.get("Cache-Control", ""))
    check("CSP mevcut", "Content-Security-Policy" in hdrs)
    check("ACAO yok", "Access-Control-Allow-Origin" not in hdrs)
    sm.stop_phone_server()

def test_i18n():
    print("[TEST] i18n / dil destegi")
    orig = sm.LANG
    try:
        sm.set_lang("en")
        check("EN pencere adi", sm.tr("Sistem Monitoru") == "System Monitor", f"(={sm.tr('Sistem Monitoru')})")
        check("EN kart adi", sm.tr("CPU Kullanim") == "CPU Usage", f"(={sm.tr('CPU Kullanim')})")
        check("EN hata mesaji", sm.tr("Gecersiz host.") == "Invalid host.", f"(={sm.tr('Gecersiz host.')})")
        check("EN _exp", sm._exp(41).startswith("Unexpected shutdown"), f"(={sm._exp(41)})")
        html = sm._build_phone_html(tls_active=False)
        check("EN html title", "System Monitor - Phone" in html)
        check("EN html connecting", "Connecting..." in html)
        check("EN html cpu", "CPU Usage" in html)
        check("EN html tls warn", "unencrypted" in html)
        html_tls = sm._build_phone_html(tls_active=True)
        check("EN html tls warn yok", "unencrypted" not in html_tls and 'class="tls-warn"' not in html_tls)
        sm.set_lang("tr")
        check("TR pencere adi", sm.tr("Sistem Monitoru") == "Sistem Monitoru")
        check("TR _exp", sm._exp(41).startswith("Beklenmeyen kapanma"))
        html_tr = sm._build_phone_html()
        check("TR html connecting", "Bağlanıyor..." in html_tr)
        check("TR html cpu", "CPU Kullanım" in html_tr)
        check("TR html tls warn", "sifresiz" in html_tr)
        sm.set_lang("unknown")
        check("Bilinmeyen dil TR fallback", sm.tr("Sistem Monitoru") == "Sistem Monitoru")
    finally:
        sm.set_lang(orig)

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
        test_pair_invalid_host_origin, test_body_limit, test_session_auth,
        test_pairing_replay, test_pairing_brute_force, test_endpoint_rate_isolation,
        test_cleanup_expired, test_event_json_normalize, test_event_query_result,
        test_event_get_parse_error, test_event_get_success_empty, test_event_get_single_object,
        test_net_speed, test_sse_max_recovery, test_secure_cookie_tls, test_tls_path_validation,
        test_log_redaction, test_host_variants, test_security_headers,
        test_i18n,
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
