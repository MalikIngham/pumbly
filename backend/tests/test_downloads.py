import asyncio
import csv
import io
import zipfile

import httpx

from app.config import Settings
from app.downloads import DownloadManager
from app.main import create_app
from app.store import BlogState, Store
from fastapi.testclient import TestClient

IMG = 'https://64.media.tumblr.com'


def image_block(name):
    return {'type': 'image', 'media': [
        {'url': f'{IMG}/{name}_2048.jpg', 'width': 2048, 'height': 1536},
        {'url': f'{IMG}/{name}_640.jpg', 'width': 640, 'height': 480},
    ]}


def text(value):
    return {'type': 'text', 'text': value}


POSTS = [
    {
        'id': 1,
        'post_url': 'https://foo.tumblr.com/post/1',
        'timestamp': 1600000000,
        'content': [text('Why?'), image_block('q'), text('Because.'), image_block('a')],
        'layout': [{'type': 'ask', 'blocks': [0, 1], 'attribution': {'blog': {'name': 'curious'}}}],
    },
    {
        'id': 2,
        'post_url': 'https://foo.tumblr.com/post/2',
        'timestamp': 1600000100,
        'content': [text('a plain text post')],
    },
    {
        'id': 3,
        'post_url': 'https://foo.tumblr.com/post/3',
        'content': [image_block('photo_only')],
    },
    {
        'id': 4,
        'post_url': 'https://foo.tumblr.com/post/4',
        'content': [{'type': 'video', 'media': {'url': 'https://va.media.tumblr.com/v.mp4', 'width': 480, 'height': 640}}],
    },
    {
        'id': 5,
        'post_url': 'https://foo.tumblr.com/post/5',
        'content': [{'type': 'video', 'provider': 'youtube', 'url': 'https://youtube.test/watch'}],
    },
    {
        'id': 6,
        'post_url': 'https://foo.tumblr.com/post/6',
        'content': [text('=HYPERLINK("https://evil.test")')],
    },
    {
        'id': 7,
        'post_url': 'https://foo.tumblr.com/post/7',
        'content': [text('reblog'), image_block('reblogged')],
        'reblogged_from_id': '1',
    },
    {
        'id': 8,
        'post_url': 'https://foo.tumblr.com/post/8',
        'content': [text('anonymous question')],
        'layout': [{'type': 'ask', 'blocks': [0]}],
    },
]


def page_of(posts, request):
    before = request.url.params.get('before')
    return [p for p in posts if before is None or p.get('timestamp', 1) < int(before)][:20]


def handler(fetched, media_overrides=None):
    def respond(request):
        url = str(request.url)
        if request.url.host == 'api.tumblr.com':
            return httpx.Response(200, json={'response': {'posts': page_of(POSTS, request), 'total_posts': len(POSTS)}})
        fetched.append(request)
        if media_overrides and url in media_overrides:
            return media_overrides[url]
        content_type = 'video/mp4' if url.endswith('.mp4') else 'image/jpeg'
        return httpx.Response(200, headers={'content-type': content_type}, content=f'bytes of {url}'.encode())
    return respond


def client_for(transport_handler, **overrides):
    settings = Settings(_env_file=None, tumblr_consumer_key='secret-key', **overrides)
    return TestClient(create_app(settings, httpx.MockTransport(transport_handler)))


def finished_scan(client):
    import time

    scan_id = client.post('/api/scans', json={'blog': 'foo'}).json()['id']
    for _ in range(200):
        if client.get(f'/api/scans/{scan_id}').json()['status'] == 'complete':
            return scan_id
        time.sleep(0.01)
    raise AssertionError('scan did not finish')


def download(client, scan_id):
    url = client.post(f'/api/scans/{scan_id}/downloads').json()['url']
    response = client.get(url)
    assert response.status_code == 200
    return response


def read_csv(archive, name='posts.csv'):
    raw = archive.read(name)
    assert raw.startswith(b'\xef\xbb\xbf')
    return list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline='')))


