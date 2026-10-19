"""
Tapo cloud client: login, JWT, a persistent MQTT session (relay requests + device
events), and the relay stream reader.  Reconstructed from the Tapo Android app
(see ~/tapo/CLAUDE.md); no local network, no site hardware.

Only one MQTT connection may exist per terminal UUID (a second one kicks the first
off), so the web app owns the single MqttSession and the stream pipes ask it for
relay credentials over loopback (see pipe.py).
"""
import base64, hashlib, hmac, json, os, re, socket, ssl, threading, time, uuid, logging
from urllib.parse import urlparse
import requests, urllib3
import paho.mqtt.client as mqtt

urllib3.disable_warnings()
log = logging.getLogger("tapo")

# App-level constants from the APK (identical in every install - not secret).
AK, SK = "4d11b6b9d5ea4d19a829adbb9714b057", "6ed7d97f3e73467f8a5bab90b577ba4c"
APP, VER = "TP-Link_Tapo_Android", "3.7.113"
GW = "https://n-wap-gw.tplinkcloud.com"
DEFAULT_MQTT_GW = "use1-app-cloudgateway.iot.i.tplinknbu.com"
DEFAULT_SECURITY = "https://use1-security.iot.i.tplinknbu.com"
CLIENT_BOUNDARY = "--client-stream-boundary--"
DEVICE_BOUNDARY = b"----device-stream-boundary--"


class TapoCloud:
    def __init__(self, email, password, term_uuid, cache_path=None):
        self.email, self.password, self.term = email, password, term_uuid
        self.cache_path = cache_path
        self._token = None
        self._lock = threading.Lock()
        if cache_path and os.path.exists(cache_path):
            try:
                self._token = json.load(open(cache_path)).get("token")
            except Exception:
                pass

    # ---------------------------------------------------------------- auth
    def login(self, force=False):
        with self._lock:
            if self._token and not force:
                return self._token
            body = {"appType": APP, "appVersion": VER, "cloudPassword": self.password,
                    "cloudUserName": self.email, "platform": "Android 10", "refreshTokenNeeded": False,
                    "terminalMeta": "1", "terminalName": "cameras.andrewmcgrath.info", "terminalUUID": self.term}
            b = json.dumps(body, separators=(',', ':')).encode()
            md5 = base64.b64encode(hashlib.md5(b).digest()).decode()
            nonce = str(uuid.uuid4()); path = "/api/v2/account/login"
            sig = hmac.new(SK.encode(), f"{md5}\n9999999999\n{nonce}\n{path}".encode(), hashlib.sha1).hexdigest()
            h = {"Content-Type": "application/json; charset=UTF-8", "Content-MD5": md5,
                 "X-Authorization": f"Timestamp=9999999999, Nonce={nonce}, AccessKey={AK}, Signature={sig}"}
            r = requests.post(GW + path, params={"appName": APP, "appVer": VER, "termID": self.term},
                              headers=h, data=b, verify=False, timeout=20).json()
            if r.get("error_code") != 0:
                raise RuntimeError(f"Tapo login failed: {r}")
            self._token = r["result"]["token"]
            if self.cache_path:
                try:
                    json.dump({"token": self._token, "at": time.time()}, open(self.cache_path, "w"))
                except Exception:
                    pass
            return self._token

    def _call(self, method, params=None, retry=True):
        token = self.login()
        r = requests.post(GW, params={"token": token}, json={"method": method, "params": params or {}},
                          verify=False, timeout=20).json()
        if r.get("error_code") != 0:
            if retry and r.get("error_code") in (-20651, -20675, -20571, -23003):  # token expired / invalid
                self.login(force=True)
                return self._call(method, params, retry=False)
            raise RuntimeError(f"{method} failed: {r}")
        return r.get("result", {})

    def device_list(self):
        return self._call("getDeviceList").get("deviceList", [])

    def service_urls(self, ids):
        try:
            return self._call("getAppServiceUrl", {"serviceIds": ids}).get("serviceUrls", {})
        except Exception:
            return {}

    def jwt(self):
        urls = self.service_urls(["nbu.iot-cloud-gateway.app", "nbu.iot-security.app"])
        sec = urls.get("nbu.iot-security.app") or DEFAULT_SECURITY
        gw = urls.get("nbu.iot-cloud-gateway.app") or DEFAULT_MQTT_GW
        base = sec if sec.startswith("http") else "https://" + sec
        r = requests.post(base.rstrip("/") + "/v1/auth/app",
                          json={"appType": APP, "terminalUUID": self.term, "token": f"ut|{self.login()}"},
                          verify=False, timeout=20).json()
        if not r.get("jwt"):
            self.login(force=True)
            r = requests.post(base.rstrip("/") + "/v1/auth/app",
                              json={"appType": APP, "terminalUUID": self.term, "token": f"ut|{self.login()}"},
                              verify=False, timeout=20).json()
        return r.get("jwt"), gw


