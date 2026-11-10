import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.limits import Clock
from app.tumblr import ReconnectRequired, TumblrError

logger = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://www.tumblr.com/oauth2/authorize'
SCOPES = 'basic offline_access'
STATE_TTL_SECONDS = 600
REFRESH_MARGIN_SECONDS = 60
RETRY_WINDOW_SECONDS = 60


@dataclass
class LoginAttempt:
    expires_at: float
    sent_redirect: bool


def _error_summary(response: httpx.Response) -> dict[str, Any]:
    '''Only the named error fields, so a credential can never reach the logs.'''
    try:
        body = response.json()
    except ValueError:
        return {}
    if not isinstance(body, dict):
        return {}
    return {key: body[key] for key in ('error', 'error_description', 'meta', 'errors') if key in body}


class TumblrAuth:
    '''Holds the Tumblr login for the one account this deployment acts as.

    Refreshing replaces the refresh token, so the newest pair is always saved
    to disk. Without that, a restart would strand the login.'''

    def __init__(
        self,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_path: str,
        clock: Clock = time.time,
    ):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_path = Path(token_path)
        self.clock = clock
        self.attempts: dict[str, LoginAttempt] = {}
        self.last_retry = 0.0
        self.lock = asyncio.Lock()
        self.token = self._load() if client_secret else None

    @property
    def configured(self) -> bool:
        return bool(self.client_secret)

    @property
    def connected(self) -> bool:
        return self.token is not None

    def _load(self) -> dict[str, Any] | None:
        try:
            token = json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return None
        return token if {'access_token', 'refresh_token', 'expires_at'} <= set(token) else None

    def _save(self, token: dict[str, Any]) -> None:
        temporary = self.token_path.with_name(f'{self.token_path.name}.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as file:
            json.dump(token, file)
        os.replace(temporary, self.token_path)
        self.token = token

    def _forget(self) -> None:
        self.token = None
        self.token_path.unlink(missing_ok=True)

    def authorize_url(self, send_redirect: bool = True) -> str:
        now = self.clock()
        self.attempts = {k: v for k, v in self.attempts.items() if v.expires_at > now}
        state = secrets.token_urlsafe(32)
        self.attempts[state] = LoginAttempt(now + STATE_TTL_SECONDS, send_redirect)
        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'scope': SCOPES,
            'state': state,
        }
        if send_redirect:
            params['redirect_uri'] = self.redirect_uri
        return f'{AUTHORIZE_URL}?{urlencode(params)}'

    def retry_without_redirect(self) -> bool:
        '''Whether to try again without naming a return address, which makes
        Tumblr use the one registered on the app. Tumblr's error carries no
        state, so a time window is what stops this repeating forever.'''
        now = self.clock()
        if now - self.last_retry < RETRY_WINDOW_SECONDS:
            return False
        self.last_retry = now
        return True

    def consume_state(self, state: str) -> LoginAttempt | None:
        '''Each state works once, so a forged callback cannot connect an account.'''
        attempt = self.attempts.pop(state, None)
        return attempt if attempt is not None and attempt.expires_at > self.clock() else None

    async def _request_token(self, fields: dict[str, str], previous_refresh: str | None) -> None:
        response = await self.http.post('/oauth2/token', data={
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            **fields,
        })
        if response.status_code != 200:
            logger.warning(
                'Tumblr %s request returned HTTP %d: %s',
                fields['grant_type'], response.status_code, _error_summary(response),
            )
        if response.status_code in (400, 401):
            raise ReconnectRequired('Tumblr login expired. Connect your account again.')
        if response.status_code != 200:
            raise TumblrError('Tumblr is having problems. Try again later.')
        try:
            body = response.json()
            if not body.get('refresh_token'):
                logger.warning('Tumblr %s response had no refresh token', fields['grant_type'])
            self._save({
                'access_token': body['access_token'],
                'refresh_token': body.get('refresh_token') or previous_refresh,
                'expires_at': self.clock() + body['expires_in'],
            })
        except (ValueError, KeyError, TypeError) as error:
            raise TumblrError('Tumblr sent an unreadable response.') from error

    async def connect(self, code: str, sent_redirect: bool = True) -> None:
        fields = {'grant_type': 'authorization_code', 'code': code}
        # Tumblr expects this only if the authorization request included it
        if sent_redirect:
            fields['redirect_uri'] = self.redirect_uri
        try:
            await self._request_token(fields, None)
        except httpx.TransportError as error:
            raise TumblrError('Tumblr is not responding.') from error
        self.last_retry = 0.0

    async def access_token(self, rejected: str | None = None) -> str | None:
        '''A current token, or None when no account is connected. Pass a token
        Tumblr just refused to force a refresh. The lock matters because a
        refresh token only works once and scans run concurrently.'''
        async with self.lock:
            token = self.token
            if token is None:
                return None
            if token['access_token'] == rejected or token['expires_at'] <= self.clock() + REFRESH_MARGIN_SECONDS:
                try:
                    await self._request_token(
                        {'grant_type': 'refresh_token', 'refresh_token': token['refresh_token']},
                        token['refresh_token'],
                    )
                except ReconnectRequired:
                    self._forget()
                    raise
                except httpx.TransportError as error:
                    raise TumblrError('Tumblr is not responding.') from error
            return self.token['access_token']
