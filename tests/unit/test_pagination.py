"""Cursor codec round-trip and tamper resistance."""

import base64
import uuid
from datetime import UTC, datetime

import pytest

from bookie_emulator.api.errors import InvalidParameterError
from bookie_emulator.api.pagination import Cursor, decode_cursor, encode_cursor


class TestCursorCodec:
    def test_round_trip(self) -> None:
        cursor = Cursor(placed_at=datetime(2026, 3, 30, 14, 22, 35, tzinfo=UTC), id=uuid.uuid4())
        assert decode_cursor(encode_cursor(cursor)) == cursor

    def test_cursor_is_opaque_base64url(self) -> None:
        cursor = Cursor(placed_at=datetime(2026, 3, 30, tzinfo=UTC), id=uuid.uuid4())
        encoded = encode_cursor(cursor)
        base64.urlsafe_b64decode(encoded.encode())  # must decode cleanly

    def test_garbage_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            decode_cursor("not-a-cursor!!!")

    def test_valid_base64_invalid_json_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            decode_cursor(base64.urlsafe_b64encode(b"pwned").decode())

    def test_missing_keys_rejected(self) -> None:
        with pytest.raises(InvalidParameterError):
            decode_cursor(base64.urlsafe_b64encode(b'{"placed_at": "2026-03-30T00:00:00+00:00"}').decode())

    def test_tampered_field_rejected(self) -> None:
        cursor = Cursor(placed_at=datetime(2026, 3, 30, tzinfo=UTC), id=uuid.uuid4())
        tampered = base64.urlsafe_b64encode(
            base64.urlsafe_b64decode(encode_cursor(cursor).encode()).replace(str(cursor.id).encode(), b"not-a-uuid")
        ).decode()
        with pytest.raises(InvalidParameterError):
            decode_cursor(tampered)