class MqttSession:
    """One long-lived MQTT connection: relay requests for streams/health checks and
    a hook for device event messages.  Reconnects itself."""

    def __init__(self, cloud: TapoCloud, on_event=None):
        self.cloud = cloud
        self.on_event = on_event          # callback(topic:str, payload:dict|bytes)
        self.client_id = f"app:{APP}:{cloud.term}"
        self.username = f"{APP}:v{VER}:Android:en_US"
        self._c = None
        self._connected = threading.Event()
        self._pending = {}                # device_id -> {"ev": Event, "reply": dict}
        self._plock = threading.Lock()
        self._subs = set()
        self._stop = False
        self.last_error = None
        self.relay_fails = 0           # consecutive relay requests with no reply
        self._reset_wanted = False

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        threading.Thread(target=self._run, name="mqtt", daemon=True).start()

    def stop(self):
        self._stop = True
        try:
            if self._c: self._c.disconnect()
        except Exception:
            pass

    def _run(self):
        backoff = 5
        while not self._stop:
            try:
                self._connect_once()
                backoff = 5
                while not self._stop and self._connected.is_set() and not self._reset_wanted:
                    time.sleep(1)
                if self._reset_wanted:
                    self._reset_wanted = False
                    log.warning("mqtt: relay requests keep failing, logging in again and reconnecting")
                    try:
                        self.cloud.login(force=True)
                    except Exception as e:
                        log.warning("re-login failed: %s", e)
                    try:
                        self._c.disconnect()
                    except Exception:
                        pass
            except Exception as e:
                self.last_error = str(e)
                log.warning("mqtt: %s (retry in %ss)", e, backoff)
            self._connected.clear()
            try:
                if self._c: self._c.loop_stop()
            except Exception:
                pass
            time.sleep(backoff); backoff = min(backoff * 2, 120)

    def _connect_once(self):
        jwt, gw = self.cloud.jwt()
        if not jwt:
            raise RuntimeError("no JWT")
        host = urlparse(gw if "://" in gw else "wss://" + gw).hostname
        c = mqtt.Client(client_id=self.client_id, clean_session=True, protocol=mqtt.MQTTv311, transport="websockets")
        c.ws_set_options(path="/mqtt")
        c.tls_set(cert_reqs=ssl.CERT_NONE); c.tls_insecure_set(True)
        c.username_pw_set(self.username, jwt)
        rc_box = {}
        ev = threading.Event()

        def on_connect(cl, u, f, rc, *a):
            rc_box["rc"] = rc
            if rc == 0:
                self._connected.set()
            ev.set()

        def on_disconnect(cl, u, rc, *a):
            log.info("mqtt disconnected rc=%s", rc)
            self._connected.clear()

        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, self._on_message
        self._c = c                      # before connect: request_relay may run as soon as on_connect fires
        c.connect(host, 443, keepalive=235)
        c.loop_start()
        if not ev.wait(20) or rc_box.get("rc") != 0:
            c.loop_stop()
            raise RuntimeError(f"MQTT connect failed rc={rc_box.get('rc')}")
        c.subscribe([(f"$tpiot/app/{self.client_id}/refresh/reply", 1),
                     (f"$tpiot/apps/{APP}/{self.cloud.term}/api/reply", 1)])
        for t in list(self._subs):
            c.subscribe(t, 1)
        c.publish(f"$tpiot/app/{self.client_id}/refresh",
                  json.dumps({"appType": APP, "terminalUUID": self.cloud.term, "token": f"ut|{self.cloud.login()}"}), qos=1)
        log.info("mqtt connected to %s", host)

    def subscribe(self, topic):
        self._subs.add(topic)
        if self._c and self._connected.is_set():
            self._c.subscribe(topic, 1)

    def _on_message(self, c, u, m):
        topic = m.topic
        try:
            payload = json.loads(m.payload)
        except Exception:
            payload = m.payload
        mm = re.match(r"\$tpiot/things/([0-9A-Fa-f]+)/relay/request/get/reply", topic)
        if mm:
            with self._plock:
                p = self._pending.get(mm.group(1))
            if p:
                p["reply"] = payload; p["ev"].set()
            return
        if self.on_event:
            try:
                self.on_event(topic, payload)
            except Exception:
                log.exception("on_event")

    # ---------------------------------------------------------------- relay
    @property
    def connected(self):
        return self._connected.is_set()

    def request_relay(self, device_id, timeout=12, resolution="HD", stream_type=0):
        """Ask the camera (via the cloud) for a relay slot. Returns (relayUrl, relayToken).
        stream_type 0 = live preview (the camera streams as soon as we connect);
        1 = a plain session that waits for requests (used for SD-card playback).
        Raises RuntimeError with the device's error when it is offline."""
        if not self._connected.wait(15):
            raise RuntimeError("MQTT not connected")
        reply_topic = f"$tpiot/things/{device_id}/relay/request/get/reply"
        p = {"ev": threading.Event(), "reply": None}
        with self._plock:
            self._pending[device_id] = p
        try:
            self._c.subscribe(reply_topic, 1)
            param = {"userToken": f"ut|{self.cloud.login()}", "timestamp": int(time.time() * 1000),
                     "requestRelayParams": {"relayParamsJson": {
                         "deviceId": device_id, "deviceType": "SMART.IPCAMERA", "cloudType": 2, "streamType": stream_type,
                         "preConnection": 0, "resolution": resolution, "playerId": str(uuid.uuid4()).upper(),
                         "rootCaVer": "1", "dataTimeoutCount": 0}},
                     "clientToken": f"{device_id}_{int(time.time())}", "userAgent": f"{APP}/{VER}",
                     "clientId": self.cloud.term, "caType": "cloud-self", "protocolVersion": 2}
            self._c.publish(f"$tpiot/things/{device_id}/relay/request/get", json.dumps(param), qos=1)
            if not p["ev"].wait(timeout):
                self.relay_fails += 1
                if self.relay_fails >= 3:
                    self.relay_fails = 0
                    self._reset_wanted = True
                raise RuntimeError("no relay reply (device offline?)")
            self.relay_fails = 0
            rep = p["reply"] or {}
            res = (rep.get("relayRespJson") or rep).get("result") or {}
            if not res.get("relayUrl"):
                raise RuntimeError(f"relay refused: {json.dumps(rep)[:300]}")
            return res["relayUrl"], res["relayToken"]
        finally:
            with self._plock:
                self._pending.pop(device_id, None)


