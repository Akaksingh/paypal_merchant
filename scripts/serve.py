"""Run the risk gateway.  python scripts/serve.py [--port 8080] [--reload]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os

import uvicorn


def load_dotenv(path: Path) -> list[str]:
    """Dependency-free KEY=VALUE loader. Real environment variables win; returns the names it set."""
    set_names: list[str] = []
    if not path.exists():
        return set_names
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val
            set_names.append(key)
    return set_names


def main() -> None:
    loaded = load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if loaded:
        print(f"[serve] loaded from .env: {', '.join(loaded)}")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    uvicorn.run("chakrashield.serving.app:app", host=a.host, port=a.port, reload=a.reload, log_level="info",
                access_log=False, workers=1)


if __name__ == "__main__":
    main()
