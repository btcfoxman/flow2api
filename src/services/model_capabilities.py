"""Protocol capability checks independent of current account availability."""
from ..core.config import config
from ..core.logger import debug_logger
from .flow_angular import IMAGE_MODELS, resolve_video_model

UNSUPPORTED_MODEL_MESSAGE = "当前服务暂不支持此模型的生成方式，请选择其他已支持的模型。"


def supports_flow_model(model_config):
    # Do not charge for a base generation when its requested upsample stage
    # has no verified native transport yet.
    if model_config.get("upsample"):
        return False
    if model_config.get("type") == "image":
        return model_config.get("model_name") in IMAGE_MODELS
    return resolve_video_model(model_config.get("model_key")) is not None


async def uses_flow_only_protocol(db):
    """A capability snapshot, not a balance, cooldown or login health check."""
    if config.captcha_method != "native_cdp":
        return False
    # Do not confuse a rejected login, cooldown or empty balance with a missing
    # adapter. Inspect configured enabled accounts, not the scheduler's choices.
    accounts = await db.get_active_tokens()
    return bool(accounts) and all(getattr(account, "auth_mode", "labs") == "flow" for account in accounts)


def log_capability_rejection(model, known_models, *, stage, request_id=None):
    """Record only a validated canonical model, never arbitrary request fields."""
    debug_logger.log_runtime_event(
        "generation_capability_rejected", stage=stage,
        reason="model_transport_unavailable", status_code=501,
        model=model if isinstance(model, str) and model in known_models else "unsupported",
        **({"request_id": request_id} if request_id else {}),
    )


async def model_transport_error(db, model_config):
    if supports_flow_model(model_config):
        return None
    if await uses_flow_only_protocol(db):
        return {"error": {"message": UNSUPPORTED_MODEL_MESSAGE, "type": "invalid_request_error",
                          "code": "model_not_supported", "status_code": 501}}
    return None
