import json

import pytest

from mcp_standby_proxy.cache import CACHE_VERSION, CacheData, CacheManager
from mcp_standby_proxy.errors import CacheError


def _make_cache_data(**extra) -> CacheData:
    data = CacheData(
        cache_version=CACHE_VERSION,
        capabilities={"tools": {}},
    )
    data.update(extra)
    return data


def test_load_valid_cache(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    data = _make_cache_data(**{"tools/list": {"tools": [{"name": "foo"}]}})
    cache_file.write_text(json.dumps(data))

    manager = CacheManager(cache_file)
    result = manager.load()

    assert result is not None
    assert result["cache_version"] == CACHE_VERSION
    assert result["capabilities"] == {"tools": {}}
    assert result["tools/list"] == {"tools": [{"name": "foo"}]}


def test_load_missing_file(tmp_path) -> None:
    manager = CacheManager(tmp_path / "nonexistent.json")
    assert manager.load() is None


def test_load_wrong_cache_version_deletes_file(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"cache_version": 99, "capabilities": {}}))

    manager = CacheManager(cache_file)
    result = manager.load()

    assert result is None
    assert not cache_file.exists()


def test_load_corrupt_json_returns_none(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("{not valid json}")

    manager = CacheManager(cache_file)
    result = manager.load()

    assert result is None


async def test_save_then_load_roundtrip(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    manager = CacheManager(cache_file)

    original = _make_cache_data(**{"tools/list": {"tools": [{"name": "bar"}]}})
    await manager.save(original)

    loaded = manager.load()
    assert loaded is not None
    assert loaded["cache_version"] == CACHE_VERSION
    assert loaded["tools/list"] == {"tools": [{"name": "bar"}]}


async def test_save_is_atomic(tmp_path) -> None:
    """No partial files remain after save — verify by checking for .tmp files."""
    cache_file = tmp_path / "cache.json"
    manager = CacheManager(cache_file)

    await manager.save(_make_cache_data())

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"
    assert cache_file.exists()


def test_exists_property_false_when_no_file(tmp_path) -> None:
    manager = CacheManager(tmp_path / "cache.json")
    assert not manager.exists


async def test_exists_property_true_after_save(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    manager = CacheManager(cache_file)
    assert not manager.exists

    await manager.save(_make_cache_data())
    assert manager.exists


def test_load_missing_cache_version_key_returns_none(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"capabilities": {}}))

    manager = CacheManager(cache_file)
    result = manager.load()

    assert result is None


def test_load_deletes_old_version_cache(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"cache_version": 0, "capabilities": {}}))

    manager = CacheManager(cache_file)
    result = manager.load()

    assert result is None
    assert not cache_file.exists()


async def test_save_raises_cache_error_when_path_component_is_file(tmp_path) -> None:
    """mkdir fails with NotADirectoryError when a path component is a regular file."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("I am a file")

    manager = CacheManager(blocker / "cache.json")

    with pytest.raises(CacheError):
        await manager.save(_make_cache_data())


async def test_save_raises_cache_error_on_permission_denied(tmp_path) -> None:
    """save() raises CacheError when file cannot be written."""
    read_only_dir = tmp_path / "readonly"
    read_only_dir.mkdir()
    read_only_dir.chmod(0o444)

    manager = CacheManager(read_only_dir / "cache.json")

    with pytest.raises(CacheError):
        await manager.save(_make_cache_data())

    # Cleanup
    read_only_dir.chmod(0o755)


async def test_save_creates_parent_directories(tmp_path) -> None:
    cache_file = tmp_path / "deep" / "nested" / "cache.json"
    manager = CacheManager(cache_file)

    await manager.save(_make_cache_data())

    assert cache_file.exists()
    loaded = manager.load()
    assert loaded is not None
    assert loaded["cache_version"] == CACHE_VERSION


async def test_save_overwrites_existing_cache(tmp_path) -> None:
    cache_file = tmp_path / "cache.json"
    manager = CacheManager(cache_file)

    first = _make_cache_data(**{"tools/list": {"tools": [{"name": "first"}]}})
    await manager.save(first)

    second = _make_cache_data(**{"tools/list": {"tools": [{"name": "second"}]}})
    await manager.save(second)

    loaded = manager.load()
    assert loaded is not None
    assert loaded["tools/list"]["tools"][0]["name"] == "second"
