import glob
import os
import re
import sqlite3


def _normalize_phone(number: str) -> str:
    """Strip to digits only, keep last 10 (US) or full international."""
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]  # strip US country code
    return digits


def _load_contacts_from_source(db_path: str, contacts: dict[str, str]):
    """Load phone and email mappings from one AddressBook source."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception:
        return

    try:
        # Phone numbers
        rows = conn.execute("""
            SELECT r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER
            FROM ZABCDRECORD r
            JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
            WHERE p.ZFULLNUMBER IS NOT NULL
        """).fetchall()
        for row in rows:
            name = _format_name(row["ZFIRSTNAME"], row["ZLASTNAME"])
            if name:
                normalized = _normalize_phone(row["ZFULLNUMBER"])
                if normalized:
                    contacts[normalized] = name

        # Email addresses
        rows = conn.execute("""
            SELECT r.ZFIRSTNAME, r.ZLASTNAME, e.ZADDRESS
            FROM ZABCDRECORD r
            JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
            WHERE e.ZADDRESS IS NOT NULL
        """).fetchall()
        for row in rows:
            name = _format_name(row["ZFIRSTNAME"], row["ZLASTNAME"])
            if name:
                contacts[row["ZADDRESS"].lower()] = name
    except Exception:
        pass
    finally:
        conn.close()


def _format_name(first: str | None, last: str | None) -> str:
    parts = [p for p in (first, last) if p]
    return " ".join(parts)


def load_contacts() -> dict[str, str]:
    """Build a lookup dict from normalized phone/email to contact name.

    Scans all AddressBook sources on the system.
    """
    contacts: dict[str, str] = {}
    ab_pattern = os.path.expanduser(
        "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
    )
    for path in glob.glob(ab_pattern):
        _load_contacts_from_source(path, contacts)
    return contacts


def search_contacts(query: str, contacts: dict[str, str], limit: int = 20) -> list[dict]:
    """Search contacts by name or identifier. Returns list of {name, identifier}.

    Returns all identifiers for a matching contact (phone + email), with phone
    numbers sorted before email addresses so the most useful one appears first.

    Results are ranked so that the most relevant matches come first:
      0. Name starts with the query              (e.g. "ryan" -> "Ryan Fitzhugh")
      1. A word in the name starts with the query (e.g. "fitz" -> "Ryan Fitzhugh")
      2. Query appears somewhere in the name      (e.g. "ryan" -> "Bryan Adams")
      3. Query only matches the identifier         (e.g. an email/phone substring)
    Within the same rank, results are alphabetical by name.
    """
    query_lower = query.lower()

    def rank(name: str, matched_name: bool) -> int:
        name_lower = name.lower()
        if not matched_name:
            return 3  # matched on identifier only
        if name_lower.startswith(query_lower):
            return 0  # name starts with query
        if any(word.startswith(query_lower) for word in name_lower.split()):
            return 1  # a word in the name starts with query
        return 2  # query is a mid-word substring of the name

    # Group all matching identifiers by contact name, tracking best rank
    by_name: dict[str, list[str]] = {}
    name_rank: dict[str, int] = {}
    for identifier, name in contacts.items():
        matched_name = query_lower in name.lower()
        matched_id = query_lower in identifier.lower()
        if not (matched_name or matched_id):
            continue
        by_name.setdefault(name, []).append(identifier)
        r = rank(name, matched_name)
        if name not in name_rank or r < name_rank[name]:
            name_rank[name] = r

    results = []
    # Sort by (rank, name) so prefix matches lead, then alphabetical
    for name in sorted(by_name, key=lambda n: (name_rank[n], n.lower())):
        identifiers = by_name[name]
        # Sort: phone numbers before email addresses
        identifiers.sort(key=lambda x: (1 if "@" in x else 0, x))
        for identifier in identifiers:
            # Format phone numbers for display
            if "@" not in identifier and identifier.isdigit():
                if len(identifier) == 10:
                    display_id = f"+1{identifier}"
                else:
                    display_id = f"+{identifier}"
            else:
                display_id = identifier
            results.append({"name": name, "identifier": display_id})
            if len(results) >= limit:
                return results
    return results


def resolve_identifier(identifier: str, contacts: dict[str, str]) -> str | None:
    """Look up a chat_identifier or handle id in the contacts dict."""
    if not identifier:
        return None
    # Try as email (lowercase)
    if "@" in identifier:
        return contacts.get(identifier.lower())
    # Try as phone number
    normalized = _normalize_phone(identifier)
    return contacts.get(normalized)


def get_group_members(db_path: str, chat_identifier: str) -> list[str]:
    """Get handle IDs for members of a group chat."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT h.id
            FROM chat c
            JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
            JOIN handle h ON chj.handle_id = h.ROWID
            WHERE c.chat_identifier = ?
        """, (chat_identifier,)).fetchall()
        conn.close()
        return [row["id"] for row in rows]
    except Exception:
        return []


def _normalize_identifier(identifier: str) -> str:
    """Normalize a phone number or email for set comparison."""
    if "@" in identifier:
        return identifier.strip().lower()
    return _normalize_phone(identifier)


def find_group_chat(db_path: str, participant_ids: list[str]) -> tuple[str, int] | None:
    """Find an existing group chat whose participants exactly match the given set.

    Returns (chat_identifier, style) or None.
    """
    target = {_normalize_identifier(p) for p in participant_ids if p}
    if len(target) < 2:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT c.ROWID as chat_rowid, c.chat_identifier, c.style,
                   MAX(m.date) as last_date, h.id as handle
            FROM chat c
            JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
            JOIN handle h ON chj.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            LEFT JOIN message m ON m.ROWID = cmj.message_id
            WHERE c.style = 43
            GROUP BY c.ROWID, h.id
        """).fetchall()
        conn.close()
    except Exception:
        return None

    chats: dict[int, dict] = {}
    for row in rows:
        rowid = row["chat_rowid"]
        entry = chats.setdefault(rowid, {
            "chat_identifier": row["chat_identifier"],
            "style": row["style"],
            "last_date": row["last_date"] or 0,
            "members": set(),
        })
        entry["members"].add(_normalize_identifier(row["handle"]))

    # Prefer the most recently active chat among exact matches
    candidates = [c for c in chats.values() if c["members"] == target]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: c["last_date"])
    return (best["chat_identifier"], best["style"])
