import sys
import types
import unittest
from unittest.mock import Mock

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
sys.modules.setdefault("httpx", types.SimpleNamespace(Client=object))

from app.llm_client import LLMClient, _extract_json_message


class FakeSemaphore:
    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self.released = []

    def acquire(self, timeout_seconds=None):
        return None

    def enabled(self):
        return self._enabled

    def active_count(self):
        return 0

    def release(self, lease):
        self.released.append(lease)


class ExtractJsonMessageTests(unittest.TestCase):
    def test_extracts_plain_json_string(self):
        payload = {
            "choices": [
                {"message": {"content": '{"review_label":"cambio_real","change_type":"modificado"}'}}
            ]
        }
        self.assertEqual(_extract_json_message(payload)["change_type"], "modificado")

    def test_extracts_json_inside_markdown_fence(self):
        payload = {
            "choices": [
                {"message": {"content": "```json\n{\"review_label\":\"cambio_real\",\"change_type\":\"modificado\"}\n```"}}
            ]
        }
        self.assertEqual(_extract_json_message(payload)["review_label"], "cambio_real")

    def test_extracts_embedded_json_from_text(self):
        payload = {
            "choices": [
                {"message": {"content": "Aquí va el resultado: {\"review_label\":\"sin_cambios_por_reflujo\",\"change_type\":\"sin_cambios\"} fin"}}
            ]
        }
        self.assertEqual(_extract_json_message(payload)["change_type"], "sin_cambios")

    def test_extracts_json_from_content_array(self):
        payload = {
            "choices": [
                {"message": {"content": [{"type": "text", "text": '{"review_label":"cambio_real","change_type":"insertado"}'}]}}
            ]
        }
        self.assertEqual(_extract_json_message(payload)["change_type"], "insertado")

    def test_extracts_json_from_split_content_fragments(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": '{"review_label":"cambio_real",'},
                            {"type": "text", "text": '"change_type":"modificado"}'},
                        ]
                    }
                }
            ]
        }
        self.assertEqual(_extract_json_message(payload)["change_type"], "modificado")

    def test_extracts_json_after_think_block(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '<think>Voy a razonar primero {"draft":true}</think>{"review_label":"cambio_real","change_type":"eliminado"}'
                    }
                }
            ]
        }
        self.assertEqual(_extract_json_message(payload)["change_type"], "eliminado")

    def test_extracts_double_encoded_json_string(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '"{\\"review_label\\":\\"cambio_real\\",\\"change_type\\":\\"sin_cambios\\"}"'
                    }
                }
            ]
        }
        self.assertEqual(_extract_json_message(payload)["change_type"], "sin_cambios")


class LLMClientTests(unittest.TestCase):
    def test_ascii_api_key_builds_authorization_header(self):
        client = LLMClient(api_key="token-123", inference_semaphore=FakeSemaphore())
        self.assertEqual(client._build_headers(), {"Authorization": "Bearer token-123"})

    def test_non_ascii_api_key_omits_authorization_header(self):
        client = LLMClient(api_key="señal", inference_semaphore=FakeSemaphore())
        self.assertIsNone(client._build_headers())

    def test_chat_completion_uses_v1_relative_endpoint(self):
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        fake_response.raise_for_status.return_value = None

        fake_http = Mock()
        fake_http.post.return_value = fake_response

        client = LLMClient(
            base_url="http://127.0.0.1:8002/v1",
            model_name="Llama3_8B_Cosmos",
            inference_semaphore=FakeSemaphore(),
        )
        client._client = fake_http

        client.chat_completion(messages=[{"role": "user", "content": "hola"}])

        fake_http.post.assert_called_once()
        call_args = fake_http.post.call_args
        self.assertEqual(call_args.args[0], "chat/completions")
        self.assertEqual(call_args.kwargs["json"]["model"], "Llama3_8B_Cosmos")
        self.assertIsNone(call_args.kwargs["headers"])

    def test_health_check_uses_v1_relative_endpoint(self):
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None

        fake_http = Mock()
        fake_http.get.return_value = fake_response

        client = LLMClient(base_url="http://127.0.0.1:8002/v1", inference_semaphore=FakeSemaphore())
        client._client = fake_http

        self.assertTrue(client.health_check())
        fake_http.get.assert_called_once_with("health")


if __name__ == "__main__":
    unittest.main()
