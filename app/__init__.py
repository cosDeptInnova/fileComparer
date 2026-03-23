from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


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
