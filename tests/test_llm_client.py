import sys
import types
import unittest

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
sys.modules.setdefault("httpx", types.SimpleNamespace(Client=object))

from app.llm_client import _extract_json_message


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


if __name__ == "__main__":
    unittest.main()
