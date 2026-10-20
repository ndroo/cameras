"""
A minimal RTSP server so go2rtc can take the cameras' sound as-is.

go2rtc reads G.711 properly only from RTSP (its MPEG-TS and FLV readers take AAC
audio only), and it converts G.711 to FLAC for browsers by itself. So instead of an
ffmpeg per live camera encoding AAC, this serves each camera over RTSP on loopback:
the relay's MPEG-TS is split into H.264 access units and G.711 frames (timestamps
kept) and sent as RTP over the RTSP connection (TCP interleaved).

Only what go2rtc's client needs: OPTIONS, DESCRIBE, SETUP (TCP), PLAY, TEARDOWN,
GET_PARAMETER keepalives. Paths are /<secret>/<camera id>.
"""
import base64, logging, os, random, re, socket, struct, threading

log = logging.getLogger("rtsp")

VIDEO_TYPES = {0x1B: "H264"}
AUDIO_TYPES = {0x90: ("PCMA", 8000, 8), 0x91: ("PCMU", 16000, 97)}   # name, clock rate, RTP payload type
MTU = 1400


def _pts(pes):
    if len(pes) < 14 or pes[:3] != b"\x00\x00\x01" or not pes[7] & 0x80:
        return None
    p = pes[9:14]
    return ((p[0] >> 1) & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1


def nal_units(es):
    """Split an Annex B byte stream into NAL units (start codes removed)."""
    out, i, n = [], 0, len(es)
    starts = [m.end() for m in re.finditer(b"\x00\x00\x01", es)]
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        nal = es[s:e]
        while nal.endswith(b"\x00"):        # trailing zero of a 4-byte start code
            nal = nal[:-1]
        if nal:
            out.append(nal)
    return out


class TsDemux:
    """Feed TS chunks; collects complete PES packets as (kind, pts, payload)."""

    def __init__(self):
        self.buf = bytearray(); self.pmt_pid = None
        self.video_pid = self.audio_pid = None
        self.audio = None                   # (name, rate, pt)
        self.pes = {}                       # pid -> [pts, bytearray]
        self.out = []

    def feed(self, chunk):
        self.buf += chunk
        n = len(self.buf) - len(self.buf) % 188
        for i in range(0, n, 188):
            self._packet(self.buf[i:i + 188])
        del self.buf[:n]
        out, self.out = self.out, []
        return out

    def _packet(self, p):
        if p[0] != 0x47:
            return
        pid = (p[1] & 0x1F) << 8 | p[2]; pusi = p[1] & 0x40; afc = (p[3] >> 4) & 3
        if not afc & 1:
            return
        pl = p[4 + (1 + p[4] if afc & 2 else 0):]
        if pid == 0 and pusi and self.pmt_pid is None:
            pl = pl[1 + pl[0]:]
            if len(pl) >= 12:
                self.pmt_pid = (pl[10] & 0x1F) << 8 | pl[11]
        elif pid == self.pmt_pid and pusi and self.video_pid is None:
            self._pmt(pl[1 + pl[0]:])
        elif pid in (self.video_pid, self.audio_pid):
            if pusi:
                self._flush(pid)
                self.pes[pid] = [_pts(pl), bytearray(pl)]
            elif pid in self.pes:
                self.pes[pid][1] += pl

    def _pmt(self, s):
        if len(s) < 12:
            return
        end = 3 + ((s[1] & 0x0F) << 8 | s[2]) - 4
        j = 12 + ((s[10] & 0x0F) << 8 | s[11])
        while j + 5 <= end:
            t, pid, il = s[j], (s[j + 1] & 0x1F) << 8 | s[j + 2], (s[j + 3] & 0x0F) << 8 | s[j + 4]
            if t in VIDEO_TYPES and self.video_pid is None:
                self.video_pid = pid
            elif t in AUDIO_TYPES and self.audio_pid is None:
                self.audio_pid = pid; self.audio = AUDIO_TYPES[t]
            j += 5 + il

    def _flush(self, pid):
        cur = self.pes.pop(pid, None)
        if not cur or cur[0] is None:
            return
        pes = bytes(cur[1])
        self.out.append(("video" if pid == self.video_pid else "audio", cur[0], pes[9 + pes[8]:]))


class RtpOut:
    """Packs frames as RTP inside RTSP interleaved framing ($, channel, length)."""

    def __init__(self, send, channel, pt, rate):
        self.send, self.ch, self.pt, self.rate = send, channel, pt, rate
        self.seq = random.randint(0, 65535); self.ssrc = random.getrandbits(32)

    def _packet(self, payload, ts, marker):
        hdr = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | self.pt, self.seq, ts & 0xFFFFFFFF, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        rtp = hdr + payload
        return b"$" + bytes([self.ch]) + struct.pack("!H", len(rtp)) + rtp

    def h264(self, nals, pts):
        parts = []
        for k, nal in enumerate(nals):
            last = k == len(nals) - 1
            if len(nal) <= MTU:
                parts.append(self._packet(nal, pts, last))
                continue
            ind, typ, body = (nal[0] & 0xE0) | 28, nal[0] & 0x1F, nal[1:]      # FU-A
            for o in range(0, len(body), MTU):
                start, end = o == 0, o + MTU >= len(body)
                parts.append(self._packet(bytes([ind, (0x80 if start else 0) | (0x40 if end else 0) | typ]) + body[o:o + MTU],
                                          pts, last and end))
        self.send(b"".join(parts))

    def audio(self, data, pts):
        ts = pts * self.rate // 90000
        parts = []
        for o in range(0, len(data), 1024):
            parts.append(self._packet(data[o:o + 1024], ts + o, False))
        self.send(b"".join(parts))


class Server:
    """RTSP on loopback. `open_ts(cam_id)` returns (iterator of TS chunks, close())."""

    def __init__(self, open_ts, secret, port=8554):
        self.open_ts, self.secret, self.port = open_ts, secret, port

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port)); srv.listen(32)
        threading.Thread(target=self._accept, args=(srv,), name="rtsp", daemon=True).start()
        log.info("rtsp listening on 127.0.0.1:%d", self.port)

    def _accept(self, srv):
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=Session(self, conn).run, name="rtsp-session", daemon=True).start()