# -------------------------------------------------------------------- relay stream
def relay_headers(relay_url, relay_token, term):
    return {
        "User-Agent": f"Client={APP} Android {VER}/1.3",
        "Keep-Relay": "3600",
        "Accept": "*/*",
        "Content-Type": f"multipart/mixed;boundary={CLIENT_BOUNDARY}",
        "Host": urlparse(relay_url).hostname,
        "Content-Length": "9223372036854775807",
        "X-token": relay_token,
        "X-Client-Model": "Python",
        "X-Client-UUID": term,
        "X-Redirect-Times": "0",
        "X-Pull-Mode": "2",
        "X-Version": "2.0",
        "X-Arrive-Latency": "-1",
    }


def connect_relay(relay_url, relay_token, term, timeout=15):
    u = urlparse(relay_url); host, port = u.hostname, u.port or 443
    path = u.path + ("?" + u.query if u.query else "") + "&retryTime=0"
    req = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in relay_headers(relay_url, relay_token, term).items()) + "\r\n"
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(socket.create_connection((host, port), timeout=timeout), server_hostname=host)
    sock.sendall(req.encode())
    return sock


def iter_parts(sock, timeout=20):
    """Incrementally demux the relay's multipart/mixed response.
    Yields (headers:dict, body:bytes) per part.  Raises on EOF/timeout/non-200."""
    sock.settimeout(timeout)
    buf = bytearray()
    # HTTP status + headers
    while b"\r\n\r\n" not in buf:
        c = sock.recv(65536)
        if not c:
            raise RuntimeError("relay closed before headers")
        buf += c
    i = buf.find(b"\r\n\r\n")
    status = bytes(buf[:i]).decode("latin1", "replace")
    if " 200 " not in status.splitlines()[0]:
        raise RuntimeError("relay: " + status.splitlines()[0])
    del buf[:i + 4]
    while True:
        b = buf.find(DEVICE_BOUNDARY)
        if b >= 0:
            del buf[:b + len(DEVICE_BOUNDARY)]
            he = buf.find(b"\r\n\r\n")
            while he < 0:
                c = sock.recv(65536)
                if not c:
                    raise RuntimeError("relay closed")
                buf += c; he = buf.find(b"\r\n\r\n")
            hdr = {}
            for line in bytes(buf[:he]).decode("latin1", "replace").split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1); hdr[k.strip().lower()] = v.strip()
            n = int(hdr.get("content-length", "0") or 0)
            del buf[:he + 4]
            while len(buf) < n:
                c = sock.recv(65536)
                if not c:
                    raise RuntimeError("relay closed")
                buf += c
            body = bytes(buf[:n]); del buf[:n]
            yield hdr, body
        else:
            c = sock.recv(65536)
            if not c:
                raise RuntimeError("relay closed")
            buf += c


