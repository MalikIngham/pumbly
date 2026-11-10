import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.limits import ApiQuota, Sleep
from app.schemas import Post, PostImage

logger = logging.getLogger(__name__)

# Tumblr rejects anything above 20 on the posts endpoint
PAGE_SIZE = 20
THUMBNAIL_WIDTH = 640
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 300

BLOG_NAME = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$')


class TumblrError(Exception):
    '''Tumblr could not serve a request.'''


class BlogNotFound(TumblrError):
    pass


class RateLimited(TumblrError):
    pass


class InvalidCredentials(TumblrError):
    pass


class ReconnectRequired(TumblrError):
    pass


def normalize_blog_name(value: str) -> str:
    '''Reduces what a person might paste to a blog name: "Foo", "@foo",
    "foo.tumblr.com" and "https://tumblr.com/foo" all become "foo".
    Raises ValueError if no valid name is left.'''
    text = value.strip().lower().lstrip('@')
    if '/' in text or '.' in text:
        parts = urlsplit(text if '://' in text else f'//{text}')
        host = parts.hostname or ''
        segments = [s for s in parts.path.split('/') if s]
        if host in ('tumblr.com', 'www.tumblr.com') and segments:
            # tumblr.com/foo and the older tumblr.com/blog/foo
            text = segments[1] if segments[0] == 'blog' and len(segments) > 1 else segments[0]
        elif host.endswith('.tumblr.com'):
            text = host.removesuffix('.tumblr.com')
        else:
            raise ValueError('Enter a Tumblr blog name or tumblr.com address.')
    if not BLOG_NAME.match(text):
        raise ValueError('Enter a Tumblr blog name or tumblr.com address.')
    return text


def is_original(post: dict[str, Any]) -> bool:
    '''A post is original unless Tumblr reports reblog data for it. Needs
    reblog_info=true on the request. The trail is not used because originals
    can have an empty one, and a blog's reblog of itself has its own name at
    the root of the trail.'''
    return not (
        post.get('parent_post_url')
        or post.get('reblogged_from_id')
        or post.get('reblogged_root_id')
    )


def _safe_url(url: Any) -> str | None:
    '''Tumblr can return http media links, which browsers may block.'''
    if not isinstance(url, str):
        return None
    if url.startswith('http://'):
        return 'https://' + url.removeprefix('http://')
    return url if url.startswith('https://') else None


def _usable_media(media: Any) -> list[dict[str, Any]]:
    if isinstance(media, dict):
        media = [media]
    if not isinstance(media, list):
        return []
    return [item for item in media if isinstance(item, dict) and _safe_url(item.get('url'))]


def _width(item: dict[str, Any]) -> int:
    return item.get('width') or 0


def _largest(media: list[dict[str, Any]]) -> dict[str, Any]:
    for item in media:
        if item.get('has_original_dimensions'):
            return item
    return max(media, key=_width)


def _thumbnail(media: list[dict[str, Any]]) -> dict[str, Any]:
    fitting = [item for item in media if _width(item) <= THUMBNAIL_WIDTH]
    return max(fitting, key=_width) if fitting else min(media, key=_width)


def _to_image(block: dict[str, Any], from_ask: bool) -> PostImage | None:
    media = _usable_media(block.get('media'))
    if not media:
        return None
    full = _largest(media)
    known_size = not full.get('original_dimensions_missing')
    return PostImage(
        full_url=_safe_url(full['url']),
        thumb_url=_safe_url(_thumbnail(media)['url']),
        width=full.get('width') if known_size else None,
        height=full.get('height') if known_size else None,
        alt=block.get('alt_text') or '',
        from_ask=from_ask,
    )


def _display_order(
    content: list[dict[str, Any]], layouts: list[dict[str, Any]]
) -> tuple[list[int], set[int]]:
    '''Block indexes in display order: the ask, then the rows layout, then
    anything the layout left out. Also returns the indexes inside the ask.'''
    ask = [i for layout in layouts if layout.get('type') == 'ask' for i in layout.get('blocks') or []]
    rows = [
        i
        for layout in layouts
        if layout.get('type') == 'rows'
        for row in layout.get('display') or []
        for i in row.get('blocks') or []
    ]
    order = dict.fromkeys([*ask, *rows, *range(len(content))])
    return [i for i in order if isinstance(i, int) and 0 <= i < len(content)], set(ask)


def _post_type(blocks: list[dict[str, Any]], is_ask: bool) -> str:
    '''Tumblr's documented mapping from NPF content to a single post type.'''
    types = {block.get('type') for block in blocks}
    subtypes = [block.get('subtype') for block in blocks if block.get('type') == 'text']
    if is_ask:
        return 'ask'
    if 'video' in types:
        return 'video'
    if 'image' in types:
        return 'photo'
    if 'audio' in types:
        return 'audio'
    if 'quote' in subtypes:
        return 'quote'
    if subtypes.count('chat') > 1:
        return 'chat'
    if 'link' in types:
        return 'link'
    return 'text'


def _join_text(blocks: list[dict[str, Any]]) -> str:
    return '\n'.join(b['text'] for b in blocks if b.get('type') == 'text' and b.get('text'))


