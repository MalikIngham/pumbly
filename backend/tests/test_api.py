import asyncio
import time

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

NOW = 1_700_000_000


class FakeTumblr:
    '''Serves posts newest first and honours `before`, like the real API.'''

    def __init__(self, count=0, start=NOW):
        self.posts = []
        self.calls = []
        self.block_after = None
        for _ in range(count):
            self.publish()
        self.start = start

    def publish(self, timestamp=None, **fields):
        number = len(self.posts)
        post = {
            'id': number,
            'id_string': str(number),
            'post_url': f'https://foo.tumblr.com/post/{number}',
            'timestamp': NOW + number * 10 if timestamp is None else timestamp,
            'content': [{'type': 'text', 'text': f'post {number}'}],
            **fields,
        }
        if number % 3 == 0 and 'reblogged_from_id' not in fields and 'original' not in fields:
            post['reblogged_from_id'] = '99'
        self.posts.append(post)
        return post

    async def __call__(self, request):
        self.calls.append(request.url.params.get('before'))
        if self.block_after is not None and len(self.calls) > self.block_after:
            await asyncio.sleep(60)
        before = request.url.params.get('before')
        ordered = sorted(
            self.posts, key=lambda p: (p.get('is_pinned', False), p['timestamp'], p['id']), reverse=True
        )
        page = [p for p in ordered if before is None or p['timestamp'] < int(before)][:20]
        return httpx.Response(200, json={'response': {'posts': page, 'total_posts': len(self.posts)}})


def make_client(handler, tmp_path=None, **overrides):
    if tmp_path is not None:
        overrides['database_path'] = str(tmp_path / 'shared.db')
    settings = Settings(_env_file=None, tumblr_consumer_key='k', **overrides)
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


client_for = make_client
NO_COOLDOWN = {'rescan_cooldown_seconds': 0}


def scan(client, blog='foo'):
    response = client.post('/api/scans', json={'blog': blog})
    assert response.status_code == 202, response.text
    return response.json()


def wait_for(client, predicate, blog='foo'):
    for _ in range(500):
        body = client.get(f'/api/scans/{blog}').json()
        if predicate(body):
            return body
        time.sleep(0.01)
    raise AssertionError(f'never happened: {body}')


def wait_until_finished(client, blog='foo'):
    return wait_for(client, lambda b: b['status'] not in ('queued', 'running'), blog)


def all_posts(client, blog='foo'):
    posts, after = [], 0
    while True:
        page = client.get(f'/api/scans/{blog}/posts', params={'after': after, 'limit': 100}).json()
        if not page['posts']:
            return posts
        posts += page['posts']
        after = page['next_after']


def ids(client, blog='foo'):
    return {post['id'] for post in all_posts(client, blog)}


def originals(tumblr):
    return {p['id_string'] for p in tumblr.posts if not p.get('reblogged_from_id')}


def test_first_scan_finds_every_original_across_pages():
    tumblr = FakeTumblr(95)
    with make_client(tumblr) as client:
        created = client.post('/api/scans', json={'blog': 'https://Foo.tumblr.com'})
        assert created.status_code == 202
        assert created.json()['blog'] == 'foo'
        body = wait_until_finished(client)
        assert body['status'] == 'complete'
        assert body['scanned'] == 95
        assert body['total'] == 95
        assert body['originals'] == 63
        assert body['new_originals'] == 0
        assert ids(client) == originals(tumblr)


def test_pages_use_timestamps_with_a_one_post_overlap():
    tumblr = FakeTumblr(45)
    with make_client(tumblr) as client:
        scan(client)
        wait_until_finished(client)
    newest_last = sorted((p['timestamp'] for p in tumblr.posts), reverse=True)
    assert tumblr.calls[0] is None
    assert int(tumblr.calls[1]) == newest_last[19] + 1
    assert all(int(call) > 0 for call in tumblr.calls[1:])


def test_rescan_with_nothing_new_costs_one_call():
    tumblr = FakeTumblr(95)
    with make_client(tumblr, **NO_COOLDOWN) as client:
        scan(client)
        wait_until_finished(client)
        before = len(tumblr.calls)
        scan(client)
        body = wait_until_finished(client)
        assert len(tumblr.calls) == before + 1
        assert body['status'] == 'complete'
        assert body['originals'] == 63
        assert body['new_originals'] == 0