def iter_ts(sock, timeout=20):
    for hdr, body in iter_parts(sock, timeout):
        if hdr.get("content-type", "").startswith("video/mp2t"):
            yield body


# -------------------------------------------------------------------- device calls via the cloud
APP_SERVER = "https://use1-app-server.iot.i.tplinknbu.com"


def device_call(cloud: TapoCloud, device_id, requests_list, timeout=40):
    """Run camera API methods through the cloud ("passthrough" thing service).
    Returns the list of per-method responses ({method, result, error_code})."""
    urls = cloud.service_urls(["nbu.iot-app-server.app"])
    base = urls.get("nbu.iot-app-server.app") or APP_SERVER
    if not base.startswith("http"):
        base = "https://" + base
    body = {"serviceId": "passthrough", "inputParams": {"requestData": {"method": "multipleRequest", "params": {"requests": requests_list}}}}
    for attempt in (0, 1):
        h = {"Content-Type": "application/json; charset=UTF-8", "Authorization": f"ut|{cloud.login(force=bool(attempt))}",
             "app-cid": f"app:{APP}:{cloud.term}"}
        r = requests.post(f"{base}/v1/things/{device_id}/services-sync", headers=h, json=body, verify=False, timeout=timeout)
        if r.status_code in (401, 403) and attempt == 0:
            continue
        if r.status_code != 200:
            raise RuntimeError(f"device call {r.status_code}: {r.text[:200]}")
        j = r.json()
        rd = (j.get("outputParams") or {}).get("responseData") or {}
        if rd.get("error_code", 0) != 0:
            raise RuntimeError(f"device error {rd.get('error_code')}")
        return (rd.get("result") or {}).get("responses", [])
    raise RuntimeError("device call failed")


def _unwrap_list(items):
    """The camera returns lists as [{"x_1": {...}}, {"x_2": {...}}]; flatten to [{...}, ...]."""
    out = []
    for it in items or []:
        for _, v in it.items():
            out.append(v)
    return out


def recording_dates(cloud, device_id, start="20000101", end=None):
    end = end or time.strftime("%Y%m%d")
    r = device_call(cloud, device_id, [{"method": "searchDateWithVideo", "params": {"playback": {"search_year_utility": {"channel": [0], "end_date": end, "start_date": start}}}}])
    res = r[0].get("result", {}).get("playback", {}) if r else {}
    return [d["date"] for d in _unwrap_list(res.get("search_results"))]


_user_ids = {}


def user_id(cloud, device_id):
    """The camera's own id for this account, needed by the playback methods (differs per camera)."""
    if device_id not in _user_ids:
        r = device_call(cloud, device_id, [{"method": "getUserID", "params": {"system": {"get_user_id": "null"}}}])
        _user_ids[device_id] = int(r[0].get("result", {}).get("user_id", 0)) if r else 0
    return _user_ids[device_id]


def recordings_of_day(cloud, device_id, date):
    """Clips on one day (YYYYMMDD) as [{start, end, type}] in epoch seconds."""
    uid = user_id(cloud, device_id)
    res = {}
    for attempt in range(4):   # the camera answers -71103 while its card is busy (e.g. a playback just ended)
        r = device_call(cloud, device_id, [{"method": "searchVideoOfDay", "params": {"playback": {"search_video_utility": {"channel": 0, "date": date, "end_index": 999, "id": uid, "start_index": 0}}}}])
        res = r[0] if r else {}
        if not res.get("error_code"):
            break
        time.sleep(1.5)
    if res.get("error_code"):
        raise RuntimeError(f"the camera is busy (error {res['error_code']}), try again in a moment")
    clips = _unwrap_list(res.get("result", {}).get("playback", {}).get("search_video_results"))
    return [{"start": int(c["startTime"]), "end": int(c["endTime"]), "type": c.get("vedio_type")} for c in clips]


