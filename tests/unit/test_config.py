import pytest
from pydantic import ValidationError

from ame.config import Settings


def test_defaults_loopback(tmp_path):
    s = Settings(data_dir=tmp_path)
    assert s.host == "127.0.0.1" and s.port == 8765
    assert s.db_path == tmp_path / "ame.db"
    assert s.database_url.startswith("sqlite:///")


def test_rejects_non_loopback(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, host="0.0.0.0")
