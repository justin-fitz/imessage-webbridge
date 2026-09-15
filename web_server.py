import asyncio
import json
import os
import secrets
import sqlite3
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
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

_SESSION_TTL = 8 * 3600        # absolute lifetime of a login
_SESSION_IDLE = 2 * 3600       # logged out after this long without activity
_SEND_ARM_TTL = 4 * 3600      # how long "unlock sending" lasts after re-entering the password (or a passkey login)
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
    _session_db.commit()


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


def _create_session(ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    _session_db.execute(
        "INSERT INTO sessions (token, expiry, last_seen, ip, send_armed_until) VALUES (?, ?, ?, ?, 0)",
        (token, now + _SESSION_TTL, now, ip),
    )
    _session_db.commit()
    return token


def _valid_session(token: str | None) -> bool:
    """True if the session exists, hasn't hit its absolute or idle limit.
    Touches last_seen (at most once a minute) so activity keeps it alive."""
    if not token or not _session_db:
        return False
    row = _session_db.execute(
        "SELECT expiry, last_seen FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return False
    now = time.time()
    expiry, last_seen = row[0], row[1] or now
    if now > expiry or now - last_seen > _SESSION_IDLE:
        _session_db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        _session_db.commit()
        return False
    if now - last_seen > 60:
        _session_db.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (now, token))
        _session_db.commit()
    return True


def _touch_session(token: str | None) -> None:
    """Keep a session alive from WebSocket traffic (pongs), bypassing the 60s throttle."""
    if token and _session_db:
        _session_db.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (time.time(), token))
        _session_db.commit()


def _send_armed(token: str | None) -> float:
    """Return the unix time until which this session may send, or 0."""
    if not token or not _session_db:
        return 0
    row = _session_db.execute("SELECT send_armed_until FROM sessions WHERE token = ?", (token,)).fetchone()
    until = (row[0] if row else 0) or 0
    return until if until > time.time() else 0


def _arm_send(token: str) -> float:
    until = time.time() + _SEND_ARM_TTL
    _session_db.execute("UPDATE sessions SET send_armed_until = ? WHERE token = ?", (until, token))
    _session_db.commit()
    return until


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


class ConnectionManager:
    def __init__(self, max_connections: int = 20):
        self.active: list[WebSocket] = []
        self.max_connections = max_connections

    async def connect(self, ws: WebSocket):
        if len(self.active) >= self.max_connections:
            await ws.close(code=1008, reason="Too many connections")
            return False
        await ws.accept()
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
                   c.chat_identifier
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
            if dr and dr != 0:
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


