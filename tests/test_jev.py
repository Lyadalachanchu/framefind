"""No-network tests of actual wire format, validation, concurrency, and retries."""

import asyncio
from email.parser import BytesParser
from email.policy import default
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.jev import JevClient, JevError
from app.taxonomy import LABELS


def profile_response():
    return {"model": "jev-1.14.0", "usage": {"input_tokens": 250}, "answers": {
        label["id"]: {"type": "noul", "noul": 0.7} for label in LABELS
    }}


class JevTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.frame = Path(self.directory.name) / "sample.jpg"
        self.frame.write_bytes(b"\xff\xd8test-image\xff\xd9")
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.directory.cleanup()

    def client(self, handler, **kwargs):
        client = JevClient("test-secret-only", transport=httpx.MockTransport(handler), **kwargs)
        self.clients.append(client)
        return client

    async def test_multipart_index_has_single_referenced_asset_and_all_questions(self):
        async def handler(request):
            self.assertEqual(request.url, "https://api.typesafe.ai/v1/systemone")
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret-only")
            self.assertEqual(request.headers["TypeSafe-Danger-Zone-Images"], "true")
            raw = b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + await request.aread()
            parts = list(BytesParser(policy=default).parsebytes(raw).iter_parts())
            self.assertEqual(parts[0].get_param("name", header="content-disposition"), "request")
            payload = json.loads(parts[0].get_payload(decode=True))
            self.assertEqual(len(parts), 2)
            reference = payload["state"]["frame"]
            self.assertEqual(payload["assets"][reference]["part"], "frame-0")
            self.assertEqual(parts[1].get_payload(decode=True), self.frame.read_bytes())
            self.assertEqual(set(payload["questions"]), {label["id"] for label in LABELS})
            self.assertTrue(all(reference not in question["instructions"] for question in payload["questions"].values()))
            return httpx.Response(200, json=profile_response())
        result = await self.client(handler).index_frame(self.frame)
        self.assertEqual(len(result["probabilities"]), len(LABELS))
        self.assertEqual(result["input_tokens"], 250)

    async def test_query_profile_uses_json_and_preserves_query(self):
        async def handler(request):
            payload = json.loads(await request.aread())
            self.assertEqual(payload["state"]["query"], "a red car in the snow")
            self.assertNotIn("assets", payload)
            return httpx.Response(200, json=profile_response())
        result = await self.client(handler).query_profile(" a red car in the snow ")
        self.assertEqual(result["probabilities"]["car"], 0.7)

    async def test_match_frames_normalizes_score(self):
        async def handler(request):
            return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {"input_tokens": 100},
                "answers": {"match": {"type": "score", "score": 2.4}}})
        result = await self.client(handler).match_frames([self.frame, self.frame], "red car")
        self.assertAlmostEqual(result["score"], 0.8)

    async def test_retries_rate_limit_and_server_error_honoring_retry_after(self):
        attempts = 0
        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429 if attempts == 1 else 529, headers={"retry-after": "0.125"})
            return httpx.Response(200, json=profile_response())
        with patch("app.jev.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.client(handler).query_profile("car")
        self.assertEqual(attempts, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.125, 0.125])

    async def test_success_keeps_conservative_usage_for_server_error_retries(self):
        statuses = iter([500, 429, 503, 200])
        async def handler(request):
            status = next(statuses)
            if status != 200:
                return httpx.Response(status, headers={"retry-after": "0"})
            return httpx.Response(200, json=profile_response())
        result = await self.client(handler).query_profile("car")
        self.assertEqual(result["input_tokens"], 250)
        self.assertEqual(result["uncertain_input_tokens"], 2 * 65536)

    async def test_long_retry_after_does_not_retry_early(self):
        handler = AsyncMock(return_value=httpx.Response(429, headers={"retry-after": "120"}))
        with self.assertRaises(JevError) as context:
            await self.client(handler).query_profile("car")
        self.assertEqual(handler.await_count, 1)
        self.assertEqual(context.exception.retry_after, 120)

    async def test_errors_do_not_echo_provider_body_or_key(self):
        handler = AsyncMock(return_value=httpx.Response(401, json={"error": "test-secret-only;base64-private"}))
        with self.assertRaises(JevError) as context:
            await self.client(handler).query_profile("car")
        self.assertNotIn("test-secret-only", str(context.exception))
        self.assertNotIn("base64-private", str(context.exception))
        self.assertEqual(handler.await_count, 1)

    async def test_missing_answers_and_bad_probability_are_rejected(self):
        for bad in (None, float("inf"), -1, 1.1, True):
            result = profile_response()
            if bad is None:
                del result["answers"]["person"]
            else:
                result["answers"]["person"]["noul"] = bad
            # Infinity is deliberately serialized to exercise client validation.
            handler = AsyncMock(return_value=httpx.Response(200, content=json.dumps(result)))
            with self.assertRaises(JevError):
                await self.client(handler).query_profile("car")

    async def test_shared_concurrency_limit_covers_index_and_search(self):
        active, maximum = 0, 0
        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json=profile_response())
        client = self.client(handler, concurrency=2)
        await asyncio.gather(*(client.index_frame(self.frame) if i % 2 else client.query_profile("car") for i in range(8)))
        self.assertEqual(maximum, 2)

    def test_taxonomy_has_unique_stable_ids(self):
        self.assertGreaterEqual(len(LABELS), 80)
        self.assertLessEqual(len(LABELS), 100)
        self.assertEqual(len({label["id"] for label in LABELS}), len(LABELS))
        self.assertTrue(all(label["keywords"] for label in LABELS))


if __name__ == "__main__":
    unittest.main()
