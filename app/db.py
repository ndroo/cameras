"""SQLite storage.  One file on the Fly volume; tiny schema, plain sqlite3."""
import os, sqlite3, time, threading

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "cameras.db"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS houses (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cameras (
  id INTEGER PRIMARY KEY, house_id INTEGER REFERENCES houses(id) ON DELETE SET NULL,
  device_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, model TEXT, device_type TEXT,
  rotate INTEGER DEFAULT 0, flip_h INTEGER DEFAULT 0, flip_v INTEGER DEFAULT 0,
  sort INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
  online INTEGER, online_since REAL, last_check REAL, last_error TEXT, fail_count INTEGER DEFAULT 0,
  thumb_at REAL);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL, is_admin INTEGER DEFAULT 0,
  created REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY, house_id INTEGER REFERENCES houses(id) ON DELETE CASCADE, name TEXT NOT NULL,
  all_cameras INTEGER DEFAULT 1, notify_doorbell INTEGER DEFAULT 0, notify_tamper INTEGER DEFAULT 0,
  notify_offline INTEGER DEFAULT 0, notify_motion INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS group_members (group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  person_id INTEGER REFERENCES people(id) ON DELETE CASCADE, PRIMARY KEY (group_id, person_id));
CREATE TABLE IF NOT EXISTS group_cameras (group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  camera_id INTEGER REFERENCES cameras(id) ON DELETE CASCADE, PRIMARY KEY (group_id, camera_id));
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
  created REAL, last_seen REAL, ua TEXT);
CREATE TABLE IF NOT EXISTS otps (phone TEXT, code TEXT, expires REAL, attempts INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, camera_id INTEGER REFERENCES cameras(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, at REAL NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS events_cam_at ON events(camera_id, at);
CREATE TABLE IF NOT EXISTS sms_log (id INTEGER PRIMARY KEY, person_id INTEGER, phone TEXT, kind TEXT, body TEXT,
  at REAL, sid TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS login_fails (ident TEXT PRIMARY KEY, count INTEGER DEFAULT 0, until REAL);
"""

MIGRATIONS = [
    "ALTER TABLE people ADD COLUMN email TEXT",
    "ALTER TABLE people ADD COLUMN passcode_hash TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS people_email ON people(email)",
]


def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = connect()
        with _conn:
            _conn.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                with _conn:
                    _conn.execute(m)
            except sqlite3.OperationalError:
                pass  # already applied
    return _conn


def q(sql, *args):
    with _lock:
        return conn().execute(sql, args).fetchall()


def one(sql, *args):
    with _lock:
        return conn().execute(sql, args).fetchone()


def run(sql, *args):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def kv_get(k, default=None):
    r = one("SELECT v FROM kv WHERE k=?", k)
    return r["v"] if r else default


def kv_set(k, v):
    run("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, v)


# ------------------------------------------------------------------ access helpers
def person_cameras(person):
    """Cameras this person may view, grouped by house. Admins see everything."""
    if person["is_admin"]:
        cams = q("SELECT c.*, h.name AS house_name, h.slug AS house_slug FROM cameras c "
                 "LEFT JOIN houses h ON h.id=c.house_id WHERE c.enabled=1 ORDER BY h.sort, h.name, c.sort, c.name")
    else:
        cams = q("""SELECT DISTINCT c.*, h.name AS house_name, h.slug AS house_slug FROM cameras c
                    JOIN houses h ON h.id=c.house_id
                    JOIN groups g ON g.house_id=h.id
                    JOIN group_members gm ON gm.group_id=g.id AND gm.person_id=?
                    LEFT JOIN group_cameras gc ON gc.group_id=g.id AND gc.camera_id=c.id
                    WHERE c.enabled=1 AND (g.all_cameras=1 OR gc.camera_id IS NOT NULL)
                    ORDER BY h.sort, h.name, c.sort, c.name""", person["id"])
    return cams


def can_view(person, camera_id):
    return any(c["id"] == camera_id for c in person_cameras(person))


def recipients_for(camera_id, kind):
    """People to text for an event on this camera: members of groups that cover the
    camera and have the matching notify flag."""
    flag = {"doorbell": "notify_doorbell", "tamper": "notify_tamper", "offline": "notify_offline",
            "online": "notify_offline", "motion": "notify_motion"}.get(kind)
    if not flag:
        return []
    return q(f"""SELECT DISTINCT p.* FROM people p
                 JOIN group_members gm ON gm.person_id=p.id
                 JOIN groups g ON g.id=gm.group_id AND g.{flag}=1
                 JOIN cameras c ON c.id=? AND c.house_id=g.house_id
                 LEFT JOIN group_cameras gc ON gc.group_id=g.id AND gc.camera_id=c.id
                 WHERE g.all_cameras=1 OR gc.camera_id IS NOT NULL""", camera_id)
