"""Tests for _setup_logging (FR-19, FR-21)."""
import logging
import logging.handlers
import re
import sys
from pathlib import Path

import pytest

from mcp_standby_proxy.config import LoggingFileConfig
from mcp_standby_proxy.proxy import _setup_logging


@pytest.fixture(autouse=True)
def reset_root_logger():
    """Isolate each test: snapshot root logger state, restore after test."""
    root = logging.getLogger()
    # Snapshot existing handlers (e.g. pytest's LogCaptureHandler)
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    # Close and remove any handler we didn't start with
    for h in list(root.handlers):
        if h not in original_handlers:
            try:
                h.close()
            except Exception:
                pass
            root.removeHandler(h)
    root.setLevel(original_level)


def test_stderr_only_default(capsys) -> None:
    root = logging.getLogger()
    before = set(root.handlers)
    _setup_logging("myserver", verbose=0)
    added = [h for h in root.handlers if h not in before]
    # Only the stderr StreamHandler should have been added
    assert len(added) == 1
    assert isinstance(added[0], logging.StreamHandler)
    assert not isinstance(added[0], logging.FileHandler)
    assert added[0].stream is sys.stderr
    assert root.level == logging.WARNING


def test_verbose_1_sets_info(capsys) -> None:
    _setup_logging("myserver", verbose=1)
    assert logging.getLogger().level == logging.INFO


def test_verbose_2_sets_debug(capsys) -> None:
    _setup_logging("myserver", verbose=2)
    assert logging.getLogger().level == logging.DEBUG


def test_verbose_high_value_clamps_to_debug(capsys) -> None:
    _setup_logging("myserver", verbose=99)
    assert logging.getLogger().level == logging.DEBUG


def test_no_stdout_handlers(capsys) -> None:
    _setup_logging("myserver", verbose=2)
    root = logging.getLogger()
    for h in root.handlers:
        if hasattr(h, "stream"):
            assert h.stream is not sys.stdout, "handler must not write to stdout"


def test_log_format_includes_server_name(capsys) -> None:
    root = logging.getLogger()
    before = set(root.handlers)
    _setup_logging("kroki", verbose=1)
    added = [h for h in root.handlers if h not in before]
    assert len(added) == 1
    handler = added[0]
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello world", args=(), exc_info=None,
    )
    formatted = handler.formatter.format(record)  # type: ignore[union-attr]
    assert "[kroki]" in formatted
    assert "hello world" in formatted


def test_log_format_not_on_stdout(capsys) -> None:
    root = logging.getLogger()
    before = set(root.handlers)
    _setup_logging("kroki", verbose=1)
    added = [h for h in root.handlers if h not in before]
    assert len(added) == 1
    assert added[0].stream is not sys.stdout  # type: ignore[union-attr]


def test_file_handler_installed_when_configured(tmp_path) -> None:
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path))
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1


def test_file_handler_creates_parent_dir(tmp_path) -> None:
    log_path = tmp_path / "nested" / "dir" / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path))
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    assert log_path.parent.exists()


def test_file_handler_writes_log_record(tmp_path) -> None:
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="info")
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    logging.getLogger("myserver").warning("unique-test-record-xyz")
    content = log_path.read_text()
    assert "unique-test-record-xyz" in content


def test_file_handler_independent_level_from_stderr(tmp_path, capsys) -> None:
    """stderr=WARNING, file=DEBUG — DEBUG records reach file but not stderr."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="debug")
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    logging.getLogger("myserver").debug("debug-only-record")
    captured = capsys.readouterr()
    # Must NOT appear on stderr (stderr level is WARNING)
    assert "debug-only-record" not in captured.err
    # Must appear in file
    assert "debug-only-record" in log_path.read_text()


def test_file_level_raises_root_level(tmp_path) -> None:
    """Root level is set to min(stderr_level, file_level)."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="debug")
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)
    # Root must be DEBUG so file handler gets the records
    assert logging.getLogger().level == logging.DEBUG


