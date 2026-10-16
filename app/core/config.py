"""Application configuration settings."""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _coerce_bool(value: str | bool | int) -> bool:
    """Coerce empty-string / falsy env values to a valid bool."""
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("", "0", "false", "no"):
            return False
        if value in ("1", "true", "yes"):
            return True
    return bool(value)


PRODUCTION_SITE_URL = "https://qfza.app"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/jumpto",
        description="PostgreSQL connection URL",
    )

    # Redis
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL used as the Celery broker (when QUEUE_PROVIDER=redis)",
    )

    # Celery / broker
    queue_provider: Literal["redis", "rabbitmq"] = Field(
        default="redis",
        description="Celery broker transport: redis or rabbitmq (QUEUE_PROVIDER env var)",
    )
    rabbitmq_url: str = Field(
        default="amqp://guest:guest@localhost:5672//",
        description="RabbitMQ connection URL used as the Celery broker (when QUEUE_PROVIDER=rabbitmq)",
    )

    # Job / transcription timeout
    job_timeout_seconds: int = Field(
        default=600,
        ge=1,
        description="Max seconds a transcription job may run before it is failed",
    )

    # Assembly AI
    assembly_api_key: str = Field(
        default="",
        description="Assembly.ai API key for transcription",
    )

    # CORS
    cors_origins: str = Field(
        default="http://localhost:5173",
        description="Comma-separated list of allowed CORS origins",
    )

    # Environment
    environment: str = Field(
        default="development",
        description="Application environment (development/production)",
    )

    # Internal worker API key
    internal_api_key: str = Field(
        default="",
        description="Shared API key for internal worker-to-backend communication",
    )

    # Vercel Blob (download artifact cleanup)
    blob_read_write_token: str = Field(
        default="",
        description="Vercel Blob read-write token, mirrored into the environment for the SDK",
    )

    # External calls
    live_external_calls: Annotated[bool, BeforeValidator(_coerce_bool)] = Field(
        default=False,
        description="Enable live external API calls (yt-dlp, Assembly.ai)",
    )
    # Transcript mode
    transcript_mode: str = Field(
        default="real",
        description="Transcript mode: real or fake",
    )

    # Sessions
    session_cookie_name: str = Field(
        default="__Host-jumpto_session",
        description="Session cookie name. The __Host- prefix makes browsers enforce Secure/Path=/.",
    )
    session_absolute_ttl_days: int = Field(
        default=30,
        ge=1,
        description="Hard cap on session lifetime regardless of activity",
    )
    session_idle_ttl_days: int = Field(
        default=7,
        ge=1,
        description="Sliding window: session expires this long after last use",
    )
    session_sweep_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often to delete expired and revoked session rows",
    )

    # CSRF: a per-deployment secret echoed in a header on cookie-authenticated
    # mutations. Not a credential, so it may sit in the frontend bundle.
    csrf_token: str = Field(
        min_length=16,
        description="Shared secret the web client sends as X-CSRF-Token",
    )
    csrf_header_name: str = Field(
        default="X-CSRF-Token",
        description="Header carrying the CSRF token on state-changing requests",
    )

    # Login lockout
    max_login_attempts: int = Field(
        default=3,
        ge=1,
        description="Consecutive wrong passwords before the account is locked",
    )
    login_ip_hourly_limit: int = Field(
        default=200,
        ge=1,
        description=(
            "Per-IP failed logins allowed per hour. Kept high: mobile carriers NAT "
            "many subscribers behind one address."
        ),
    )
    trusted_proxy_networks: str = Field(
        default="",
        description=(
            "Comma-separated IP addresses or CIDR ranges of proxies that may set "
            "X-Forwarded-For. Empty means trust none, so the header is ignored and "
            "the connecting address is used."
        ),
    )

    # Email (verification / password reset) over SMTP
    smtp_host: str = Field(default="smtp.gmail.com", description="SMTP host")
    smtp_port: int = Field(default=587, ge=1, description="SMTP port")
    smtp_user: str = Field(default="", description="SMTP account address")
    smtp_app_password: str = Field(
        default="",
        description="Mailbox password. Grants IMAP as well as SMTP - treat as a secret.",
    )
    smtp_use_ssl: bool | None = Field(
        default=None,
        description=(
            "True for implicit TLS (Hostinger 465), False for STARTTLS (Gmail 587). "
            "Leave unset to infer it from the port."
        ),
    )
    email_from: str = Field(default="", description="Sender address for outgoing mail")
    email_from_name: str = Field(default="Qfza", description="Sender display name")

    @field_validator("smtp_user")
    @classmethod
    def _smtp_user_must_be_an_address(cls, value: str) -> str:
        """Reject a hostname in the username slot at startup.

        The most common way this breaks is pasting the server hostname into
        SMTP_USER because SMTP_HOST is missing, which leaves the host on its
        default and looks configured: mail is then attempted against the wrong
        server as a nonexistent user and the send fails quietly. Refusing to
        start turns that into one obvious error instead of undelivered mail.
        """
        if value and "@" not in value:
            raise ValueError(
                "SMTP_USER must be a full email address such as you@example.com, "
                f"not {value!r}. If this is your mail server's hostname, move it to SMTP_HOST."
            )
        return value

    public_site_url: str = Field(
        default="",
        description=(
            "Public origin, used to build the links inside emails. Defaults to "
            "localhost in development and the production origin otherwise, so a "
            "local request never mails a real person a production link."
        ),
    )
    password_reset_token_ttl_minutes: int = Field(
        default=30,
        ge=1,
        description="Expiry for password reset and email verification tokens",
    )

    # Google sign-in (identity only)
    google_client_id: str = Field(
        default="", description="Google OAuth client id for ID token validation"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def broker_url(self) -> str:
        """Return the Celery broker URL for the configured queue provider."""
        if self.queue_provider == "rabbitmq":
            return self.rabbitmq_url
        return self.redis_url

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment.lower() == "development"

    @property
    def session_absolute_ttl(self) -> timedelta:
        """Hard cap on session lifetime, however active the session is."""
        return timedelta(days=self.session_absolute_ttl_days)

    @property
    def session_idle_ttl(self) -> timedelta:
        """Sliding window after which an unused session stops resolving."""
        return timedelta(days=self.session_idle_ttl_days)

    @property
    def email_token_ttl(self) -> timedelta:
        """Lifetime of verification and reset links."""
        return timedelta(minutes=self.password_reset_token_ttl_minutes)

    @property
    def cookie_is_secure(self) -> bool:
        """Whether the session cookie should carry the Secure attribute."""
        return not self.is_development

    @model_validator(mode="after")
    def _cookie_name_matches_secure_attribute(self) -> Settings:
        """Keep the cookie name and its ``Secure`` attribute in agreement.

        A browser rejects a ``__Host-`` cookie that arrives without ``Secure``,
        and it does so silently: sign-in returns the account, the page shows it
        signed in, and then every state-changing request fails as if the
        session had expired. So the prefix is only used where ``Secure`` will
        accompany it, and dropped otherwise.
        """
        if not self.cookie_is_secure and self.session_cookie_name.startswith("__Host-"):
            self.session_cookie_name = self.session_cookie_name.removeprefix("__Host-")
        return self

    @model_validator(mode="after")
    def _public_site_url_matches_environment(self) -> Settings:
        """Point emailed links at whichever origin this process actually serves.

        Every password-reset and verification link is built from this value, so
        both ways of getting it wrong are silent and both are fatal to whoever
        receives the mail. A local run left on the production origin sends a
        developer to the deployed site, where their token means nothing; a
        deployed run left on a loopback origin sends every real recipient a
        ``localhost`` link that can never open. So the origin follows the
        environment unless stated explicitly, and a production loopback is a
        boot failure rather than a mailer that quietly burns tokens.
        """
        configured = self.public_site_url.strip().rstrip("/")
        if not configured:
            configured = "http://localhost:5173" if self.is_development else PRODUCTION_SITE_URL
        self.public_site_url = configured

        try:
            parsed = urlparse(configured)
        except ValueError as exc:
            raise ValueError(f"PUBLIC_SITE_URL is not a valid URL: {configured!r}") from exc
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"PUBLIC_SITE_URL is not a valid URL: {configured!r}")

        if not self.is_development:
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
            if loopback or parsed.scheme != "https":
                raise ValueError(
                    f"PUBLIC_SITE_URL is {configured!r} but this is a production "
                    "deployment. Every password-reset and verification email would "
                    "carry a link that cannot open for a real recipient. Set it to "
                    f"the public origin, such as {PRODUCTION_SITE_URL}."
                )
        return self

    @property
    def trusted_proxy_list(self) -> list[IPv4Network | IPv6Network]:
        """Parse the trusted proxy list into networks.

        An unparseable entry is dropped rather than fatal: a typo in an env var
        should not stop the API booting, and dropping an entry only makes the
        deployment stricter.
        """
        networks: list[IPv4Network | IPv6Network] = []
        for raw in self.trusted_proxy_networks.split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ip_network(entry, strict=False))
            except ValueError:
                # stdlib logging, not the app's: that wrapper imports settings,
                # and settings cannot depend on something that imports it.
                logging.getLogger(__name__).warning(
                    "Ignoring an unparseable trusted proxy entry", extra={"entry": entry}
                )
        return networks


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
