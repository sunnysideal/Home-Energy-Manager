from __future__ import annotations

from pathlib import Path

from launcher import PublicLogFile, _sanitise_public_log_text


def test_public_log_sanitises_common_secrets() -> None:
    text = (
        "Authorization: Bearer abc.def.ghi password=hunter2 "
        "https://example.test/path?access_token=secret-token&ok=1 "
        "mqtt://alan:swordfish@example.test/topic\n"
    )

    clean = _sanitise_public_log_text(text)

    assert "abc.def.ghi" not in clean
    assert "hunter2" not in clean
    assert "secret-token" not in clean
    assert "swordfish" not in clean
    assert "[REDACTED]" in clean
    assert "ok=1" in clean


def test_public_log_file_is_bounded_and_keeps_recent_lines(tmp_path: Path) -> None:
    path = tmp_path / "latest.log"
    log = PublicLogFile(path, max_bytes=256)

    for index in range(40):
        log.write(f"line-{index:02d} {'x' * 20}\n")

    content = path.read_text()
    assert path.stat().st_size <= 256
    assert "line-39" in content
    assert "public log trimmed" in content


def test_public_log_file_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "www" / "home_energy_manager" / "latest.log"
    log = PublicLogFile(path, max_bytes=1024)
    log.write("hello\n")

    assert path.read_text() == "hello\n"
