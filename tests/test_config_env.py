"""Tests for config._load_env_file edge cases."""

import os
from pathlib import Path
import importlib


def test_load_env_basic(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TG_TEST_KEY=abc123\n", encoding="utf-8")

    import config
    config._load_env_file(env_file)
    assert os.environ.get("TG_TEST_KEY") == "abc123"


def test_load_env_quoted_double(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_TEST_QUOTED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('TG_TEST_QUOTED="hello world"\n', encoding="utf-8")
    import config
    config._load_env_file(env_file)
    assert os.environ.get("TG_TEST_QUOTED") == "hello world"


def test_load_env_quoted_single_no_escape(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_TEST_SINGLE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TG_TEST_SINGLE='line\\nstays'\n", encoding="utf-8")
    import config
    config._load_env_file(env_file)
    # single quotes => no unicode_escape interpretation
    assert os.environ.get("TG_TEST_SINGLE") == "line\\nstays"


def test_load_env_strips_inline_comment(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_TEST_INLINE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TG_TEST_INLINE=value123 # this is a comment\n", encoding="utf-8")
    import config
    config._load_env_file(env_file)
    assert os.environ.get("TG_TEST_INLINE") == "value123"


def test_load_env_setdefault_does_not_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_TEST_PRESET", "from_environ")
    env_file = tmp_path / ".env"
    env_file.write_text("TG_TEST_PRESET=from_file\n", encoding="utf-8")
    import config
    config._load_env_file(env_file)
    # Pre-existing env var must win
    assert os.environ.get("TG_TEST_PRESET") == "from_environ"


def test_load_env_skips_comments_and_blanks(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "TG_TEST_VALID=ok\n"
        "  \n"
        "no_equals_sign\n"
        "@invalid_key=value\n",
        encoding="utf-8",
    )
    import config
    if "TG_TEST_VALID" in os.environ:
        del os.environ["TG_TEST_VALID"]
    config._load_env_file(env_file)
    assert os.environ.get("TG_TEST_VALID") == "ok"
    assert "@invalid_key" not in os.environ


def test_parse_slot_times_accepts_end_of_day():
    import config
    assert config._parse_slot_times("08:00,16:00,24:00") == [(8, 0), (16, 0), (24, 0)]
    assert config._parse_slot_times("24:01") == []
