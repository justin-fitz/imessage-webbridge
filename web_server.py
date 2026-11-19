import asyncio
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import Cookie, FastAPI, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app_core import AppCore
from contacts import find_group_chat, get_group_members, load_contacts, resolve_identifier, search_contacts
from imessage_reader import APPLE_EPOCH_OFFSET
from models import ChatMessage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _sanitize(obj):
    """Recursively sanitize strings in a data structure to remove invalid Unicode."""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Defaults; create_app() overrides both from config (session_max_days /
# session_idle_days). A session ends at whichever limit it hits first.
_SESSION_TTL = 90 * 86400      # absolute lifetime of a login
_SESSION_IDLE = 30 * 86400     # logged out after this long without activity
_session_db: sqlite3.Connection | None = None
_session_db_path: str | None = None
_login_attempts: dict[str, list[float]] = {}  # ip -> [timestamps]

# Outbound-send rate limit (per session). Guards against a compromised session
# being used to blast iMessages at contacts.
_SEND_RATE_MAX = 20           # sends per window
_SEND_RATE_WINDOW = 60        # seconds
_send_attempts: dict[str, list[float]] = {}


def _check_send_rate(session: str) -> bool:
    """Return True if the send is allowed, False if rate-limited."""
    if not session:
        return True
    now = time.time()
    attempts = [t for t in _send_attempts.get(session, []) if now - t < _SEND_RATE_WINDOW]
    if len(attempts) >= _SEND_RATE_MAX:
        _send_attempts[session] = attempts
        return False
    attempts.append(now)
    _send_attempts[session] = attempts
    return True


# ---------------------------------------------------------------------------
# Login / security notifications → Home Assistant → phone. Fire-and-forget on a
# thread so a slow HA never delays a request. Config: web.ha_url / ha_token_file /
# ha_notify_service in config.yaml (defaults read ~/.config/ha/{url,token}).
_notify_cfg: dict = {}


def _geo(ip: str) -> str:
    if not ip or ip.startswith(("10.", "127.", "192.168.")):
        return "LAN"
    try:
        with urllib.request.urlopen(f"http://ip-api.com/json/{ip}?fields=status,city,regionName,country,isp", timeout=4) as r:
            j = json.load(r)
        if j.get("status") == "success":
            return f"{j.get('city')}, {j.get('regionName')} ({j.get('isp')})"
    except Exception:
        pass
    return "unknown location"


def _notify(title: str, message: str, critical: bool = False) -> None:
    cfg = _notify_cfg
    if not cfg.get("enabled"):
        return

    def _run():
        try:
            token = open(os.path.expanduser(cfg["token_file"])).read().strip()
            url = cfg["url"].rstrip("/")
            if not url.startswith("http"):
                url = "http://" + url
            svc = cfg["service"].replace("notify.", "")
            data = {"title": title, "message": message}
            if critical:
                data["data"] = {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}
            req = urllib.request.Request(
                f"{url}/api/services/notify/{svc}",
                data=json.dumps(data).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:  # never let notification failures affect the app
            print(f"notify failed: {e}")

    threading.Thread(target=_run, daemon=True).start()


def _notify_login(ip: str, ok: bool, detail: str = "") -> None:
    where = _geo(ip)
    if ok:
        _notify("iMessage bridge: new login", f"From {ip} — {where}")
    else:
        _notify("iMessage bridge: login blocked", f"{detail} from {ip} — {where}", critical=True)


def _init_session_db(db_path: str):
    global _session_db, _session_db_path
    _session_db_path = db_path
    _session_db = sqlite3.connect(db_path, check_same_thread=False)
    # See ChannelMap.__init__ — WAL is required here too, since this connection
    # and ChannelMap's both write the same file.
    _session_db.execute("PRAGMA journal_mode=WAL")
    _session_db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            expiry REAL NOT NULL
        )
    """)
    # Added 2026-09-15: idle timeout + per-session "sending unlocked until".
    for col, ddl in (("last_seen", "REAL"), ("ip", "TEXT"), ("send_armed_until", "REAL")):
        try:
            _session_db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # webapp_read_horizon: per-chat read dismissal persisted across page reloads.
    # Stores the highest message ROWID that was visible when the user opened a
    # chat in the web UI. get_recent_chats excludes messages at or below the
    # horizon from unread_count, so dismissed badges don't reappear on reload.
    # New incoming messages (ROWID > horizon) still count as unread.
    # The row is deleted once iMessage itself marks the chat read (is_read=1),
    # because at that point the persistent override is no longer needed.
    _session_db.execute("""
        CREATE TABLE IF NOT EXISTS webapp_read_horizon (
            chat_identifier TEXT PRIMARY KEY,
            max_rowid       INTEGER NOT NULL,
            dismissed_at    REAL    NOT NULL
        )
    """)
    # Passkeys (WebAuthn). One row per enrolled device/authenticator.
    _session_db.execute("""
        CREATE TABLE IF NOT EXISTS passkeys (
            credential_id TEXT PRIMARY KEY,
            public_key    BLOB NOT NULL,
            sign_count    INTEGER NOT NULL DEFAULT 0,
            name          TEXT NOT NULL,
            transports    TEXT,
            created_at    REAL NOT NULL,
            last_used_at  REAL
        )
    """)
    _session_db.execute("DROP TABLE IF EXISTS chat_read_state")
    _session_db.execute("DELETE FROM sessions WHERE expiry < ?", (time.time(),))
    # Sessions used to be stored as the raw cookie value. Replace each with its
    # hash in place, so existing logins keep working (see _token_key).
    migrated = 0
    for (tok,) in _session_db.execute("SELECT token FROM sessions").fetchall():
        if not re.fullmatch(r"[0-9a-f]{64}", tok):
            _session_db.execute("UPDATE OR REPLACE sessions SET token = ? WHERE token = ?", (_token_key(tok), tok))
            migrated += 1
    _session_db.commit()
    if migrated:
        # Squeeze the old values out of the file itself (they'd otherwise
        # linger in the write-ahead log and in freed pages).
        try:
            _session_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _session_db.execute("VACUUM")
            _session_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            print(f"Could not compact the state DB after hashing sessions: {e}")
    # Logins, passkeys and read state: nobody else on this Mac needs to read them.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(db_path + suffix, 0o600)
        except OSError:
            pass


def _set_read_horizon(chat_identifier: str, max_rowid: int) -> None:
    """Record that the user dismissed unread messages up through max_rowid."""
    if not _session_db or max_rowid <= 0:
        return
    _session_db.execute(
        """INSERT INTO webapp_read_horizon (chat_identifier, max_rowid, dismissed_at)
           VALUES (?, ?, ?)
           ON CONFLICT(chat_identifier) DO UPDATE SET
             max_rowid    = MAX(excluded.max_rowid, max_rowid),
             dismissed_at = excluded.dismissed_at""",
        (chat_identifier, max_rowid, time.time()),
    )
    _session_db.commit()


def _clear_read_horizon(chat_identifier: str) -> None:
    """Remove the webapp-local horizon once iMessage confirms the chat is read."""
    if not _session_db:
        return
    _session_db.execute(
        "DELETE FROM webapp_read_horizon WHERE chat_identifier = ?",
        (chat_identifier,),
    )
    _session_db.commit()


def _token_key(token: str | None) -> str:
    """What the sessions table stores for a cookie value: its SHA-256. The
    cookie itself is never written to disk, so a copy of bridge.db (a backup,
    another account on this Mac) can't be turned back into a working login."""
    return hashlib.sha256((token or "").encode()).hexdigest()


def _create_session(ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    _session_db.execute(
        "INSERT INTO sessions (token, expiry, last_seen, ip) VALUES (?, ?, ?, ?)",
        (_token_key(token), now + _SESSION_TTL, now, ip),
    )
    _session_db.commit()
    return token


def _session_remaining(token: str | None, touch: bool = True) -> float:
    """Seconds until this session ends (0 if it is missing or over). A session
    ends at its absolute expiry or after _SESSION_IDLE without activity,
    whichever is sooner. With `touch`, counts as activity (at most one write a
    minute); pass touch=False for server-side checks that say nothing about
    whether the person is still there."""
    if not token or not _session_db:
        return 0.0
    token = _token_key(token)
    row = _session_db.execute(
        "SELECT expiry, last_seen FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return 0.0
    now = time.time()
    expiry, last_seen = row[0], row[1] or now
    if now > expiry or now - last_seen > _SESSION_IDLE:
        _session_db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        _session_db.commit()
        return 0.0
    if touch and now - last_seen > 60:
        _session_db.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (now, token))
        _session_db.commit()
        last_seen = now
    return max(0.0, min(expiry - now, last_seen + _SESSION_IDLE - now))


def _valid_session(token: str | None, touch: bool = True) -> bool:
    """True if the session exists and hasn't hit its absolute or idle limit."""
    return _session_remaining(token, touch) > 0


def _touch_session(token: str | None) -> None:
    """Keep a session alive from WebSocket traffic (pongs), bypassing the 60s throttle."""
    if token and _session_db:
        _session_db.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (time.time(), _token_key(token)))
        _session_db.commit()


def _logout_everywhere() -> int:
    n = _session_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    _session_db.execute("DELETE FROM sessions")
    _session_db.commit()
    return n


# ---------------------------------------------------------------------------
# Passkeys (WebAuthn). Phishing-resistant, biometric login. Challenges are held
# in memory for a few minutes keyed by a random id the browser echoes back.
_PASSKEY_USER_ID = b"imessage-bridge-owner"
_challenges: dict[str, tuple[bytes, float, str]] = {}   # id -> (challenge, expiry, purpose)
_CHALLENGE_TTL = 300


def _new_challenge(challenge: bytes, purpose: str) -> str:
    now = time.time()
    for k, (_, exp, _) in list(_challenges.items()):
        if exp < now:
            _challenges.pop(k, None)
    cid = secrets.token_urlsafe(16)
    _challenges[cid] = (challenge, now + _CHALLENGE_TTL, purpose)
    return cid


def _take_challenge(cid: str, purpose: str) -> bytes | None:
    item = _challenges.pop(cid or "", None)
    if not item or item[1] < time.time() or item[2] != purpose:
        return None
    return item[0]


def _passkeys() -> list[dict]:
    rows = _session_db.execute(
        "SELECT credential_id, public_key, sign_count, name, transports, created_at, last_used_at FROM passkeys ORDER BY created_at"
    ).fetchall()
    return [dict(credential_id=r[0], public_key=r[1], sign_count=r[2], name=r[3],
                 transports=json.loads(r[4]) if r[4] else [], created_at=r[5], last_used_at=r[6]) for r in rows]


def _rp(request: Request) -> tuple[str, str]:
    """(rp_id, origin) for this request — the hostname the browser sees.
    Behind caddy uvicorn honors X-Forwarded-Proto/Host, so this is home.studiox.net/https."""
    host = request.url.hostname or "localhost"
    scheme = request.url.scheme
    port = request.url.port
    origin = f"{scheme}://{host}" + (f":{port}" if port and port not in (80, 443) else "")
    return host, origin


# ---------------------------------------------------------------------------
# Browser-facing hardening.
#
# Everything in a conversation is written by someone else: message text, file
# names, reaction text. The page escapes all of it, and this policy is the
# second line: scripts run only from this server's own files or with the
# per-response nonce, so markup that slips through can't execute, load
# anything, or send what it reads to another site.
def _csp(nonce: str, host: str) -> str:
    # 'self' covers same-origin WebSockets in current browsers; name the
    # socket explicitly for the ones where it doesn't.
    ws = f" ws://{host} wss://{host}" if re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host or "") else ""
    return "; ".join((
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        f"connect-src 'self'{ws}",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ))


