import os
import re
import shutil
import subprocess
import tempfile
import threading
import time


# How long to wait before deleting a staged attachment.  The AppleScript
# `send` call returns immediately, but Messages.app reads the file off disk
# asynchronously a moment later.  Deleting too soon produces a "Delivered"
# message with no attachment (a silent image-send failure).  60s is generous
# headroom even for large images on a slow uplink.
_STAGED_CLEANUP_DELAY = 60.0


def _deferred_unlink(path: str, delay: float = _STAGED_CLEANUP_DELAY) -> None:
    """Delete a file after a delay, on a daemon thread.

    Gives Messages.app time to read & transmit a staged attachment before the
    file is removed.  Runs as a daemon so it never blocks process shutdown.
    """
    def _worker():
        time.sleep(delay)
        try:
            os.unlink(path)
        except OSError:
            pass
    threading.Thread(target=_worker, daemon=True).start()


# Only allow phone numbers, emails, and iMessage group chat IDs
_BUDDY_PATTERN = re.compile(r"^[+\d\w@.\-]+$")
_GROUP_PATTERN = re.compile(r"^(chat)?[a-f0-9]+$")


def _validate_identifier(identifier: str, chat_style: int) -> bool:
    if chat_style == 43:
        return bool(_GROUP_PATTERN.match(identifier))
    return bool(_BUDDY_PATTERN.match(identifier))


def _staging_dir() -> str:
    """Return the directory used to stage attachments for Messages.app.

    On macOS 26 (Tahoe), Messages' transcoder agent (IMTranscoderAgent) is
    sandboxed by TCC and can ONLY read attachment files from media-blessed
    locations such as ~/Pictures.  Files under /tmp, ~/Documents, or project-
    local paths are accepted by the AppleScript `send` (which returns success!)
    but then fail to ingest: the attachment row lands in transfer_state=6 with
    its filename still pointing at the original path, the image is never copied
    into ~/Library/Messages/Attachments, and the recipient gets a "Delivered"
    message with NO image.

    Empirically verified on this host (2026-06-15):
      ~/Pictures/...        -> transfer_state=5 (ingested, transmits)  OK
      ~/Library/Messages/.. -> transfer_state=5                        OK
      ~/Documents/...       -> transfer_state=6 (stuck, no transmit)   FAIL
      /tmp/...              -> transfer_state=6                        FAIL

    A hidden subfolder of ~/Pictures inherits the blessed status and keeps the
    Pictures root uncluttered.
    """
    d = os.path.expanduser("~/Pictures/.imsg-webbridge-staging")
    os.makedirs(d, exist_ok=True)
    return d


def _stage_for_applescript(file_path: str) -> str:
    """Copy a file into a Messages-readable directory so the attachment ingests.

    See _staging_dir() for the macOS 26 TCC rationale — the staging location
    MUST be under ~/Pictures or Messages silently drops the attachment.

    Returns the path to the staged copy (caller must delete it when done).
    Falls back to the original path if the copy fails.
    """
    try:
        ext = os.path.splitext(file_path)[1] or ".bin"
        fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=_staging_dir())
        os.close(fd)
        shutil.copy2(file_path, tmp_path)
        os.chmod(tmp_path, 0o644)
        return tmp_path
    except OSError as e:
        print(f"Warning: could not stage file for AppleScript: {e}")
        return file_path


