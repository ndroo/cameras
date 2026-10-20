"""
Sound for the camera streams.

Tapo cameras put G.711 audio in their MPEG-TS under private stream types that neither
ffmpeg nor go2rtc reads (0x90 = A-law at 8 kHz, 0x91 = mu-law at 16 kHz on the C210),
so the audio was silently dropped.  Splitter pulls that track out of the TS and decodes
it to 16-bit PCM, laid out by its timestamps (gaps become silence) so it stays in step
with the video; ffmpeg then takes the TS (for the video) plus the PCM as a second input
and encodes AAC, which go2rtc and browsers understand.
"""
import math, os, queue, subprocess, threading

AUDIO_TYPES = {0x90: ("alaw", 8000), 0x91: ("ulaw", 16000)}
VIDEO_TYPES = (0x1B, 0x24)


def _alaw(b):
    b ^= 0x55
    t = (b & 0x0F) << 4
    seg = (b & 0x70) >> 4
    if seg == 0: t += 8
    elif seg == 1: t += 0x108
    else: t = (t + 0x108) << (seg - 1)
    return t if b & 0x80 else -t


def _ulaw(b):
    b = ~b & 0xFF
    t = (((b & 0x0F) << 3) + 0x84) << ((b & 0x70) >> 4)
    return 0x84 - t if b & 0x80 else t - 0x84


# The cameras' microphones are quiet (a still room sits around -65 dB), and a browser
# can't turn up past 100 %, so boost here. tanh is a soft limiter: quiet sounds get the
# full gain, loud ones (someone talking right at the doorbell) round off instead of clipping.
GAIN = 10 ** (float(os.environ.get("AUDIO_GAIN_DB", "18")) / 20)


def _boost(x):
    return int(round(32767 * math.tanh(GAIN * x / 32767)))


_TABLES = {name: [_boost(f(i)).to_bytes(2, "little", signed=True) for i in range(256)]
           for name, f in (("alaw", _alaw), ("ulaw", _ulaw))}


def _alaw_enc(x):
    sign = 0x00 if x < 0 else 0x80
    x = min(-x if x < 0 else x, 32635)
    if x >= 256:
        exp = x.bit_length() - 8              # 1..7
        mant = (x >> (exp + 3)) & 0x0F
    else:
        exp, mant = 0, x >> 4
    return ((exp << 4) | mant | sign) ^ 0x55


def _ulaw_enc(x):
    sign = 0x80 if x < 0 else 0
    x = min(-x if x < 0 else x, 32635) + 0x84
    exp = x.bit_length() - 8                  # 0..7
    mant = (x >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mant) & 0xFF


# Same boost for G.711 passed through untouched (RTSP live): byte -> boosted byte
G711_BOOST = {"PCMA": bytes(_alaw_enc(_boost(_alaw(i))) for i in range(256)),
              "PCMU": bytes(_ulaw_enc(_boost(_ulaw(i))) for i in range(256))}