def build_zip(media_overrides=None):
    fetched = []
    with client_for(handler(fetched, media_overrides)) as client:
        response = download(client, finished_scan(client))
    return zipfile.ZipFile(io.BytesIO(response.content)), fetched, response


def test_zip_has_full_size_media_only_from_originals():
    archive, fetched, response = build_zip()
    assert archive.testzip() is None
    assert response.headers['content-type'] == 'application/zip'
    assert 'attachment; filename="pumbly-foo.zip"' in response.headers['content-disposition']
    assert sorted(n for n in archive.namelist() if n != 'posts.csv') == [
        'images/1_01.jpg', 'images/1_02.jpg', 'images/3_01.jpg', 'videos/4_01.mp4',
    ]
    assert archive.read('images/1_01.jpg') == f'bytes of {IMG}/q_2048.jpg'.encode()
    assert archive.read('videos/4_01.mp4') == b'bytes of https://va.media.tumblr.com/v.mp4'
    assert not any('reblogged' in str(r.url) or '_640' in str(r.url) for r in fetched)
    assert not any('secret-key' in str(r.url) for r in fetched)


def test_csv_puts_the_question_first_then_the_answer():
    archive, _, _ = build_zip()
    rows = read_csv(archive)
    assert rows[0][:2] == ['Question', 'Answer or Text']
    ask = rows[1]
    assert ask[:3] == ['Why?', 'Because.', 'curious']
    assert ask[3] == 'ask'
    assert ask[4] == '2020-09-13 12:26:40'
    assert ask[5] == 'https://foo.tumblr.com/post/1'
    assert ask[6] == 'images/1_01.jpg images/1_02.jpg'


def test_csv_handles_text_external_and_anonymous_posts():
    archive, _, _ = build_zip()
    by_link = {row[5]: row for row in read_csv(archive)[1:]}
    assert by_link['https://foo.tumblr.com/post/2'][:2] == ['', 'a plain text post']
    assert by_link['https://foo.tumblr.com/post/5'][7] == 'https://youtube.test/watch'
    assert by_link['https://foo.tumblr.com/post/8'][:3] == ['anonymous question', '', 'Anonymous']
    assert 'https://foo.tumblr.com/post/3' not in by_link
    assert 'https://foo.tumblr.com/post/7' not in by_link


def test_spreadsheet_formulas_are_neutralized():
    archive, _, _ = build_zip()
    by_link = {row[5]: row for row in read_csv(archive)[1:]}
    assert by_link['https://foo.tumblr.com/post/6'][1] == '\'=HYPERLINK("https://evil.test")'


def test_failed_and_non_media_files_are_reported_not_saved():
    archive, _, _ = build_zip({
        f'{IMG}/q_2048.jpg': httpx.Response(404),
        f'{IMG}/a_2048.jpg': httpx.Response(200, headers={'content-type': 'text/html'}, content=b'<html>'),
    })
    assert 'images/1_01.jpg' not in archive.namelist()
    assert 'images/1_02.jpg' not in archive.namelist()
    failures = read_csv(archive, 'failed_downloads.csv')
    assert failures[1] == ['https://foo.tumblr.com/post/1', f'{IMG}/q_2048.jpg', 'HTTP 404']
    assert 'unexpected content type text/html' in failures[2][2]
    assert read_csv(archive)[1][6] == ''


def test_clean_download_has_no_failure_file():
    archive, _, _ = build_zip()
    assert 'failed_downloads.csv' not in archive.namelist()


def test_nothing_found_yet_cannot_be_downloaded():
    async def slow(request):
        await asyncio.sleep(60)

    with client_for(slow) as client:
        scan_id = client.post('/api/scans', json={'blog': 'foo'}).json()['id']
        assert client.post(f'/api/scans/{scan_id}/downloads').status_code == 409
        assert client.post('/api/scans/missing/downloads').status_code == 404


def test_token_is_single_use_and_unknown_tokens_fail():
    with client_for(handler([])) as client:
        url = client.post(f'/api/scans/{finished_scan(client)}/downloads').json()['url']
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 404
        assert client.get('/api/downloads/nope').status_code == 404