def _as_str(s: str) -> str:
    """Escape a Python string for safe inclusion as an AppleScript string literal.

    AppleScript string literals only need backslash and double-quote escaped.
    We use this instead of passing values via `system attribute` (env vars) or
    `on run argv` because — verified empirically on macOS 26 — `POSIX file`
    only attaches correctly when given a COMPILE-TIME LITERAL path.  Passing the
    path through any runtime indirection (env var, argv item) makes AppleScript
    report success (rc=0) but silently drop the attachment: the message ships
    empty with no attachment row in chat.db.  See the
    apple/imessage-attachment-debugging skill for the full investigation.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


_VALID_SERVICES = {"iMessage", "SMS", "RCS"}


def _service_keyword(service: str | None) -> str:
    """Map a chat.db service name to an AppleScript `service type` keyword.

    Defaults to iMessage for unknown/None so behaviour is unchanged for the
    common case.  Only iMessage/SMS/RCS are real service types in Messages.
    """
    return service if service in _VALID_SERVICES else "iMessage"


class IMessageSender:
    def send_text(self, chat_identifier: str, chat_style: int, text: str, service: str | None = None) -> bool:
        if not _validate_identifier(chat_identifier, chat_style):
            print(f"Invalid chat identifier: {chat_identifier}")
            return False
        if chat_style == 43:
            return self._send_to_group(chat_identifier, text=text)
        return self._send_to_buddy(chat_identifier, text=text, service=service)

    def send_file(self, chat_identifier: str, chat_style: int, file_path: str, service: str | None = None) -> bool:
        if not _validate_identifier(chat_identifier, chat_style):
            print(f"Invalid chat identifier: {chat_identifier}")
            return False
        # Messages.app's sandbox can only access standard user directories (/tmp,
        # ~/Downloads, ~/Desktop, ~/Documents).  Arbitrary project-relative paths
        # (e.g. tmp/ inside the webbridge directory) are silently inaccessible,
        # causing the AppleScript to create an empty message with no attachment.
        # Copy the file to a temporary path under /tmp that Messages.app can read.
        tmp_path = _stage_for_applescript(file_path)
        print(f"[IMSG-DIAG] send_file id={chat_identifier!r} style={chat_style} "
              f"orig={file_path!r} staged={tmp_path!r} "
              f"staged_exists={os.path.isfile(tmp_path)}", flush=True)
        try:
            if chat_style == 43:
                return self._send_to_group(chat_identifier, file_path=tmp_path)
            return self._send_to_buddy(chat_identifier, file_path=tmp_path, service=service)
        finally:
            # Defer cleanup of the staged copy.  The AppleScript `send` returns
            # immediately but Messages.app reads the file asynchronously; deleting
            # it synchronously here races the read and produces a "Delivered"
            # message with no attachment.  Delete on a delay instead.
            if tmp_path and tmp_path != file_path:
                _deferred_unlink(tmp_path)

    def _send_to_buddy(self, identifier: str, text: str | None = None, file_path: str | None = None,
                       service: str | None = None) -> bool:
        # Send to `participant "id" of <service>` rather than the legacy
        # `buddy "id"` form.  On macOS 26 (Tahoe), `buddy "<number>"` only
        # resolves iMessage contacts — for an SMS-only number it raises
        # "AppleEvent handler failed", or (via the `send ... to buddy`
        # shortcut) writes a chat.db row marked is_sent=1 that is NEVER
        # actually transmitted.  Addressing the participant on an explicit
        # service routes correctly for both iMessage and SMS.  Verified
        # 2026-07-14: SMS to +13135450117 wrote is_sent=1 rows but never
        # delivered until switched to `participant ... of (SMS service)`.
        svc = _service_keyword(service)
        if text is not None:
            # Text is passed via env var to prevent AppleScript injection.  This
            # works reliably for text (unlike file paths — see _as_str).
            script = """
tell application "Messages"
    set targetService to 1st service whose service type = %SERVICE%
    send (system attribute "IMSG_TEXT") to participant "%IDENTIFIER%" of targetService
end tell
""".replace("%SERVICE%", svc).replace("%IDENTIFIER%", _as_str(identifier))
            return self._run_applescript(script, env_text=text)
        else:
            # File path MUST be a compile-time literal or Messages silently drops
            # the attachment (verified on macOS 26).  Escape it as an AppleScript
            # string literal instead of passing via env var / argv.
            script = """
tell application "Messages"
    set targetService to 1st service whose service type = %SERVICE%
    send (POSIX file "%FILE%") to participant "%IDENTIFIER%" of targetService
end tell
""".replace("%SERVICE%", svc).replace("%FILE%", _as_str(file_path)).replace("%IDENTIFIER%", _as_str(identifier))
            return self._run_applescript(script, env_file=file_path)

    def _send_to_group(self, chat_identifier: str, text: str | None = None, file_path: str | None = None) -> bool:
        chat_id = f"any;+;{chat_identifier}"
        if text is not None:
            script = """
tell application "Messages"
    set targetChat to chat id "%CHAT_ID%"
    send (system attribute "IMSG_TEXT") to targetChat
end tell
""".replace("%CHAT_ID%", _as_str(chat_id))
            return self._run_applescript(script, env_text=text)
        else:
            # File path must be a literal (see _send_to_buddy).
            script = """
tell application "Messages"
    set targetChat to chat id "%CHAT_ID%"
    send (POSIX file "%FILE%") to targetChat
end tell
""".replace("%CHAT_ID%", _as_str(chat_id)).replace("%FILE%", _as_str(file_path))
            return self._run_applescript(script, env_file=file_path)

    @staticmethod
    def _run_applescript(script: str, env_text: str | None = None, env_file: str | None = None) -> bool:
        env = os.environ.copy()
        if env_text is not None:
            env["IMSG_TEXT"] = env_text
        if env_file is not None:
            env["IMSG_FILE"] = env_file
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        # Verbose diagnostics for attachment-send debugging (rc is unreliable —
        # see apple/imessage-attachment-debugging skill).
        if env_file is not None:
            exists = os.path.isfile(env_file)
            print(f"[IMSG-DIAG] file-send rc={result.returncode} "
                  f"file={env_file!r} exists={exists} "
                  f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}",
                  flush=True)
        if result.returncode != 0:
            print(f"AppleScript error: {result.stderr.strip()}", flush=True)
            return False
        return True
