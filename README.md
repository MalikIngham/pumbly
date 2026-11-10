# Pumbly

Finds all of the original (non-reblogged) posts on a Tumblr blog.

- `backend/`: FastAPI service that scans blogs through the Tumblr API
- `frontend/`: Next.js app that starts scans and displays the results

## Running locally

You need Docker and a Tumblr consumer key from https://www.tumblr.com/oauth/apps.

    cp backend/.env.example backend/.env    # then set TUMBLR_CONSUMER_KEY
    docker compose up --build

This runs both dev servers in one container, and reloads them when you edit code:

- Frontend: http://localhost:3000
- Backend: http://localhost:8000

After changing `backend/requirements.txt` or `frontend/package.json`, restart with:

    docker compose up --build -V

## Login-only blogs

To include blogs that are only visible when logged in:

1. On your app at https://www.tumblr.com/oauth/apps, add
   `http://localhost:8000/api/auth/callback` to the OAuth2 redirect URLs.
2. Add `TUMBLR_CONSUMER_SECRET` to `backend/.env` and restart the container.
3. Click **Connect your Tumblr account** on the search page.

Your login is saved in `backend/.tumblr_token.json`. Delete that file and restart
to disconnect.

Every scan then runs as your account, so anyone who can reach the app can see
what your account can. It is for personal use only: do not expose it publicly
without adding a login for each visitor.

## Tests

    docker compose exec app bash -c 'cd backend && python -m pytest -p no:cacheprovider'

## API

| Route | Purpose |
|---|---|
| `POST /api/scans` | Start or resume a scan with `{"blog": "name"}` |
| `GET /api/scans/{id}` | Status and progress. The id is the blog name |
| `POST /api/scans/{id}/stop` | Stop and keep the position |
| `GET /api/scans/{id}/posts?after=0&limit=50` | Original posts saved after a position |
| `POST /api/scans/{id}/downloads` | Get a single use link to a zip of everything found so far |
| `GET /api/downloads/{token}` | The zip: `images/`, `videos/` and `posts.csv` |
| `GET /api/auth/status` | Whether a Tumblr account is connected |
| `GET /api/auth/login` | Start connecting a Tumblr account |
| `GET /api/auth/callback` | Where Tumblr returns after approval |
| `GET /api/health` | Health check |

## Deploying

Scan results and positions are saved in `backend/data/pumbly.db`, so they survive
restarts. Run one worker (`uvicorn app.main:create_app --factory`), because running
scans are tracked in that process. A scan pauses if nobody checks on it for two
minutes, and picks up from the same place when the blog is searched again.

Set `ALLOWED_ORIGINS` on the backend and `NEXT_PUBLIC_API_URL` when building the
frontend. Behind a reverse proxy, run uvicorn with `--proxy-headers` and
`--forwarded-allow-ips` set to the proxy's address, or every visitor shares one
scan limit.
