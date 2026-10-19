import json

import pytest

from mewcode.session import Session
from mewcode.storage import SessionStorage, SessionStorageError


def test_save_load_and_latest(tmp_path):
    storage = SessionStorage(tmp_path)
    session = Session(id="abc", model="demo")
    session.add_message("user", "你好")
    session.add_message("assistant", "你好！")
    storage.save(session)
    loaded = storage.load("abc")
    assert loaded.api_messages() == [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}]
    assert storage.latest().id == "abc"


def test_corrupt_session(tmp_path):
    storage = SessionStorage(tmp_path)
    (tmp_path / "bad.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(SessionStorageError, match="损坏"):
        storage.load("bad")
