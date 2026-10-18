"""Twilio SMS.  Without credentials, messages are logged instead of sent."""
import os, time, logging
from . import db

log = logging.getLogger("sms")


def _client():
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    if not sid:
        return None
    from twilio.rest import Client
    if os.environ.get("TWILIO_API_KEY_SID"):
        return Client(os.environ["TWILIO_API_KEY_SID"], os.environ["TWILIO_API_KEY_SECRET"], sid)
    return Client(sid, os.environ.get("TWILIO_AUTH_TOKEN", ""))


def send(phone, body, kind="misc", person_id=None):
    c = _client()
    sid = err = None
    if c is None:
        log.info("SMS (not configured) to %s: %s", phone, body)
        err = "twilio not configured"
    else:
        try:
            m = c.messages.create(to=phone, from_=os.environ["TWILIO_FROM"], body=body)
            sid = m.sid
        except Exception as e:
            err = str(e)[:300]
            log.warning("SMS to %s failed: %s", phone, err)
    db.run("INSERT INTO sms_log(person_id,phone,kind,body,at,sid,error) VALUES(?,?,?,?,?,?,?)",
           person_id, phone, kind, body, time.time(), sid, err)
    return sid, err


def normalize_phone(raw):
    """Loose North-American normalisation to E.164."""
    d = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == "+")
    if d.startswith("+"):
        return d if len(d) >= 11 else None
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None
