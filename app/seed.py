"""One-off, idempotent setup:  python3 -m app.seed
Creates the three houses, imports the cameras from the Tapo account, files each
camera under its house by name, fixes known orientations, and creates an Owners
group per house with the admin in it.  Safe to re-run; it never deletes anything."""
import os, sys, time, base64
from . import db, sms, tapo

HOUSES = [("180 Beatrice", 0), ("Strachan Ave", 1), ("Cottage", 2)]
BY_NAME = {"Strachan": "Strachan Ave", "Boiler": "180 Beatrice", "Zone": "180 Beatrice"}
ROTATE = {"Strachan Front": 180}


def main():
    for name, sort in HOUSES:
        if not db.one("SELECT id FROM houses WHERE name=?", name):
            slug = name.lower().replace(" ", "-")
            db.run("INSERT INTO houses(name,slug,sort) VALUES(?,?,?)", name, slug, sort)
    hid = {h["name"]: h["id"] for h in db.q("SELECT * FROM houses")}

    admin_phone = sms.normalize_phone(os.environ.get("ADMIN_PHONE", ""))
    if admin_phone and not db.one("SELECT id FROM people WHERE phone=?", admin_phone):
        db.run("INSERT INTO people(name,phone,is_admin,created) VALUES(?,?,1,?)", os.environ.get("ADMIN_NAME", "Admin"), admin_phone, time.time())
    admin = db.one("SELECT * FROM people WHERE phone=?", admin_phone) if admin_phone else None
    admin_email = sms.normalize_email(os.environ.get("ADMIN_EMAIL", ""))
    if admin and admin_email and not admin["email"]:
        db.run("UPDATE people SET email=? WHERE id=?", admin_email, admin["id"])

    cloud = tapo.TapoCloud(os.environ["TPLINK_EMAIL"], os.environ["TPLINK_PASSWORD"], os.environ.get("TPLINK_TERMINAL_UUID"))
    n = 0
    for d in cloud.device_list():
        if d.get("deviceType") not in ("SMART.IPCAMERA", "SMART.TAPODOORBELL"):
            continue
        try:
            alias = base64.b64decode(d.get("alias", "")).decode()
        except Exception:
            alias = d.get("alias") or "Camera"
        if not db.one("SELECT id FROM cameras WHERE device_id=?", d["deviceId"]):
            db.run("INSERT INTO cameras(device_id,name,model,device_type,enabled) VALUES(?,?,?,?,1)", d["deviceId"], alias, d.get("deviceModel"), d.get("deviceType"))
            n += 1
    for c in db.q("SELECT * FROM cameras"):
        if c["house_id"] is None:
            for key, house in BY_NAME.items():
                if key.lower() in c["name"].lower():
                    db.run("UPDATE cameras SET house_id=? WHERE id=?", hid[house], c["id"])
        if c["name"] in ROTATE and not c["rotate"]:
            db.run("UPDATE cameras SET rotate=? WHERE id=?", ROTATE[c["name"]], c["id"])

    for name, _ in HOUSES:
        g = db.one("SELECT id FROM groups WHERE house_id=? AND name='Owners'", hid[name])
        if not g:
            gid = db.run("INSERT INTO groups(name,house_id,all_cameras,notify_doorbell,notify_tamper,notify_offline,notify_motion) VALUES('Owners',?,1,1,1,1,0)", hid[name])
        else:
            gid = g["id"]
        if admin:
            db.run("INSERT OR IGNORE INTO group_members(group_id,person_id) VALUES(?,?)", gid, admin["id"])
    if not db.one("SELECT id FROM groups WHERE house_id=? AND name='Tenants'", hid["180 Beatrice"]):
        db.run("INSERT INTO groups(name,house_id,all_cameras,notify_doorbell,notify_tamper,notify_offline,notify_motion) VALUES('Tenants',?,0,1,0,0,0)", hid["180 Beatrice"])
    print(f"seed done: {n} new cameras; houses={list(hid)}; admin={'yes' if admin else 'NO (set ADMIN_PHONE)'}")


if __name__ == "__main__":
    sys.exit(main())
