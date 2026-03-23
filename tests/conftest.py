from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("COMPARE_INLINE_JOBS", "1")
os.environ.setdefault("COMPARE_DATA_DIR", str(Path(__file__).resolve().parent / "tmp_data"))
os.environ.setdefault("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8002/v1")
