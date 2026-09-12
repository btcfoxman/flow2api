"""Protocol capability checks independent of current account availability."""
from ..core.config import config
from .flow_angular import IMAGE_MODELS, resolve_video_model

UNSUPPORTED_MODEL_MESSAGE = "当前服务暂不支持此模型的生成方式，请选择其他已支持的模型。"


def supports_flow_model(model_config):
    if model_config.get("type") == "image":
        return model_config.get("model_name") in IMAGE_MODELS and not model_config.get("upsample")
    return resolve_video_model(model_config.get("model_key")) is not None


async def model_transport_error(db, model_config):
    if config.captcha_method != "native_cdp" or supports_flow_model(model_config):
        return None
    # Do not confuse a rejected login, cooldown or empty balance with a missing
    # adapter. Inspect configured enabled accounts, not the scheduler's choices.
    accounts = await db.get_active_tokens()
    if accounts and all(getattr(account, "auth_mode", "labs") == "flow" for account in accounts):
        return {"error": {"message": UNSUPPORTED_MODEL_MESSAGE, "type": "invalid_request_error",
                          "code": "model_not_supported", "status_code": 501}}
    return None
