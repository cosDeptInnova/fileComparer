from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - fallback when python-dotenv is not yet installed.
    def load_dotenv(*args, **kwargs):
        return False


def load_project_env() -> None:
    package_dir = Path(__file__).resolve().parent
    project_dir = package_dir.parent
    env_candidates = (
        project_dir / ".env",
        package_dir / ".env",
    )
    for env_path in env_candidates:
        if env_path.exists():
            load_dotenv(env_path, override=False)


load_project_env()