def _pts(pes):
    if len(pes) < 14 or pes[:3] != b"\x00\x00\x01" or not pes[7] & 0x80:
        return None
    p = pes[9:14]
    return ((p[0] >> 1) & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1


class Splitter:
    """Feed it TS chunks; it hands back the same TS (for ffmpeg's video) and collects
    the audio as PCM. `ready` turns true once it knows whether there is audio and at
    what rate (or after ~3 MB with no audio, when it gives up and stays video-only)."""

    def __init__(self):
        self.buf = bytearray()
        self.pmt_pid = self.audio_pid = self.video_pid = None
        self.codec = None; self.rate = None
        self.v0 = None                  # first video PTS: the zero point of the timeline
        self.pes = None; self.pes_pts = None
        self.written = 0                # PCM samples emitted so far
        self.pcm = bytearray()
        self.seen = 0
        self.ready = False
        self.first = []                 # (pts, payload) held back until the rate is known

    def feed(self, chunk):
        self.seen += len(chunk)
        self.buf += chunk
        n = len(self.buf) - len(self.buf) % 188
        for i in range(0, n, 188):
            self._packet(self.buf[i:i + 188])
        del self.buf[:n]
        if not self.ready and self.audio_pid is None and self.seen > 3_000_000:
            self.ready = True           # no audio track: video only
        return chunk

    def take_pcm(self):
        out = bytes(self.pcm); self.pcm.clear()
        return out

    def _packet(self, p):
        if p[0] != 0x47:
            return
        pid = (p[1] & 0x1F) << 8 | p[2]; pusi = p[1] & 0x40; afc = (p[3] >> 4) & 3
        if not afc & 1:
            return
        o = 4 + (1 + p[4] if afc & 2 else 0)
        pl = p[o:]
        if pid == 0 and pusi and self.pmt_pid is None:
            pl = pl[1 + pl[0]:]
            if len(pl) >= 12:
                self.pmt_pid = (pl[10] & 0x1F) << 8 | pl[11]
        elif pid == self.pmt_pid and pusi and self.video_pid is None:
            self._pmt(pl[1 + pl[0]:])
        elif pid == self.video_pid and pusi and self.v0 is None:
            self.v0 = _pts(pl)
        elif pid == self.audio_pid:
            if pusi:
                self._flush()
                self.pes = bytearray(pl); self.pes_pts = _pts(pl)
            elif self.pes is not None:
                self.pes += pl

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
                self.audio_pid = pid; self.codec, self.rate = AUDIO_TYPES[t]
            j += 5 + il

    def _flush(self):
        if self.pes is None or self.pes_pts is None:
            return
        pes = bytes(self.pes); self.pes = None
        data = pes[9 + pes[8]:]
        if not self.ready:
            self.first.append((self.pes_pts, data))
            if len(self.first) >= 2:
                (p1, d1), (p2, _) = self.first[:2]
                if p2 > p1:             # trust the timestamps over the type's usual rate
                    measured = len(d1) * 90000 / (p2 - p1)
                    self.rate = min((8000, 16000), key=lambda r: abs(r - measured))
                self.ready = True
                for pts, d in self.first:
                    self._emit(pts, d)
                self.first = []
            return
        self._emit(self.pes_pts, data)

    def _emit(self, pts, data):
        if self.v0 is None:
            self.v0 = pts
        at = int(((pts - self.v0) % (1 << 33)) * self.rate / 90000)
        if at > (1 << 32) * self.rate / 90000:      # before the first video frame
            at = 0
        gap = at - self.written
        if gap > self.rate // 4 and gap < self.rate * 30:   # dropped audio: fill with silence
            self.pcm += b"\x00\x00" * gap; self.written += gap
        elif -self.rate * 30 < gap < -(self.rate // 4):     # camera clock runs fast: skip the overlap so sound doesn't drift ahead
            data = data[-gap:]
            if not data:
                return
        t = _TABLES[self.codec]
        self.pcm += b"".join([t[x] for x in data]); self.written += len(data)


# drop H.264 SEI: the D130 doorbells put it after the IDR slice, which iPhone's decoder
# rejects (MEDIA_ERR_DECODE); same as the RTSP path in rtsp.py
VIDEO_BSF = ["-bsf:v", "filter_units=remove_types=6"]


def start_ffmpeg(sp, out_args, in_args=()):
    """ffmpeg reading the TS on stdin (video copied) plus, when the stream has sound, the
    PCM on a second pipe (encoded to AAC). Returns (proc, audio_write_fd or None)."""
    # no "-fflags nobuffer": it throws away the packets read while probing, first
    # keyframe included, so the picture started a whole GOP (~2 s) late
    # one thread: eight of these run at once on a small machine, and copying video plus
    # encoding 8-16 kHz mono needs almost nothing
    base = ["ffmpeg", "-loglevel", "error", "-threads", "1", "-filter_threads", "1", "-probesize", "300000", "-analyzeduration", "500000",
            *in_args, "-f", "mpegts", "-i", "pipe:0"]
    if not sp.rate:
        return subprocess.Popen(base + ["-map", "0:v", "-c:v", "copy", *VIDEO_BSF, *out_args],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL), None
    # ffmpeg starts each input's clock at its first timestamp; the PCM's first sample is
    # the first video timestamp (v0), so the two line up
    r, w = os.pipe()
    proc = subprocess.Popen(base + ["-f", "s16le", "-ar", str(sp.rate), "-ac", "1", "-i", f"pipe:{r}",
                                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", *VIDEO_BSF, "-c:a", "aac", "-b:a", "48k", *out_args],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(r,))
    os.close(r)
    return proc, w


def pump(chunks, proc, afd, sp, pre=()):
    """Write TS chunks to ffmpeg's stdin and the decoded audio to its second input, each
    from its own thread so ffmpeg can read the two in whatever order it likes."""
    # unbounded, so a full video queue can never hold back audio ffmpeg is waiting for
    # (or the reverse); if ffmpeg stops reading altogether the backlog cap ends it
    qv, qa = queue.Queue(), queue.Queue()
    dead = threading.Event()            # ffmpeg went away (viewer left): stop reading the camera

    def writer(q, write, close):
        try:
            while (b := q.get()) is not None:
                write(b)
        except (BrokenPipeError, OSError, ValueError):
            dead.set()
            while q.get() is not None:      # drain so the producer never blocks on a dead pipe
                pass
        finally:
            try: close()
            except Exception: pass

    def write_fd(b):
        mv = memoryview(b)
        while mv:
            mv = mv[os.write(afd, mv):]

    ts = [threading.Thread(target=writer, args=(qv, proc.stdin.write, proc.stdin.close), daemon=True)]
    if afd is not None:
        ts.append(threading.Thread(target=writer, args=(qa, write_fd, lambda: os.close(afd)), daemon=True))
    for t in ts: t.start()
    try:
        def put(chunk):
            qv.put(chunk)
            if afd is not None:
                pcm = sp.take_pcm()
                if pcm:
                    qa.put(pcm)
        for c in pre:
            put(c)
        for c in chunks:
            if dead.is_set() or qv.qsize() > 200:     # ~3 MB: ffmpeg has stopped reading
                break
            sp.feed(c); put(c)
    finally:
        qv.put(None)
        if afd is not None:
            qa.put(None)
        if hasattr(chunks, "close"):
            chunks.close()              # generator: lets it hang up on the relay


def prime(chunks, sp):
    """Read from the camera until the Splitter knows about the audio. Returns the chunks
    read so far (to replay into ffmpeg) and the iterator to carry on from."""
    it = iter(chunks); pre = []
    for c in it:
        sp.feed(c); pre.append(c)
        if sp.ready:
            break
    return pre, it
