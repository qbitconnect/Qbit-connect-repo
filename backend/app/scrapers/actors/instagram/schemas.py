"""Instagram Intelligence actor schemas (spec §7.A)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class InstagramMode(str, Enum):
    PROFILE = "profile"
    POSTS = "posts"
    COMMENTS = "comments"
    HASHTAG = "hashtag"
    SEARCH = "search"


class InstagramInput(BaseModel):
    mode: InstagramMode = InstagramMode.PROFILE
    #: single username (profile/posts/comments)
    username: str | None = Field(default=None, max_length=100)
    #: full profile/post/reel URL (used when username is unknown)
    profile_url: HttpUrl | None = None
    #: hashtag without '#'
    hashtag: str | None = Field(default=None, min_length=1, max_length=100)
    #: free-text keyword (search mode)
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    #: bulk usernames (spec §7.A mode 7)
    usernames: list[str] = Field(default_factory=list, max_length=50)
    max_results: int = Field(default=50, ge=1, le=500)
    include_contact_info: bool = True
    request_timeout: int = Field(default=20, ge=1, le=120)
    respect_robots: bool = True

    @field_validator("usernames")
    @classmethod
    def _clean_usernames(cls, v: list[str]) -> list[str]:
        out = []
        for name in v:
            name = name.strip().lstrip("@")
            if name:
                out.append(name)
        return out

    def target_usernames(self) -> list[str]:
        """Resolved username list for profile-shaped modes."""
        if self.username:
            return [self.username.strip().lstrip("@")]
        if self.usernames:
            return self.usernames
        return []


OUTPUT_FIELDS = (
    "business_name",
    "website",
    "email",
    "phone",
    "source",
    "source_url",
    "metadata",
    "scraped_at",
)
