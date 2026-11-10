from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    '''Loaded from the environment or backend/.env. See .env.example.'''

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    tumblr_consumer_key: str
    tumblr_api_base: str = 'https://api.tumblr.com/v2'

    # Needed only to connect a Tumblr account, for login-only blogs
    tumblr_consumer_secret: str = ''
    tumblr_redirect_uri: str = 'http://localhost:8000/api/auth/callback'
    tumblr_token_path: str = '.tumblr_token.json'

    database_path: str = 'data/pumbly.db'

    # Comma separated list of frontend origins allowed to call this API
    allowed_origins: str = 'http://localhost:3000'

    # Tumblr's published limits per consumer key
    tumblr_calls_per_hour: int = 1000
    tumblr_calls_per_day: int = 5000

    max_concurrent_scans: int = 3
    max_pending_scans: int = 20
    new_scans_per_client_per_hour: int = 20
    rescan_cooldown_seconds: int = 300
    scan_idle_timeout_seconds: int = 120

    max_active_downloads: int = 2
    downloads_per_client_per_hour: int = 10

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(',') if o.strip()]
