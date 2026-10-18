"""Twilio SMS.  Without credentials, messages are logged instead of sent."""
import os, re, time, logging
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


def _logged(body, kind):
    """Never keep one-time codes in the log."""
    return re.sub(r"\b\d{6}\b", "••••••", body) if kind == "otp" else body


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
           person_id, phone, kind, _logged(body, kind), time.time(), sid, err)
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


# ------------------------------------------------------------------ email
def normalize_email(raw):
    e = (raw or "").strip().lower()
    return e if "@" in e and "." in e.split("@")[-1] and " " not in e else None


def send_email(to, subject, body, kind="misc", person_id=None):
    """Plain-text mail from the Gmail account (app password), like the 180 Beatrice site."""
    import smtplib
    from email.message import EmailMessage
    user = os.environ.get("GMAIL_USER"); pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    err = None
    if not user or not pw:
        log.info("EMAIL (not configured) to %s: %s", to, body); err = "gmail not configured"
    else:
        try:
            m = EmailMessage(); m["From"] = f"Cameras <{user}>"; m["To"] = to; m["Subject"] = subject
            m.set_content(body)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
                s.login(user, pw); s.send_message(m)
        except Exception as e:
            err = str(e)[:300]; log.warning("email to %s failed: %s", to, err)
    db.run("INSERT INTO sms_log(person_id,phone,kind,body,at,sid,error) VALUES(?,?,?,?,?,?,?)",
           person_id, to, kind, _logged(body, kind), time.time(), None if err else "email", err)
    return (None if err else "email"), err


# ------------------------------------------------------------------ passcodes
def hash_passcode(code):
    import hashlib, secrets, base64
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 200_000)
    return base64.b64encode(salt + h).decode()


def check_passcode(code, stored):
    import hashlib, hmac, base64
    if not stored or not code:
        return False
    raw = base64.b64decode(stored); salt, h = raw[:16], raw[16:]
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 200_000), h)
