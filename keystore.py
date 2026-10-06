import os, time, sqlite3, secrets, hashlib, threading
from contextlib import contextmanager

DB_PATH = os.environ.get("KEYSTORE_PATH", "api_keys.db")
_lock = threading.Lock()


@contextmanager
def _db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS api_keys (
            id         TEXT PRIMARY KEY,
            app_name   TEXT NOT NULL,
            key_hash   TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL,
            revoked    INTEGER NOT NULL DEFAULT 0,
            last_used  REAL)""")


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(app_name, days_valid=None):
    """Returns the plaintext key ONCE. Only its hash is stored."""
    key = "lims_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(6)
    now = time.time()
    expires = now + days_valid * 86400 if days_valid else None
    with _db() as c:
        c.execute("INSERT INTO api_keys (id, app_name, key_hash, key_prefix, created_at, expires_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)",
                  (key_id, app_name.strip(), _hash(key), key[:9], now, expires))
    return {"id": key_id, "app_name": app_name.strip(), "api_key": key, "expires_at": expires}


def verify_key(key):
    """Returns the app name if the key is valid, else None."""
    if not key:
        return None
    now = time.time()
    with _db() as c:
        row = c.execute("SELECT * FROM api_keys WHERE key_hash = ?", (_hash(key),)).fetchone()
        if not row or row["revoked"] or (row["expires_at"] and row["expires_at"] < now):
            return None
        if not row["last_used"] or now - row["last_used"] > 60:      # avoid a write on every request
            c.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, row["id"]))
        return row["app_name"]


def list_keys():
    with _db() as c:
        rows = c.execute("SELECT id, app_name, key_prefix, created_at, expires_at, revoked, last_used "
                         "FROM api_keys ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def revoke_key(key_id):
    with _db() as c:
        return c.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,)).rowcount > 0


if __name__ == "__main__":          # python keystore.py create portal   |  list  |  revoke <id>
    import sys, json
    init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "create":
        print(json.dumps(create_key(sys.argv[2]), indent=2))
    elif cmd == "revoke":
        print("revoked" if revoke_key(sys.argv[2]) else "not found")
    else:
        print(json.dumps(list_keys(), indent=2))