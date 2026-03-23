from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class CompareTaskPayload:
    sid: str
    job_dir: str
    file_a_path: str
    file_b_path: str
    file_a_name: str
    file_b_name: str
    user_id: int
    conv_id_redis: int | None = None
    conv_row_id: int | None = None
    av: dict[str, Any] = field(default_factory=dict)
    opts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CompareTaskPayload":
        return cls(**payload)
