"""Opaque cursor codec for keyset pagination (base64url-encoded JSON).

Cursors carry the (placed_at, id) keyset of the last row on a page; the
ledger query orders by placed_at DESC, id DESC and resumes strictly after
that pair. Clients must treat cursors as opaque strings.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from bookie_emulator.api.errors import InvalidParameterError


@dataclass(frozen=True)
class Cursor:
    placed_at: datetime
    id: uuid.UUID


def encode_cursor(cursor: Cursor) -> str:
    payload = {"placed_at": cursor.placed_at.isoformat(), "id": str(cursor.id)}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_cursor(raw: str) -> Cursor:
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))
        return Cursor(placed_at=datetime.fromisoformat(payload["placed_at"]), id=uuid.UUID(payload["id"]))
    except (binascii.Error, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidParameterError("Invalid pagination cursor") from exc
