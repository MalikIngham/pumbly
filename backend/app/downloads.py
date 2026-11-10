import asyncio
import csv
import io
import logging
import re
import secrets
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from typing import AsyncIterator, Iterator
from urllib.parse import urlsplit

import httpx

from app.limits import Clock, Sleep
from app.schemas import Post
from app.store import Store
from app.tumblr import to_post

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024
SPOOL_BYTES = 16 * 1024 * 1024
# Zip entries are written without zip64 headers, which limits each to 2 GiB
MAX_FILE_BYTES = 2_000_000_000
MAX_ATTEMPTS = 3
TOKEN_TTL_SECONDS = 60
READ_BATCH = 200

# Ask for original formats, or Tumblr's CDN may convert images to webp
ACCEPT = 'image/jpeg, image/png, image/gif, video/mp4, image/webp;q=0.5, */*;q=0.1'
EXTENSIONS = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'video/mp4': 'mp4',
    'video/webm': 'webm',
    'video/quicktime': 'mov',
}
CSV_HEADER = [
    'Question',
    'Answer or Text',
    'Asked By',
    'Type',
    'Date (UTC)',
    'Link',
    'Files',
    'External Links',
]


class DownloadsBusy(Exception):
    '''Too many downloads are in progress.'''


class FetchFailed(Exception):
    '''A media file could not be downloaded. The message says why.'''


class _Sink(io.RawIOBase):
    '''Collects what zipfile writes so it can be streamed and released.'''

    def __init__(self):
        self.data = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.data += data
        return len(data)

    def drain(self) -> bytes:
        chunk = bytes(self.data)
        self.data.clear()
        return chunk


def _is_tumblr_media(url: str) -> bool:
    '''The server fetches these addresses itself, so it must not follow one
    pointing anywhere else.'''
    parts = urlsplit(url)
    host = parts.hostname or ''
    return parts.scheme == 'https' and host.endswith('.tumblr.com')


def _safe_cell(value: str) -> str:
    '''Spreadsheets run cells starting with these characters as formulas, and
    this text was written by strangers.'''
    return f"'{value}" if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


def _safe_id(post: Post) -> str:
    return re.sub(r'[^A-Za-z0-9_-]', '', post.id) or 'post'


def _zip_time(post: Post) -> tuple[int, int, int, int, int, int]:
    moment = datetime.fromtimestamp(post.timestamp or 0, timezone.utc)
    return tuple(moment.timetuple())[:6] if moment.year >= 1980 else (1980, 1, 1, 0, 0, 0)