def test_file_handler_rotation_config(tmp_path) -> None:
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(
        path=str(log_path),
        max_size="1KB",
        backup_count=5,
    )
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    root = logging.getLogger()
    rotating = next(
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert rotating.maxBytes == 1_000
    assert rotating.backupCount == 5


def test_file_logging_disabled_on_permission_error(tmp_path, capsys) -> None:
    """Unwritable parent produces a warning on stderr; no file handler installed."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="info")

    # Make parent read-only so mkdir and open fail
    tmp_path.chmod(0o555)
    try:
        _setup_logging(
            "myserver",
            verbose=0,
            log_file_config=log_file_config,
            resolved_log_path=log_path,
        )
        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert file_handlers == [], "no file handler should be installed on failure"
        captured = capsys.readouterr()
        assert "file logging disabled" in captured.err
    finally:
        tmp_path.chmod(0o755)


def test_file_logging_enabled_info_on_stderr(tmp_path, capsys) -> None:
    """Startup path announcement always appears on stderr regardless of -v flag (FR-21.5).

    Uses verbose=0 (WARNING stderr) to cover the production case: the announcement
    must bypass the stderr handler level filter and reach stderr unconditionally.
    """
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="info")
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    captured = capsys.readouterr()
    assert "file logging enabled" in captured.err
    assert str(log_path) in captured.err


def test_no_file_handler_when_config_is_none(tmp_path) -> None:
    root = logging.getLogger()
    before = set(root.handlers)
    _setup_logging("myserver", verbose=0, log_file_config=None, resolved_log_path=None)
    added = [h for h in root.handlers if h not in before]
    rotating = [h for h in added if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert rotating == []


# ---- Spec case 3: root level computation ----

@pytest.mark.parametrize("verbose,file_level,expected_root", [
    (0, "debug",   logging.DEBUG),   # WARNING vs DEBUG  → DEBUG
    (2, "info",    logging.DEBUG),   # DEBUG   vs INFO   → DEBUG
    (1, "warning", logging.INFO),    # INFO    vs WARNING → INFO
])
def test_root_level_is_min_of_stderr_and_file(
    tmp_path, verbose: int, file_level: str, expected_root: int
) -> None:
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level=file_level)
    _setup_logging("myserver", verbose=verbose, log_file_config=log_file_config, resolved_log_path=log_path)
    assert logging.getLogger().level == expected_root


# ---- Spec case 6: stdout-handler assertion fires ----

def test_stdout_handler_assertion_fires(tmp_path) -> None:
    """FR-21.7: adding a stdout StreamHandler to the root logger triggers AssertionError."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="info")

    # Inject a stdout handler before calling _setup_logging so the assertion
    # at the stderr-handler construction site fires.
    stdout_handler = logging.StreamHandler(sys.stdout)
    root = logging.getLogger()
    root.addHandler(stdout_handler)
    try:
        with pytest.raises(AssertionError, match="stdout"):
            _setup_logging(
                "myserver",
                verbose=0,
                log_file_config=log_file_config,
                resolved_log_path=log_path,
            )
    finally:
        root.removeHandler(stdout_handler)


# ---- Spec case 7: log format regex ----

def test_log_format_matches_spec_regex(tmp_path) -> None:
    """FR-19.2: format is 'YYYY-MM-DDTHH:MM:SS LEVEL [server_name] message'."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="info")
    _setup_logging("testsvr", verbose=1, log_file_config=log_file_config, resolved_log_path=log_path)

    logging.getLogger("testsvr").info("unique-fmt-check-msg")
    content = log_path.read_text().strip().splitlines()
    # Find the line from this test — keyed on both server name and unique message text
    target_line = next(
        (l for l in content if "[testsvr]" in l and "unique-fmt-check-msg" in l), None
    )
    assert target_line is not None, f"Expected log line not found. File content:\n{''.join(content)}"
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} INFO \[testsvr\] unique-fmt-check-msg$",
        target_line,
    ), f"Format mismatch: {target_line!r}"


# ---- Spec case 8: exception tracebacks go to file ----

def test_exception_traceback_written_to_file(tmp_path) -> None:
    """FR-21.3: logger.exception appends traceback to file below the header line."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="error")
    _setup_logging("myserver", verbose=0, log_file_config=log_file_config, resolved_log_path=log_path)

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("myserver").exception("caught it")

    content = log_path.read_text()
    assert "caught it" in content
    assert "Traceback (most recent call last):" in content
    assert "ValueError: boom" in content


# ---- FR-19.4 / FR-21.7: stdout contamination invariant ----

@pytest.mark.smoke
def test_no_handler_writes_to_stdout_stderr_only(tmp_path) -> None:
    """FR-19.4: no handler may have stdout as its stream — ever."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="debug")
    _setup_logging(
        "myserver",
        verbose=2,
        log_file_config=log_file_config,
        resolved_log_path=log_path,
    )
    root = logging.getLogger()
    for h in root.handlers:
        if hasattr(h, "stream"):
            assert h.stream is not sys.stdout, (
                f"Handler {h!r} writes to stdout — violates FR-19.4"
            )


@pytest.mark.smoke
def test_debug_records_reach_file_not_stdout(tmp_path, capsys) -> None:
    """With stderr=WARNING and file=DEBUG, DEBUG records go to the file only (FR-21.2)."""
    log_path = tmp_path / "proxy.log"
    log_file_config = LoggingFileConfig(path=str(log_path), level="debug")
    _setup_logging(
        "myserver",
        verbose=0,  # stderr stays at WARNING
        log_file_config=log_file_config,
        resolved_log_path=log_path,
    )

    logging.getLogger("myserver").debug("payload-debug-abc123")

    captured = capsys.readouterr()
    assert captured.out == "", "DEBUG log must never appear on stdout"
    assert "payload-debug-abc123" not in captured.err, (
        "DEBUG log must not appear on stderr when stderr level is WARNING"
    )
    assert "payload-debug-abc123" in log_path.read_text(), (
        "DEBUG log must be written to the log file"
    )
