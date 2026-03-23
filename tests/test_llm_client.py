from app.llm_client import LLMClient, _extract_json_message
from app.schemas import LLMComparisonResponse


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, path, json):
        self.calls.append((path, json))
        return FakeResponse(self.payload)

    def close(self):
        return None


def test_extract_json_message_handles_markdown_and_inline_json():
    payload = {
        "choices": [
            {
                "message": {
                    "content": '```json\n{"changes":[{"change_type":"modificado","source_a":"A","source_b":"B"}]}\n```'
                }
            }
        ]
    }
    data = _extract_json_message(payload)
    assert data["changes"][0]["change_type"] == "modificado"


def test_llm_client_compare_returns_validated_response():
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"changes":[{"change_type":"añadido","source_a":"","source_b":"nuevo"}]}'
                }
            }
        ]
    }
    client = LLMClient(client=FakeHttpClient(payload))
    result = client.compare([
        {"role": "system", "content": "x"},
        {"role": "user", "content": "y"},
    ])
    assert isinstance(result, LLMComparisonResponse)
    assert result.changes[0].change_type == "añadido"
