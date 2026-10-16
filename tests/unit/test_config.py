"""Unit tests for the job timeout setting."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


class TestJobTimeoutSetting:
    """Tests for the per-job transcription timeout setting."""

    def test_default_is_ten_minutes(self) -> None:
        assert Settings(_env_file=None).job_timeout_seconds == 600

    def test_reads_configured_value_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "120")
        assert Settings(_env_file=None).job_timeout_seconds == 120

    def test_rejects_non_positive_values(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, job_timeout_seconds=0)


class TestBrokerUrl:
    """Tests for Celery broker URL selection."""

    def test_default_is_redis(self) -> None:
        assert Settings(_env_file=None).queue_provider == "redis"
        assert Settings(_env_file=None).broker_url == "redis://localhost:6379/0"

    def test_broker_url_uses_redis_url_for_redis_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "redis")
        monkeypatch.setenv("REDIS_URL", "rediss://user:pass@redis.example:6379/0")
        settings = Settings(_env_file=None)
        assert settings.broker_url == "rediss://user:pass@redis.example:6379/0"

    def test_rabbitmq_broker_url_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "rabbitmq")
        monkeypatch.setenv("RABBITMQ_URL", "amqp://user:pass@rabbit:5672//")
        settings = Settings(_env_file=None)
        assert settings.queue_provider == "rabbitmq"
        assert settings.broker_url == "amqp://user:pass@rabbit:5672//"

    def test_default_rabbitmq_broker_url(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "rabbitmq")
        assert Settings(_env_file=None).broker_url == "amqp://guest:guest@localhost:5672//"

    def test_rejects_invalid_queue_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "kafka")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


class TestLiveExternalCallsSetting:
    """Tests for the live external calls gate setting."""

    def test_defaults_to_false(self, monkeypatch) -> None:
        monkeypatch.delenv("LIVE_EXTERNAL_CALLS", raising=False)
        assert Settings(_env_file=None).live_external_calls is False

    def test_reads_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("LIVE_EXTERNAL_CALLS", "true")
        assert Settings(_env_file=None).live_external_calls is True


class TestTranscriptModeSetting:
    """Tests for the transcript mode setting."""

    def test_defaults_to_real(self, monkeypatch) -> None:
        monkeypatch.delenv("TRANSCRIPT_MODE", raising=False)
        assert Settings(_env_file=None).transcript_mode == "real"

    def test_reads_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("TRANSCRIPT_MODE", "fake")
        assert Settings(_env_file=None).transcript_mode == "fake"

    # Every construction here passes ``_env_file=None``. Without it these tests read
    # the developer's real .env, so a local value such as a loopback
    # PUBLIC_SITE_URL decides whether the production guards below fire - the suite
    # then passes or fails depending on one developer's machine.


class TestSessionCookieNameMatchesSecureAttribute:
    """The name and the Secure attribute have to agree, or the browser drops the cookie.

    ``__Host-`` tells the browser to enforce ``Secure``. When the cookie will not
    be ``Secure`` - a development server on plain http - a name still carrying
    the prefix is discarded on arrival. Nothing reports that: sign-in answers
    with the account, the page renders it signed in, and the first mutation
    comes back ``UNAUTHENTICATED``.
    """

    def test_prefix_is_dropped_when_the_cookie_is_not_secure(self) -> None:
        settings = Settings(_env_file=None, environment="development")

        assert not settings.cookie_is_secure
        assert not settings.session_cookie_name.startswith("__Host-")

    def test_prefix_is_kept_when_the_cookie_is_secure(self) -> None:
        settings = Settings(_env_file=None, environment="production")

        assert settings.cookie_is_secure
        assert settings.session_cookie_name.startswith("__Host-")

    def test_a_plain_name_survives_either_way(self) -> None:
        for environment in ("development", "production"):
            settings = Settings(
                _env_file=None, environment=environment, session_cookie_name="jumpto_session"
            )
            assert settings.session_cookie_name == "jumpto_session"


class TestCsrfSecretIsRequired:
    """An unset CSRF secret left every cookie-authenticated mutation impossible.

    With no secret configured the client has nothing to send, and the guard
    rejects a missing header, so sign-out, password-set and history deletion
    all failed with CSRF_FAILED while the API booted happily. Requiring the
    secret turns that into a startup failure instead of a per-request one.
    """

    def test_an_unset_secret_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, csrf_token="")

    def test_a_short_secret_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, csrf_token="tooshort")

    def test_a_configured_secret_is_accepted(self) -> None:
        assert Settings(_env_file=None, csrf_token="a-long-enough-secret").csrf_token


class TestPublicSiteUrl:
    """The origin that password-reset and verification links are built from.

    Every reset and verification mail is addressed by this value, so both
    failure directions are silent: a local run on the production origin sends a
    developer to the deployed site where their token means nothing, and a
    deployed run on a loopback origin sends every real recipient a link that
    cannot open.
    """

    def test_development_defaults_to_the_dev_server(self) -> None:
        assert (
            Settings(_env_file=None, environment="development").public_site_url
            == "http://localhost:5173"
        )

    def test_production_defaults_to_the_public_origin(self) -> None:
        assert (
            Settings(_env_file=None, environment="production").public_site_url == "https://qfza.app"
        )

    def test_an_explicit_value_wins_in_development(self, monkeypatch) -> None:
        monkeypatch.setenv("PUBLIC_SITE_URL", "http://localhost:5173")
        assert (
            Settings(_env_file=None, environment="development").public_site_url
            == "http://localhost:5173"
        )

    def test_a_trailing_slash_is_stripped_so_links_do_not_double_up(self) -> None:
        settings = Settings(_env_file=None, public_site_url="https://qfza.app///")
        assert settings.public_site_url == "https://qfza.app"

    def test_production_refuses_a_loopback_origin(self) -> None:
        with pytest.raises(ValidationError, match="PUBLIC_SITE_URL"):
            Settings(
                _env_file=None, environment="production", public_site_url="http://localhost:5173"
            )

    def test_production_refuses_plain_http_even_on_a_real_host(self) -> None:
        with pytest.raises(ValidationError, match="PUBLIC_SITE_URL"):
            Settings(_env_file=None, environment="production", public_site_url="http://qfza.app")

    def test_development_may_use_a_loopback_origin(self) -> None:
        settings = Settings(
            _env_file=None, environment="development", public_site_url="http://localhost:5173"
        )
        assert settings.public_site_url == "http://localhost:5173"

    @pytest.mark.parametrize("bad", ["not-a-url", "qfza.app", "https://"])
    def test_rejects_a_value_that_is_not_an_absolute_url(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="PUBLIC_SITE_URL"):
            Settings(_env_file=None, public_site_url=bad)


class TestBlobReadWriteToken:
    """Tests for the Vercel Blob token setting."""

    def test_default_is_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
        assert Settings(_env_file=None).blob_read_write_token == ""

    def test_reads_configured_value_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "tok-1")
        assert Settings(_env_file=None).blob_read_write_token == "tok-1"
