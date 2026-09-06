from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .urls import validate_public_http_url


ShortString = Annotated[str, Field(max_length=100)]


class WorldEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=64)
    source_id: str = Field(max_length=100)
    source_name: str = Field(max_length=200)
    title: str = Field(max_length=300)
    url: str = Field(max_length=2048)
    summary: str = Field(max_length=500)
    published_at: str
    time_kind: Literal["published", "observed"] = "published"
    topics: list[ShortString] = Field(max_length=20)
    regions: list[ShortString] = Field(max_length=20)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return validate_public_http_url(value)

    @field_validator("published_at")
    @classmethod
    def validate_published_at(cls, value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("published_at must be a valid ISO timestamp") from exc
        if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
            raise ValueError("published_at must be explicitly UTC")
        return value


def _normalized(values: list[str], *, upper: bool, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", " ", raw.strip())[:100]
        value = value.upper() if upper else value.lower()
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    if len(result) > limit:
        raise ValueError(f"at most {limit} values are allowed")
    return result


class WorldProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    watch_symbols: list[ShortString] = Field(default_factory=list, max_length=100)
    interests: list[ShortString] = Field(default_factory=list, max_length=20)
    regions: list[ShortString] = Field(default_factory=list, max_length=20)

    @field_validator("watch_symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        result = _normalized(values, upper=True, limit=100)
        if any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.^_=/+-]{0,31}", value) for value in result):
            raise ValueError("watch symbols must be ticker identifiers")
        return result

    @field_validator("interests")
    @classmethod
    def normalize_interests(cls, values: list[str]) -> list[str]:
        return _normalized(values, upper=False, limit=20)

    @field_validator("regions")
    @classmethod
    def normalize_regions(cls, values: list[str]) -> list[str]:
        return _normalized(values, upper=True, limit=20)


class WorldConfig(BaseModel):
    x_enabled: bool = False
    x_bearer_token: SecretStr = SecretStr("")
    x_query: str = Field(default="", max_length=512)
    reddit_enabled: bool = False
    reddit_client_id: str = ""
    reddit_client_secret: SecretStr = SecretStr("")
    reddit_refresh_token: SecretStr = SecretStr("")
    reddit_user_agent: str = ""
    reddit_subreddits: str = Field(default="investing,stocks,Economics", max_length=300)
    sec_user_agent: str = Field(default="", max_length=300)

    @field_validator("reddit_subreddits")
    @classmethod
    def normalize_subreddits(cls, value: str) -> str:
        names = [part.strip() for part in value.split(",") if part.strip()]
        if not names or len(names) > 10 or any(not re.fullmatch(r"[A-Za-z0-9_]{1,21}", name) for name in names):
            raise ValueError("provide 1 to 10 valid subreddit names")
        return ",".join(dict.fromkeys(names))