# Detection kinds, from the app's playback event table. alarm_type carries the main
# kind; the "events_1" bitmask has bit (kind-1) set for every kind in the detection
# (e.g. 66048 = bits 9 and 16 = ring + missed doorbell ring).
ALARM_TYPES = {1: "scheduled", 2: "motion", 3: "tamper", 4: "line crossing", 5: "intrusion", 6: "person", 7: "baby cry",
               8: "vehicle", 9: "pet", 10: "ring", 11: "bark", 12: "meow", 13: "glass break", 14: "smoke alarm",
               15: "package delivered", 16: "package picked up", 17: "missed doorbell ring", 18: "doorbell ring answered",
               19: "anti-theft", 20: "sound", 21: "manual recording", 22: "person"}
EVENT_BITS = {1 << (k - 1): v for k, v in ALARM_TYPES.items()}


def describe_detection(d):
    """Human labels for one detection-list entry."""
    labels = []
    bits = 0
    for k, v in d.items():
        if k.startswith("events_"):
            try: bits |= int(v)
            except Exception: pass
    for bit, name in EVENT_BITS.items():
        if bits & bit:
            labels.append(name)
    at = ALARM_TYPES.get(d.get("alarm_type"))
    if at and at not in labels:
        labels.append(at)
    return labels or ["motion"], bits


def detections_of_day(cloud, device_id, day_start, day_end):
    """Detections (motion / person / ring ...) between two epoch times, newest last."""
    uid = user_id(cloud, device_id)
    out = []; idx = 0
    while True:
        r = device_call(cloud, device_id, [{"method": "searchDetectionList", "params": {"playback": {"search_detection_list": {
            "channel": 0, "start_time": int(day_start), "end_time": int(day_end), "start_index": idx, "end_index": idx + 199, "id": uid}}}}])
        res = r[0] if r else {}
        if res.get("error_code"):
            if res["error_code"] == -71114:   # no card / not supported
                return []
            raise RuntimeError(f"searchDetectionList error {res['error_code']}")
        pb = res.get("result", {}).get("playback", {})
        items = pb.get("search_detection_list", [])
        for d in items:
            labels, bits = describe_detection(d)
            out.append({"start": int(d["start_time"]), "end": int(d.get("end_time", d["start_time"])), "labels": labels,
                        "alarm_type": d.get("alarm_type"), "bits": bits})
        total = int(pb.get("total_num", len(out)))
        idx += len(items)
        if not items or idx >= total:
            break
    out.sort(key=lambda e: e["start"])
    return out


def sd_status(cloud, device_id):
    r = device_call(cloud, device_id, [{"method": "getSdCardStatus", "params": {"harddisk_manage": {"table": "hd_info"}}}])
    info = _unwrap_list(r[0].get("result", {}).get("harddisk_manage", {}).get("hd_info")) if r else []
    log.info("sd status %s: %s", device_id[:8], json.dumps(info)[:400])
    return info[0] if info else None


def _client_part(obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    return (b"--" + CLIENT_BOUNDARY.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")


def iter_recording(mq, device_id, term, start, end, mode="download", timeout=25, cloud=None):
    """Yield MPEG-TS bytes of the SD-card recording between two epoch times.
    mode "download" = as fast as the link allows (what the app uses for clips),
    "playback" = paced at real time."""
    uid = user_id(cloud, device_id) if cloud else 1
    url, tok = mq.request_relay(device_id, timeout=15, stream_type=1)
    sock = connect_relay(url, tok, term)
    if mode == "download":
        params = {"download": {"client_id": uid, "channels": [0], "media_type": 0, "start_time": str(int(start)),
                               "end_time": str(int(end)), "player_id": str(uuid.uuid4()).upper()}, "method": "get"}
    else:
        params = {"playback": {"client_id": uid, "channels": [0, 1], "scale": "1/1", "start_time": str(int(start)),
                               "end_time": str(int(end)), "event_type": [1, 2]}, "method": "get"}
    sock.sendall(_client_part({"type": "request", "seq": 1, "params": params}))
    try:
        for hdr, body in iter_parts(sock, timeout=timeout):
            ct = hdr.get("content-type", "")
            if ct.startswith("video/mp2t"):
                yield body
            elif "json" in ct:
                try:
                    j = json.loads(body)
                except Exception:
                    continue
                p = j.get("params", {})
                if j.get("type") == "response" and p.get("error_code", 0) != 0:
                    raise RuntimeError(f"camera refused playback: {p.get('error_code')}")
                if j.get("type") == "notification" and (
                        (p.get("event_type") == "stream_status" and p.get("status") == "finished")
                        or p.get("event_type") == "connection_closed"):
                    return
    finally:
        try: sock.close()
        except Exception: pass
