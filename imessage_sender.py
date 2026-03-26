import json
import re
import subprocess


# Only allow phone numbers, emails, and iMessage group chat IDs
_BUDDY_PATTERN = re.compile(r"^[+\d\w@.\-]+$")
_GROUP_PATTERN = re.compile(r"^[a-f0-9]+$")


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
            return self._send_to_group_jxa(chat_identifier, text)
        return self._send_to_buddy_jxa(chat_identifier, text)

    def send_file(self, chat_identifier: str, chat_style: int, file_path: str) -> bool:
        if not _validate_identifier(chat_identifier, chat_style):
            print(f"Invalid chat identifier: {chat_identifier}")
            return False
        if chat_style == 43:
            return self._send_file_group_jxa(chat_identifier, file_path)
        return self._send_file_buddy_jxa(chat_identifier, file_path)

    def _send_to_buddy_jxa(self, identifier: str, text: str) -> bool:
        # Pass text as JSON to avoid any injection
        script = f"""
var app = Application("Messages");
var service = app.services().find(function(s) {{ return s.serviceType() === "iMessage"; }});
var buddy = service.buddies.whose({{id: {json.dumps(identifier)}}})[0];
app.send({json.dumps(text)}, {{to: buddy}});
"""
        return self._run_jxa(script)

    def _send_to_group_jxa(self, chat_identifier: str, text: str) -> bool:
        chat_id = f"iMessage;+;{chat_identifier}"
        script = f"""
var app = Application("Messages");
var chat = app.chats.whose({{id: {json.dumps(chat_id)}}})[0];
app.send({json.dumps(text)}, {{to: chat}});
"""
        return self._run_jxa(script)

    def _send_file_buddy_jxa(self, identifier: str, file_path: str) -> bool:
        script = f"""
var app = Application("Messages");
var service = app.services().find(function(s) {{ return s.serviceType() === "iMessage"; }});
var buddy = service.buddies.whose({{id: {json.dumps(identifier)}}})[0];
app.send(Path({json.dumps(file_path)}), {{to: buddy}});
"""
        return self._run_jxa(script)

    def _send_file_group_jxa(self, chat_identifier: str, file_path: str) -> bool:
        chat_id = f"iMessage;+;{chat_identifier}"
        script = f"""
var app = Application("Messages");
var chat = app.chats.whose({{id: {json.dumps(chat_id)}}})[0];
app.send(Path({json.dumps(file_path)}), {{to: chat}});
"""
        return self._run_jxa(script)

    @staticmethod
    def _run_jxa(script: str) -> bool:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"JXA error: {result.stderr.strip()}")
            return False
        return True
