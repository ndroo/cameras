# cameras.andrewmcgrath.info

A small self-hosted viewer and alert system for the Tapo cameras at 180 Beatrice,
Strachan Ave and the cottage. Replaces the Tapo app for *viewing and sharing*; the
Tapo app is still used to configure cameras.

- **Login**: mobile number + 6-digit SMS code (Twilio). No passwords. Only numbers an
  admin has added can log in.
- **Houses / cameras / people / groups** (Admin page). A *group* belongs to one house,
  has members, and grants either all of that house's cameras or a ticked subset, plus
  the text alerts it has turned on (doorbell, tamper, offline/back online, motion).
  Admins see everything. Example: "Owners" at 180 Beatrice (Andrew + Jess, all cameras,
  offline alerts) and "Tenants" at 180 Beatrice (only the shared cameras, doorbell texts).
- **Live view** through the Tapo cloud relay, no site hardware, no port-forwarding:
  `app/tapo.py` (login, JWT, one persistent MQTT session, relay requests) ->
  `app/pipe.py` (MPEG-TS to stdout) -> go2rtc `exec:` source -> MSE/WebRTC/MJPEG in the
  browser via go2rtc's `video-stream` web component, proxied with auth by FastAPI.
- **Orientation**: rotate / mirror / flip per camera. Admin clicks save as the default
  for everyone; a non-admin's choice is remembered in their browser.
- **Pan / tilt**: cameras with a motor (C210 and the like) get an arrow pad over the
  live picture, for admins only (everyone else can watch but not move the camera); each press turns the camera 10 degrees, hold to keep going. Arrows follow
  the displayed orientation, so "up" moves the picture up even on a camera mounted upside
  down. Support is probed once (`getPresetConfig`) during the first health check and
  stored in `cameras.ptz`; the move itself is `motorMove` through the cloud passthrough.
- **Health**: every `HEALTH_INTERVAL` seconds each camera is asked for a relay and a
  frame (also the thumbnail on the home page). Two failures in a row = offline event
  (+ text to groups with the offline alert), recovery = back-online event.
- **Device events**: the app subscribes to `$tpiot/things/<dev>/events/accepted` and
  `.../shadow/update/accepted`. Raw messages are kept in the `kv` table and shown at
  `/admin/mqtt-log`; `app/events.py` maps them to doorbell / tamper / motion events.
  The payload shapes are not confirmed yet - press the doorbell, then look at the log
  and adjust `classify()`.

## Heating monitor (moved in from ndroo/tapo-boiler-monitor on 2026-10-19)

`heating/` holds the boiler/zone-panel reader (`monitor.py`, `lcd_pixels.py`), the ecobee and
weather pollers and the dashboard (`index.html`, `legend.html`). The app runs one cycle every
`HEATING_EVERY` seconds (default 15 min) as subprocesses; stills are taken through the app's own
relay session (`heating/grab_boiler_cloud.py` asks `/internal/relay/<device>` for a relay). Data
lives on the volume under `$DATA_DIR/heating/stills` (readings.jsonl, ecobee.jsonl, weather.jsonl,
state files, latest stills; timestamped stills are pruned after two days). The dashboard is public
at `/heating` and is proxied by the 180 Beatrice site as https://180beatrice.andrewmcgrath.info/heating .
Secrets: `ANTHROPIC_API_KEY`, `ECOBEE_TOKEN_KEY` (+ `ecobee_token.enc`, copied to the volume on first
run), `ALERT_TO`, optional `ECOBEE_EMAIL/PASSWORD/TOTP_SECRET`.

## Watchdog and backups

The app pings `WATCH_URLS` (the 180 Beatrice site and andrewmcgrath.info) every 5 minutes and
texts every admin after two failed checks (and again when the site is back). At 03:30 Toronto
time it copies the SQLite database and the heating data to `$DATA_DIR/backups` (four weeks kept)
and uploads both to the Tigris bucket in `BUCKET_NAME`. Admin → System shows the state of all three
and has "run a cycle now" / "back up now" buttons.

## Running

```
cp .env.example .env   # fill in
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PATH=$PWD/.venv/bin:$PATH sh bin/start.sh          # go2rtc on 127.0.0.1:1984, app on :8000
.venv/bin/python -m app.seed                        # houses, camera import, Owners groups
```

`go2rtc` must be on PATH (or `GO2RTC_BIN`). Local-only login without SMS:
`DEV_LOGIN=1` then open `/dev/login?phone=<admin phone>`.

## Deploy (Fly.io, app `cameras-andrewmcgrath`, region yyz, volume `cameras_data`)

```
fly deploy --remote-only
fly ssh console -C "python3 -m app.seed"
```

Secrets: `TPLINK_EMAIL`, `TPLINK_PASSWORD`, `TPLINK_TERMINAL_UUID` (**must differ** from
the boiler monitor's and from any local `.env`: one MQTT connection per UUID, a second
one kicks the first off), `TWILIO_*`, `ADMIN_PHONE`, `ADMIN_NAME`, `INTERNAL_SECRET`.
DNS: A/AAAA records at Cloudflare, DNS-only, to the Fly IPs.

## Notes / gotchas

- go2rtc refuses `exec:` sources added over its API ("insecure producer"), so the app
  rewrites `$DATA_DIR/go2rtc.yaml` and calls `POST /api/restart` when cameras change.
- The relay returns `multipart/mixed` parts of `video/mp2t` (H.264 + an audio track
  ffmpeg does not recognise, so video only for now). ~5 s to first frame.
- The cloud `getDeviceList` `status` field is not a reliable online indicator; the
  relay request is.
- Vendored from go2rtc v1.9.14: `app/static/video-rtc.js`, `video-stream.js`.

## Recordings (SD card)

`/cam/<id>/recordings`: a month calendar (dots = days with footage), a 24-hour bar for
the chosen day shaded where the card has footage, and a one-hour strip around the
playhead for fine scrubbing. Drag or tap either bar to start playing from that time;
the playhead follows the video and the next chunk loads automatically. "Save" downloads
the chunk as an MP4.

How it works (`app/tapo.py`): camera API methods run through the cloud with the IoT
thing service (`POST /v1/things/<deviceId>/services-sync`, serviceId `passthrough`,
headers `Authorization: ut|<token>` and `app-cid: app:TP-Link_Tapo_Android:<uuid>`):
`searchDateWithVideo` for days, `searchVideoOfDay` for clips (needs the camera's own
`getUserID`; `-71103` means the card is busy, retried), `getSdCardStatus`. Playback
asks for a relay with `streamType: 1` and writes one JSON part into the client
multipart body (`download` = as fast as the link allows, `playback` = real time);
the camera answers with a session id and MPEG-TS parts, and a `stream_status finished`
notification at the end. ffmpeg remuxes to fragmented MP4 with a small probe window
so the browser starts within a few seconds. One playback per camera at a time.

## Next steps

- Audio: identify the private-stream audio codec in the TS and remux it.
- Two-way talk, PTZ: not planned.
