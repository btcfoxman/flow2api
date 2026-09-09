import unittest
from unittest.mock import AsyncMock

from src.core.credits import has_minimum_generation_credits, normalize_credits_response
from src.services.flow_client import FlowClient

TIER = {"serviceTier":"SERVICE_TIER_INTERMEDIATE", "sku":"G1_TIER1", "userPaygateTier":"PAYGATE_TIER_ONE"}


class ZeroCreditSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_authenticated_zero_balance_is_normalized(self):
        client = FlowClient(None)
        client._make_request = AsyncMock(return_value=TIER.copy())
        result = await client.get_credits("fake-at")
        self.assertEqual(result, {**TIER, "credits": 0})
        self.assertNotIn("credits", TIER)
        self.assertFalse(has_minimum_generation_credits(result['credits'], 1))

    def test_missing_or_error_responses_are_not_authentication_proof(self):
        for payload in [None, [], {}, {"sku":"G1_TIER1"},
                        {**TIER, "error":{}}, {**TIER,"serviceTier":None},
                        {**TIER,"userPaygateTier":"invalid"}, {**TIER,"sku":""}]:
            with self.subTest(payload=payload):
                self.assertIs(normalize_credits_response(payload), payload)

    def test_explicit_balance_is_never_replaced_by_default(self):
        for value in [0, 15, -1, True, None, "invalid"]:
            payload={**TIER,"credits":value}
            self.assertIs(normalize_credits_response(payload), payload)
