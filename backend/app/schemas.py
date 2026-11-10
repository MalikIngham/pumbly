from typing import Literal

from pydantic import BaseModel, Field

ScanState = Literal['queued', 'running', 'paused', 'complete', 'failed']


class ScanRequest(BaseModel):
    blog: str = Field(max_length=256)


class PostImage(BaseModel):
    full_url: str
    thumb_url: str
    width: int | None
    height: int | None
    alt: str
    from_ask: bool


class Post(BaseModel):
    id: str
    url: str
    type: str
    timestamp: int | None
    note_count: int | None
    summary: str
    asker: str | None
    question: str | None
    images: list[PostImage]
    poster_url: str | None
    video_urls: list[str]
    external_links: list[str]
    is_nsfw: bool


class ScanStatus(BaseModel):
    id: str
    blog: str
    status: ScanState
    scanned: int
    total: int | None
    originals: int
    new_originals: int
    error: str | None


class AuthStatus(BaseModel):
    configured: bool
    connected: bool


class DownloadLink(BaseModel):
    url: str


class PostPage(BaseModel):
    posts: list[Post]
    next_after: int