_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Nothing to other sites. Not "no-referrer": that also makes browsers send
    # `Origin: null` on this app's own form posts, defeating the check below.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _same_origin(origin: str, host: str, allowed: set[str]) -> bool:
    """Is a request's Origin this server itself (or one the config allows)?"""
    if origin in allowed:
        return True
    try:
        return urllib.parse.urlsplit(origin).netloc.lower() == (host or "").lower() != ""
    except ValueError:
        return False


# Attachment types a browser may display in place. Anything else (HTML, SVG,
# text, unknown) is handed over as a download: opened as a page it would run
# with this site's login, and the file comes from whoever sent the message.
_INLINE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/heif",
                 "image/tiff", "image/bmp", "application/pdf"}


def _attachment_response(path: str, media_type: str | None = None) -> FileResponse:
    media_type = media_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    inline = media_type in _INLINE_TYPES or media_type.startswith(("video/", "audio/"))
    headers = {"Cache-Control": "private, max-age=3600"}
    if not inline:
        # `sandbox` as well: an <img> can still show an SVG, but nothing in the
        # file can run even if it is opened directly.
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
        return FileResponse(path, media_type=media_type, headers=headers,
                            filename=os.path.basename(path), content_disposition_type="attachment")
    return FileResponse(path, media_type=media_type, headers=headers)


def _prune_dir(directory: str, max_age: float) -> int:
    """Delete plain files in `directory` not touched for `max_age` seconds."""
    removed, cutoff = 0, time.time() - max_age
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            pass
    return removed


# WebSocket close codes the web client acts on (application range 4000-4999).
WS_AUTH_LOST = 4401     # no valid session: sign in again
WS_TOO_MANY = 4429      # connection limit reached: back off


class ConnectionManager:
    def __init__(self, max_connections: int = 20):
        self.active: list[WebSocket] = []
        self.max_connections = max_connections

    async def connect(self, ws: WebSocket):
        await ws.accept()
        if len(self.active) >= self.max_connections:
            # Close *after* accepting: a close during the handshake reaches the
            # browser as a bare 1006, indistinguishable from a network drop.
            await ws.close(code=WS_TOO_MANY, reason="Too many connections")
            return False
        self.active.append(ws)
        return True

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                if ws in self.active:
                    self.active.remove(ws)


class StatusPoller:
    def __init__(self, db_path: str, manager: ConnectionManager, interval: int = 3):
        self.db_path = db_path
        self.manager = manager
        self.interval = interval
        self._status_cache: dict[int, str] = {}
        self._read_cache: dict[int, bool] = {}
        self._seeded = False

    async def poll_loop(self):
        while True:
            try:
                await self._check_status_changes()
            except Exception as e:
                print(f"Status poll error: {e}")
            await asyncio.sleep(self.interval)

    async def _check_status_changes(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT m.ROWID, m.date_delivered, m.date_read, m.was_delivered_quietly,
                   COALESCE(m.error, 0) AS error, c.chat_identifier
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.is_from_me = 1
              AND m.item_type = 0
              AND m.associated_message_type = 0
            ORDER BY m.ROWID DESC
            LIMIT 20
        """).fetchall()
        conn.close()

        for row in rows:
            rid = row["ROWID"]
            dr = row["date_read"]
            dd = row["date_delivered"]
            if row["error"]:
                s = "failed"
                if self._seeded and self._status_cache.get(rid) not in (None, "failed"):
                    print(f"[IMSG-SEND] message {rid} to {row['chat_identifier']!r} failed: "
                          f"Messages error {row['error']}", flush=True)
            elif dr and dr != 0:
                s = "read"
            elif dd and dd != 0:
                s = "delivered"
            else:
                s = "sent"

            old = self._status_cache.get(rid)
            if old != s:
                self._status_cache[rid] = s
                # Suppress broadcasts during initial seed (avoids flooding clients
                # with status updates for the backlog at daemon startup). After
                # the first pass, broadcast on first sighting too — otherwise a
                # message that's already delivered when the poller first sees it
                # never triggers a UI update.
                if self._seeded and (old is not None or s != "sent"):
                    await self.manager.broadcast({
                        "type": "status_update",
                        "chat_identifier": row["chat_identifier"],
                        "status": s,
                        # Quiet flag of this message, so the client can decide
                        # whether the "notifications silenced" banner applies once
                        # this becomes the last message in the thread.
                        "delivered_quietly": bool(row["was_delivered_quietly"]),
                    })

        if len(self._status_cache) > 100:
            keep = {row["ROWID"] for row in rows}
            self._status_cache = {k: v for k, v in self._status_cache.items() if k in keep}

        self._seeded = True

        # Check for incoming messages read on other devices (iPhone, Messages.app)
        await self._check_read_sync(conn_path=self.db_path)

    async def _check_read_sync(self, conn_path: str):
        conn = sqlite3.connect(conn_path)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT m.ROWID, m.is_read, c.chat_identifier
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.is_from_me = 0
              AND m.item_type = 0
              AND m.associated_message_type = 0
            ORDER BY m.ROWID DESC
            LIMIT 50
        """).fetchall()
        conn.close()

        chats_now_read = set()
        for row in rows:
            rid = row["ROWID"]
            is_read = bool(row["is_read"])
            old = self._read_cache.get(rid)
            if old is not None and not old and is_read:
                chats_now_read.add(row["chat_identifier"])
            self._read_cache[rid] = is_read

        for chat_id in chats_now_read:
            _clear_read_horizon(chat_id)
            await self.manager.broadcast({
                "type": "read_sync",
                "chat_identifier": chat_id,
            })

        if len(self._read_cache) > 200:
            keep = {row["ROWID"] for row in rows}
            self._read_cache = {k: v for k, v in self._read_cache.items() if k in keep}