def test_active_downloads_are_capped_and_released():
    with client_for(handler([]), max_active_downloads=1) as client:
        scan_id = finished_scan(client)
        first = client.post(f'/api/scans/{scan_id}/downloads')
        assert client.post(f'/api/scans/{scan_id}/downloads').status_code == 503
        assert client.get(first.json()['url']).status_code == 200
        assert client.post(f'/api/scans/{scan_id}/downloads').status_code == 200


def test_clients_are_rate_limited():
    with client_for(handler([]), downloads_per_client_per_hour=1) as client:
        scan_id = finished_scan(client)
        download(client, scan_id)
        limited = client.post(f'/api/scans/{scan_id}/downloads')
        assert limited.status_code == 429
        assert int(limited.headers['Retry-After']) > 0


def make_manager(respond, tmp_path):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return DownloadManager(http, Store(str(tmp_path / 'd.db')), max_active=1, sleep=sleep), sleeps


def save_post(manager, url):
    manager.store.save_page(BlogState(blog='foo'), [{
        'id_string': '../../evil',
        'post_url': 'https://foo.tumblr.com/post/1',
        'content': [{'type': 'image', 'media': [{'url': url, 'width': 1, 'height': 1}]}],
    }])


def run_stream(manager):
    async def run():
        token = manager.issue('foo')
        blog, up_to = manager.redeem(token)
        return b''.join([chunk async for chunk in manager.stream(blog, up_to)])

    return zipfile.ZipFile(io.BytesIO(asyncio.run(run())))


def test_server_errors_are_retried(tmp_path):
    responses = [httpx.Response(503), httpx.Response(200, headers={'content-type': 'image/png'}, content=b'png')]
    manager, sleeps = make_manager(lambda request: responses.pop(0), tmp_path)
    save_post(manager, 'https://64.media.tumblr.com/a.jpg')
    archive = run_stream(manager)
    assert archive.read('images/evil_01.png') == b'png'
    assert sleeps == [2]
    assert manager.active == 0


def test_non_tumblr_addresses_are_never_fetched(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    for index, url in enumerate(('https://evil.test/a.jpg', 'https://tumblr.com.evil.test/a.jpg')):
        folder = tmp_path / str(index)
        folder.mkdir()
        manager, _ = make_manager(respond, folder)
        save_post(manager, url)
        archive = run_stream(manager)
        assert 'failed_downloads.csv' in archive.namelist()
    assert requests == []


def test_cancelling_mid_download_frees_the_slot(tmp_path):
    manager, _ = make_manager(
        lambda request: httpx.Response(200, headers={'content-type': 'image/png'}, content=b'png'),
        tmp_path,
    )
    save_post(manager, 'https://64.media.tumblr.com/a.jpg')

    async def run():
        blog, up_to = manager.redeem(manager.issue('foo'))
        stream = manager.stream(blog, up_to)
        await anext(stream)
        assert manager.active == 1
        await stream.aclose()

    asyncio.run(run())
    assert manager.active == 0
    assert manager.has_capacity()


def test_download_during_a_scan_contains_only_what_was_found_so_far():
    posts = [
        {'id': i, 'post_url': f'https://foo.tumblr.com/post/{i}', 'timestamp': 1600000000 - i, 'content': [text(f'p{i}')]}
        for i in range(45)
    ]
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) > 2:
            await asyncio.sleep(60)
        return httpx.Response(200, json={'response': {'posts': page_of(posts, request), 'total_posts': 45}})

    import time

    with client_for(respond) as client:
        scan_id = client.post('/api/scans', json={'blog': 'foo'}).json()['id']
        for _ in range(300):
            status = client.get(f'/api/scans/{scan_id}').json()
            if status['originals'] >= 39:
                break
            time.sleep(0.01)
        assert status['status'] == 'running'
        response = download(client, scan_id)
        rows = read_csv(zipfile.ZipFile(io.BytesIO(response.content)))
        assert len(rows) - 1 == status['originals'] < 45
