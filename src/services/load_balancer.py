"""Load balancing module for Flow2API"""
import asyncio
import random
from typing import Collection, Optional, Dict
from ..core.models import Token
from ..core.config import config
from ..core.credits import (
    get_minimum_generation_credits,
    has_minimum_generation_credits,
    normalize_credits,
)
from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .concurrency_manager import ConcurrencyManager
from ..core.logger import debug_logger


class LoadBalancer:
    """Token load balancer with load-aware selection"""

    def __init__(self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager
        self._image_pending: Dict[int, int] = {}
        self._video_pending: Dict[int, int] = {}
        self._video_proxy_pending: Dict[str, int] = {}
        self._video_proxy_pending_keys: Dict[int, list[str]] = {}
        self._pending_credits: Dict[int, int] = {}
        self._pending_lock = asyncio.Lock()
        self._round_robin_state: Dict[str, Optional[int]] = {"image": None, "video": None, "default": None}
        self._rr_lock = asyncio.Lock()

    async def _get_pending_count(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> int:
        async with self._pending_lock:
            if for_image_generation:
                return max(0, int(self._image_pending.get(token_id, 0)))
            if for_video_generation:
                return max(0, int(self._video_pending.get(token_id, 0)))
            return 0

    async def _get_pending_credits(self, token_id: int) -> int:
        async with self._pending_lock:
            return max(0, int(self._pending_credits.get(token_id, 0)))

    async def _add_pending(
        self,
        token_id: int,
        for_image_generation: bool,
        for_video_generation: bool,
        *,
        credit_cost: int = 0,
        available_credits: Optional[int] = None,
        video_proxy_key: Optional[str] = None,
    ) -> bool:
        """Atomically add pending load and, when known, reserve model credits."""
        normalized_cost = max(0, normalize_credits(credit_cost))
        async with self._pending_lock:
            if (
                for_video_generation
                and video_proxy_key
                and int(self._video_proxy_pending.get(video_proxy_key, 0)) > 0
            ):
                return False
            reserved_credits = max(0, int(self._pending_credits.get(token_id, 0)))
            if (
                normalized_cost > 0
                and available_credits is not None
                and normalize_credits(available_credits) - reserved_credits < normalized_cost
            ):
                return False

            if for_image_generation:
                self._image_pending[token_id] = max(0, int(self._image_pending.get(token_id, 0))) + 1
            elif for_video_generation:
                self._video_pending[token_id] = max(0, int(self._video_pending.get(token_id, 0))) + 1
                if video_proxy_key:
                    self._video_proxy_pending[video_proxy_key] = 1
                    self._video_proxy_pending_keys.setdefault(token_id, []).append(
                        video_proxy_key
                    )
            if normalized_cost > 0:
                self._pending_credits[token_id] = reserved_credits + normalized_cost
            return True

    async def release_pending(
        self,
        token_id: int,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        *,
        credit_cost: Optional[int] = None,
    ):
        async with self._pending_lock:
            if for_image_generation:
                current = max(0, int(self._image_pending.get(token_id, 0)))
                if current <= 1:
                    self._image_pending.pop(token_id, None)
                else:
                    self._image_pending[token_id] = current - 1
            elif for_video_generation:
                current = max(0, int(self._video_pending.get(token_id, 0)))
                if current <= 1:
                    self._video_pending.pop(token_id, None)
                else:
                    self._video_pending[token_id] = current - 1
                proxy_keys = self._video_proxy_pending_keys.get(token_id) or []
                if proxy_keys:
                    proxy_key = proxy_keys.pop(0)
                    self._video_proxy_pending.pop(proxy_key, None)
                    if not proxy_keys:
                        self._video_proxy_pending_keys.pop(token_id, None)

            normalized_cost = max(0, normalize_credits(credit_cost))
            if normalized_cost > 0:
                reserved_credits = max(0, int(self._pending_credits.get(token_id, 0)))
                remaining_credits = max(0, reserved_credits - normalized_cost)
                if remaining_credits:
                    self._pending_credits[token_id] = remaining_credits
                else:
                    self._pending_credits.pop(token_id, None)

    async def _is_video_proxy_pending(self, proxy_key: Optional[str]) -> bool:
        if not proxy_key:
            return False
        async with self._pending_lock:
            return int(self._video_proxy_pending.get(proxy_key, 0)) > 0

    async def _get_native_video_proxy_state(self, token: Token) -> Dict[str, object]:
        """Resolve a credential-free proxy group and its current quarantine state."""
        from .browser_captcha_native_cdp import (
            BrowserCaptchaService,
            _proxy_egress_key,
        )

        token_proxy_url = str(getattr(token, "captcha_proxy_url", "") or "").strip()
        service = BrowserCaptchaService._instance
        if service is None:
            db = getattr(self.token_manager, "db", None)
            if db is not None:
                service = await BrowserCaptchaService.get_instance(db)
        if service is not None:
            return await service.get_video_proxy_state(
                int(token.id),
                token_proxy_url=token_proxy_url or None,
            )
        if token_proxy_url:
            proxy_key = _proxy_egress_key(token_proxy_url)
            return {
                "proxy_key": proxy_key,
                "proxy_fingerprint": proxy_key[:12],
                "available": True,
                "failure_streak": 0,
                "cooldown_remaining_seconds": 0,
            }
        raise RuntimeError("native_cdp proxy route is unavailable")

    async def get_video_proxy_diagnostics(self, token: Token) -> Optional[Dict[str, object]]:
        """Return safe route metadata suitable for request performance logs."""
        if str(getattr(config, "captcha_method", "") or "").strip().lower() != "native_cdp":
            return None
        state = await self._get_native_video_proxy_state(token)
        return {
            "fingerprint": str(state.get("proxy_fingerprint") or ""),
            "failure_streak": int(state.get("failure_streak") or 0),
            "cooldown_remaining_seconds": int(
                state.get("cooldown_remaining_seconds") or 0
            ),
        }

    async def _get_token_load(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> tuple[int, Optional[int]]:
        """获取 token 当前负载。

        Returns:
            (inflight, remaining)
            remaining 为 None 表示无限制
        """
        if not self.concurrency_manager:
            return 0, None

        if for_image_generation:
            inflight = await self.concurrency_manager.get_image_inflight(token_id)
            remaining = await self.concurrency_manager.get_image_remaining(token_id)
            pending = await self._get_pending_count(token_id, True, False)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        if for_video_generation:
            inflight = await self.concurrency_manager.get_video_inflight(token_id)
            remaining = await self.concurrency_manager.get_video_remaining(token_id)
            pending = await self._get_pending_count(token_id, False, True)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        return 0, None

    async def _reserve_slot(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> bool:
        """尝试为当前 token 预占一个生成槽位。"""
        if not self.concurrency_manager:
            return True

        if for_image_generation:
            return await self.concurrency_manager.acquire_image(token_id)

        if for_video_generation:
            return await self.concurrency_manager.acquire_video(token_id)

        return True

    async def _select_round_robin(self, tokens: list[dict], scenario: str) -> Optional[dict]:
        """Select candidate in round-robin order for the given scenario."""
        if not tokens:
            return None

        tokens_sorted = sorted(tokens, key=lambda item: item["token"].id or 0)
        async with self._rr_lock:
            last_id = self._round_robin_state.get(scenario)
            start_idx = 0
            if last_id is not None:
                for idx, item in enumerate(tokens_sorted):
                    if item["token"].id == last_id:
                        start_idx = (idx + 1) % len(tokens_sorted)
                        break
            selected = tokens_sorted[start_idx]
            self._round_robin_state[scenario] = selected["token"].id
        return selected

    async def _check_extension_route(self, token: Token) -> tuple[bool, str]:
        """Ensure extension captcha requests are routed to the selected account."""
        if config.captcha_method != "extension":
            return True, ""

        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            service = await ExtensionCaptchaService.get_instance(getattr(self.token_manager, "db", None))
            has_connection, route_key = await service.has_connection_for_token(token.id)
            if has_connection:
                return True, ""

            available = service.describe_routes() or "none"
            if route_key:
                return False, f"扩展路由 {route_key} 未连接（可用路由: {available}）"
            return False, f"扩展路由未配置或匿名插件未连接（可用路由: {available}）"
        except Exception as exc:
            return False, f"扩展路由检查失败: {exc}"

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        reserve: bool = False,
        enforce_concurrency_filter: bool = True,
        track_pending: bool = False,
        exclude_token_ids: Optional[Collection[int]] = None,
        minimum_credits: Optional[int] = None,
    ) -> Optional[Token]:
        """
        Select a token using load-aware balancing

        Args:
            for_image_generation: If True, only select tokens with image_enabled=True
            for_video_generation: If True, only select tokens with video_enabled=True
            model: Model name (used to filter tokens for specific models)
            reserve: Whether to atomically reserve one concurrency slot for the selected token
            enforce_concurrency_filter:
                Whether to pre-filter tokens by current inflight/remaining capacity.
                For reserve=False generation paths, this should usually be False so
                requests can enter the downstream wait queue instead of failing fast.
            track_pending:
                Whether to count the selected token as a queued request immediately.
                This smooths burst distribution before the hard concurrency slot is acquired.
            exclude_token_ids:
                Token IDs already attempted by the current request. They remain excluded
                even when their cached credits still look sufficient.
            minimum_credits:
                Exact credits required by the resolved model. When omitted, use the
                administrator-configured fallback threshold.

        Returns:
            Selected token or None if no available tokens
        """
        debug_logger.log_info(
            f"[LOAD_BALANCER] 开始选择Token (图片生成={for_image_generation}, "
            f"视频生成={for_video_generation}, 模型={model}, 预占槽位={reserve})"
        )

        active_tokens = await self.token_manager.get_active_tokens()
        debug_logger.log_info(f"[LOAD_BALANCER] 获取到 {len(active_tokens)} 个活跃Token")

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有活跃的Token")
            return None

        available_tokens = []
        filtered_reasons = {}
        required_tier = get_required_paygate_tier_for_model(model)
        has_exact_credit_cost = minimum_credits is not None
        minimum_credits = (
            get_minimum_generation_credits()
            if minimum_credits is None
            else max(0, int(minimum_credits))
        )
        pending_credit_cost = minimum_credits if has_exact_credit_cost else 0
        excluded_ids = {
            int(token_id)
            for token_id in (exclude_token_ids or ())
            if token_id is not None
        }

        for token in active_tokens:
            video_proxy_state = None
            if token.id in excluded_ids:
                filtered_reasons[token.id] = "excluded after a failed attempt in this request"
                continue
            reserved_credits = await self._get_pending_credits(token.id)
            effective_credits = max(0, normalize_credits(token.credits) - reserved_credits)
            if not has_minimum_generation_credits(effective_credits, minimum_credits):
                filtered_reasons[token.id] = (
                    f"effective credits below {minimum_credits} "
                    f"(stored={normalize_credits(token.credits)}, reserved={reserved_credits})"
                )
                continue

            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                filtered_reasons[token.id] = '账号等级不足，需要 ' + get_paygate_tier_label(required_tier)
                continue
            if for_image_generation:
                if not token.image_enabled:
                    filtered_reasons[token.id] = "图片生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_route(token)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_image(token.id)
                ):
                    filtered_reasons[token.id] = "图片并发已满"
                    continue

            if for_video_generation:
                if not token.video_enabled:
                    filtered_reasons[token.id] = "视频生成已禁用"
                    continue

                if str(getattr(config, "captcha_method", "") or "").strip().lower() == "native_cdp":
                    try:
                        video_proxy_state = await self._get_native_video_proxy_state(token)
                    except Exception as exc:
                        filtered_reasons[token.id] = f"native proxy route unavailable: {exc}"
                        continue
                    proxy_key = str(video_proxy_state.get("proxy_key") or "")
                    proxy_fingerprint = str(
                        video_proxy_state.get("proxy_fingerprint") or proxy_key[:12]
                    )
                    if not bool(video_proxy_state.get("available", True)):
                        retry_after = int(
                            video_proxy_state.get("cooldown_remaining_seconds") or 0
                        )
                        filtered_reasons[token.id] = (
                            f"proxy {proxy_fingerprint} cooling down ({retry_after}s)"
                        )
                        continue
                    if await self._is_video_proxy_pending(proxy_key):
                        filtered_reasons[token.id] = (
                            f"proxy {proxy_fingerprint} already has an active video task"
                        )
                        continue

                route_ok, route_reason = await self._check_extension_route(token)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_video(token.id)
                ):
                    filtered_reasons[token.id] = "视频并发已满"
                    continue

            inflight, remaining = await self._get_token_load(
                token.id,
                for_image_generation=for_image_generation,
                for_video_generation=for_video_generation
            )
            available_tokens.append({
                "token": token,
                "inflight": inflight,
                "remaining": remaining,
                "needs_refresh": self.token_manager.needs_at_refresh(token),
                "reserved_credits": reserved_credits,
                "effective_credits": effective_credits,
                "video_proxy_key": (
                    str(video_proxy_state.get("proxy_key") or "")
                    if video_proxy_state
                    else None
                ),
                "random": random.random()
            })

        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] 已过滤Token:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")

        if not available_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有可用的Token (图片生成={for_image_generation}, 视频生成={for_video_generation})")
            return None

        # 最低 in-flight 优先；有并发上限时，剩余槽位更多的 token 优先；最后随机打散
        call_mode = config.call_logic_mode
        if call_mode == "polling":
            scenario = "default"
            if for_image_generation:
                scenario = "image"
            elif for_video_generation:
                scenario = "video"

            ordered_candidates = []
            first_candidate = await self._select_round_robin(available_tokens, scenario)
            if first_candidate is not None:
                ordered_candidates.append(first_candidate)
                ordered_candidates.extend(
                    item for item in sorted(available_tokens, key=lambda item: item["token"].id or 0)
                    if item["token"].id != first_candidate["token"].id
                )
            available_tokens = ordered_candidates
        else:
            available_tokens.sort(
                key=lambda item: (
                    1 if item["needs_refresh"] else 0,
                    item["inflight"],
                    0 if item["remaining"] is None else 1,
                    -(item["remaining"] or 0),
                    item["random"]
                )
            )

        ready_candidates = [item for item in available_tokens if not item["needs_refresh"]]
        refresh_candidates = [item for item in available_tokens if item["needs_refresh"]]
        if ready_candidates and refresh_candidates:
            available_tokens = ready_candidates + refresh_candidates

        debug_logger.log_info("[LOAD_BALANCER] 候选Token负载:")
        for item in available_tokens:
            token = item["token"]
            remaining = "unlimited" if item["remaining"] is None else item["remaining"]
            debug_logger.log_info(
                f"[LOAD_BALANCER]   - Token {token.id} ({token.email}) "
                f"inflight={item['inflight']}, remaining={remaining}, "
                f"needs_refresh={item['needs_refresh']}, credits={token.credits}, "
                f"reserved_credits={item['reserved_credits']}, "
                f"effective_credits={item['effective_credits']}"
            )

        # 只为候选列表中真正尝试到的 token 做 AT 校验，避免每次请求把所有 token 全扫一遍
        for item in available_tokens:
            token = item["token"]
            token_id = token.id

            token = await self.token_manager.ensure_valid_token(token)
            if not token:
                debug_logger.log_info(f"[LOAD_BALANCER] 跳过 Token {token_id}: AT无效或已过期")
                continue

            if track_pending:
                pending_added = await self._add_pending(
                    token.id,
                    for_image_generation,
                    for_video_generation,
                    credit_cost=pending_credit_cost,
                    available_credits=token.credits,
                    video_proxy_key=item.get("video_proxy_key"),
                )
                if not pending_added:
                    debug_logger.log_info(
                        f"[LOAD_BALANCER] 跳过 Token {token.id}: "
                        f"pending credit/proxy reservation failed "
                        f"(required_credits={pending_credit_cost})"
                    )
                    continue

            if reserve and not await self._reserve_slot(token.id, for_image_generation, for_video_generation):
                if track_pending:
                    await self.release_pending(
                        token.id,
                        for_image_generation=for_image_generation,
                        for_video_generation=for_video_generation,
                        credit_cost=pending_credit_cost,
                    )
                debug_logger.log_info(f"[LOAD_BALANCER] 跳过 Token {token.id}: 预占槽位失败")
                continue

            debug_logger.log_info(
                f"[LOAD_BALANCER] ✅ 已选择Token {token.id} ({token.email}) - "
                f"余额: {token.credits}, inflight={item['inflight']}"
            )
            return token

        debug_logger.log_info(f"[LOAD_BALANCER] ❌ 候选Token均不可用 (图片生成={for_image_generation}, 视频生成={for_video_generation})")
        return None

    async def get_unavailable_reason(
        self,
        *,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        minimum_credits: Optional[int] = None,
    ) -> Optional[str]:
        """给出更明确的“无可用账号”原因，优先用于分辨率/tier 档位提示。"""
        active_tokens = await self.token_manager.get_active_tokens()
        if not active_tokens:
            return None

        required_tier = get_required_paygate_tier_for_model(model)
        supported_tokens = []
        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                continue
            supported_tokens.append(token)

        if model and not supported_tokens:
            tier_label = get_paygate_tier_label(required_tier)
            return f"当前模型需要 {tier_label} 账号，但没有可用的 {tier_label} 账号: {model}"

        capability_tokens = []
        for token in supported_tokens:
            if for_image_generation and not token.image_enabled:
                continue
            if for_video_generation and not token.video_enabled:
                continue
            capability_tokens.append(token)

        minimum_credits = (
            get_minimum_generation_credits()
            if minimum_credits is None
            else max(0, int(minimum_credits))
        )
        funded_tokens = []
        for token in capability_tokens:
            reserved_credits = await self._get_pending_credits(token.id)
            effective_credits = max(0, normalize_credits(token.credits) - reserved_credits)
            if has_minimum_generation_credits(effective_credits, minimum_credits):
                funded_tokens.append(token)
        if capability_tokens and not funded_tokens:
            return (
                f"\u5f53\u524d\u7b26\u5408\u6761\u4ef6\u7684\u8d26\u53f7"
                f"\u989d\u5ea6\u5747\u4f4e\u4e8e {minimum_credits}"
                "\uff0c\u5df2\u505c\u6b62\u8c03\u7528\u4f4e\u989d\u5ea6"
                "\u8d26\u53f7\u3002\u8bf7\u5237\u65b0\u989d\u5ea6"
                "\u6216\u8865\u5145\u53ef\u7528\u8d26\u53f7\u3002"
            )

        if (
            for_video_generation
            and funded_tokens
            and str(getattr(config, "captcha_method", "") or "").strip().lower() == "native_cdp"
        ):
            blocked_states = []
            for token in funded_tokens:
                try:
                    state = await self._get_native_video_proxy_state(token)
                except Exception:
                    blocked_states.append((True, 0))
                    continue
                proxy_key = str(state.get("proxy_key") or "")
                cooling_down = not bool(state.get("available", True))
                proxy_pending = await self._is_video_proxy_pending(proxy_key)
                blocked_states.append(
                    (
                        cooling_down or proxy_pending,
                        int(state.get("cooldown_remaining_seconds") or 0),
                    )
                )
            if blocked_states and all(blocked for blocked, _ in blocked_states):
                retry_after = min(
                    (seconds for _, seconds in blocked_states if seconds > 0),
                    default=0,
                )
                if retry_after:
                    return f"当前可用账号的代理出口正在风险冷却，请约 {retry_after} 秒后重试。"
                return "当前可用账号的代理出口已有视频任务，任务结束后即可继续提交。"

        if supported_tokens and not capability_tokens:
            if for_image_generation:
                return "当前有符合档位的账号，但图片生成功能已全部禁用。"
            if for_video_generation:
                return "当前有符合档位的账号，但视频生成功能已全部禁用。"

        return None
