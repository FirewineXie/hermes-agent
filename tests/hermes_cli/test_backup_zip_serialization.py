"""The manual and automatic archive paths use the same WAL-safe serialization."""
import os
import sqlite3
import zipfile
from argparse import Namespace
from contextlib import closing
from pathlib import Path

import pytest

from hermes_cli import backup


def test_automatic_zip_clamps_pre1980_timestamp_without_losing_file(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    old_file = home / "ancient.txt"
    old_file.write_text("old data", encoding="utf-8")
    os.utime(old_file, (0, 0))
    source_mtime_ns = old_file.stat().st_mtime_ns
    archive = tmp_path / "backup.zip"

    assert backup._write_full_zip_backup(archive, home) == archive

    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read("ancient.txt") == b"old data"
        assert zipped.getinfo("ancient.txt").date_time == (1980, 1, 1, 0, 0, 0)
    assert old_file.stat().st_mtime_ns == source_mtime_ns


@pytest.mark.parametrize("automatic", [False, True])
def test_zip_captures_live_wal_and_cleans_failed_staging(tmp_path, monkeypatch, automatic):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    output = tmp_path / "archive"
    output.mkdir()
    archive = output / "backup.zip"

    def run():
        if automatic:
            return backup._write_full_zip_backup(archive, home)
        return backup.run_backup(Namespace(output=str(archive)))

    with closing(sqlite3.connect(home / "state.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE messages (body TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO messages VALUES ('WAL-only row')")
        writer.commit()
        run()
        with zipfile.ZipFile(archive) as zipped:
            assert not any(name.endswith(('-wal', '-shm')) for name in zipped.namelist())
            member = next(name for name in zipped.namelist() if name.endswith('state.db'))
            restored = tmp_path / "restored.db"
            restored.write_bytes(zipped.read(member))
        with closing(sqlite3.connect(restored)) as snapshot:
            assert snapshot.execute("SELECT body FROM messages").fetchall() == [("WAL-only row",)]

        def refuse_write(self, filename, arcname=None, **kwargs):
            raise OSError("archive device full")

        with zipfile.ZipFile(archive) as zipped:
            previous = {info.filename: info.CRC for info in zipped.infolist()}
        monkeypatch.setattr(zipfile.ZipFile, "write", refuse_write)
        run()
        assert sorted(output.iterdir()) == [archive], "a failed write must not leak a private database snapshot"
        if automatic:
            with zipfile.ZipFile(archive) as zipped:
                assert {info.filename: info.CRC for info in zipped.infolist()} == previous, \
                    "an all-failed run must not overwrite the previous good archive"
