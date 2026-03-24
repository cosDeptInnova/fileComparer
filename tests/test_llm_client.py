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
        if isinstance(self.payload, list):
            current = self.payload.pop(0)
        else:
            current = self.payload
        return FakeResponse(current)

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


def test_extract_json_message_reads_tool_call_arguments_when_content_is_empty():
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "arguments": '{"changes":[{"change_type":"eliminado","source_a":"viejo","source_b":""}]}'
                            }
                        }
                    ],
                }
            }
        ]
    }

    data = _extract_json_message(payload)

    assert data["changes"][0]["change_type"] == "eliminado"


def test_llm_client_compare_retries_without_response_format_when_content_is_empty():
    payloads = [
        {"choices": [{"message": {"content": ""}}]},
        {
            "choices": [
                {
                    "message": {
                        "content": '{"changes":[{"change_type":"modificado","source_a":"A","source_b":"B"}]}'
                    }
                }
            ]
        },
    ]
    fake_http_client = FakeHttpClient(payloads)
    client = LLMClient(client=fake_http_client, max_retries=1)

    result = client.compare([
        {"role": "system", "content": "x"},
        {"role": "user", "content": "y"},
    ])

    assert result.changes[0].change_type == "modificado"
    assert len(fake_http_client.calls) == 2
    assert fake_http_client.calls[0][1]["response_format"] == {"type": "json_object"}
    assert "response_format" not in fake_http_client.calls[1][1]


def test_extract_json_message_accepts_escaped_json_string_payload():
    payload = {
        "choices": [
            {
                "message": {
                    "content": '"{\\"changes\\":[{\\"change_type\\":\\"modificado\\",\\"source_a\\":\\"A\\",\\"source_b\\":\\"B\\"}]}"'
                }
            }
        ]
    }

    data = _extract_json_message(payload)

    assert data["changes"][0]["change_type"] == "modificado"


def test_llm_client_compare_normalizes_english_keys_and_labels():
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"rows":[{"type":"added","a":"","b":"nuevo","description":"alta","confidence":"high","severity":"critical"}]}'
                }
            }
        ]
    }
    client = LLMClient(client=FakeHttpClient(payload))

    result = client.compare([
        {"role": "system", "content": "x"},
        {"role": "user", "content": "y"},
    ])

    assert result.changes[0].change_type == "añadido"
    assert result.changes[0].source_b == "nuevo"
    assert result.changes[0].confidence == "alta"
    assert result.changes[0].severity == "critica"


def test_extract_json_message_accepts_trailing_comma():
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"changes":[{"change_type":"modificado","source_a":"A","source_b":"B",}],}'
                }
            }
        ]
    }

    data = _extract_json_message(payload)

    assert data["changes"][0]["change_type"] == "modificado"
