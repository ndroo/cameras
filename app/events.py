"""Map raw Tapo MQTT messages to (device_id, kind, detail).  Unknown messages are
kept in the kv table (see /admin/mqtt-log) so new event shapes can be added here."""
import re, json

KINDS = {
    # substrings seen in event payloads -> our event kind
    "ring": "doorbell", "doorbell": "doorbell", "button": "doorbell",
    "tamper": "tamper",
    "person": "motion", "motion": "motion",
}


def classify(topic, payload):
    m = re.search(r"\$tpiot/things/([0-9A-Fa-f]{20,})/", topic)
    if not m or isinstance(payload, bytes):
        return None
    device_id = m.group(1)
    text = json.dumps(payload).lower()
    if "relay" in topic:
        return None
    for needle, kind in KINDS.items():
        if needle in text:
            return device_id, kind, text[:300]
    return None