def to_post(post: dict[str, Any]) -> Post:
    '''Keeps only what the frontend displays. Text comes from plain text
    blocks, so no Tumblr supplied HTML ever reaches a browser.'''
    content = post.get('content') or []
    layouts = post.get('layout') or []
    order, ask_indexes = _display_order(content, layouts)
    ask_layout = next((layout for layout in layouts if layout.get('type') == 'ask'), None)
    is_ask = ask_layout is not None

    blocks = [content[i] for i in order]
    ask_blocks = [content[i] for i in order if i in ask_indexes]
    other_blocks = [content[i] for i in order if i not in ask_indexes]

    images = [
        image
        for i in order
        if content[i].get('type') == 'image'
        and (image := _to_image(content[i], i in ask_indexes)) is not None
    ]
    poster = next(
        (
            usable
            for block in blocks
            if block.get('type') == 'video' and (usable := _usable_media(block.get('poster')))
        ),
        None,
    )
    attribution = (ask_layout or {}).get('attribution') or {}

    # Native videos have a file. Embeds such as YouTube only have a link.
    video_urls: list[str] = []
    external_links: list[str] = []
    for block in blocks:
        if block.get('type') != 'video':
            continue
        media = _usable_media(block.get('media'))
        if media:
            video_urls.append(_safe_url(_largest(media)['url']))
        elif url := _safe_url(block.get('url')):
            external_links.append(url)

    return Post(
        id=post.get('id_string') or str(post['id']),
        url=post.get('post_url', ''),
        type=_post_type(blocks, is_ask),
        timestamp=post.get('timestamp'),
        note_count=post.get('note_count'),
        summary=_join_text(other_blocks) or post.get('summary') or '',
        asker=(attribution.get('blog') or {}).get('name'),
        question=_join_text(ask_blocks) if is_ask else None,
        images=images,
        poster_url=_safe_url(_thumbnail(poster)['url']) if poster else None,
        video_urls=video_urls,
        external_links=external_links,
        is_nsfw=bool(post.get('is_nsfw')),
    )


class TumblrClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        quota: ApiQuota,
        sleep: Sleep = asyncio.sleep,
        auth: Any = None,
    ):
        self.http = http
        self.api_key = api_key
        self.quota = quota
        self.sleep = sleep
        self.auth = auth

    async def _get_page(self, blog: str, before: int | None) -> dict[str, Any]:
        params = {
            'api_key': self.api_key,
            'limit': PAGE_SIZE,
            'reblog_info': 'true',
            'notes_info': 'false',
            'npf': 'true',
        }
        if before is not None:
            params['before'] = before
        path = f'/blog/{blog}.tumblr.com/posts'
        failure: TumblrError = TumblrError('Tumblr is not responding.')
        rejected: str | None = None

        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                await self.sleep(delay)
            delay = min(5 * 2**attempt, MAX_BACKOFF_SECONDS)

            token = await self.auth.access_token(rejected) if self.auth else None
            headers = {'Authorization': f'Bearer {token}'} if token else None

            await self.quota.acquire()
            try:
                response = await self.http.get(path, params=params, headers=headers)
            except httpx.TransportError as error:
                logger.warning(
                    'Page of %s failed with %s (attempt %d of %d)',
                    blog, type(error).__name__, attempt + 1, MAX_ATTEMPTS,
                )
                failure = TumblrError('Tumblr is not responding.')
                continue

            if response.status_code != 200:
                logger.warning(
                    'Page of %s returned HTTP %d (attempt %d of %d, logged in: %s)',
                    blog, response.status_code, attempt + 1, MAX_ATTEMPTS, bool(token),
                )
            if response.status_code == 200:
                try:
                    return response.json()['response']
                except (ValueError, KeyError):
                    failure = TumblrError('Tumblr sent an unreadable response.')
                    continue
            if response.status_code == 404:
                if token:
                    raise BlogNotFound(f'No blog named "{blog}" was found, or your Tumblr account can\'t see it.')
                raise BlogNotFound(
                    f'No public blog named "{blog}" was found. '
                    'If it is only visible when logged in, connect your Tumblr account.'
                )
            if response.status_code == 401:
                if not token:
                    raise InvalidCredentials('Tumblr rejected the consumer key.')
                if token == rejected:
                    raise ReconnectRequired('Tumblr login expired. Connect your account again.')
                rejected = token
                delay = 0
                continue
            if response.status_code == 429:
                failure = RateLimited('Tumblr is rate limiting us. Try again later.')
                retry_after = response.headers.get('Retry-After', '')
                if retry_after.isdigit():
                    delay = min(int(retry_after), MAX_BACKOFF_SECONDS)
                continue
            if response.status_code >= 500:
                failure = TumblrError('Tumblr is having problems. Try again later.')
                continue
            raise TumblrError(f'Tumblr refused the request ({response.status_code}).')

        raise failure

    async def fetch_page(
        self, blog: str, before: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        '''Returns (posts, total_posts), newest first, published before the
        given time. Timestamps stay valid when new posts arrive, unlike offsets.'''
        page = await self._get_page(blog, before)
        posts = page.get('posts') or []
        total = page.get('total_posts') or (page.get('blog') or {}).get('posts') or 0
        # Tumblr marks mature blogs on the page's blog object, not each post
        if (page.get('blog') or {}).get('is_nsfw'):
            for post in posts:
                post['is_nsfw'] = True
        return posts, total
