import os
import re
import subprocess


# Only allow phone numbers, emails, and iMessage group chat IDs
_BUDDY_PATTERN = re.compile(r"^[+\d\w@.\-]+$")
_GROUP_PATTERN = re.compile(r"^(chat)?[a-f0-9]+$")


def _validate_identifier(identifier: str, chat_style: int) -> bool:
    if chat_style == 43:
        return bool(_GROUP_PATTERN.match(identifier))
    return bool(_BUDDY_PATTERN.match(identifier))


class IMessageSender:
    def send_text(self, chat_identifier: str, chat_style: int, text: str) -> bool:
        if not _validate_identifier(chat_identifier, chat_style):
            print(f"Invalid chat identifier: {chat_identifier}")
            return False
        if chat_style == 43:
            return self._send_to_group(chat_identifier, text=text)
        return self._send_to_buddy(chat_identifier, text=text)

    def send_file(self, chat_identifier: str, chat_style: int, file_path: str) -> bool:
        if not _validate_identifier(chat_identifier, chat_style):
            print(f"Invalid chat identifier: {chat_identifier}")
            return False
        if chat_style == 43:
            return self._send_to_group(chat_identifier, file_path=file_path)
        return self._send_to_buddy(chat_identifier, file_path=file_path)

    def _send_to_buddy(self, identifier: str, text: str | None = None, file_path: str | None = None) -> bool:
        if text is not None:
            # Pass text via env var to prevent AppleScript injection
            script = """
tell application "Messages"
    set targetService to (1st service whose service type is iMessage)
    set targetBuddy to buddy "%IDENTIFIER%" of targetService
    send (system attribute "IMSG_TEXT") to targetBuddy
end tell
""".replace("%IDENTIFIER%", identifier)
            return self._run_applescript(script, env_text=text)
        else:
            script = f"""
tell application "Messages"
    set targetService to (1st service whose service type is iMessage)
    set targetBuddy to buddy "{identifier}" of targetService
    send POSIX file "{file_path}" to targetBuddy
end tell
"""
            return self._run_applescript(script)

    def _send_to_group(self, chat_identifier: str, text: str | None = None, file_path: str | None = None) -> bool:
        chat_id = f"any;+;{chat_identifier}"
        if text is not None:
            script = """
tell application "Messages"
    set targetChat to chat id "%CHAT_ID%"
    send (system attribute "IMSG_TEXT") to targetChat
end tell
""".replace("%CHAT_ID%", chat_id)
            return self._run_applescript(script, env_text=text)
        else:
            script = f"""
tell application "Messages"
    set targetChat to chat id "{chat_id}"
    send POSIX file "{file_path}" to targetChat
end tell
"""
            return self._run_applescript(script)

    @staticmethod
    def _run_applescript(script: str, env_text: str | None = None) -> bool:
        env = os.environ.copy()
        if env_text is not None:
            env["IMSG_TEXT"] = env_text
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if result.returncode != 0:
            print(f"AppleScript error: {result.stderr.strip()}")
            return False
        return True
