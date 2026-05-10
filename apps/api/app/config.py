from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    redis_url: str = Field("redis://redis:6379/0", alias="REDIS_URL")

    public_hostname: str = Field("shillscore.tg-itsavibe.com", alias="PUBLIC_HOSTNAME")

    twitter_client_id: str = Field("", alias="TWITTER_CLIENT_ID")
    twitter_client_secret: str = Field("", alias="TWITTER_CLIENT_SECRET")
    twitter_redirect_uri: str = Field("", alias="TWITTER_REDIRECT_URI")
    # App-only OAuth 2.0 Bearer (client_credentials grant). Required for
    # /tweets/search/all and /tweets/counts/all — those endpoints reject
    # user-context tokens.
    twitter_app_bearer: str = Field("", alias="TWITTER_APP_BEARER")

    github_client_id: str = Field("", alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field("", alias="GITHUB_CLIENT_SECRET")
    github_redirect_uri: str = Field("", alias="GITHUB_REDIRECT_URI")

    coingecko_api_key: str = Field("", alias="COINGECKO_API_KEY")
    defillama_base_url: str = Field("https://coins.llama.fi", alias="DEFILLAMA_BASE_URL")

    session_secret: str = Field("", alias="SESSION_SECRET")
    log_level: str = Field("INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
