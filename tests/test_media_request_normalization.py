import unittest

from src.api.routes import (
    _normalize_gemini_request,
    _normalize_openai_request,
    _normalize_video_create_payload,
)
from src.core.models import ChatCompletionRequest, GeminiGenerateContentRequest


SCAFFOLDED_PROMPT = """You are a function calling AI model.
You are provided with function signatures within <tools></tools> XML tags.
<tools>
{"type":"function","function":{"name":"generate","parameters":{"$schema":"http://json-schema.org"}}}
</tools>
Here are the available tools:
A quiet city street at sunrise.
"""

CLEAN_PROMPT = "A quiet city street at sunrise."


class MediaRequestNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_image_request_strips_tool_scaffolding(self):
        request = ChatCompletionRequest.model_validate(
            {
                "model": "gemini-3.0-pro-image-landscape",
                "messages": [
                    {
                        "role": "user",
                        "content": SCAFFOLDED_PROMPT,
                    }
                ],
            }
        )

        normalized = await _normalize_openai_request(request)

        self.assertEqual(normalized.prompt, CLEAN_PROMPT)

    async def test_video_create_payload_strips_tool_scaffolding(self):
        normalized = await _normalize_video_create_payload(
            {
                "model": "veo_3_1_t2v_lite_landscape",
                "prompt": SCAFFOLDED_PROMPT,
            }
        )

        self.assertEqual(normalized.prompt, CLEAN_PROMPT)

    async def test_gemini_media_request_keeps_existing_sanitizer_behavior(self):
        request = GeminiGenerateContentRequest.model_validate(
            {
                "systemInstruction": {
                    "parts": [{"text": SCAFFOLDED_PROMPT}],
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": SCAFFOLDED_PROMPT}],
                    }
                ],
            }
        )

        normalized = await _normalize_gemini_request(
            "gemini-3.0-pro-image-landscape",
            request,
        )

        self.assertEqual(normalized.prompt, CLEAN_PROMPT)


if __name__ == "__main__":
    unittest.main()
