from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import LLMComparisonResponse


class StubLLMClient:
    def compare(self, messages):
        user_payload = messages[-1]["content"]
        if '"rows"' in user_payload:
            return LLMComparisonResponse.model_validate({"changes": []})
        if "Texto nuevo añadido" in user_payload:
            return LLMComparisonResponse.model_validate(
                {
                    "changes": [
                        {
                            "change_type": "añadido",
                            "source_a": "",
                            "source_b": "Texto nuevo añadido",
                            "summary": "nuevo bloque",
                        }
                    ]
                }
            )
        return LLMComparisonResponse.model_validate(
            {
                "changes": [
                    {
                        "change_type": "modificado",
                        "source_a": "Texto base",
                        "source_b": "Texto base ajustado",
                        "summary": "cambio de redacción",
                    }
                ]
            }
        )


def test_compare_endpoint_contract(monkeypatch, tmp_path: Path):
    from app.services import comparison_pipeline

    monkeypatch.setattr(comparison_pipeline, "LLMClient", lambda: StubLLMClient())

    client = TestClient(app)
    token = client.get("/csrf-token").json()["csrf_token"]

    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("Texto base", encoding="utf-8")
    file_b.write_text("Texto base ajustado. Texto nuevo añadido", encoding="utf-8")

    response = client.post(
        "/comparar",
        headers={"X-CSRFToken": token},
        cookies={"csrftoken_app": token},
        files={
            "file_a": ("a.txt", file_a.read_bytes(), "text/plain"),
            "file_b": ("b.txt", file_b.read_bytes(), "text/plain"),
        },
        data={"engine": "auto"},
    )
    assert response.status_code == 200
    sid = response.json()["sid"]

    progress = client.get(f"/progress/{sid}")
    assert progress.status_code == 200
    assert progress.json()["status"] == "done"

    result = client.get(f"/resultado/{sid}/json")
    assert result.status_code == 200
    payload = result.json()
    assert payload["sid"] == sid
    assert payload["rows"]
    assert payload["rows"][0]["change_type"] in {"añadido", "modificado", "eliminado"}