def _iso_date(post: Post) -> str:
    if post.timestamp is None:
        return ''
    return datetime.fromtimestamp(post.timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _csv_row(post: Post, files: list[str]) -> list[str]:
    is_ask = post.question is not None
    return [
        _safe_cell(cell)
        for cell in (
            post.question or '',
            post.summary,
            (post.asker or 'Anonymous') if is_ask else '',
            post.type,
            _iso_date(post),
            post.url,
            ' '.join(files),
            ' '.join(post.external_links),
        )
    ]


def _belongs_in_csv(post: Post) -> bool:
    return bool(post.summary or post.question is not None or post.external_links)


def _encode_csv(rows: list[list[str]]) -> bytes:
    text = io.StringIO(newline='')
    csv.writer(text).writerows(rows)
    # The byte order mark makes Excel read accents and emoji correctly
    return text.getvalue().encode('utf-8-sig')


class DownloadManager:
    '''Builds each zip as it is sent, so a large blog is never held in memory.

    The browser fetches the zip with a plain link, where an error response
    would replace the page. Limits are therefore checked when a short lived,
    single use token is issued, where the frontend can show the error.'''

    def __init__(
        self,
        http: httpx.AsyncClient,
        store: Store,
        max_active: int,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ):
        self.http = http
        self.store = store
        self.max_active = max_active
        self.clock = clock
        self.sleep = sleep
        self.tokens: dict[str, tuple[str, int, float]] = {}
        self.active = 0

    def _expire_tokens(self) -> None:
        now = self.clock()
        self.tokens = {k: v for k, v in self.tokens.items() if v[2] > now}

    def has_capacity(self) -> bool:
        self._expire_tokens()
        return self.active + len(self.tokens) < self.max_active

    def issue(self, blog: str) -> str:
        '''The token remembers the newest post right now, so the zip holds
        exactly what had been found when the download was requested.'''
        if not self.has_capacity():
            raise DownloadsBusy
        token = secrets.token_urlsafe(32)
        self.tokens[token] = (blog, self.store.latest_seq(blog), self.clock() + TOKEN_TTL_SECONDS)
        return token

    def redeem(self, token: str) -> tuple[str, int] | None:
        self._expire_tokens()
        entry = self.tokens.pop(token, None)
        if entry is None:
            return None
        self.active += 1
        return entry[0], entry[1]

    async def _download_once(self, url: str):
        '''Returns (file, extension). The file is complete before it is added
        to the zip, so a failure never leaves half a file inside it.'''
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
        try:
            async with self.http.stream('GET', url, headers={'Accept': ACCEPT}) as response:
                if response.status_code != 200:
                    raise FetchFailed(f'HTTP {response.status_code}')
                content_type = response.headers.get('content-type', '').split(';')[0].strip()
                extension = EXTENSIONS.get(content_type.lower())
                if extension is None:
                    raise ValueError(f'unexpected content type {content_type or "none"}')
                size = 0
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError('file is too large')
                    spool.write(chunk)
            spool.seek(0)
            return spool, extension
        except BaseException:
            spool.close()
            raise

    async def _fetch(self, url: str):
        if not _is_tumblr_media(url):
            raise FetchFailed('not a Tumblr media address')
        reason = 'unknown'
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                await self.sleep(2**attempt)
            try:
                return await self._download_once(url)
            except ValueError as error:
                raise FetchFailed(str(error)) from error
            except FetchFailed as error:
                reason = str(error)
                if reason not in ('HTTP 429', 'HTTP 408') and not reason.startswith('HTTP 5'):
                    raise
            except httpx.TransportError:
                reason = 'network error'
        raise FetchFailed(reason)

    def _snapshot(self, blog: str, up_to: int) -> Iterator[Post]:
        after = 0
        while batch := self.store.read_posts(blog, after, READ_BATCH, up_to):
            for seq, raw in batch:
                after = seq
                yield to_post(raw)

    async def stream(self, blog: str, up_to: int) -> AsyncIterator[bytes]:
        '''The zip holds images/, videos/, posts.csv, and failed_downloads.csv
        if anything could not be fetched.'''
        sink = _Sink()
        archive = zipfile.ZipFile(sink, 'w', zipfile.ZIP_STORED)
        try:
            rows = [CSV_HEADER]
            failures = [['Link', 'File Address', 'Reason']]

            for post in self._snapshot(blog, up_to):
                media = [('images', image.full_url) for image in post.images]
                media += [('videos', url) for url in post.video_urls]
                files: list[str] = []
                for number, (folder, url) in enumerate(media, start=1):
                    try:
                        spool, extension = await self._fetch(url)
                    except FetchFailed as error:
                        failures.append([post.url, url, str(error)])
                        continue
                    name = f'{folder}/{_safe_id(post)}_{number:02}.{extension}'
                    with spool, archive.open(zipfile.ZipInfo(name, _zip_time(post)), 'w') as entry:
                        while chunk := spool.read(CHUNK_SIZE):
                            entry.write(chunk)
                            yield sink.drain()
                    files.append(name)
                if _belongs_in_csv(post):
                    rows.append(_csv_row(post, files))

            archive.writestr('posts.csv', _encode_csv(rows), zipfile.ZIP_DEFLATED)
            if len(failures) > 1:
                logger.warning('%d files failed in download of %s', len(failures) - 1, blog)
                archive.writestr('failed_downloads.csv', _encode_csv(failures), zipfile.ZIP_DEFLATED)
            archive.close()
            yield sink.drain()
        finally:
            self.active -= 1
            # Also covers a cancelled download, which never reaches the close above
            archive.close()
