"""Blob storage cleanup for offloaded downloads.

Only the deletion side lives here: the worker uploads the produced MP4, and
the backend removes the blob once the client has the file (or a retention
sweep decides nobody is coming back for it). Authentication is the SDK's own:
OIDC when running on Vercel with a connected store, else the
``BLOB_READ_WRITE_TOKEN`` environment variable.

The SDK import is deferred so a deployment without it still boots, and only
fails when a delete is actually attempted. The token is mirrored from settings
into the environment the SDK reads, for runs where pydantic read ``.env`` but
the process environment never saw it.
"""

import os

from app.core.config import get_settings

_TOKEN_ENV = "BLOB_READ_WRITE_TOKEN"


def _ensure_token_in_environment() -> None:
    """Mirror a .env-only blob token into the environment the SDK reads."""
    token = get_settings().blob_read_write_token
    if token and not os.environ.get(_TOKEN_ENV):
        os.environ[_TOKEN_ENV] = token


async def delete_blob_url(url: str) -> None:
    """Delete one blob from the store. Idempotent: a missing blob is fine."""
    _ensure_token_in_environment()
    from vercel.blob import delete_blob

    await delete_blob(url)