def _chat_title(db_path: str, chat_identifier: str, contacts: dict[str, str]) -> str:
    """A group's own name if it has one, else its members' names."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT display_name FROM chat WHERE chat_identifier = ? ORDER BY ROWID DESC LIMIT 1",
                           (chat_identifier,)).fetchone()
    finally:
        conn.close()
    return (row[0] if row and row[0] else "") or group_display_name(db_path, chat_identifier, contacts)


def group_display_name(db_path: str, chat_identifier: str, contacts: dict[str, str]) -> str:
    """Name for an unnamed group chat: the members, resolved through Contacts."""
    members = get_group_members(db_path, chat_identifier)
    return ", ".join(resolve_identifier(m, contacts) or m for m in members)


class WebHandler:
    def __init__(self, manager: ConnectionManager, contacts: dict[str, str] | None = None, db_path: str = ""):
        self.manager = manager
        self.contacts = contacts or {}
        self.db_path = db_path
        self._group_names: dict[str, str] = {}   # chat_identifier -> resolved member list

    async def forward_to_output(self, msg: ChatMessage):
        sender_name = msg.sender_id
        if msg.sender_id and msg.sender_id != "me":
            sender_name = resolve_identifier(msg.sender_id, self.contacts) or msg.sender_id
        # Unnamed group chats have an empty display_name in chat.db. Fill it with
        # the member list so a brand-new thread appearing live in the sidebar is
        # labelled "Ty, Katie, Luis" rather than whoever spoke first.
        chat_display_name = msg.chat_display_name
        if not chat_display_name and msg.chat_style == 43 and self.db_path:
            chat_display_name = self._group_names.get(msg.chat_identifier)
            if chat_display_name is None:
                chat_display_name = await asyncio.to_thread(group_display_name, self.db_path, msg.chat_identifier, self.contacts)
                self._group_names[msg.chat_identifier] = chat_display_name
        data = {
            "type": "message",
            "chat_identifier": msg.chat_identifier,
            "chat_display_name": chat_display_name,
            "chat_style": msg.chat_style,
            "service": msg.service,
            "sender_id": sender_name,
            "is_from_me": msg.is_from_me,
            "rowid": msg.rowid,
            "text": _sanitize(msg.text),
            "timestamp": msg.timestamp.isoformat(),
            "attachments": [
                {
                    "transfer_name": a.transfer_name,
                    "mime_type": a.mime_type,
                    "url": _register_attachment(a.filename),
                }
                for a in msg.attachments
                if a.filename and os.path.exists(a.filename)
            ],
        }
        await self.manager.broadcast(_sanitize(data))


def _read_horizons() -> dict[str, int]:
    """Load the per-chat read horizons from the state DB.

    Read on a short-lived connection of its own rather than by ATTACHing the
    state DB to the chat.db connection: the ATTACH held a lock on bridge.db for
    the whole (slow) chat.db query, which deadlocked against the poller's
    writes and 500'd the page. A lock or a not-yet-created DB degrades to "no
    horizons" — badges may reappear once, which beats failing the request.
    """
    if not _session_db_path or not os.path.exists(_session_db_path):
        return {}
    try:
        # Opened read-write, not mode=ro: a read-only connection cannot create the
        # -shm file a WAL database needs, so it fails outright whenever no other
        # connection happens to have the DB open. Under WAL a read-write reader
        # still never blocks the poller's writes.
        hconn = sqlite3.connect(_session_db_path, timeout=2.0)
        try:
            return {
                r[0]: r[1]
                for r in hconn.execute(
                    "SELECT chat_identifier, max_rowid FROM webapp_read_horizon"
                )
            }
        finally:
            hconn.close()
    except sqlite3.Error:
        return {}


def get_recent_chats(db_path: str, contacts: dict[str, str], limit: int = 50) -> list[dict]:
    from imessage_reader import IMessageReader
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Two things made this take a full second on a large chat.db, on every
    # page load and every re-sync:
    #   * "last message per chat" grouped over every message ever received.
    #     Instead, for each chat, step back through its (chat_id, message_id)
    #     index to the first displayable message: a few index reads per chat.
    #   * the unread count walked each listed chat's whole history. Instead,
    #     start from the (few) unread incoming messages and group them by chat.
    rows = conn.execute("""
        WITH last_msgs AS (
            SELECT c.ROWID AS chat_id,
                   (SELECT j.message_id
                    FROM chat_message_join j
                    CROSS JOIN message m ON m.ROWID = j.message_id
                    WHERE j.chat_id = c.ROWID
                      AND m.item_type = 0 AND m.associated_message_type = 0
                    ORDER BY j.message_id DESC
                    LIMIT 1) AS last_rowid
            FROM chat c
        )
        SELECT c.ROWID AS chat_rowid, c.chat_identifier, c.display_name, c.style, c.service_name,
               last_msg.service AS last_service,
               last_msg.date AS last_date,
               last_msg.text AS last_text,
               last_msg.attributedBody AS last_attributed_body,
               last_msg.cache_has_attachments AS last_has_attachments
        FROM chat c
        JOIN last_msgs lm ON lm.chat_id = c.ROWID
        JOIN message last_msg ON last_msg.ROWID = lm.last_rowid
        ORDER BY last_msg.date DESC
        LIMIT ?
    """, (limit,)).fetchall()

    # Unread incoming messages per chat, ignoring those at or below the chat's
    # read horizon (already seen in the web UI; see webapp_read_horizon).
    horizons = _read_horizons()
    unread: dict[int, int] = {}
    for chat_rowid, chat_identifier, msg_rowid in conn.execute("""
        SELECT cmj.chat_id, c.chat_identifier, mu.ROWID
        FROM message mu
        JOIN chat_message_join cmj ON cmj.message_id = mu.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE mu.is_read = 0
          AND mu.is_from_me = 0
          AND mu.item_type = 0
          AND mu.associated_message_type = 0
    """):
        if msg_rowid > horizons.get(chat_identifier, 0):
            unread[chat_rowid] = unread.get(chat_rowid, 0) + 1
    conn.close()

    chats = []
    for row in rows:
        display_name = row["display_name"] or ""
        style = row["style"]
        if not display_name:
            if style == 43:
                display_name = group_display_name(db_path, row["chat_identifier"], contacts)
            else:
                display_name = resolve_identifier(row["chat_identifier"], contacts) or row["chat_identifier"]

        last_text = row["last_text"]
        if not last_text and row["last_attributed_body"]:
            last_text = IMessageReader._extract_attributed_text(row["last_attributed_body"])
        # Strip the object-replacement char iMessage uses to mark embedded attachments
        if last_text:
            last_text = last_text.replace("￼", "").strip()
        if not last_text and row["last_has_attachments"]:
            last_text = "📎 Attachment"
        last_text = (last_text or "")[:80]

        chats.append({
            "chat_identifier": row["chat_identifier"],
            "display_name": display_name,
            "style": style,
            # Theme by the ACTUAL last message's service, not chat.service_name:
            # Messages leaves the chat-level flag stale (e.g. an all-iMessage
            # thread pinned to "RCS"), which mis-colored the composer green.
            "service": row["last_service"] or row["service_name"],
            "last_text": last_text,
            "unread_count": unread.get(row["chat_rowid"], 0),
        })
    return _sanitize(chats)


def find_chat_for_identifier(db_path: str, identifier: str) -> dict | None:
    """Find the most-recent individual chat matching a contact identifier.

    The sidebar only shows the 50 most-recent chats, but a contact's existing
    conversation may rank lower (e.g. #64) and thus be absent from the DOM — so
    selecting that contact in the composer would otherwise start a blank thread
    with no history. This resolves the identifier against ALL chats in chat.db,
    matching phone numbers by their normalized last-10 digits and emails case-
    insensitively, and returns the best (most-recent, non-empty, style 45) match.

    Returns a chat dict ({chat_identifier, style}) or None if no chat exists.
    """
    from contacts import _normalize_phone

    is_email = "@" in identifier
    norm = identifier.lower() if is_email else _normalize_phone(identifier)
    if not norm:
        return None

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Only individual chats (style 45) with at least one message, most-recent first.
    # "Most recent" = highest message id, read straight off the join table's
    # index (aggregating every message's date took about a second).
    rows = conn.execute("""
        SELECT chat_identifier, style, last_id FROM (
            SELECT c.chat_identifier, c.style,
                   (SELECT MAX(j.message_id) FROM chat_message_join j WHERE j.chat_id = c.ROWID) AS last_id
            FROM chat c
            WHERE c.style = 45
        )
        WHERE last_id IS NOT NULL
        ORDER BY last_id DESC
    """).fetchall()
    conn.close()

    for row in rows:
        cid = row["chat_identifier"]
        if not cid:
            continue
        if is_email:
            if cid.lower() == norm:
                return {"chat_identifier": cid, "style": row["style"]}
        else:
            if _normalize_phone(cid) == norm:
                return {"chat_identifier": cid, "style": row["style"]}
    return None


_attachment_registry: dict[str, tuple[str, float]] = {}
_ATTACHMENT_TTL = 3600
_TEMP_MAX_AGE = 3 * 86400     # uploads and converted photos in temp_dir


def _register_attachment(filepath: str) -> str | None:
    if not filepath or not os.path.exists(filepath):
        return None
    token = secrets.token_urlsafe(24)
    _attachment_registry[token] = (filepath, time.time())
    name = os.path.basename(filepath)
    return f"/api/attachments/{token}/{urllib.parse.quote(name)}"


def _prune_attachments():
    now = time.time()
    expired = [k for k, (_, ts) in _attachment_registry.items() if now - ts > _ATTACHMENT_TTL]
    for k in expired:
        del _attachment_registry[k]


_TAPBACK_MAP = {
    2000: "\u2764\ufe0f", 2001: "\U0001f44d", 2002: "\U0001f44e",
    2003: "\U0001f602", 2004: "\u203c\ufe0f", 2005: "\u2753",
}
# Types 3000-3005 remove the corresponding 2000-2005 reaction
_TAPBACK_REMOVE = {3000, 3001, 3002, 3003, 3004, 3005}

_REACTED_PATTERN = re.compile(r'^Reacted (.+?) to ')


def _extract_custom_emoji(text: str | None, attributed_body: bytes | None) -> str | None:
    """Extract emoji from 'Reacted X to ...' text for type 2006 reactions."""
    msg = text
    if not msg and attributed_body:
        from imessage_reader import IMessageReader
        msg = IMessageReader._extract_attributed_text(attributed_body)
    if not msg:
        return None
    m = _REACTED_PATTERN.match(msg)
    return m.group(1) if m else None


def _chat_rowids(conn, chat_identifier: str) -> list[int]:
    """chat.ROWIDs for an identifier (usually one; the same identifier can have
    a row per service)."""
    return [r[0] for r in conn.execute("SELECT ROWID FROM chat WHERE chat_identifier = ?", (chat_identifier,))]


def _chat_id_filter(chat_ids: list[int]) -> str:
    """SQL for "cmj is in one of these chats". A single id is written as an
    equality so SQLite can read the (chat_id, message_id) index in order."""
    if len(chat_ids) == 1:
        return "cmj.chat_id = ?"
    return f"cmj.chat_id IN ({', '.join('?' * len(chat_ids))})"


# Prefixes Messages puts in front of the target guid in associated_message_guid:
# none, "bp:" (whole bubble), or "p:N/" (part N of a multi-part message).
_REACTION_GUID_PREFIXES = ("", "bp:") + tuple(f"p:{n}/" for n in range(6))


def _get_reactions_for_messages(conn, chat_identifier: str, since_rowid: int = 0,
                                guids: list[str] | None = None) -> dict[str, list[dict]]:
    """Load a chat's reactions, keyed by target message guid.

    With no arguments beyond the chat, loads every reaction in it (a busy
    thread holds thousands). A page of messages passes `since_rowid` (its
    oldest message) and its `guids`:
      * a tapback is normally written after its target, so the chat's rows
        from `since_rowid` on cover almost everything;
      * history synced from iCloud can land out of order, so older rows are
        also looked up by target guid, through the index on that column.
    """
    chat_ids = _chat_rowids(conn, chat_identifier)
    if not chat_ids:
        return {}
    rows = conn.execute(f"""
        SELECT m.associated_message_type, m.associated_message_guid,
               m.is_from_me, m.text, m.attributedBody
        FROM chat_message_join cmj
        CROSS JOIN message m ON m.ROWID = cmj.message_id
        WHERE {_chat_id_filter(chat_ids)}
          AND cmj.message_id >= ?
          AND m.associated_message_type >= 2000
    """, (*chat_ids, since_rowid)).fetchall()
    if guids and since_rowid > 0:
        targets = [prefix + g for g in guids for prefix in _REACTION_GUID_PREFIXES]
        for i in range(0, len(targets), 900):   # stay under SQLite's bound-parameter limit
            chunk = targets[i:i + 900]
            rows += conn.execute(f"""
                SELECT associated_message_type, associated_message_guid, is_from_me, text, attributedBody
                FROM message
                WHERE associated_message_guid IN ({', '.join('?' * len(chunk))})
                  AND associated_message_type >= 2000
                  AND ROWID < ?
            """, (*chunk, since_rowid)).fetchall()

    # Build reactions, then remove any that have a corresponding 3000+ removal
    reactions: dict[str, list[dict]] = {}
    removals: dict[str, list[dict]] = {}

    for row in rows:
        guid = row["associated_message_guid"]
        # "p:0/GUID", "bp:GUID" or bare GUID. ("bp:" wasn't stripped, so
        # tapbacks on a whole bubble were filed under a key nothing looked up.)
        msg_guid = guid.split("/")[-1] if "/" in guid else guid.removeprefix("bp:")
        rtype = row["associated_message_type"]
        is_from_me = bool(row["is_from_me"])

        if rtype in _TAPBACK_REMOVE:
            # This is a removal of a standard tapback
            original_type = rtype - 1000
            emoji = _TAPBACK_MAP.get(original_type, "")
            if emoji:
                removals.setdefault(msg_guid, []).append({"emoji": emoji, "is_from_me": is_from_me})
            continue

        if rtype == 2006:
            emoji = _extract_custom_emoji(row["text"], row["attributedBody"])
        else:
            emoji = _TAPBACK_MAP.get(rtype, "")

        if not emoji:
            continue
        reactions.setdefault(msg_guid, []).append({
            "emoji": emoji,
            "is_from_me": is_from_me,
        })

    # Apply removals
    for guid, removal_list in removals.items():
        if guid not in reactions:
            continue
        for removal in removal_list:
            for i, r in enumerate(reactions[guid]):
                if r["emoji"] == removal["emoji"] and r["is_from_me"] == removal["is_from_me"]:
                    reactions[guid].pop(i)
                    break

    # Clean empty entries
    return {k: v for k, v in reactions.items() if v}


def _get_attachments_for_message(conn, message_rowid: int) -> list[dict]:
    rows = conn.execute("""
        SELECT a.filename, a.mime_type, a.transfer_name, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
          AND a.mime_type IS NOT NULL
    """, (message_rowid,)).fetchall()
    attachments = []
    for row in rows:
        filename = row["filename"]
        if filename:
            filename = os.path.expanduser(filename)
        url = _register_attachment(filename) if filename else None
        if url:
            attachments.append({
                "transfer_name": row["transfer_name"] or os.path.basename(filename),
                "mime_type": row["mime_type"],
                "url": url,
            })
    return attachments


def _get_known_chat_identifiers(db_path: str) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT chat_identifier FROM chat").fetchall()
    conn.close()
    return {row["chat_identifier"] for row in rows}


def get_chat_messages(db_path: str, chat_identifier: str, contacts: dict[str, str], limit: int = 100, offset: int = 0) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    chat_ids = _chat_rowids(conn, chat_identifier)
    if not chat_ids:
        conn.close()
        return []
    # Start from chat_message_join and walk its (chat_id, message_id) index
    # newest-first, stopping after `limit` rows. Starting from the chat's whole
    # history and sorting it (what the planner did with `message` first) cost
    # most of a second on a thread with hundreds of thousands of messages, on
    # every single switch to it. CROSS JOIN pins the join order.
    rows = conn.execute(f"""
        SELECT m.ROWID, m.guid, m.text, m.is_from_me, m.date, m.attributedBody,
               m.cache_has_attachments, m.service, m.was_delivered_quietly,
               m.date_delivered, m.date_read, COALESCE(m.error, 0) AS error, h.id as sender_id,
               m.thread_originator_guid,
               orig.text as reply_to_text, orig.attributedBody as reply_to_body,
               orig.is_from_me as reply_to_from_me, h2.id as reply_to_sender
        FROM chat_message_join cmj
        CROSS JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN message orig ON m.thread_originator_guid = orig.guid
        LEFT JOIN handle h2 ON orig.handle_id = h2.ROWID
        WHERE {_chat_id_filter(chat_ids)}
          AND m.item_type = 0
          AND m.associated_message_type = 0
        ORDER BY cmj.message_id DESC
        LIMIT ? OFFSET ?
    """, (*chat_ids, limit, offset)).fetchall()

    from imessage_reader import IMessageReader

    # Only reactions that can apply to this page, not the chat's whole history.
    oldest = min((r["ROWID"] for r in rows), default=0)
    reactions = _get_reactions_for_messages(conn, chat_identifier, since_rowid=oldest,
                                            guids=[r["guid"] for r in rows if r["guid"]])

    messages = []
    for row in reversed(rows):
        text = row["text"]
        if text is None and row["attributedBody"]:
            text = IMessageReader._extract_attributed_text(row["attributedBody"])

        attachments = []
        if row["cache_has_attachments"]:
            attachments = _get_attachments_for_message(conn, row["ROWID"])

        date = row["date"]
        if date and date != 0:
            ts = datetime.fromtimestamp(date / 1_000_000_000 + APPLE_EPOCH_OFFSET, tz=timezone.utc).isoformat()
        else:
            ts = ""
        if text is None and not attachments and row["attributedBody"] is None:
            continue
        sender_id = row["sender_id"] or "me"
        sender_name = resolve_identifier(sender_id, contacts) if sender_id != "me" else "me"

        status = None
        if row["is_from_me"]:
            dr = row["date_read"]
            dd = row["date_delivered"]
            if row["error"]:
                status = "failed"
            elif dr and dr != 0:
                status = "read"
            elif dd and dd != 0:
                status = "delivered"
            else:
                status = "sent"

        msg_reactions = reactions.get(row["guid"], [])

        reply_to = None
        if row["thread_originator_guid"]:
            reply_text = row["reply_to_text"]
            if reply_text is None and row["reply_to_body"]:
                reply_text = IMessageReader._extract_attributed_text(row["reply_to_body"])
            if reply_text:
                reply_sender = row["reply_to_sender"] or "me"
                reply_sender_name = resolve_identifier(reply_sender, contacts) if reply_sender != "me" else "me"
                reply_to = {
                    "text": _sanitize(reply_text)[:100],
                    "sender": _sanitize(reply_sender_name or reply_sender),
                    "is_from_me": bool(row["reply_to_from_me"]),
                }

        messages.append({
            "rowid": row["ROWID"],
            "text": _sanitize(text),
            "is_from_me": bool(row["is_from_me"]),
            "service": row["service"],
            "delivered_quietly": bool(row["was_delivered_quietly"]),
            "sender_id": _sanitize(sender_name or sender_id),
            "timestamp": ts,
            "status": status,
            "attachments": attachments,
            "reactions": msg_reactions,
            "reply_to": reply_to,
        })

    conn.close()
    return _sanitize(messages)


LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>iMessage Web Gateway - Login</title>
<link rel="icon" type="image/svg+xml" href="/static/logo2.svg">
<link rel="manifest" href="/static/manifest.json">
<style>
  body { font-family: -apple-system, sans-serif; background: #1a1a1a; color: #e0e0e0;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
  .card { background: #222; padding: 32px; border-radius: 12px; width: 300px; text-align: center; }
  .login-logo { width: 64px; height: 64px; border-radius: 14px; margin-bottom: 16px; }
  h2 { margin: 0 0 20px; font-size: 18px; }
  input { width: 100%; padding: 10px 14px; border: 1px solid #444; border-radius: 8px;
          background: #2a2a2a; color: #e0e0e0; font-size: 14px; box-sizing: border-box; }
  input:focus { border-color: #0b84fe; outline: none; }
  button { width: 100%; padding: 10px; margin-top: 12px; background: #0b84fe; color: #fff;
           border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }
  button:hover { background: #0a75e0; }
  .error { color: #ff3b30; font-size: 12px; margin-top: 8px; }
  .alt-link { display: inline-block; margin-top: 16px; color: #8e8e93; font-size: 12px; text-decoration: none; }
  .alt-link:hover { color: #e0e0e0; text-decoration: underline; }
  button:disabled { opacity: .6; cursor: default; }
  /* `hidden` must win over the display rules above (it didn't for .alt-link). */
  [hidden] { display: none !important; }
  .notice { color: #8e8e93; font-size: 12px; margin: -8px 0 16px; }
  .hint { color: #636366; font-size: 11px; margin-top: 18px; line-height: 1.4; }
</style>
</head><body>
<script nonce="{nonce}">if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');</script>
<noscript><style>#pw-form { display: block !important; } #pk-view { display: none !important; }</style></noscript>
<div class="card">
  <img src="/static/logo2.svg" alt="" class="login-logo">
  <h2>iMessage Web Gateway</h2>
  {notice}
  <div id="pk-view"{pk_hidden}>
    <button type="button" id="pk-btn">&#128273; Sign in with passkey</button>
    <div class="error" id="pk-err"></div>
    <a href="#" class="alt-link" id="pw-link">Use password instead</a>
  </div>
  <form id="pw-form" method="POST" action="/login"{pw_hidden}>
    <!-- Lets a password manager file this login under a name and offer to fill it. -->
    <input type="text" name="username" value="imessage-bridge" autocomplete="username" hidden>
    <input type="password" name="password" id="pw-input" placeholder="Password" autocomplete="current-password" required>
    <button type="submit" id="pw-btn">Sign in</button>
    {error}
    <a href="#" class="alt-link" id="pk-link">Use a passkey instead</a>
  </form>
  <div class="hint">You stay signed in on this browser for {idle_days} days after you last use it.</div>
</div>
<script src="/static/passkey.js?v={build}"></script>
<script nonce="{nonce}">
const SIGNED_OUT = {signed_out}, PASSWORD_FIRST = {pw_first}, HAVE_PASSKEYS = {have_passkeys};
const pkBtn = document.getElementById('pk-btn'), pkErr = document.getElementById('pk-err');
const PK_LABEL = pkBtn.innerHTML;

async function passkeyLogin() {
  pkErr.textContent = '';
  pkBtn.disabled = true; pkBtn.textContent = 'Waiting for your passkey…';
  const res = await window.passkeySignIn();
  if (res.ok) { pkBtn.textContent = 'Signed in'; window.location.replace('/'); return; }
  pkBtn.disabled = false; pkBtn.innerHTML = PK_LABEL;
  pkErr.textContent = res.message;
}
// Passkey is the default view; the password form stays hidden behind a link.
// The server opens on the password form instead when no passkey is enrolled
// or a password attempt just failed.
function showPassword(show) {
  document.getElementById('pk-view').hidden = show;
  document.getElementById('pw-form').hidden = !show;
  if (show) document.getElementById('pw-input').focus();
}
pkBtn.addEventListener('click', passkeyLogin);
document.getElementById('pw-link').addEventListener('click', e => { e.preventDefault(); showPassword(true); });
document.getElementById('pk-link').addEventListener('click', e => { e.preventDefault(); showPassword(false); });
document.getElementById('pw-form').addEventListener('submit', () => {
  const b = document.getElementById('pw-btn'); b.disabled = true; b.textContent = 'Signing in…';
});
const canPasskey = window.passkeySupported() && HAVE_PASSKEYS;
if (!canPasskey) {
  document.getElementById('pk-link').hidden = true;
  showPassword(true);
}

(async () => {
  // Already signed in? (The session cookie is SameSite=Strict, so arriving from
  // a link in another app or site lands here even with a perfectly good
  // session. Asking from this page sends the cookie.) Skip the login page.
  try {
    const r = await fetch('/api/session', {cache: 'no-store'});
    if (r.ok) { window.location.replace('/'); return; }
  } catch (_) {}
  // Otherwise go straight to the passkey prompt, so opening the app is one
  // Touch ID / Face ID rather than a click and then a prompt. Not right after
  // signing out, and only in a tab that's in front. Browsers that insist on a
  // tap first refuse quietly and the button is still there.
  if (canPasskey && !SIGNED_OUT && !PASSWORD_FIRST && document.visibilityState === 'visible' && document.hasFocus()) {
    passkeyLogin();
  }
})();
</script>
</body></html>"""


def _fmt_days(seconds: float) -> str:
    days = seconds / 86400
    return f"{days:g}" if days < 10 else str(round(days))


def _login_html(error: str = "", password_first: bool = False, signed_out: bool = False, nonce: str = "") -> str:
    """Render the login page. Passkey sign-in is the default view; the password
    form opens first only when no passkey is enrolled or `password_first` is set
    (a password attempt just failed, so keep the user where they were).
    `signed_out` (just logged out) stops the page prompting for a passkey on its own."""
    no_passkeys = not _passkeys()
    pw_first = password_first or no_passkeys
    js = lambda flag: "true" if flag else "false"
    return (LOGIN_HTML
            .replace("{pk_hidden}", " hidden" if pw_first else "")
            .replace("{pw_hidden}", "" if pw_first else " hidden")
            .replace("{have_passkeys}", js(not no_passkeys))
            .replace("{pw_first}", js(password_first))
            .replace("{signed_out}", js(signed_out))
            .replace("{notice}", '<div class="notice">You\'re signed out.</div>' if signed_out else "")
            .replace("{idle_days}", _fmt_days(_SESSION_IDLE))
            .replace("{build}", _read_build_number())
            .replace("{nonce}", nonce)
            .replace("{error}", error))


class ContactStore:
    def __init__(self):
        self.contacts: dict[str, str] = {}
        self.count: int = 0
        self.last_sync: str = ""
        self.sync()

    def sync(self):
        self.contacts = load_contacts()
        self.count = len(self.contacts)
        self.last_sync = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Loaded {self.count} contacts from AddressBook")


def _read_build_number() -> str:
    """Read the build number from the VERSION file next to this module."""
    version_path = os.path.join(BASE_DIR, "VERSION")
    try:
        with open(version_path) as f:
            return f.read().strip()
    except OSError:
        return "?"


def create_app(core: AppCore) -> FastAPI:
    build_number = _read_build_number()
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    _sw_path = os.path.join(BASE_DIR, "static", "sw.js")

    @app.get("/sw.js", response_class=Response)
    async def service_worker():
        with open(_sw_path) as f:
            content = f.read()
        # no-cache: a service worker fix should reach an installed app on its
        # next launch, not whenever the browser's cached copy happens to age out.
        return Response(content=content, media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    _init_session_db(core.config.app.state_db)
    # Temp dir can contain decrypted message attachments (HEIC→JPEG conversions).
    # Restrict to owner-only so other local users can't read them.
    _temp_dir = core.config.app.temp_dir
    os.makedirs(_temp_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(_temp_dir, 0o700)
    except OSError:
        pass
    manager = ConnectionManager(max_connections=core.config.web.max_connections)
    contact_store = ContactStore()
    web_handler = WebHandler(manager, contact_store.contacts, core.config.imessage.db_path)
    core.add_handler(web_handler)

    status_poller = StatusPoller(core.config.imessage.db_path, manager)
    known_chats = _get_known_chat_identifiers(core.config.imessage.db_path)
    password = core.config.web.password
    global _SESSION_TTL, _SESSION_IDLE
    _SESSION_TTL = int(core.config.web.session_max_days * 86400)
    _SESSION_IDLE = int(core.config.web.session_idle_days * 86400)

    def _set_session_cookie(resp: Response, request: Request, token: str) -> None:
        # The cookie outlives the idle limit on purpose: the server decides
        # when a session is over, so activity never has to re-issue the cookie.
        resp.set_cookie("session", token, httponly=True, samesite="strict",
                        secure=(request.url.scheme == "https"), max_age=_SESSION_TTL)
    _notify_cfg.update({
        "enabled": core.config.web.login_notify,
        "url": core.config.web.ha_url,
        "token_file": core.config.web.ha_token_file,
        "service": core.config.web.ha_notify_service,
    })
    max_msg_len = core.config.web.max_message_length
    allowed_origins = set(core.config.web.allowed_origins)
    login_rate_limit = core.config.web.login_rate_limit
    login_rate_window = core.config.web.login_rate_window
    failed_logins: list[float] = []            # wrong passwords from any address
    failed_login_cap = login_rate_limit * 6    # per login_rate_window

    def _too_many_failed_logins(now: float) -> bool:
        failed_logins[:] = [t for t in failed_logins if now - t < login_rate_window]
        return len(failed_logins) >= failed_login_cap

    def _note_failed_login(now: float) -> None:
        failed_logins.append(now)
        if len(failed_logins) == failed_login_cap:
            _notify("iMessage bridge: password sign-in paused",
                    f"{failed_login_cap} wrong passwords in {login_rate_window // 60} minutes, from several addresses",
                    critical=True)
        # Forget addresses whose attempts have aged out, so the table can't grow without bound.
        for ip in [ip for ip, ts in _login_attempts.items() if not ts or now - ts[-1] > login_rate_window]:
            _login_attempts.pop(ip, None)

    @app.middleware("http")
    async def harden(request: Request, call_next):
        # A request that changes something must come from this app's own pages.
        # The session cookie is SameSite=Strict, but "same site" includes every
        # other *.studiox.net host, so a page on a sibling subdomain could
        # otherwise post here with the cookie attached.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site is not None:
                # The browser's own statement of where the request came from.
                # Preferred over Origin, which browsers blank to "null" on some
                # same-origin posts (depending on the referrer policy).
                ok = fetch_site in ("same-origin", "none") or origin in allowed_origins
            elif origin is not None:
                ok = _same_origin(origin, request.headers.get("host", ""), allowed_origins)
            else:
                ok = True       # neither header: not a browser, so no cookie to ride on
            if not ok:
                print(f"Refused cross-origin {request.method} {request.url.path} from {origin or 'another site'}", flush=True)
                return Response("Cross-origin request refused", status_code=403)
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        # Attachments set their own policy (see _attachment_response). The
        # page's would stop a browser's built-in PDF viewer from opening one.
        is_file = request.url.path.startswith("/api/attachments/")
        for name, value in _STATIC_HEADERS.items():
            if not (is_file and name == "X-Frame-Options"):
                response.headers.setdefault(name, value)
        if not is_file:
            response.headers.setdefault(
                "Content-Security-Policy", _csp(request.state.csp_nonce, request.headers.get("host", "")))
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Conversations and contacts shouldn't sit in the browser's disk cache.
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(core.poll_loop())
        asyncio.create_task(status_poller.poll_loop())
        asyncio.create_task(_attachment_prune_loop())
        if not password:
            print("WARNING: No password set — web UI is unauthenticated!")
        print(f"Web UI started — http://{core.config.web.host}:{core.config.web.port}")

    async def _attachment_prune_loop():
        from imessage_sender import _staging_dir
        last_sweep = 0.0
        while True:
            _prune_attachments()
            # Uploads and converted photos are copies of private pictures; don't
            # keep them once they've done their job. Hourly is plenty.
            if time.time() - last_sweep > 3600:
                last_sweep = time.time()
                n = await asyncio.to_thread(_prune_dir, _temp_dir, _TEMP_MAX_AGE)
                # Copies staged for Messages, normally removed a minute after
                # the send; a restart inside that minute strands them.
                n += await asyncio.to_thread(_prune_dir, _staging_dir(), 86400)
                if n:
                    print(f"Removed {n} stale temporary file(s)", flush=True)
            await asyncio.sleep(300)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, signedout: str = "", session: str | None = Cookie(default=None, alias="session")):
        if not password or _valid_session(session):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_login_html(signed_out=bool(signedout), nonce=request.state.csp_nonce))

    @app.get("/api/session")
    async def api_session(session: str | None = Cookie(default=None, alias="session")):
        """Is this browser signed in? 401 if not. The web client asks this to
        tell "session over" apart from "network down" (both look the same on a
        dropped WebSocket), and the login page asks it to skip itself."""
        if not password:
            return {"ok": True}
        remaining = _session_remaining(session)
        if remaining <= 0:
            raise HTTPException(status_code=401)
        expiry = _session_db.execute("SELECT expiry FROM sessions WHERE token = ?", (_token_key(session),)).fetchone()[0]
        return {"ok": True, "expires_in": int(remaining), "ends_by": int(expiry),
                "idle_days": _fmt_days(_SESSION_IDLE)}

    @app.post("/login")
    async def login_submit(request: Request, response: Response, password_input: str = Form(alias="password")):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        nonce = request.state.csp_nonce
        # Rate limiting
        attempts = _login_attempts.get(client_ip, [])
        attempts = [t for t in attempts if now - t < login_rate_window]
        if len(attempts) >= login_rate_limit:
            if len(attempts) == login_rate_limit:      # notify once per lockout, not per retry
                attempts.append(now)
                _login_attempts[client_ip] = attempts
                _notify_login(client_ip, ok=False, detail=f"{login_rate_limit} wrong passwords")
            return HTMLResponse(
                _login_html('<div class="error">Too many attempts. Try again later.</div>', password_first=True, nonce=nonce),
                status_code=429,
            )
        # The per-address limit above means little to someone with many
        # addresses (any IPv6 network has billions). Cap wrong passwords from
        # everywhere combined too; passkey sign-in is unaffected.
        if _too_many_failed_logins(now):
            return HTMLResponse(
                _login_html('<div class="error">Password sign-in is paused after repeated wrong passwords. '
                            'Use a passkey, or try again later.</div>', password_first=True, nonce=nonce),
                status_code=429,
            )

        if not password or secrets.compare_digest(password_input.encode(), password.encode()):
            _login_attempts.pop(client_ip, None)
            token = _create_session(client_ip)
            _notify_login(client_ip, ok=True)
            resp = RedirectResponse("/", status_code=303)
            _set_session_cookie(resp, request, token)
            return resp

        attempts.append(now)
        _login_attempts[client_ip] = attempts
        _note_failed_login(now)
        return HTMLResponse(_login_html('<div class="error">Invalid password</div>', password_first=True, nonce=nonce), status_code=401)

    @app.get("/logout")
    async def logout(session: str | None = Cookie(default=None, alias="session")):
        if session and _session_db:
            _session_db.execute("DELETE FROM sessions WHERE token = ?", (_token_key(session),))
            _session_db.commit()
        resp = RedirectResponse("/login?signedout=1", status_code=303)
        resp.delete_cookie("session")
        return resp

    @app.post("/logout-all")
    async def logout_all(request: Request, session: str | None = Cookie(default=None, alias="session")):
        """Invalidate every session on every device (panic button)."""
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        n = _logout_everywhere()
        ip = request.client.host if request.client else "unknown"
        _notify("iMessage bridge: logged out everywhere", f"{n} session(s) revoked from {ip}")
        resp = RedirectResponse("/login?signedout=1", status_code=303)
        resp.delete_cookie("session")
        return resp

    @app.get("/api/passkeys/available")
    async def passkeys_available(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        return {"count": len(_passkeys())}

    # ---- passkeys: enrolment (must already be logged in) ----
    @app.post("/api/passkeys/register/options")
    async def passkey_register_options(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        rp_id, _ = _rp(request)
        opts = generate_registration_options(
            rp_id=rp_id, rp_name="iMessage Bridge",
            user_id=_PASSKEY_USER_ID, user_name="justin", user_display_name="Justin",
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k["credential_id"])) for k in _passkeys()],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        cid = _new_challenge(opts.challenge, "register")
        return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}

    @app.post("/api/passkeys/register/verify")
    async def passkey_register_verify(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        body = await request.json()
        challenge = _take_challenge(body.get("challenge_id", ""), "register")
        if not challenge:
            raise HTTPException(status_code=400, detail="challenge expired; try again")
        rp_id, origin = _rp(request)
        try:
            v = verify_registration_response(
                credential=body["credential"], expected_challenge=challenge,
                expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"registration failed: {e}")
        name = (body.get("name") or "Passkey").strip()[:60]
        transports = body.get("credential", {}).get("response", {}).get("transports") or []
        from webauthn.helpers import bytes_to_base64url
        _session_db.execute(
            "INSERT INTO passkeys (credential_id, public_key, sign_count, name, transports, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (bytes_to_base64url(v.credential_id), v.credential_public_key, v.sign_count, name, json.dumps(transports), time.time()),
        )
        _session_db.commit()
        ip = request.client.host if request.client else "unknown"
        _notify("iMessage bridge: passkey added", f"'{name}' enrolled from {ip} — {_geo(ip)}")
        return {"ok": True}

    @app.get("/api/passkeys")
    async def passkey_list(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        return [{"id": k["credential_id"], "name": k["name"], "created_at": k["created_at"], "last_used_at": k["last_used_at"]}
                for k in _passkeys()]

    @app.delete("/api/passkeys/{credential_id}")
    async def passkey_delete(credential_id: str, request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        row = _session_db.execute("SELECT name FROM passkeys WHERE credential_id = ?", (credential_id,)).fetchone()
        _session_db.execute("DELETE FROM passkeys WHERE credential_id = ?", (credential_id,))
        _session_db.commit()
        ip = request.client.host if request.client else "unknown"
        _notify("iMessage bridge: passkey removed", f"'{row[0] if row else credential_id[:8]}' removed from {ip} — {_geo(ip)}")
        return {"ok": True}

    # ---- passkeys: login (no session needed) ----
    @app.post("/login/passkey/options")
    async def passkey_login_options(request: Request):
        rp_id, _ = _rp(request)
        opts = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        cid = _new_challenge(opts.challenge, "login")
        return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}

    async def _verify_passkey(request: Request, purpose: str) -> dict:
        """Shared by passkey login and passkey send-unlock. Rate-limited like passwords.
        Returns the matched key row on success, raises HTTPException otherwise."""
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < login_rate_window]
        if len(attempts) >= login_rate_limit:
            raise HTTPException(status_code=429, detail="Too many attempts")
        body = await request.json()
        challenge = _take_challenge(body.get("challenge_id", ""), purpose)
        cred = body.get("credential") or {}
        key = next((k for k in _passkeys() if k["credential_id"] == cred.get("id")), None)
        if not challenge or not key:
            attempts.append(now); _login_attempts[client_ip] = attempts
            raise HTTPException(status_code=401, detail="unknown passkey")
        rp_id, origin = _rp(request)
        try:
            v = verify_authentication_response(
                credential=cred, expected_challenge=challenge, expected_rp_id=rp_id, expected_origin=origin,
                credential_public_key=key["public_key"], credential_current_sign_count=key["sign_count"],
                require_user_verification=True,
            )
        except Exception as e:
            attempts.append(now); _login_attempts[client_ip] = attempts
            raise HTTPException(status_code=401, detail=f"passkey rejected: {e}")
        _session_db.execute("UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
                            (v.new_sign_count, now, key["credential_id"]))
        _session_db.commit()
        _login_attempts.pop(client_ip, None)
        return key

    @app.post("/login/passkey/verify")
    async def passkey_login_verify(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        key = await _verify_passkey(request, "login")
        token = _create_session(client_ip)
        _notify("iMessage bridge: passkey login", f"'{key['name']}' from {client_ip} — {_geo(client_ip)}")
        resp = Response(content='{"ok": true}', media_type="application/json")
        _set_session_cookie(resp, request, token)
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            return RedirectResponse("/login", status_code=303)
        # to_thread: get_recent_chats is blocking sqlite against a ~1.5GB chat.db.
        # Run on the loop it stalled every other task (including the poller that
        # would release the lock it was waiting on) for the full busy timeout.
        chats = await asyncio.to_thread(
            get_recent_chats, core.config.imessage.db_path, contact_store.contacts
        )
        return templates.TemplateResponse(request, "chat.html", {
            "chats": chats, "build_number": build_number, "csp_nonce": request.state.csp_nonce})

    @app.get("/api/version")
    async def api_version():
        """Public endpoint — returns the running build number and server time. No auth required."""
        return {"build": build_number, "server_time": time.time()}

    @app.get("/api/chats")
    async def api_chats(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        return await asyncio.to_thread(
            get_recent_chats, core.config.imessage.db_path, contact_store.contacts
        )

    @app.post("/api/contacts/sync")
    async def sync_contacts(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        contact_store.sync()
        web_handler.contacts = contact_store.contacts
        return {"count": contact_store.count, "last_sync": contact_store.last_sync}

    @app.get("/api/contacts/search")
    async def contacts_search(q: str = "", session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if len(q) < 2:
            return []
        return search_contacts(q, contact_store.contacts)

    @app.get("/api/contacts/resolve-chat")
    async def resolve_chat(identifier: list[str] = Query(default=[]),
                           session: str | None = Cookie(default=None, alias="session")):
        """Find the existing conversation with exactly these people, if any.

        Used by the New Message composer to show the thread a message is about
        to go into (with its history), even when that chat is outside the
        50-chat sidebar list. One `identifier` resolves a 1:1 chat; repeat the
        parameter for a group. Returns {chat_identifier, style, display_name}
        or {chat_identifier: null}.
        """
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        ids = [i for i in identifier if i]
        db_path = core.config.imessage.db_path
        if len(ids) == 1:
            match = await asyncio.to_thread(find_chat_for_identifier, db_path, ids[0])
            if match:
                match["display_name"] = resolve_identifier(match["chat_identifier"], contact_store.contacts) or match["chat_identifier"]
            return match or {"chat_identifier": None}
        if len(ids) > 1:
            group = await asyncio.to_thread(find_group_chat, db_path, ids)
            if group:
                name = await asyncio.to_thread(_chat_title, db_path, group[0], contact_store.contacts)
                return {"chat_identifier": group[0], "style": group[1], "display_name": name}
        return {"chat_identifier": None}

    @app.get("/api/contacts/status")
    async def contacts_status(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        return {"count": contact_store.count, "last_sync": contact_store.last_sync}

    @app.post("/api/messages/new")
    async def send_new_message(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if not _check_send_rate(session or ""):
            raise HTTPException(status_code=429, detail="Send rate limit exceeded; slow down.")
        body = await request.json()
        recipients = body.get("recipients", [])
        text = body.get("text", "").strip()
        if not recipients or not text:
            raise HTTPException(status_code=400, detail="recipients and text required")
        if len(text) > max_msg_len:
            text = text[:max_msg_len]
        created = False
        if len(recipients) > 1:
            group = find_group_chat(core.config.imessage.db_path, recipients)
            if group:
                chat_id, style = group
                ok, err = await asyncio.to_thread(core.send_to_imessage, chat_id, style, text=text)
                if not ok:
                    raise HTTPException(status_code=502, detail=f"Messages could not send: {err}")
            else:
                # No such group yet: have Messages create it with the first message.
                ok, chat_id, err = await asyncio.to_thread(core.sender.create_group_and_send, recipients, text, core.config.imessage.db_path)
                if not ok:
                    raise HTTPException(status_code=502, detail=f"Could not start a new group chat: {err}")
                style, created = 43, True
        else:
            chat_id, style = recipients[0], 45
            ok, err = await asyncio.to_thread(core.send_to_imessage, chat_id, style, text=text)
            if not ok:
                raise HTTPException(status_code=502, detail=f"Messages could not send: {err}")
        # Refresh known chats so WebSocket sending works for this chat going forward
        nonlocal known_chats
        known_chats = _get_known_chat_identifiers(core.config.imessage.db_path)
        if created:
            web_handler._group_names.pop(chat_id, None)
        name = group_display_name(core.config.imessage.db_path, chat_id, contact_store.contacts) if style == 43 else (resolve_identifier(chat_id, contact_store.contacts) or chat_id)
        return {"ok": True, "chat_identifier": chat_id, "chat_style": style, "display_name": name, "created": created}

    @app.get("/api/chats/{chat_identifier:path}/messages")
    async def api_messages(chat_identifier: str, offset: int = 0, limit: int = 100, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        limit = min(limit, 200)
        # to_thread: blocking sqlite against a large chat.db; on the event loop
        # it stalls every WebSocket (pings included) while a thread loads.
        return await asyncio.to_thread(
            get_chat_messages, core.config.imessage.db_path, chat_identifier, contact_store.contacts,
            limit=limit, offset=offset,
        )

    @app.post("/api/chats/{chat_identifier:path}/read")
    async def mark_chat_read(chat_identifier: str, request: Request, session: str | None = Cookie(default=None, alias="session")):
        """Record that the user has seen messages up through max_rowid.

        The body should be JSON: {"max_rowid": <int>}
        This persists across page reloads — get_recent_chats excludes messages
        at or below this ROWID from the unread_count until iMessage syncs is_read=1.
        """
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        body = await request.json()
        max_rowid = int(body.get("max_rowid", 0))
        if max_rowid > 0:
            _set_read_horizon(chat_identifier, max_rowid)
        return {"ok": True}

    @app.get("/api/attachments/{token}/{filename}")
    async def serve_attachment(token: str, filename: str, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        entry = _attachment_registry.get(token)
        if not entry:
            raise HTTPException(status_code=404)
        filepath, created_at = entry
        if time.time() - created_at > _ATTACHMENT_TTL:
            del _attachment_registry[token]
            raise HTTPException(status_code=404)
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404)

        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".heic", ".heif"):
            # One converted copy per photo, named for the photo rather than for
            # this link. (Named per link, every view of a conversation made a
            # fresh JPEG of every photo in it and none was ever deleted.)
            try:
                st = os.stat(filepath)
                key = hashlib.sha256(f"{filepath}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:32]
            except OSError:
                key = token
            jpeg_path = os.path.join(core.config.app.temp_dir, f"heic-{key}.jpg")
            if os.path.exists(jpeg_path):
                try:
                    os.utime(jpeg_path)     # still in use: keep it from the pruner
                except OSError:
                    pass
            else:
                os.makedirs(core.config.app.temp_dir, mode=0o700, exist_ok=True)
                import subprocess
                part = f"{jpeg_path[:-4]}.{secrets.token_hex(4)}.part.jpg"   # sips wants a .jpg name
                await asyncio.to_thread(
                    subprocess.run,
                    ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", filepath, "--out", part],
                    capture_output=True, timeout=15,
                )
                if os.path.exists(part):
                    os.chmod(part, 0o600)
                    os.replace(part, jpeg_path)
            if os.path.exists(jpeg_path):
                return _attachment_response(jpeg_path, "image/jpeg")

        return _attachment_response(filepath)

    _UPLOAD_MAX = 25 * 1024 * 1024  # 25 MB
    _UPLOAD_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp",
                             "image/heic", "image/heif", "image/tiff", "image/bmp",
                             "video/mp4", "video/quicktime",
                             "application/pdf"}

    @app.post("/api/upload")
    async def upload_file(file: UploadFile, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if file.content_type and file.content_type not in _UPLOAD_ALLOWED_TYPES:
            raise HTTPException(status_code=400, detail=f"File type not allowed: {file.content_type}")
        data = await file.read(_UPLOAD_MAX + 1)
        if len(data) > _UPLOAD_MAX:
            raise HTTPException(status_code=413, detail="File too large (25 MB max)")
        ext = os.path.splitext(file.filename or "file")[1].lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
            ext = ".png"
        safe_name = f"{secrets.token_urlsafe(16)}{ext}"
        dest = os.path.abspath(os.path.join(core.config.app.temp_dir, safe_name))
        os.makedirs(core.config.app.temp_dir, mode=0o700, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        os.chmod(dest, 0o600)
        return {"file_path": dest}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        nonlocal known_chats
        # Check Origin header before doing anything else (defense against
        # cross-origin WS upgrades that could ride on the session cookie).
        # Browsers always send Origin on a WebSocket; a missing one is a
        # non-browser client, which has no cookie jar for a page to ride on.
        origin = ws.headers.get("origin", "")
        if origin and not _same_origin(origin, ws.headers.get("host", ""), allowed_origins):
            print(f"Refused WebSocket from origin {origin!r}", flush=True)
            await ws.close(code=1008)
            return

        # Authenticate via the session cookie — browsers send it automatically
        # on same-origin WS upgrades, and cookies carry HttpOnly/SameSite/Secure
        # flags that query-string tokens don't.
        session_token = ws.cookies.get("session", "")
        if password:
            if not _valid_session(session_token):
                # Accept, then close with a code the page can read. Refusing
                # the handshake instead reaches the browser as a bare 1006, so
                # a signed-out tab would retry forever as if the network were down.
                await ws.accept()
                await ws.close(code=WS_AUTH_LOST, reason="Not signed in")
                return

        connected = await manager.connect(ws)
        if not connected:
            return

        async def _ping_loop():
            """Ping every 25s so both ends notice a dead socket, and drop the
            socket once its session is over (expired, or logged out elsewhere)
            so a signed-out tab stops receiving messages."""
            import asyncio as _aio
            while True:
                await _aio.sleep(25)
                try:
                    if password and not _valid_session(session_token, touch=False):
                        await ws.close(code=WS_AUTH_LOST, reason="Session ended")
                        break
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

        import asyncio as _asyncio

        async def _send_result(ws, client_id: str, chat_id: str, ok: bool, error: str = "", rowid: int = 0):
            if not ok:
                print(f"[IMSG-SEND] send to {chat_id!r} failed: {error}", flush=True)
            try:
                await ws.send_text(json.dumps({"type": "send_result", "client_id": client_id,
                                               "chat_identifier": chat_id, "ok": ok, "error": error,
                                               "rowid": rowid}))
            except Exception:
                pass

        async def _echo_sent(ws, chat_id: str, chat_style: int, text: str, rowid: int):
            """Show a message sent from this browser in every *other* open
            session. The poller skips the bridge's own sends (the sending page
            already drew them), so without this they'd only appear elsewhere
            after a reload."""
            others = [o for o in manager.active if o is not ws]
            if not others or not text:
                return
            service, _guid = await _asyncio.to_thread(core._chat_route, chat_id, chat_style)
            payload = json.dumps(_sanitize({
                "type": "message", "chat_identifier": chat_id, "chat_display_name": "",
                "chat_style": chat_style, "service": service, "sender_id": "me", "is_from_me": True,
                "rowid": rowid, "text": text, "timestamp": datetime.now(timezone.utc).isoformat(),
                "attachments": [],
            }))
            for other in others:
                try:
                    await other.send_text(payload)
                except Exception:
                    pass

        async def _confirm_send(ws, client_id: str, chat_id: str, chat_style: int, text: str,
                                before: int, timeout: float = 60.0):
            # Wait for Messages' final word on the row, not just its existence:
            # it writes the row as pending and only sets `error` when the send
            # fails, which for SMS can take ~20s.
            deadline = time.time() + timeout
            found = None
            while time.time() < deadline:
                await _asyncio.sleep(1.0)
                try:
                    found = await _asyncio.to_thread(core.find_sent_row, chat_id, before)
                except Exception as e:
                    print(f"[IMSG-SEND] confirm query failed: {e}", flush=True)
                    return
                if not found:
                    continue
                rowid, code, settled = found
                if code:
                    await _send_result(ws, client_id, chat_id, False,
                                       f"Messages reported error {code} (message {rowid})", rowid=rowid)
                    return
                if settled:
                    await _send_result(ws, client_id, chat_id, True, rowid=rowid)
                    await _echo_sent(ws, chat_id, chat_style, text, rowid)
                    return
            if found:
                # Still pending; don't call it failed. The status poller will
                # flip the bubble to Not Delivered if Messages errors later.
                print(f"[IMSG-SEND] message {found[0]} to {chat_id!r} still pending after {timeout:.0f}s", flush=True)
                await _send_result(ws, client_id, chat_id, True, rowid=found[0])
                await _echo_sent(ws, chat_id, chat_style, text, found[0])
                return
            await _send_result(ws, client_id, chat_id, False,
                               "Messages accepted the send but never recorded it. Check that Messages on the "
                               "Mac is signed in and, for SMS, that Text Message Forwarding to this Mac is on.")

        ping_task = _asyncio.create_task(_ping_loop())
        try:
            while True:
                data = await ws.receive_text()
                if len(data) > max_msg_len * 2:
                    continue
                msg = json.loads(data)
                if msg.get("type") == "pong":
                    _touch_session(session_token)  # client is alive → keeps idle timer fresh
                    continue
                if msg.get("type") == "ping":
                    # The page asking "are you still there?" (after waking, or
                    # coming back to the tab) so it needn't wait for our next ping.
                    await ws.send_text('{"type": "pong"}')
                    continue
                if msg.get("type") == "send":
                    if password and not _valid_session(session_token):
                        await ws.close(code=WS_AUTH_LOST, reason="Session ended")
                        break
                    chat_id = msg.get("chat_identifier", "")
                    chat_style = msg.get("chat_style", 45)
                    text = msg.get("text", "")
                    file_path = msg.get("file_path", "")

                    if chat_id not in known_chats:
                        # The list is read at startup; a thread that began
                        # since then (say, from the phone) isn't in it yet.
                        known_chats = await _asyncio.to_thread(_get_known_chat_identifiers, core.config.imessage.db_path)
                    if chat_id not in known_chats:
                        # Say so. Dropping it silently left the page waiting
                        # 75 seconds for a verdict that was never coming.
                        await _send_result(ws, str(msg.get("client_id", ""))[:64], chat_id, False,
                                           "The bridge doesn't know this conversation.")
                        continue
                    if not _check_send_rate(session_token):
                        await ws.send_text(json.dumps({"type": "error", "message": "Send rate limit exceeded"}))
                        await _send_result(ws, str(msg.get("client_id", ""))[:64], chat_id, False, "Send rate limit exceeded")
                        continue
                    # Validate file_path is inside the temp dir (prevent path traversal)
                    if file_path:
                        real = os.path.realpath(file_path)
                        temp_real = os.path.realpath(core.config.app.temp_dir)
                        if not real.startswith(temp_real + os.sep) or not os.path.isfile(real):
                            file_path = ""
                    if len(text) > max_msg_len:
                        text = text[:max_msg_len]
                    client_id = str(msg.get("client_id", ""))[:64]
                    before = core.latest_message_rowid()
                    # osascript can take seconds; keep it off the event loop.
                    if file_path:
                        f_ok, f_err = await _asyncio.to_thread(core.send_to_imessage, chat_id, chat_style, file_path=file_path)
                    else:
                        f_ok, f_err = True, ""
                    if text:
                        ok, err = await _asyncio.to_thread(core.send_to_imessage, chat_id, chat_style, text=text)
                    else:
                        ok, err = True, ""
                    if not (f_ok and ok):
                        await _send_result(ws, client_id, chat_id, False, "; ".join(e for e in (f_err, err) if e))
                    elif text or file_path:
                        # Messages accepting the AppleScript isn't proof it sent:
                        # confirm the row lands in chat.db without an error.
                        # (Attachment-only sends too: with no verdict at all the
                        # page times out and marks a delivered photo as failed.)
                        _asyncio.create_task(_confirm_send(ws, client_id, chat_id, chat_style, text, before))
        except Exception:      # includes WebSocketDisconnect
            pass
        finally:
            manager.disconnect(ws)
            ping_task.cancel()

    return app