def test_rescan_only_fetches_new_posts_and_adds_them():
    tumblr = FakeTumblr(95)
    with make_client(tumblr, **NO_COOLDOWN) as client:
        scan(client)
        wait_until_finished(client)
        before = len(tumblr.calls)
        for _ in range(5):
            tumblr.publish(original=True)
        scan(client)
        body = wait_until_finished(client)
        assert len(tumblr.calls) == before + 1
        assert body['new_originals'] == 5
        assert body['originals'] == 68
        assert ids(client) == originals(tumblr)


def test_large_batch_of_new_posts_is_fully_collected():
    tumblr = FakeTumblr(30)
    with make_client(tumblr, **NO_COOLDOWN) as client:
        scan(client)
        wait_until_finished(client)
        before = len(tumblr.calls)
        for _ in range(50):
            tumblr.publish(original=True)
        scan(client)
        body = wait_until_finished(client)
        assert body['new_originals'] == 50
        assert ids(client) == originals(tumblr)
        assert len(tumblr.calls) - before <= 4


def test_recent_rescan_makes_no_calls_during_cooldown():
    tumblr = FakeTumblr(25)
    with make_client(tumblr, rescan_cooldown_seconds=300) as client:
        scan(client)
        wait_until_finished(client)
        before = len(tumblr.calls)
        assert scan(client, 'FOO')['status'] == 'complete'
        assert len(tumblr.calls) == before


def test_results_survive_a_restart_without_calling_tumblr(tmp_path):
    tumblr = FakeTumblr(45)
    with make_client(tumblr, tmp_path) as client:
        scan(client)
        wait_until_finished(client)
    before = len(tumblr.calls)
    with make_client(tumblr, tmp_path) as client:
        assert client.get('/api/scans/foo').json()['status'] == 'complete'
        assert ids(client) == originals(tumblr)
        assert len(tumblr.calls) == before


def test_stop_keeps_position_and_resume_does_not_refetch():
    tumblr = FakeTumblr(95)
    tumblr.block_after = 2
    with make_client(tumblr) as client:
        scan(client)
        wait_for(client, lambda b: b['scanned'] >= 39)
        stopped = client.post('/api/scans/foo/stop').json()
        assert stopped['status'] == 'paused'
        saved = stopped['originals']
        assert saved > 0
        calls_made = list(tumblr.calls)

        tumblr.block_after = None
        scan(client)
        body = wait_until_finished(client)
        assert body['status'] == 'complete'
        assert body['new_originals'] == 0
        assert ids(client) == originals(tumblr)
        resumed_from = tumblr.calls[len(calls_made)]
        assert resumed_from is not None
        assert None not in tumblr.calls[len(calls_made):-1]


def test_interrupted_scan_resumes_after_a_restart(tmp_path):
    tumblr = FakeTumblr(95)
    tumblr.block_after = 2
    with make_client(tumblr, tmp_path) as client:
        scan(client)
        wait_for(client, lambda b: b['scanned'] >= 39)
    tumblr.block_after = None
    made = len(tumblr.calls)
    with make_client(tumblr, tmp_path) as client:
        assert client.get('/api/scans/foo').json()['status'] == 'paused'
        assert len(tumblr.calls) == made
        scan(client)
        assert wait_until_finished(client)['status'] == 'complete'
        assert ids(client) == originals(tumblr)


def test_posts_published_while_paused_are_collected_on_resume():
    tumblr = FakeTumblr(95)
    tumblr.block_after = 2
    with make_client(tumblr) as client:
        scan(client)
        wait_for(client, lambda b: b['scanned'] >= 39)
        client.post('/api/scans/foo/stop')
        tumblr.block_after = None
        for _ in range(3):
            tumblr.publish(original=True)
        scan(client)
        body = wait_until_finished(client)
        assert body['status'] == 'complete'
        assert ids(client) == originals(tumblr)


def test_unwatched_scan_pauses_and_can_resume():
    tumblr = FakeTumblr(95)
    with make_client(tumblr, scan_idle_timeout_seconds=-1) as client:
        started = scan(client)
        assert started['status'] in ('queued', 'paused')
        time.sleep(0.2)
        body = client.get('/api/scans/foo').json()
        assert body['status'] == 'paused'
        assert len(tumblr.calls) == 0


def test_failure_midway_keeps_results_and_position():
    tumblr = FakeTumblr(95)
    with make_client(tumblr, tumblr_calls_per_day=2) as client:
        scan(client)
        body = wait_until_finished(client)
        assert body['status'] == 'failed'
        assert 'limit' in body['error']
        assert body['scanned'] >= 39
        assert body['originals'] > 20
    assert len(tumblr.calls) == 2


