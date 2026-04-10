import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from mcp_standby_proxy.errors import CacheError

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


class CacheData(dict):  # type: ignore[type-arg]
    """Cache data container. A dict with typed access to fixed keys.

    Fixed keys: cache_version (int), capabilities (dict).
    Method-keyed responses (e.g. "tools/list") are stored as extra keys.
    """

    cache_version: int
    capabilities: dict[str, Any]


def _load_sync(path: Path) -> CacheData | None:
    """Read and validate cache from disk. Returns None on any failure."""
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read cache file %s: %s", path, exc)
        return None

    if not isinstance(raw, dict):
        logger.warning("Cache file %s has unexpected format, ignoring", path)
        return None

    version = raw.get("cache_version")
    if version != CACHE_VERSION:
        logger.warning(
            "Cache file %s has version %r (expected %d), deleting",
            path,
            version,
            CACHE_VERSION,
        )
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to delete stale cache %s: %s", path, exc)
        return None

    return CacheData(raw)


def _save_sync(path: Path, data: CacheData) -> None:
    """Write cache atomically. Raises CacheError on failure."""
    parent = path.parent
    tmp_path: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=parent,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(data, tmp, indent=2)
        tmp_path.rename(path)
    except OSError as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise CacheError(f"Failed to write cache to {path}: {exc}") from exc


class CacheManager:
    """Manages a versioned JSON cache file for MCP capability responses."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> CacheData | None:
        """Load and validate cache. Returns None if missing or invalid version."""
        return _load_sync(self._path)

    async def save(self, data: CacheData) -> None:
        """Write cache to disk asynchronously (run_in_executor for file I/O)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_sync, self._path, data)

    @property
    def exists(self) -> bool:
        """Check if cache file exists on disk."""
        return self._path.exists()
