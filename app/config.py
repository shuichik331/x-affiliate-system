"""Local configuration. Secrets are never sent to the browser or stored in SQLite."""
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ENV = {"APP_MODE", "DATA_DIR", "X_BEARER_TOKEN", "AFFILIATE_API_KEY"}


def read_config(root=ROOT):
    values = {}
    env_file = Path(root) / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or key not in ALLOWED_ENV:
                raise ValueError(".env の設定名を確認してください。利用可能: APP_MODE, DATA_DIR, X_BEARER_TOKEN, AFFILIATE_API_KEY")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    values.update({key: os.environ[key] for key in ALLOWED_ENV if key in os.environ})
    mode = values.get("APP_MODE", "mock")
    if mode != "mock":
        raise ValueError("この初期版は APP_MODE=mock のみ起動できます。実接続は未実装です。")
    directory = Path(values.get("DATA_DIR") or "data").expanduser()
    if not directory.is_absolute():
        directory = Path(root) / directory
    return {"mode": mode, "db_path": directory / "workspace.sqlite3"}