class WebHandler:
    def __init__(self, manager: ConnectionManager, contacts: dict[str, str] | None = None):
        self.manager = manager
        self.contacts = contacts or {}

    async def forward_to_output(self, msg: ChatMessage):
        sender_name = msg.sender_id
        if msg.sender_id and msg.sender_id != "me":
            sender_name = resolve_identifier(msg.sender_id, self.contacts) or msg.sender_id
        data = {
            "type": "message",
            "chat_identifier": msg.chat_identifier,
            "chat_display_name": msg.chat_display_name,
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

    # Inline the horizons as a VALUES CTE. The table holds at most one row per
    # dismissed chat, so this stays small and keeps the query single-database.
    horizons = _read_horizons()
    horizon_params: list = []
    if horizons:
        values = ", ".join(["(?, ?)"] * len(horizons))
        for chat_id, max_rowid in horizons.items():
            horizon_params += [chat_id, max_rowid]
        horizon_cte = f"horizons(chat_identifier, max_rowid) AS (VALUES {values}), "
        horizon_expr = """COALESCE(
                (SELECT max_rowid FROM horizons
                 WHERE chat_identifier = c.chat_identifier), 0)"""
    else:
        horizon_cte = ""
        horizon_expr = "0"

    rows = conn.execute(f"""
        WITH {horizon_cte}last_msgs AS (
            SELECT cmj.chat_id AS chat_id, MAX(m.ROWID) AS last_rowid
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            WHERE m.item_type = 0 AND m.associated_message_type = 0
            GROUP BY cmj.chat_id
        )
        SELECT c.chat_identifier, c.display_name, c.style, c.service_name,
               last_msg.service AS last_service,
               last_msg.date AS last_date,
               last_msg.text AS last_text,
               last_msg.attributedBody AS last_attributed_body,
               last_msg.cache_has_attachments AS last_has_attachments,
               (SELECT COUNT(*) FROM message mu
                JOIN chat_message_join cmju ON mu.ROWID = cmju.message_id
                WHERE cmju.chat_id = c.ROWID
                  AND mu.is_from_me = 0
                  AND mu.is_read = 0
                  AND mu.item_type = 0
                  AND mu.associated_message_type = 0
                  AND mu.ROWID > {horizon_expr}
               ) AS unread_count
        FROM chat c
        JOIN last_msgs lm ON lm.chat_id = c.ROWID
        JOIN message last_msg ON last_msg.ROWID = lm.last_rowid
        ORDER BY last_msg.date DESC
        LIMIT ?
    """, (*horizon_params, limit)).fetchall()
    conn.close()

    chats = []
    for row in rows:
        display_name = row["display_name"] or ""
        style = row["style"]
        if not display_name:
            if style == 43:
                members = get_group_members(db_path, row["chat_identifier"])
                member_names = [resolve_identifier(m, contacts) or m for m in members]
                display_name = ", ".join(member_names)
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
            "unread_count": row["unread_count"] or 0,
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
    rows = conn.execute("""
        SELECT c.chat_identifier, c.style, MAX(m.date) AS last_date
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m ON m.ROWID = cmj.message_id
        WHERE c.style = 45
        GROUP BY c.ROWID
        ORDER BY last_date DESC
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

import re
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


def _get_reactions_for_messages(conn, chat_identifier: str) -> dict[str, list[dict]]:
    """Load all reactions for a chat, keyed by target message guid."""
    rows = conn.execute("""
        SELECT m.associated_message_type, m.associated_message_guid,
               m.is_from_me, m.text, m.attributedBody
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE c.chat_identifier = ?
          AND m.associated_message_type >= 2000
    """, (chat_identifier,)).fetchall()

    # Build reactions, then remove any that have a corresponding 3000+ removal
    reactions: dict[str, list[dict]] = {}
    removals: dict[str, list[dict]] = {}

    for row in rows:
        guid = row["associated_message_guid"]
        msg_guid = guid.split("/")[-1] if "/" in guid else guid
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
    rows = conn.execute("""
        SELECT m.ROWID, m.guid, m.text, m.is_from_me, m.date, m.attributedBody,
               m.cache_has_attachments, m.service, m.was_delivered_quietly,
               m.date_delivered, m.date_read, h.id as sender_id,
               m.thread_originator_guid,
               orig.text as reply_to_text, orig.attributedBody as reply_to_body,
               orig.is_from_me as reply_to_from_me, h2.id as reply_to_sender
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN message orig ON m.thread_originator_guid = orig.guid
        LEFT JOIN handle h2 ON orig.handle_id = h2.ROWID
        WHERE c.chat_identifier = ?
          AND m.item_type = 0
          AND m.associated_message_type = 0
        ORDER BY m.ROWID DESC
        LIMIT ? OFFSET ?
    """, (chat_identifier, limit, offset)).fetchall()

    from imessage_reader import IMessageReader

    reactions = _get_reactions_for_messages(conn, chat_identifier)

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
            if dr and dr != 0:
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
  form { background: #222; padding: 32px; border-radius: 12px; width: 300px; text-align: center; }
  .login-logo { width: 64px; height: 64px; border-radius: 14px; margin-bottom: 16px; }
  h2 { margin: 0 0 20px; font-size: 18px; }
  input { width: 100%; padding: 10px 14px; border: 1px solid #444; border-radius: 8px;
          background: #2a2a2a; color: #e0e0e0; font-size: 14px; box-sizing: border-box; }
  input:focus { border-color: #0b84fe; outline: none; }
  button { width: 100%; padding: 10px; margin-top: 12px; background: #0b84fe; color: #fff;
           border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }
  button:hover { background: #0a75e0; }
  .error { color: #ff3b30; font-size: 12px; margin-top: 8px; }
</style>
</head><body>
<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');</script>
<form method="POST" action="/login">
  <img src="/static/logo2.svg" alt="" class="login-logo">
  <h2>iMessage Web Gateway</h2>
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Login</button>
  <button type="button" id="pk-btn" style="background:#2c2c2e;margin-top:8px">&#128273; Sign in with passkey</button>
  <div class="error" id="pk-err"></div>
  {error}
</form>
<script>
const b64u = {
  dec: s => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)),
  enc: b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/[+]/g,'-').replace(/[/]/g,'_').replace(/=+$/,''),
};
async function passkeyLogin() {
  const err = document.getElementById('pk-err'); err.textContent = '';
  try {
    const r = await fetch('/login/passkey/options', {method:'POST'});
    const {challenge_id, options} = await r.json();
    options.challenge = b64u.dec(options.challenge);
    (options.allowCredentials||[]).forEach(c => c.id = b64u.dec(c.id));
    const cred = await navigator.credentials.get({publicKey: options});
    const body = {challenge_id, credential: {
      id: cred.id, rawId: b64u.enc(cred.rawId), type: cred.type,
      response: {
        authenticatorData: b64u.enc(cred.response.authenticatorData),
        clientDataJSON: b64u.enc(cred.response.clientDataJSON),
        signature: b64u.enc(cred.response.signature),
        userHandle: cred.response.userHandle ? b64u.enc(cred.response.userHandle) : null,
      }}};
    const v = await fetch('/login/passkey/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (v.ok) { window.location.href = '/'; return; }
    err.textContent = v.status === 429 ? 'Too many attempts. Try again later.' : 'Passkey not recognised';
  } catch (e) { if (e.name !== 'NotAllowedError') err.textContent = 'Passkey sign-in failed'; }
}
document.getElementById('pk-btn').addEventListener('click', passkeyLogin);
if (!window.PublicKeyCredential) document.getElementById('pk-btn').style.display = 'none';
</script>
</body></html>"""


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
        return Response(content=content, media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})

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
    web_handler = WebHandler(manager, contact_store.contacts)
    core.add_handler(web_handler)

    status_poller = StatusPoller(core.config.imessage.db_path, manager)
    known_chats = _get_known_chat_identifiers(core.config.imessage.db_path)
    password = core.config.web.password
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

    def require_auth(session: str | None = Cookie(default=None, alias="session")):
        if not password:
            return
        if not _valid_session(session):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(core.poll_loop())
        asyncio.create_task(status_poller.poll_loop())
        asyncio.create_task(_attachment_prune_loop())
        if not password:
            print("WARNING: No password set — web UI is unauthenticated!")
        print(f"Web UI started — http://{core.config.web.host}:{core.config.web.port}")

    async def _attachment_prune_loop():
        while True:
            _prune_attachments()
            await asyncio.sleep(300)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        if not password:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(LOGIN_HTML.replace("{error}", ""))

    @app.post("/login")
    async def login_submit(request: Request, response: Response, password_input: str = Form(alias="password")):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Rate limiting
        attempts = _login_attempts.get(client_ip, [])
        attempts = [t for t in attempts if now - t < login_rate_window]
        if len(attempts) >= login_rate_limit:
            if len(attempts) == login_rate_limit:      # notify once per lockout, not per retry
                attempts.append(now)
                _login_attempts[client_ip] = attempts
                _notify_login(client_ip, ok=False, detail=f"{login_rate_limit} wrong passwords")
            return HTMLResponse(
                LOGIN_HTML.replace("{error}", '<div class="error">Too many attempts. Try again later.</div>'),
                status_code=429,
            )

        if not password or secrets.compare_digest(password_input.encode(), password.encode()):
            _login_attempts.pop(client_ip, None)
            token = _create_session(client_ip)
            _notify_login(client_ip, ok=True)
            resp = RedirectResponse("/", status_code=303)
            is_secure = request.url.scheme == "https"
            resp.set_cookie("session", token, httponly=True, samesite="strict", secure=is_secure, max_age=_SESSION_TTL)
            return resp

        attempts.append(now)
        _login_attempts[client_ip] = attempts
        return HTMLResponse(LOGIN_HTML.replace("{error}", '<div class="error">Invalid password</div>'), status_code=401)

    @app.get("/logout")
    async def logout(session: str | None = Cookie(default=None, alias="session")):
        if session and _session_db:
            _session_db.execute("DELETE FROM sessions WHERE token = ?", (session,))
            _session_db.commit()
        resp = RedirectResponse("/login", status_code=303)
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
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("session")
        return resp

    @app.get("/api/send/status")
    async def send_status(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        until = _send_armed(session)
        return {"armed": bool(until), "until": until}

    @app.post("/api/send/arm")
    async def send_arm(request: Request, session: str | None = Cookie(default=None, alias="session")):
        """Sessions are read-only until the password is re-entered; then sending is
        allowed for _SEND_ARM_TTL. Shares the login rate limiter so it can't be brute-forced."""
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if not password:
            return {"armed": True, "until": time.time() + _SEND_ARM_TTL}
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < login_rate_window]
        if len(attempts) >= login_rate_limit:
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
        body = await request.json()
        if not secrets.compare_digest(str(body.get("password", "")).encode(), password.encode()):
            attempts.append(now)
            _login_attempts[client_ip] = attempts
            raise HTTPException(status_code=401, detail="Invalid password")
        _login_attempts.pop(client_ip, None)
        until = _arm_send(session)
        _notify("iMessage bridge: sending unlocked", f"From {client_ip} — {_geo(client_ip)} for {_SEND_ARM_TTL // 60} min")
        return {"armed": True, "until": until}

    # ---- passkeys: enrolment (must already be logged in) ----
    @app.post("/api/passkeys/register/options")
    async def passkey_register_options(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if password and not _send_armed(session):
            raise HTTPException(status_code=403, detail="send_locked")   # re-enter password before adding a key
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
        if password and not _send_armed(session):
            raise HTTPException(status_code=403, detail="send_locked")
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

    @app.post("/login/passkey/verify")
    async def passkey_login_verify(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < login_rate_window]
        if len(attempts) >= login_rate_limit:
            raise HTTPException(status_code=429, detail="Too many attempts")
        body = await request.json()
        challenge = _take_challenge(body.get("challenge_id", ""), "login")
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
        token = _create_session(client_ip)
        _arm_send(token)        # biometric + phishing-resistant → sending unlocked without a second prompt
        _notify("iMessage bridge: passkey login", f"'{key['name']}' from {client_ip} — {_geo(client_ip)}")
        resp = Response(content='{"ok": true}', media_type="application/json")
        resp.set_cookie("session", token, httponly=True, samesite="strict", secure=(request.url.scheme == "https"), max_age=_SESSION_TTL)
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
        return templates.TemplateResponse(request, "chat.html", {"chats": chats, "build_number": build_number})

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
    async def resolve_chat(identifier: str = "", session: str | None = Cookie(default=None, alias="session")):
        """Find an existing individual chat for a contact identifier, if any.

        Used by the composer so picking a contact opens their existing thread
        (with history) even when that chat is outside the 50-chat sidebar list.
        Returns {chat_identifier, style} or {chat_identifier: null}.
        """
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if not identifier:
            return {"chat_identifier": None}
        match = find_chat_for_identifier(core.config.imessage.db_path, identifier)
        return match or {"chat_identifier": None}

    @app.get("/api/contacts/status")
    async def contacts_status(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        return {"count": contact_store.count, "last_sync": contact_store.last_sync}

    @app.post("/api/messages/new")
    async def send_new_message(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if password and not _send_armed(session):
            raise HTTPException(status_code=403, detail="send_locked")
        if not _check_send_rate(session or ""):
            raise HTTPException(status_code=429, detail="Send rate limit exceeded; slow down.")
        body = await request.json()
        recipients = body.get("recipients", [])
        text = body.get("text", "").strip()
        if not recipients or not text:
            raise HTTPException(status_code=400, detail="recipients and text required")
        if len(text) > max_msg_len:
            text = text[:max_msg_len]
        if len(recipients) > 1:
            group = find_group_chat(core.config.imessage.db_path, recipients)
            if not group:
                raise HTTPException(
                    status_code=404,
                    detail="No existing group chat matches these participants. Start the group from Messages.app first.",
                )
            chat_id, style = group
            core.send_to_imessage(chat_id, style, text=text)
        else:
            core.send_to_imessage(recipients[0], 45, text=text)
        # Refresh known chats so WebSocket sending works for this chat going forward
        nonlocal known_chats
        known_chats = _get_known_chat_identifiers(core.config.imessage.db_path)
        return {"ok": True}

    @app.get("/api/chats/{chat_identifier:path}/messages")
    async def api_messages(chat_identifier: str, offset: int = 0, limit: int = 100, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        limit = min(limit, 200)
        return get_chat_messages(core.config.imessage.db_path, chat_identifier, contact_store.contacts, limit=limit, offset=offset)

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
            jpeg_path = os.path.join(core.config.app.temp_dir, f"{token}.jpg")
            if not os.path.exists(jpeg_path):
                os.makedirs(core.config.app.temp_dir, mode=0o700, exist_ok=True)
                import subprocess
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", filepath, "--out", jpeg_path],
                    capture_output=True, timeout=15,
                )
            if os.path.exists(jpeg_path):
                return FileResponse(jpeg_path, media_type="image/jpeg")

        return FileResponse(filepath)

    _UPLOAD_MAX = 25 * 1024 * 1024  # 25 MB
    _UPLOAD_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp",
                             "image/heic", "image/heif", "image/tiff", "image/bmp",
                             "video/mp4", "video/quicktime",
                             "application/pdf"}

    @app.post("/api/upload")
    async def upload_file(file: UploadFile, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        if password and not _send_armed(session):
            raise HTTPException(status_code=403, detail="send_locked")
        if file.content_type and file.content_type not in _UPLOAD_ALLOWED_TYPES:
            raise HTTPException(status_code=400, detail=f"File type not allowed: {file.content_type}")
        data = await file.read(_UPLOAD_MAX + 1)
        if len(data) > _UPLOAD_MAX:
            raise HTTPException(status_code=413, detail="File too large (25 MB max)")
        ext = os.path.splitext(file.filename or "file")[1].lower() or ".png"
        safe_name = f"{secrets.token_urlsafe(16)}{ext}"
        dest = os.path.abspath(os.path.join(core.config.app.temp_dir, safe_name))
        os.makedirs(core.config.app.temp_dir, mode=0o700, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        os.chmod(dest, 0o600)
        return {"file_path": dest}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        # Check Origin header before doing anything else (defense against
        # cross-origin WS upgrades that could ride on the session cookie).
        origin = ws.headers.get("origin", "")
        if allowed_origins and origin and origin not in allowed_origins:
            await ws.close(code=1008)
            return

        # Authenticate via the session cookie — browsers send it automatically
        # on same-origin WS upgrades, and cookies carry HttpOnly/SameSite/Secure
        # flags that query-string tokens don't.
        session_token = ws.cookies.get("session", "")
        if password:
            if not _valid_session(session_token):
                await ws.close(code=1008)
                return

        connected = await manager.connect(ws)
        if not connected:
            return

        async def _ping_loop():
            """Send application-level pings every 25s to detect zombie sockets."""
            import asyncio as _aio
            while True:
                await _aio.sleep(25)
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

        import asyncio as _asyncio
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
                if msg.get("type") == "send":
                    chat_id = msg.get("chat_identifier", "")
                    chat_style = msg.get("chat_style", 45)
                    text = msg.get("text", "")
                    file_path = msg.get("file_path", "")

                    if chat_id not in known_chats:
                        continue
                    if password and not _send_armed(session_token):
                        await ws.send_text(json.dumps({"type": "error", "code": "send_locked",
                                                       "message": "Sending is locked — unlock with your password"}))
                        continue
                    if not _check_send_rate(session_token):
                        await ws.send_text(json.dumps({"type": "error", "message": "Send rate limit exceeded"}))
                        continue
                    # Validate file_path is inside the temp dir (prevent path traversal)
                    if file_path:
                        real = os.path.realpath(file_path)
                        temp_real = os.path.realpath(core.config.app.temp_dir)
                        if not real.startswith(temp_real + os.sep) or not os.path.isfile(real):
                            file_path = ""
                    if len(text) > max_msg_len:
                        text = text[:max_msg_len]
                    if file_path:
                        core.send_to_imessage(chat_id, chat_style, file_path=file_path)
                    if text:
                        core.send_to_imessage(chat_id, chat_style, text=text)
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:
            manager.disconnect(ws)
        finally:
            ping_task.cancel()

    return app
