import asyncio
import json
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import TumblrAuth
from app.config import Settings
from app.limits import ApiQuota
from app.main import create_app
from app.tumblr import BlogNotFound, ReconnectRequired, TumblrClient, TumblrError


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def token_response(access='new-access', refresh='new-refresh', expires=2520):
    return httpx.Response(200, json={'access_token': access, 'refresh_token': refresh, 'expires_in': expires})


def make_auth(tmp_path, handler, clock=None, secret='shh'):
    http = httpx.AsyncClient(base_url='https://api.test/v2', transport=httpx.MockTransport(handler))
    return TumblrAuth(http, 'client', secret, 'http://localhost:8000/api/auth/callback', str(tmp_path / 'token.json'), clock or Clock())


def form(request):
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def stored(tmp_path, access='old', refresh='old-refresh', expires_at=99999):
    (tmp_path / 'token.json').write_text(json.dumps({'access_token': access, 'refresh_token': refresh, 'expires_at': expires_at}))


def test_authorize_url_asks_for_offline_access(tmp_path):
    auth = make_auth(tmp_path, lambda request: httpx.Response(500))
    url = urlsplit(auth.authorize_url())
    query = parse_qs(url.query)
    assert url.netloc == 'www.tumblr.com'
    assert query['client_id'] == ['client']
    assert query['response_type'] == ['code']
    assert query['scope'] == ['basic offline_access']
    assert query['redirect_uri'] == ['http://localhost:8000/api/auth/callback']
    assert query['state'][0]


def test_state_works_once_and_expires(tmp_path):
    clock = Clock()
    auth = make_auth(tmp_path, lambda request: httpx.Response(500), clock)
    state = parse_qs(urlsplit(auth.authorize_url()).query)['state'][0]
    assert auth.consume_state(state)
    assert not auth.consume_state(state)
    assert not auth.consume_state('forged')

    late = parse_qs(urlsplit(auth.authorize_url()).query)['state'][0]
    clock.now += 601
    assert not auth.consume_state(late)


def test_connect_saves_a_private_token_that_survives_restart(tmp_path):
    seen = []

    def handler(request):
        seen.append(form(request))
        return token_response()

    auth = make_auth(tmp_path, handler)
    asyncio.run(auth.connect('the-code'))
    assert seen[0]['grant_type'] == 'authorization_code'
    assert seen[0]['code'] == 'the-code'
    assert seen[0]['client_secret'] == 'shh'
    path = tmp_path / 'token.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / 'token.json.tmp').exists()
    assert make_auth(tmp_path, handler).connected


def test_no_secret_means_no_login_even_if_a_token_file_exists(tmp_path):
    stored(tmp_path)
    auth = make_auth(tmp_path, lambda request: httpx.Response(500), secret='')
    assert not auth.configured
    assert not auth.connected
    assert asyncio.run(auth.access_token()) is None
    assert (tmp_path / 'token.json').exists()


def test_fresh_token_is_used_without_refreshing(tmp_path):
    stored(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return token_response()

    assert asyncio.run(make_auth(tmp_path, handler).access_token()) == 'old'
    assert calls == []


def test_expiring_token_is_refreshed_and_rotation_is_saved(tmp_path):
    stored(tmp_path, expires_at=1030)
    seen = []

    def handler(request):
        seen.append(form(request))
        return token_response()

    assert asyncio.run(make_auth(tmp_path, handler).access_token()) == 'new-access'
    assert seen[0]['grant_type'] == 'refresh_token'
    assert seen[0]['refresh_token'] == 'old-refresh'
    assert json.loads((tmp_path / 'token.json').read_text())['refresh_token'] == 'new-refresh'


def test_concurrent_requests_refresh_only_once(tmp_path):
    stored(tmp_path, expires_at=0)
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)
        return token_response()

    auth = make_auth(tmp_path, handler)

    async def run():
        return await asyncio.gather(*[auth.access_token() for _ in range(5)])

    assert asyncio.run(run()) == ['new-access'] * 5
    assert len(calls) == 1


def test_revoked_login_is_forgotten(tmp_path):
    stored(tmp_path, expires_at=0)
    auth = make_auth(tmp_path, lambda request: httpx.Response(401))
    with pytest.raises(ReconnectRequired):
        asyncio.run(auth.access_token())
    assert not auth.connected
    assert not (tmp_path / 'token.json').exists()


def test_tumblr_outage_keeps_the_login(tmp_path):
    stored(tmp_path, expires_at=0)
    auth = make_auth(tmp_path, lambda request: httpx.Response(503))
    with pytest.raises(TumblrError):
        asyncio.run(auth.access_token())
    assert auth.connected
    assert (tmp_path / 'token.json').exists()


def run_page(auth, handler):
    http = httpx.AsyncClient(base_url='https://api.test/v2', transport=httpx.MockTransport(handler))

    async def no_sleep(seconds):
        pass

    client = TumblrClient(http, 'key', ApiQuota(100, 100), no_sleep, auth)

    async def collect():
        return await client.fetch_page('foo')

    return asyncio.run(collect())


EMPTY = {'response': {'posts': [], 'total_posts': 0}}


def test_scans_send_the_bearer_token(tmp_path):
    stored(tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=EMPTY)

    run_page(make_auth(tmp_path, handler), handler)
    assert seen[0].headers['Authorization'] == 'Bearer old'
    assert seen[0].url.params['api_key'] == 'key'


def test_scans_without_a_login_send_no_header(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=EMPTY)

    run_page(make_auth(tmp_path, handler), handler)
    assert 'Authorization' not in seen[0].headers


