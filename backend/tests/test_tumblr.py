import asyncio

import httpx
import pytest

from app.limits import ApiQuota, ClientLimiter, QuotaExhausted
from app.tumblr import (
    BlogNotFound,
    RateLimited,
    TumblrClient,
    TumblrError,
    is_original,
    normalize_blog_name,
    to_post,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def make_client(handler, per_hour=1000, per_day=5000):
    clock = FakeClock()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    http = httpx.AsyncClient(base_url='https://tumblr.test', transport=httpx.MockTransport(handler))
    return TumblrClient(http, 'key', ApiQuota(per_hour, per_day, clock, clock.sleep), sleep), sleeps


async def collect(client, blog='foo'):
    return [await client.fetch_page(blog)]


@pytest.mark.parametrize('value', [
    'foo', 'Foo', ' @foo ', 'foo.tumblr.com', 'https://foo.tumblr.com/post/123',
    'https://www.tumblr.com/foo', 'tumblr.com/foo/123', 'https://www.tumblr.com/blog/foo',
])
def test_normalize_accepts_common_forms(value):
    assert normalize_blog_name(value) == 'foo'


@pytest.mark.parametrize('value', [
    '', '   ', 'example.com', 'https://evil.test/foo', 'foo bar', '../foo', '-foo', 'tumblr.com', 'a' * 33,
])
def test_normalize_rejects_invalid(value):
    with pytest.raises(ValueError):
        normalize_blog_name(value)


@pytest.mark.parametrize('post,expected', [
    ({'trail': [{'blog': {'name': 'foo'}}]}, True),
    ({'trail': []}, True),
    ({}, True),
    ({'trail': [{'blog': {'name': 'other'}}], 'parent_post_url': 'https://x'}, False),
    ({'trail': [{'blog': {'name': 'foo'}}], 'reblogged_from_id': '1'}, False),
    ({'reblogged_root_id': '1'}, False),
])
def test_is_original(post, expected):
    assert is_original(post) is expected


def image_block(name, alt=None):
    block = {'type': 'image', 'media': [
        {'url': f'https://m.test/{name}_2048.jpg', 'width': 2048, 'height': 1536},
        {'url': f'https://m.test/{name}_640.jpg', 'width': 640, 'height': 480},
        {'url': f'https://m.test/{name}_250.jpg', 'width': 250, 'height': 188},
    ]}
    if alt:
        block['alt_text'] = alt
    return block


def urls(post):
    return [image.full_url for image in post.images]


def test_images_in_a_text_post_are_all_collected():
    post = to_post({'id': 1, 'content': [
        {'type': 'text', 'text': 'first'},
        image_block('a', 'described'),
        {'type': 'text', 'text': 'second'},
        image_block('b'),
    ]})
    assert post.type == 'photo'
    assert post.summary == 'first\nsecond'
    assert urls(post) == ['https://m.test/a_2048.jpg', 'https://m.test/b_2048.jpg']
    assert post.images[0].thumb_url == 'https://m.test/a_640.jpg'
    assert (post.images[0].width, post.images[0].height) == (2048, 1536)
    assert post.images[0].alt == 'described'


def test_ask_images_come_first_and_text_is_separated():
    post = to_post({
        'id': 1,
        'content': [
            {'type': 'text', 'text': 'the answer'},
            image_block('answer'),
            {'type': 'text', 'text': 'the question'},
            image_block('question'),
        ],
        'layout': [{'type': 'ask', 'blocks': [2, 3], 'attribution': {'type': 'blog', 'blog': {'name': 'someone'}}}],
    })
    assert post.type == 'ask'
    assert post.asker == 'someone'
    assert post.question == 'the question'
    assert post.summary == 'the answer'
    assert urls(post) == ['https://m.test/question_2048.jpg', 'https://m.test/answer_2048.jpg']
    assert [image.from_ask for image in post.images] == [True, False]


def test_anonymous_ask_has_no_asker():
    post = to_post({'id': 1, 'content': [{'type': 'text', 'text': 'q'}], 'layout': [{'type': 'ask', 'blocks': [0]}]})
    assert post.type == 'ask'
    assert post.asker is None


def test_non_ask_has_no_question():
    post = to_post({'id': 1, 'content': [{'type': 'text', 'text': 'hi'}]})
    assert post.question is None
    assert post.asker is None
    assert post.type == 'text'


def test_rows_layout_sets_order_and_bad_indexes_are_ignored():
    post = to_post({
        'id': 1,
        'content': [image_block('a'), image_block('b'), image_block('c')],
        'layout': [{'type': 'rows', 'display': [{'blocks': [2, 99]}, {'blocks': [0]}]}],
    })
    assert urls(post) == ['https://m.test/c_2048.jpg', 'https://m.test/a_2048.jpg', 'https://m.test/b_2048.jpg']


def test_flagged_original_wins_over_widest():
    block = {'type': 'image', 'media': [
        {'url': 'https://m.test/wide.jpg', 'width': 2048, 'height': 100},
        {'url': 'https://m.test/orig.jpg', 'width': 1000, 'height': 100, 'has_original_dimensions': True},
    ]}
    assert urls(to_post({'id': 1, 'content': [block]})) == ['https://m.test/orig.jpg']


def test_small_only_media_and_unknown_dimensions():
    block = {'type': 'image', 'media': [
        {'url': 'https://m.test/huge.jpg', 'width': 900, 'height': 900, 'original_dimensions_missing': True},
    ]}
    image = to_post({'id': 1, 'content': [block]}).images[0]
    assert image.thumb_url == 'https://m.test/huge.jpg'
    assert image.width is None
    assert image.height is None


def test_gif_keeps_the_animated_file_not_the_poster():
    block = {
        'type': 'image',
        'media': [{'url': 'https://m.test/a.gif', 'type': 'image/gif', 'width': 400, 'height': 300}],
        'poster': {'url': 'https://m.test/still.jpg', 'type': 'image/jpeg', 'width': 400, 'height': 300},
    }
    assert urls(to_post({'id': 1, 'content': [block]})) == ['https://m.test/a.gif']


def test_video_poster_is_for_the_card_only():
    post = to_post({'id': 1, 'content': [{
        'type': 'video',
        'poster': [{'url': 'http://m.test/p.jpg', 'width': 500, 'height': 300}],
    }]})
    assert post.type == 'video'
    assert post.images == []
    assert post.poster_url == 'https://m.test/p.jpg'


def test_native_videos_have_files_and_embeds_have_links():
    post = to_post({'id': 1, 'content': [
        {'type': 'video', 'media': {'url': 'http://va.media.tumblr.com/v.mp4', 'width': 480, 'height': 640}},
        {'type': 'video', 'provider': 'youtube', 'url': 'https://youtube.test/watch'},
    ]})
    assert post.video_urls == ['https://va.media.tumblr.com/v.mp4']
    assert post.external_links == ['https://youtube.test/watch']


def test_urls_are_upgraded_and_unsafe_ones_dropped():
    good = {'type': 'image', 'media': [{'url': 'http://m.test/a.jpg', 'width': 10, 'height': 10}]}
    bad = {'type': 'image', 'media': [{'url': 'javascript:alert(1)', 'width': 10, 'height': 10}]}
    assert urls(to_post({'id': 1, 'content': [good, bad]})) == ['https://m.test/a.jpg']


def test_post_without_content_falls_back_to_summary():
    post = to_post({'id': 7, 'summary': 'plain'})
    assert post.id == '7'
    assert post.summary == 'plain'
    assert post.images == []


def test_fetch_page_requests_the_new_format_and_passes_the_cursor():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'response': {'posts': [{'id': 1}], 'total_posts': 7}})

    client = make_client(handler)[0]

    async def run():
        return await client.fetch_page('foo'), await client.fetch_page('foo', before=123)

    (first, total), _ = asyncio.run(run())
    assert total == 7
    assert first == [{'id': 1}]
    assert seen[0].url.params['npf'] == 'true'
    assert seen[0].url.params['reblog_info'] == 'true'
    assert seen[0].url.params['limit'] == '20'
    assert 'before' not in seen[0].url.params
    assert 'offset' not in seen[0].url.params
    assert seen[1].url.params['before'] == '123'