class Session:
    def __init__(self, server, conn):
        self.srv, self.conn = server, conn
        self.rd = conn.makefile("rb")
        self.lock = threading.Lock()
        self.session = "%08x" % random.getrandbits(32)
        self.cam = None; self.ts = None; self.close_ts = None
        self.demux = TsDemux(); self.pending = []
        self.sps = self.pps = None
        self.channels = {}                  # track -> interleaved channel
        self.playing = threading.Event(); self.done = False

    def send(self, b):
        with self.lock:
            self.conn.sendall(b)

    def reply(self, cseq, code=200, reason="OK", headers=None, body=b""):
        h = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}", f"Session: {self.session};timeout=60"]
        for k, v in (headers or {}).items():
            h.append(f"{k}: {v}")
        if body:
            h += ["Content-Type: application/sdp", f"Content-Length: {len(body)}"]
        self.send(("\r\n".join(h) + "\r\n\r\n").encode() + body)

    def run(self):
        try:
            while not self.done:
                first = self.rd.read(1)
                if not first:
                    break
                if first == b"$":           # interleaved RTCP from the client: skip it
                    ch_len = self.rd.read(3)
                    self.rd.read(struct.unpack("!H", ch_len[1:])[0])
                    continue
                lines = [first + self.rd.readline()]
                while lines[-1] not in (b"\r\n", b"\n", b""):
                    lines.append(self.rd.readline())
                req = [l.decode("latin1").strip() for l in lines if l.strip()]
                hdr = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in req[1:])}
                if int(hdr.get("content-length", 0) or 0):
                    self.rd.read(int(hdr["content-length"]))
                self.handle(req[0].split(" "), hdr)
        except (OSError, ValueError) as e:
            log.info("rtsp session ended: %s", e)
        finally:
            self.stop()

    def handle(self, line, hdr):
        method, url = line[0], line[1] if len(line) > 1 else ""
        cseq = hdr.get("cseq", "0")
        if method == "OPTIONS":
            return self.reply(cseq, headers={"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"})
        if method == "GET_PARAMETER":
            return self.reply(cseq)
        if method == "DESCRIBE":
            m = re.search(r"/([^/]+)/(\d+)", url)
            if not m or m.group(1) != self.srv.secret:
                return self.reply(cseq, 401, "Unauthorized")
            try:
                self.cam = int(m.group(2))
                self.ts, self.close_ts = self.srv.open_ts(self.cam)
                self.prime()
            except Exception as e:
                log.info("rtsp cam%s: %s", m.group(2), e)
                return self.reply(cseq, 503, "Service Unavailable")
            return self.reply(cseq, headers={"Content-Base": url.rstrip("/") + "/"}, body=self.sdp())
        if method == "SETUP":
            track = 1 if url.rstrip("/").endswith("trackID=1") else 0
            tr = hdr.get("transport", "")
            m = re.search(r"interleaved=(\d+)", tr)
            if "TCP" not in tr.upper():
                return self.reply(cseq, 461, "Unsupported Transport")
            ch = int(m.group(1)) if m else track * 2
            self.channels[track] = ch
            return self.reply(cseq, headers={"Transport": f"RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}"})
        if method == "PLAY":
            self.reply(cseq, headers={"Range": "npt=0.000-"})
            threading.Thread(target=self.stream, name=f"rtsp-cam{self.cam}", daemon=True).start()
            return
        if method == "TEARDOWN":
            self.reply(cseq); self.done = True
            return
        self.reply(cseq, 501, "Not Implemented")

    def prime(self):
        """Read until we know the SPS/PPS and the audio format, keeping what we read."""
        got = 0
        for chunk in self.ts:
            got += len(chunk)
            for kind, pts, data in self.demux.feed(chunk):
                self.pending.append((kind, pts, data))
                if kind == "video":
                    for nal in nal_units(data):
                        t = nal[0] & 0x1F
                        if t == 7: self.sps = nal
                        elif t == 8: self.pps = nal
            if self.sps and self.pps and (self.demux.audio or got > 2_000_000):
                return
            if got > 4_000_000:
                raise RuntimeError("no H.264 parameter sets in the stream")

    def sdp(self):
        sp = base64.b64encode(self.sps).decode() + "," + base64.b64encode(self.pps).decode()
        pl = self.sps[1:4].hex()
        s = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", f"s=cam{self.cam}", "c=IN IP4 0.0.0.0", "t=0 0",
             "m=video 0 RTP/AVP 96", "a=rtpmap:96 H264/90000",
             f"a=fmtp:96 packetization-mode=1;profile-level-id={pl};sprop-parameter-sets={sp}", "a=control:trackID=0"]
        if self.demux.audio:
            name, rate, pt = self.demux.audio
            s += [f"m=audio 0 RTP/AVP {pt}", f"a=rtpmap:{pt} {name}/{rate}", "a=control:trackID=1"]
        return ("\r\n".join(s) + "\r\n").encode()

    def stream(self):
        v = RtpOut(self.send, self.channels.get(0, 0), 96, 90000)
        a = RtpOut(self.send, self.channels.get(1, 2), self.demux.audio[2], self.demux.audio[1]) if self.demux.audio and 1 in self.channels else None
        try:
            def frames():
                yield from self.pending
                self.pending = []
                for chunk in self.ts:
                    yield from self.demux.feed(chunk)
            for kind, pts, data in frames():
                if self.done:
                    break
                if kind == "video":
                    nals = [n for n in nal_units(data) if n[0] & 0x1F != 9]     # drop access unit delimiters
                    if nals:
                        v.h264(nals, pts)
                elif a:
                    a.audio(data, pts)
        except Exception as e:
            log.info("rtsp cam%s stream ended: %s", self.cam, e)
        finally:
            self.stop()

    def stop(self):
        if self.close_ts:
            close, self.close_ts = self.close_ts, None
            try: close()
            except Exception: pass
        self.done = True
        try: self.conn.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: self.conn.close()
        except OSError: pass
