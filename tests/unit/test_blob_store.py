"""Unit tests for the blob deletion bridge."""

import pytest

from app.core.config import Settings
from app.services import blob_store
from app.services.blob_store import delete_blob_url

_TOKEN_ENV = "BLOB_READ_WRITE_TOKEN"
_BLOB_URL = "https://store.public.blob.vercel-storage.com/downloads/video.mp4"


@pytest.fixture(autouse=True)
def _scrub_written_token():
    """The bridge writes os.environ directly; never leak it across tests."""
    yield
    blob_store.os.environ.pop(_TOKEN_ENV, None)


def _settings_with_token(token: str) -> Settings:
    """Build isolated settings carrying a blob token (or not)."""
    return Settings(_env_file=None, blob_read_write_token=token)


def _install_fake_delete(monkeypatch) -> list[str]:
    """Capture blob URLs passed to the SDK's delete."""
    calls: list[str] = []

    async def fake_delete(url: str) -> None:
        calls.append(url)

    monkeypatch.setattr("vercel.blob.delete_blob", fake_delete)
    return calls


class TestDeleteBlobUrl:
    """delete_blob_url forwards to the SDK with working credentials."""

    @pytest.mark.asyncio
    async def test_deletes_the_given_blob(self, monkeypatch) -> None:
        monkeypatch.delenv(_TOKEN_ENV, raising=False)
        monkeypatch.setattr(blob_store, "get_settings", lambda: _settings_with_token("tok-1"))
        calls = _install_fake_delete(monkeypatch)

        await delete_blob_url(_BLOB_URL)

        assert calls == [_BLOB_URL]

    @pytest.mark.asyncio
    async def test_mirrors_dotenv_token_into_the_environment(self, monkeypatch) -> None:
        monkeypatch.delenv(_TOKEN_ENV, raising=False)
        monkeypatch.setattr(blob_store, "get_settings", lambda: _settings_with_token("tok-1"))
        _install_fake_delete(monkeypatch)

        await delete_blob_url(_BLOB_URL)

        assert blob_store.os.environ[_TOKEN_ENV] == "tok-1"

    @pytest.mark.asyncio
    async def test_existing_environment_token_is_not_overwritten(self, monkeypatch) -> None:
        monkeypatch.setenv(_TOKEN_ENV, "tok-env")
        monkeypatch.setattr(blob_store, "get_settings", lambda: _settings_with_token("tok-2"))
        _install_fake_delete(monkeypatch)

        await delete_blob_url(_BLOB_URL)

        assert blob_store.os.environ[_TOKEN_ENV] == "tok-env"

    @pytest.mark.asyncio
    async def test_missing_token_leaves_the_environment_untouched(self, monkeypatch) -> None:
        monkeypatch.delenv(_TOKEN_ENV, raising=False)
        monkeypatch.setattr(blob_store, "get_settings", lambda: _settings_with_token(""))
        _install_fake_delete(monkeypatch)

        await delete_blob_url(_BLOB_URL)

        assert _TOKEN_ENV not in blob_store.os.environ
