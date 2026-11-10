import logging
import math
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse

from app.auth import TumblrAuth
from app.config import Settings
from app.downloads import DownloadManager, DownloadsBusy
from app.limits import ApiQuota, ClientLimiter
from app.scans import ScanManager, TooManyScans
from app.schemas import AuthStatus, DownloadLink, PostPage, ScanRequest, ScanStatus
from app.store import Store
from app.tumblr import TumblrClient, TumblrError, normalize_blog_name, to_post

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
# httpx logs full URLs at INFO, and ours carry the consumer key
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    '''Run with: uvicorn app.main:create_app --factory'''
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=settings.tumblr_api_base, timeout=30, transport=transport
        ) as http:
            quota = ApiQuota(settings.tumblr_calls_per_hour, settings.tumblr_calls_per_day)
            auth = TumblrAuth(
                http,
                settings.tumblr_consumer_key,
                settings.tumblr_consumer_secret,
                settings.tumblr_redirect_uri,
                settings.tumblr_token_path,
            )
            app.state.auth = auth
            store = Store(settings.database_path)
            app.state.manager = ScanManager(
                TumblrClient(http, settings.tumblr_consumer_key, quota, auth=auth),
                store,
                max_concurrent=settings.max_concurrent_scans,
                max_pending=settings.max_pending_scans,
                cooldown=settings.rescan_cooldown_seconds,
                idle_timeout=settings.scan_idle_timeout_seconds,
            )
            # Separate from the API client, so the consumer key has no route to the CDN
            async with httpx.AsyncClient(timeout=60, transport=transport) as media_http:
                app.state.downloads = DownloadManager(media_http, store, settings.max_active_downloads)
                yield
            await app.state.manager.close()
            store.close()

    app = FastAPI(title='Pumbly', lifespan=lifespan)
    app.state.client_limiter = ClientLimiter(settings.new_scans_per_client_per_hour, 3600)
    app.state.download_limiter = ClientLimiter(settings.downloads_per_client_per_hour, 3600)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_methods=['GET', 'POST'],
        allow_headers=['Content-Type'],
    )

    def get_status(request: Request, scan_id: str) -> ScanStatus:
        status = request.app.state.manager.status(scan_id)
        if status is None:
            raise HTTPException(404, 'That blog has not been scanned yet.')
        return status

    @app.get('/api/health')
    async def health() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/api/auth/status')
    async def auth_status(request: Request) -> AuthStatus:
        auth: TumblrAuth = request.app.state.auth
        return AuthStatus(configured=auth.configured, connected=auth.connected)

    @app.get('/api/auth/login')
    async def auth_login(request: Request) -> RedirectResponse:
        auth: TumblrAuth = request.app.state.auth
        if not auth.configured:
            raise HTTPException(503, 'TUMBLR_CONSUMER_SECRET is not set.')
        return RedirectResponse(auth.authorize_url())

    async def auth_return(
        request: Request,
        code: str | None = None,
        state: str = '',
        error: str | None = None,
    ) -> RedirectResponse:
        '''Where Tumblr lands after login. Tumblr uses whichever address is
        registered on the app, so this also answers at the site root.'''
        auth: TumblrAuth = request.app.state.auth
        frontend = settings.origins[0]
        if error == 'redirect_uri_mismatch':
            if auth.configured and auth.retry_without_redirect():
                return RedirectResponse(auth.authorize_url(send_redirect=False))
            return RedirectResponse(f'{frontend}?tumblr=mismatch')
        if code is None and error is None:
            return RedirectResponse(frontend)

        result = 'failed'
        attempt = auth.consume_state(state) if auth.configured and code else None
        if attempt is not None:
            try:
                await auth.connect(code, attempt.sent_redirect)
                result = 'connected'
            except TumblrError as connect_error:
                logger.warning('Connecting a Tumblr account failed: %s', connect_error)
        return RedirectResponse(f'{frontend}?tumblr={result}')

    app.add_api_route('/api/auth/callback', auth_return, methods=['GET'])
    app.add_api_route('/', auth_return, methods=['GET'], include_in_schema=False)

    @app.post('/api/scans', status_code=202)
    async def create_scan(body: ScanRequest, request: Request) -> ScanStatus:
        '''Starts a scan, or returns the existing one for this blog.'''
        try:
            blog = normalize_blog_name(body.blog)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

        manager: ScanManager = request.app.state.manager
        if manager.is_current(blog):
            manager.touch(blog)
            return manager.status(blog)

        if not manager.has_capacity():
            raise HTTPException(503, 'Too many scans are running. Try again soon.')

        client = request.client.host if request.client else 'unknown'
        wait = request.app.state.client_limiter.hit(client)
        if wait > 0:
            raise HTTPException(
                429,
                'You have started too many scans. Try again later.',
                headers={'Retry-After': str(math.ceil(wait))},
            )

        try:
            manager.start(blog)
        except TooManyScans as error:
            raise HTTPException(503, 'Too many scans are running. Try again soon.') from error
        return manager.status(blog)

    @app.get('/api/scans/{scan_id}')
    async def scan_status(scan_id: str, request: Request) -> ScanStatus:
        status = get_status(request, scan_id)
        request.app.state.manager.touch(scan_id)
        return status

    @app.post('/api/scans/{scan_id}/stop')
    async def stop_scan(scan_id: str, request: Request) -> ScanStatus:
        '''Stops the scan and keeps its position for next time.'''
        get_status(request, scan_id)
        await request.app.state.manager.stop(scan_id)
        return get_status(request, scan_id)

    @app.get('/api/scans/{scan_id}/posts')
    async def scan_posts(
        scan_id: str,
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ) -> PostPage:
        '''Posts saved after the given position, including during a scan.'''
        get_status(request, scan_id)
        rows = request.app.state.manager.store.read_posts(scan_id, after, limit)
        return PostPage(
            posts=[to_post(raw) for _, raw in rows],
            next_after=rows[-1][0] if rows else after,
        )

    @app.post('/api/scans/{scan_id}/downloads')
    async def create_download(scan_id: str, request: Request) -> DownloadLink:
        '''Checks limits and returns a single use link to a zip of everything
        found so far.'''
        if get_status(request, scan_id).originals == 0:
            raise HTTPException(409, 'No original posts have been found yet.')

        downloads: DownloadManager = request.app.state.downloads
        if not downloads.has_capacity():
            raise HTTPException(503, 'Too many downloads are running. Try again soon.')

        client = request.client.host if request.client else 'unknown'
        wait = request.app.state.download_limiter.hit(client)
        if wait > 0:
            raise HTTPException(
                429,
                'You have started too many downloads. Try again later.',
                headers={'Retry-After': str(math.ceil(wait))},
            )

        try:
            return DownloadLink(url=f'/api/downloads/{downloads.issue(scan_id)}')
        except DownloadsBusy as error:
            raise HTTPException(503, 'Too many downloads are running. Try again soon.') from error

    @app.get('/api/downloads/{token}')
    async def get_download(token: str, request: Request) -> StreamingResponse:
        downloads: DownloadManager = request.app.state.downloads
        snapshot = downloads.redeem(token)
        if snapshot is None:
            raise HTTPException(404, 'That download link has expired.')
        blog, up_to = snapshot
        return StreamingResponse(
            downloads.stream(blog, up_to),
            media_type='application/zip',
            headers={'Content-Disposition': f'attachment; filename="pumbly-{blog}.zip"'},
        )

    return app
