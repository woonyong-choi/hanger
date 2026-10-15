from __future__ import annotations

__all__ = ["Change", "Proposal", "digest", "issue_key", "validate_key"]

import hashlib
import hmac
import json
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from markchain.errors import RequestError

KEY_LIFETIME_SECONDS = 24 * 60 * 60


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    before: str | None = None
    after: str | None = None


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: tuple[Change, ...] = Field(min_length=1, max_length=1000)

    @property
    def fingerprint(self) -> str:
        data = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return digest(data.encode())

    @model_validator(mode="after")
    def validate_paths(self) -> Proposal:
        paths = [change.path for change in self.changes]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate target path")
        if any(c.before is None and c.after is None for c in self.changes):
            raise ValueError("change must contain a source or result")
        return self


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def issue_key(secret: bytes) -> str:
    payload = f"{int(time.time())}.{uuid4().hex}"
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


# The caller recovers by issuing a fresh key and sending the request again.
_NEW_KEY = (
    "요청 키를 사용할 수 없습니다: {}. issue_key로 새 키를 발급받아 다시 요청하십시오."
)


def validate_key(key: str, secret: bytes) -> float:
    try:
        issued, nonce, signature = key.split(".")
        issued_seconds = int(issued)
    except ValueError as error:
        raise RequestError(_NEW_KEY.format("형식 오류"), code="invalid_key") from error
    payload = f"{issued}.{nonce}"
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise RequestError(
            _NEW_KEY.format("이 서버에서 발급한 키가 아님"), code="invalid_key"
        )
    age_seconds = time.time() - issued_seconds
    if age_seconds < 0 or age_seconds >= KEY_LIFETIME_SECONDS:
        raise RequestError(_NEW_KEY.format("유효 기간 만료"), code="invalid_key")
    return float(issued_seconds + KEY_LIFETIME_SECONDS)
