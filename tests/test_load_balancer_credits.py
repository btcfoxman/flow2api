import unittest

from src.core.config import config
from src.core.credits import (
    DEFAULT_MIN_GENERATION_CREDITS,
    get_minimum_generation_credits,
)
from src.core.models import Token
from src.services.load_balancer import LoadBalancer


def make_token(token_id: int, credits: int) -> Token:
    return Token(
        id=token_id,
        st=f"st-{token_id}",
        at=f"at-{token_id}",
        email=f"user{token_id}@example.com",
        credits=credits,
        image_enabled=True,
        video_enabled=True,
    )


class FakeTokenManager:
    def __init__(self, tokens):
        self.tokens = tokens
        self.ensure_calls = []

    async def get_active_tokens(self):
        return list(self.tokens)

    def needs_at_refresh(self, token):
        return False

    async def ensure_valid_token(self, token):
        self.ensure_calls.append(token.id)
        return token


class LoadBalancerCreditsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_minimum_credits = config.minimum_generation_credits
        config.set_minimum_generation_credits(DEFAULT_MIN_GENERATION_CREDITS)

    async def asyncTearDown(self):
        config.set_minimum_generation_credits(self.original_minimum_credits)

    async def test_default_minimum_generation_credits_is_fifteen(self):
        self.assertEqual(DEFAULT_MIN_GENERATION_CREDITS, 15)
        self.assertEqual(get_minimum_generation_credits(), 15)

    async def test_select_token_skips_accounts_below_minimum_credits(self):
        threshold = get_minimum_generation_credits()
        low = make_token(1, threshold - 1)
        enough = make_token(2, threshold)
        token_manager = FakeTokenManager([low, enough])
        balancer = LoadBalancer(token_manager)

        selected = await balancer.select_token(for_video_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, enough.id)
        self.assertEqual(token_manager.ensure_calls, [enough.id])

    async def test_select_token_returns_none_when_all_accounts_are_below_minimum_credits(self):
        token_manager = FakeTokenManager([
            make_token(1, 0),
            make_token(2, get_minimum_generation_credits() - 1),
        ])
        balancer = LoadBalancer(token_manager)

        selected = await balancer.select_token(for_video_generation=True)
        reason = await balancer.get_unavailable_reason(for_video_generation=True)

        self.assertIsNone(selected)
        self.assertEqual(token_manager.ensure_calls, [])
        self.assertIn(str(get_minimum_generation_credits()), reason)

    async def test_runtime_configuration_changes_selection_threshold(self):
        config.set_minimum_generation_credits(8)
        below = make_token(1, 7)
        enough = make_token(2, 8)
        token_manager = FakeTokenManager([below, enough])
        balancer = LoadBalancer(token_manager)

        selected = await balancer.select_token(for_video_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, enough.id)
        self.assertEqual(token_manager.ensure_calls, [enough.id])


if __name__ == "__main__":
    unittest.main()
