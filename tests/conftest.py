import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quill  # noqa: E402


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """Never read or write the real saved signature and details."""
    home = tmp_path / "quill-home"
    home.mkdir()
    # existing guided-mode tests expect no "save your details?" question
    (home / "profile.json").write_text('{"_setup": "skipped"}', encoding="utf-8")
    monkeypatch.setenv("QUILL_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    """Tests must never open Chrome; results go to os.startfile, which tests stub."""
    monkeypatch.setattr(quill, "find_chrome", lambda: None)
    monkeypatch.setattr(quill.os, "startfile", lambda p: None, raising=False)