def test_retries_429_using_retry_after():
    responses = [
        httpx.Response(429, headers={'Retry-After': '7'}, json={}),
        httpx.Response(200, json={'response': {'posts': [], 'total_posts': 0}}),
    ]
    client, sleeps = make_client(lambda request: responses.pop(0))
    asyncio.run(collect(client))
    assert sleeps == [7]


def test_gives_up_after_repeated_429():
    client, sleeps = make_client(lambda request: httpx.Response(429, json={}))
    with pytest.raises(RateLimited):
        asyncio.run(collect(client))
    assert len(sleeps) == 4


def test_retries_network_errors():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError('down', request=request)
        return httpx.Response(200, json={'response': {'posts': [], 'total_posts': 0}})

    asyncio.run(collect(make_client(handler)[0]))
    assert len(attempts) == 2


def test_missing_blog_is_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(404, json={'meta': {'status': 404}, 'response': []})

    with pytest.raises(BlogNotFound):
        asyncio.run(collect(make_client(handler)[0]))
    assert len(attempts) == 1


def test_error_messages_do_not_leak_the_key():
    client, _ = make_client(lambda request: httpx.Response(403, json={}))
    with pytest.raises(TumblrError) as error:
        asyncio.run(collect(client))
    assert 'key' not in str(error.value)


def test_quota_waits_for_hourly_and_fails_on_daily():
    clock = FakeClock()
    quota = ApiQuota(per_hour=1, per_day=2, clock=clock, sleep=clock.sleep)

    async def run():
        await quota.acquire()
        before = clock.now
        await quota.acquire()
        assert clock.now - before == pytest.approx(3600)
        with pytest.raises(QuotaExhausted):
            await quota.acquire()

    asyncio.run(run())


def test_client_limiter_is_per_client_and_recovers():
    clock = FakeClock()
    limiter = ClientLimiter(1, 60, clock)
    assert limiter.hit('a') == 0
    assert limiter.hit('b') == 0
    assert limiter.hit('a') == pytest.approx(60)
    clock.now += 61
    assert limiter.hit('a') == 0


def test_post_level_nsfw_flag_is_kept():
    assert to_post({'id': 1, 'is_nsfw': True}).is_nsfw is True
    assert to_post({'id': 2}).is_nsfw is False


def test_blog_level_nsfw_flag_marks_every_post():
    def handler(request):
        return httpx.Response(200, json={'response': {
            'posts': [{'id': 1}, {'id': 2}], 'total_posts': 2, 'blog': {'is_nsfw': True},
        }})

    pages = asyncio.run(collect(make_client(handler)[0]))
    assert [to_post(post).is_nsfw for post in pages[0][0]] == [True, True]


def test_safe_blog_does_not_mark_posts():
    def handler(request):
        return httpx.Response(200, json={'response': {
            'posts': [{'id': 1}], 'total_posts': 1, 'blog': {'is_nsfw': False},
        }})

    pages = asyncio.run(collect(make_client(handler)[0]))
    assert to_post(pages[0][0][0]).is_nsfw is False
