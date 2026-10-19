"""
Stream pipe used by go2rtc's exec source:  python -m app.pipe <device_id>
Writes the camera's MPEG-TS to stdout.  Relay credentials come from the running web
app over loopback (it owns the single MQTT session); the pipe only speaks TLS to the
relay host.  Exits when stdout closes (go2rtc stops it when nobody is watching).
"""
import os, sys, time, logging, threading, requests
from . import audio, tapo

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="pipe %(message)s")
log = logging.getLogger("pipe")


def main():
    device_id = sys.argv[1]
    base = os.environ.get("INTERNAL_URL", "http://127.0.0.1:8000")
    secret = os.environ.get("INTERNAL_SECRET", "")
    out = sys.stdout.buffer
    r = requests.get(f"{base}/internal/relay/{device_id}", headers={"X-Internal": secret}, timeout=40)
    if r.status_code != 200:
        log.error("relay request failed: %s %s", r.status_code, r.text[:200]); sys.exit(2)
    j = r.json()
    sock = tapo.connect_relay(j["relayUrl"], j["relayToken"], j["term"])
    tapo.start_preview(sock)
    t0 = time.time(); n = 0
    if os.environ.get("STREAM_AUDIO", "1") != "1":
        chunks = tapo.iter_ts(sock)
    else:
        # sound: ffmpeg copies the video and turns the camera's G.711 into AAC (see audio.py)
        sp = audio.Splitter()
        pre, rest = audio.prime(tapo.iter_ts(sock), sp)
        log.info("%s audio: %s", device_id, f"{sp.codec} {sp.rate} Hz" if sp.rate else "none")
        proc, afd = audio.start_ffmpeg(sp, ["-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"])
        threading.Thread(target=audio.pump, args=(rest, proc, afd, sp, pre), daemon=True).start()
        chunks = iter(lambda: proc.stdout.read1(65536), b"")
    try:
        for chunk in chunks:
            out.write(chunk); out.flush(); n += len(chunk)
    except BrokenPipeError:
        pass
    except Exception as e:
        log.error("stream ended: %s after %.0fs / %d bytes", e, time.time() - t0, n); sys.exit(1)
    finally:
        try: sock.close()
        except Exception: pass


if __name__ == "__main__":
    main()