def test_failed_scan_retries_from_its_position(tmp_path):
    tumblr = FakeTumblr(95)
    with make_client(tumblr, tmp_path, tumblr_calls_per_day=2) as client:
        scan(client)
        wait_until_finished(client)
    made = len(tumblr.calls)
    with make_client(tumblr, tmp_path) as client:
        scan(client)
        assert wait_until_finished(client)['status'] == 'complete'
        assert tumblr.calls[made] is not None
        assert ids(client) == originals(tumblr)


def test_posts_sharing_a_second_across_a_page_boundary_are_not_lost():
    tumblr = FakeTumblr()
    for number in range(30):
        tumblr.publish(timestamp=NOW + (0 if 18 <= number <= 22 else number * 10), original=True)
    with make_client(tumblr) as client:
        scan(client)
        assert wait_until_finished(client)['status'] == 'complete'
        assert ids(client) == originals(tumblr)


def test_full_page_in_one_second_does_not_hang():
    tumblr = FakeTumblr()
    for _ in range(25):
        tumblr.publish(timestamp=NOW, original=True)
    for number in range(5):
        tumblr.publish(timestamp=NOW - 100 - number, original=True)
    with make_client(tumblr) as client:
        scan(client)
        assert wait_until_finished(client)['status'] == 'complete'
    assert len(tumblr.calls) < 10


def test_old_pinned_post_does_not_end_a_rescan_early():
    tumblr = FakeTumblr(30)
    with make_client(tumblr, **NO_COOLDOWN) as client:
        scan(client)
        wait_until_finished(client)
        tumblr.posts[1]['is_pinned'] = True
        for _ in range(45):
            tumblr.publish(original=True)
        scan(client)
        wait_until_finished(client)
        assert ids(client) == originals(tumblr)


def test_blog_with_no_posts_completes():
    tumblr = FakeTumblr()
    with make_client(tumblr) as client:
        scan(client)
        body = wait_until_finished(client)
        assert body['status'] == 'complete'
        assert body['originals'] == 0


def test_zero_originals_is_not_an_error():
    tumblr = FakeTumblr()
    tumblr.publish(reblogged_from_id='2')
    with make_client(tumblr) as client:
        scan(client)
        body = wait_until_finished(client)
        assert body['status'] == 'complete'
        assert body['originals'] == 0
        assert body['scanned'] == 1


def test_missing_blog_fails_cleanly_and_can_be_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, json={'meta': {'status': 404}, 'response': []})

    with make_client(handler) as client:
        scan(client, 'nope')
        body = wait_until_finished(client, 'nope')
        assert body['status'] == 'failed'
        assert 'nope' in body['error']
        scan(client, 'nope')
        wait_until_finished(client, 'nope')
        assert len(calls) == 2


def test_bad_consumer_key_is_not_shown_to_visitors():
    with make_client(lambda request: httpx.Response(401, json={})) as client:
        scan(client)
        body = wait_until_finished(client)
        assert body['status'] == 'failed'
        assert 'consumer' not in body['error']


def test_invalid_blog_name_is_rejected():
    with make_client(FakeTumblr()) as client:
        response = client.post('/api/scans', json={'blog': 'https://evil.test/x'})
        assert response.status_code == 422
        assert 'Tumblr' in response.json()['detail']


def test_unknown_blog_is_404():
    with make_client(FakeTumblr()) as client:
        assert client.get('/api/scans/missing').status_code == 404
        assert client.get('/api/scans/missing/posts').status_code == 404
        assert client.post('/api/scans/missing/stop').status_code == 404


def test_client_limit_applies_to_new_scans_but_not_cooldown_reuse():
    with make_client(FakeTumblr(1), new_scans_per_client_per_hour=1) as client:
        scan(client)
        wait_until_finished(client)
        scan(client)
        limited = client.post('/api/scans', json={'blog': 'bar'})
        assert limited.status_code == 429
        assert int(limited.headers['Retry-After']) > 0


def test_full_queue_is_refused():
    async def handler(request):
        await asyncio.sleep(60)

    with make_client(handler, max_pending_scans=1) as client:
        assert client.post('/api/scans', json={'blog': 'foo'}).status_code == 202
        assert client.post('/api/scans', json={'blog': 'bar'}).status_code == 503


def test_cors_allows_only_configured_origins():
    with make_client(FakeTumblr(), allowed_origins='https://pumbly.test') as client:
        allowed = client.get('/api/health', headers={'Origin': 'https://pumbly.test'})
        assert allowed.headers['access-control-allow-origin'] == 'https://pumbly.test'
        denied = client.get('/api/health', headers={'Origin': 'https://evil.test'})
        assert 'access-control-allow-origin' not in denied.headers