def test_refused_token_is_refreshed_once_then_retried(tmp_path):
    stored(tmp_path)
    posts_calls = []

    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        posts_calls.append(request.headers['Authorization'])
        return httpx.Response(401) if len(posts_calls) == 1 else httpx.Response(200, json=EMPTY)

    run_page(make_auth(tmp_path, handler), handler)
    assert posts_calls == ['Bearer old', 'Bearer new-access']


def test_repeatedly_refused_login_asks_to_reconnect(tmp_path):
    stored(tmp_path)

    def handler(request):
        if request.url.path.endswith('/oauth2/token'):
            return token_response()
        return httpx.Response(401)

    with pytest.raises(ReconnectRequired):
        run_page(make_auth(tmp_path, handler), handler)


def test_missing_blog_message_depends_on_login(tmp_path):
    handler = lambda request: httpx.Response(404, json={})

    with pytest.raises(BlogNotFound, match='connect your Tumblr account'):
        run_page(make_auth(tmp_path, handler), handler)

    stored(tmp_path)
    with pytest.raises(BlogNotFound, match="your Tumblr account can't see it"):
        run_page(make_auth(tmp_path, handler), handler)


def client_for(tmp_path, handler, secret='shh'):
    settings = Settings(
        _env_file=None,
        tumblr_consumer_key='client',
        tumblr_consumer_secret=secret,
        tumblr_token_path=str(tmp_path / 'token.json'),
    )
    return TestClient(create_app(settings, httpx.MockTransport(handler)), follow_redirects=False)


def start_login(client):
    response = client.get('/api/auth/login')
    assert response.status_code == 307
    return parse_qs(urlsplit(response.headers['location']).query)['state'][0]


def test_login_routes_connect_the_account(tmp_path):
    with client_for(tmp_path, lambda request: token_response()) as client:
        assert client.get('/api/auth/status').json() == {'configured': True, 'connected': False}
        state = start_login(client)
        response = client.get('/api/auth/callback', params={'code': 'c', 'state': state})
        assert response.headers['location'] == 'http://localhost:3000?tumblr=connected'
        assert client.get('/api/auth/status').json() == {'configured': True, 'connected': True}


def test_callback_rejects_forged_state(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return token_response()

    with client_for(tmp_path, handler) as client:
        response = client.get('/api/auth/callback', params={'code': 'c', 'state': 'forged'})
        assert response.headers['location'] == 'http://localhost:3000?tumblr=failed'
        assert calls == []
        assert client.get('/api/auth/status').json()['connected'] is False


def test_denied_consent_does_not_connect(tmp_path):
    with client_for(tmp_path, lambda request: token_response()) as client:
        state = start_login(client)
        response = client.get('/api/auth/callback', params={'error': 'access_denied', 'state': state})
        assert response.headers['location'] == 'http://localhost:3000?tumblr=failed'
        assert client.get('/api/auth/status').json()['connected'] is False


def test_login_is_unavailable_without_a_secret(tmp_path):
    with client_for(tmp_path, lambda request: token_response(), secret='') as client:
        assert client.get('/api/auth/status').json() == {'configured': False, 'connected': False}
        assert client.get('/api/auth/login').status_code == 503


def test_tokens_never_appear_in_api_responses(tmp_path):
    with client_for(tmp_path, lambda request: token_response(access='SECRET-A', refresh='SECRET-R')) as client:
        state = start_login(client)
        callback = client.get('/api/auth/callback', params={'code': 'c', 'state': state})
        status = client.get('/api/auth/status')
        assert 'SECRET' not in callback.text + callback.headers['location'] + status.text


def test_mismatch_retries_once_without_naming_a_return_address(tmp_path):
    with client_for(tmp_path, lambda request: token_response()) as client:
        first = client.get('/', params={'error': 'redirect_uri_mismatch'})
        query = parse_qs(urlsplit(first.headers['location']).query)
        assert urlsplit(first.headers['location']).netloc == 'www.tumblr.com'
        assert 'redirect_uri' not in query

        second = client.get('/', params={'error': 'redirect_uri_mismatch'})
        assert second.headers['location'] == 'http://localhost:3000?tumblr=mismatch'


def test_login_landing_at_the_root_connects_without_a_return_address(tmp_path):
    seen = []

    def handler(request):
        seen.append(form(request))
        return token_response()

    with client_for(tmp_path, handler) as client:
        retry = client.get('/', params={'error': 'redirect_uri_mismatch'})
        state = parse_qs(urlsplit(retry.headers['location']).query)['state'][0]
        response = client.get('/', params={'code': 'c', 'state': state})
        assert response.headers['location'] == 'http://localhost:3000?tumblr=connected'
        assert 'redirect_uri' not in seen[0]
        assert client.get('/api/auth/status').json()['connected'] is True


def test_retry_is_available_again_after_connecting(tmp_path):
    with client_for(tmp_path, lambda request: token_response()) as client:
        retry = client.get('/', params={'error': 'redirect_uri_mismatch'})
        state = parse_qs(urlsplit(retry.headers['location']).query)['state'][0]
        client.get('/', params={'code': 'c', 'state': state})
        again = client.get('/', params={'error': 'redirect_uri_mismatch'})
        assert urlsplit(again.headers['location']).netloc == 'www.tumblr.com'


def test_mismatch_without_a_secret_does_not_start_a_login(tmp_path):
    with client_for(tmp_path, lambda request: token_response(), secret='') as client:
        response = client.get('/', params={'error': 'redirect_uri_mismatch'})
        assert response.headers['location'] == 'http://localhost:3000?tumblr=mismatch'


def test_plain_visit_to_the_root_goes_to_the_frontend(tmp_path):
    with client_for(tmp_path, lambda request: token_response()) as client:
        assert client.get('/').headers['location'] == 'http://localhost:3000'
