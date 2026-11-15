"""Garage door openers controlled by a local relay on the LAN.

The app never talks to myQ or any cloud for this: a small relay (default a Shelly
Plus 1 / Shelly 1 Gen3 on the same wifi) is wired across the opener's button (or
across a spare remote's button), and a momentary pulse = one press = open/close toggle.

Backend 'shelly' uses the Gen2+ local RPC API:
  pulse  : GET http://<host>/rpc/Switch.Set?id=<ch>&on=true&toggle_after=<secs>
  status : GET http://<host>/rpc/Switch.GetStatus?id=<ch>
Digest auth is used when a password is set on the device (user is always 'admin').
A door-state reed switch (optional) can be wired to the Shelly input; when present,
Input.GetStatus gives open/closed. Without it, state is 'unknown' and we just pulse.
"""
import time
import httpx

TIMEOUT = 8.0


def _client(opener):
    auth = None
    pw = (opener["auth"] or "").strip()
    if pw:
        auth = httpx.DigestAuth("admin", pw)      # Shelly Gen2 uses digest, user fixed to 'admin'
    return httpx.Client(timeout=TIMEOUT, auth=auth)


def _rpc(opener, method, **params):
    host = (opener["host"] or "").strip().rstrip("/")
    if not host:
        raise ValueError("no relay address set")
    if not host.startswith("http"):
        host = "http://" + host
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{host}/rpc/{method}" + (f"?{q}" if q else "")
    with _client(opener) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def press(opener):
    """One momentary press (open/close toggle). Returns None on success, raises on failure."""
    backend = opener["backend"] or "shelly"
    if backend != "shelly":
        raise ValueError(f"unknown backend {backend}")
    secs = float(opener["pulse"] or 0.5)
    ch = int(opener["relay"] or 0)
    # on + auto-off after <secs>: a clean momentary contact even if the request to turn
    # it back off is lost. (toggle_after is seconds, accepts fractions.)
    _rpc(opener, "Switch.Set", id=ch, on="true", toggle_after=secs)


def door_state(opener):
    """'open' | 'closed' | 'unknown'. Uses a reed switch on the relay's input if configured
    (input 0 closed = door closed). Without one, returns 'unknown'."""
    if (opener["backend"] or "shelly") != "shelly" or not (opener["state_input"] is not None):
        return "unknown"
    try:
        st = _rpc(opener, "Input.GetStatus", id=int(opener["state_input"]))
        on = st.get("state")
        if on is None:
            return "unknown"
        # convention: input closed (True) = door closed; invert with state_invert
        closed = bool(on) != bool(opener["state_invert"])
        return "closed" if closed else "open"
    except Exception:
        return "unknown"


def reachable(opener):
    """True if the relay answers. Quick check for the admin 'Test' button."""
    try:
        _rpc(opener, "Shelly.GetDeviceInfo")
        return True, ""
    except Exception as e:
        return False, str(e)[:160]
