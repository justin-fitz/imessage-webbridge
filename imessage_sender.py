import subprocess


class IMessageSender:
    def send_text(self, chat_identifier: str, chat_style: int, text: str) -> bool:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        if chat_style == 43:  # group
            return self._send_to_group(chat_identifier, f'"{escaped}"')
        else:  # 1-on-1
            return self._send_to_buddy(chat_identifier, f'"{escaped}"')

    def send_file(self, chat_identifier: str, chat_style: int, file_path: str) -> bool:
        content = f'POSIX file "{file_path}"'
        if chat_style == 43:
            return self._send_to_group(chat_identifier, content)
        else:
            return self._send_to_buddy(chat_identifier, content)

    def _send_to_buddy(self, identifier: str, content: str) -> bool:
        script = f"""
tell application "Messages"
    set targetService to (1st service whose service type is iMessage)
    set targetBuddy to buddy "{identifier}" of targetService
    send {content} to targetBuddy
end tell
"""
        return self._run_applescript(script)

    def _send_to_group(self, chat_identifier: str, content: str) -> bool:
        script = f"""
tell application "Messages"
    set targetChat to chat id "iMessage;+;{chat_identifier}"
    send {content} to targetChat
end tell
"""
        return self._run_applescript(script)

    @staticmethod
    def _run_applescript(script: str) -> bool:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"AppleScript error: {result.stderr.strip()}")
            return False
        return True
