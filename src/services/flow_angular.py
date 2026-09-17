"""Flow Angular wire adapter, based on the 2026-09-08 browser capture.

Generation is opt-in per model or verified model family.
Page bootstrap and credentials are read in the same browser that submits the RPC.
"""
from dataclasses import dataclass
import base64
import json
import re
import uuid
from urllib.parse import quote, urlsplit, parse_qsl
from .flow_rpc_errors import decode_rpc_status, POLICY_ERRORS, TRAFFIC_ERRORS


class AngularProtocolError(RuntimeError):
    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class AngularSubmissionUncertain(AngularProtocolError):
    """A launch may exist upstream; do not retry or switch transports."""


class AngularRpcRejected(AngularProtocolError):
    """One unambiguous RPC error frame with a definitive rejection status."""

    def __init__(self, diagnostic):
        self.grpc_code = diagnostic['grpc_code']
        self.public_error = diagnostic.get('public_error')
        self.status_code = {3: 400, 5: 404, 7: 403, 8: 429, 9: 400, 11: 400, 12: 501, 16: 401}[self.grpc_code]
        if self.public_error in POLICY_ERRORS:
            self.status_code = 400
        elif self.public_error in TRAFFIC_ERRORS:
            self.status_code = 429
        elif self.public_error == 'PUBLIC_ERROR_USER_QUOTA_REACHED':
            self.status_code = 503
        elif self.public_error in {'PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED',
                                   'PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED_UPGRADEABLE',
                                   'PUBLIC_ERROR_MODEL_ACCESS_DENIED'}:
            # A per-model limit must not quarantine every model on this proxy,
            # nor overwrite the account's remaining credits with zero.
            self.status_code = 403
        super().__init__(f"Flow RPC rejected: HTTP Error {self.status_code}; "
                         f"{self.public_error or 'RPC_REJECTED'}", diagnostics=diagnostic)


VIDEO_GENERATION_RPC_IDS = frozenset({"YhhmEf", "MZZa6b", "jIps6", "nprQif"})
GENERATION_RPC_IDS = VIDEO_GENERATION_RPC_IDS | {"ogiZ0b"}
MUTATING_RPC_IDS = GENERATION_RPC_IDS | {"jHPbke", "maseQ"}
RPC_IDS = MUTATING_RPC_IDS | {"jwpduf", "as29s", "ngNC2", "UpteDb", "nzlxg"}
IMAGE_MODELS = frozenset({"GEM_PIX_2", "NARWHAL"})
IMAGE_ASPECT_RATIOS = {"IMAGE_ASPECT_RATIO_SQUARE": 1, "IMAGE_ASPECT_RATIO_PORTRAIT": 2,
                      "IMAGE_ASPECT_RATIO_LANDSCAPE": 3, "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": 4,
                      "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": 5}
FLOW_RPC_URL = "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute"
FLOW_IDENTITY_EXPRESSION = """(() => {
  const w = window.WIZ_global_data || {};
  return {origin: location.origin, path: location.pathname,
    bootstrap: !!(w.SNlM0e && w.cfb2h && w.FdrFJe), email: w.oPEP7c || ''};
})()"""


def verified_flow_email(snapshot, expected_email=""):
    """Identity comes from the target's authenticated page, never client claims."""
    if (not isinstance(snapshot, dict) or snapshot.get("origin") != "https://flow.google.com"
            or not snapshot.get("bootstrap") or str(snapshot.get("path", "")).startswith("/about")):
        raise AngularProtocolError("Flow login unavailable")
    email = str(snapshot.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 320:
        raise AngularProtocolError("Flow account identity unavailable")
    if expected_email and email != expected_email.strip().lower():
        raise AngularProtocolError("Flow account identity mismatch")
    return email


def flow_credits(payload):
    """Current GetCredits proto: credits=1, paygate tier=2, SKU=3.

    From the 2026-09-11 frontend service (nzlxg). Protobuf omits zero;
    require an authenticated tier so an arbitrary empty response fails closed.
    """
    tiers = {5: "ZERO", 1: "ONE", 7: "GEMNOVA", 2: "TWO", 3: "NOT_PAID",
             4: "UNSUBSCRIBED_WITH_CREDITS", 6: "EXEMPT", 8: "TIER1P5"}
    if (not isinstance(payload, list) or len(payload) < 2 or isinstance(payload[1], bool)
            or not isinstance(payload[1], int) or payload[1] not in tiers):
        raise AngularProtocolError("Unrecognized Flow credit response")
    credits = payload[0] if payload[0] is not None else 0
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or not 0 <= credits < 10**12:
        raise AngularProtocolError("Invalid Flow credit balance")
    return {"credits": int(credits), "userPaygateTier": "PAYGATE_TIER_" + tiers[payload[1]]}


def flow_projects(payload):
    if not isinstance(payload, list):
        raise AngularProtocolError("Invalid Flow project list")
    rows = payload[0] if payload and payload[0] is not None else []
    if not isinstance(rows, list):
        raise AngularProtocolError("Invalid Flow project list")
    projects = []
    for row in rows:
        if (not isinstance(row, list) or len(row) < 2 or not isinstance(row[0], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", row[0])
                or not isinstance(row[1], list) or not row[1] or not isinstance(row[1][0], str)):
            raise AngularProtocolError("Unrecognized Flow project entry")
        projects.append({"project_id": row[0], "project_name": row[1][0]})
    return projects
VIDEO_FAMILIES = frozenset({"abra_t2v", "abra_r2v", "abra_edit"})
VIDEO_RESOLUTIONS = {"VIDEO_RESOLUTION_360P": 4, "VIDEO_RESOLUTION_720P": 1}
VIDEO_ASPECT_RATIOS = {"VIDEO_ASPECT_RATIO_LANDSCAPE": 2, "VIDEO_ASPECT_RATIO_PORTRAIT": 1}


@dataclass(frozen=True)
class VideoModel:
    model_key: str
    family: str
    rpc_id: str
    resolution: int


def resolve_video_model(model):
    """Resolve public aliases without expanding beyond the supported catalog."""
    if not isinstance(model, str):
        return None
    match = re.fullmatch(r"(abra_(?:t2v|r2v)_(?:4|6|8|10)s|abra_edit)(?:_(360p|720p))?", model)
    if match:
        base, resolution = match.groups()
        family = "abra_edit" if base == "abra_edit" else base.rsplit("_", 1)[0]
        return VideoModel(
            model_key=base + ("_360p" if resolution == "360p" else ""),
            family=family,
            rpc_id={"abra_edit": "jIps6", "abra_r2v": "MZZa6b", "abra_t2v": "YhhmEf"}[family],
            resolution=4 if resolution == "360p" else 1,
        )
    # 2026-09-17 MZZa6b captures and HTrJv catalog. Do not enroll
    # relaxed/low-priority or T2V/I2V models just because they share a prefix.
    if (model == "veo_3_1_r2v_lite"
            or re.fullmatch(r"veo_3_1_r2v_fast_(?:landscape|portrait)(?:_ultra)?", model)):
        return VideoModel(model, "veo_reference", "MZZa6b", 1)
    match = re.fullmatch(r"(omni_flash_i2v_(?:4|6|8|10)s_first_last)(?:_(360p|720p))?", model)
    if match:
        base, resolution = match.groups()
        return VideoModel(base + ("_360p" if resolution == "360p" else ""),
                          "omni_first_last", "nprQif", 4 if resolution == "360p" else 1)
    return None


def use_angular_video(model, *, models=(), families=()):
    # Preserve exact opt-in (including preflight errors for unsupported models).
    # A 360p-only opt-in must not silently enroll its 720p counterpart.
    if model in models:
        return True
    spec = resolve_video_model(model)
    if spec is None:
        return False
    if spec.family in VIDEO_FAMILIES and spec.family in families:
        return True
    return any(resolve_video_model(enabled) == spec for enabled in models)


def wire_shape(value, depth=0):
    """Bounded type-only evidence; never retain scalar values or object keys."""
    if isinstance(value, list):
        if depth >= 2:
            return f"list({len(value)})"
        return f"list({len(value)})[" + ",".join(wire_shape(v, depth + 1) for v in value[:12]) + "]"
    return {str: "str", int: "int", float: "float", bool: "bool",
            dict: "object", type(None): "null"}.get(type(value), "other")


def parse_rpc_response(text: str, rpc_id: str):
    # Decode JSON rather than slicing by character count: frame lengths count
    # UTF-8 bytes, and captured/redacted fixtures can change the declared length.
    diagnostics = {"response_bytes": len(text.encode("utf-8")) if isinstance(text, str) else 0,
                   "frame_count": 0, "matching_rows": 0}

    def fail(reason, message, value=None):
        raise AngularProtocolError(message, diagnostics={
            **diagnostics, "parse_reason": reason, "wire_shape": wire_shape(value)}) from None

    if not isinstance(text, str):
        fail("response_type_invalid", "Invalid Flow RPC response")
    raw = text.lstrip()
    if raw.startswith(")]}'"):
        raw = raw[4:]
    decoder = json.JSONDecoder()
    payloads = []
    error_rows = []
    while raw.strip():
        raw = raw.lstrip()
        length = re.match(r"\d+\r?\n", raw)
        if length:
            raw = raw[length.end():].lstrip()
        try:
            frame, end = decoder.raw_decode(raw)
        except ValueError:
            fail("frame_json_invalid", "Malformed Flow RPC response")
        raw = raw[end:]
        diagnostics["frame_count"] += 1
        if not isinstance(frame, list):
            fail("frame_type_invalid", "Invalid Flow RPC frame", frame)
        for row in frame:
            if not isinstance(row, list) or len(row) < 3 or row[:2] != ["wrb.fr", rpc_id]:
                continue
            diagnostics["matching_rows"] += 1
            if not isinstance(row[2], str):
                error_rows.append(row)
                continue
            if len(row) > 5 and row[5] is not None:
                fail("payload_ambiguous", "Flow RPC contains both data and error", row)
            try:
                payloads.append(json.loads(row[2]))
            except ValueError:
                fail("payload_json_invalid", "Malformed Flow RPC payload", row)
    if diagnostics["matching_rows"] != 1:
        fail("payload_missing" if not diagnostics["matching_rows"] else "payload_ambiguous",
             "Flow RPC response missing or ambiguous")
    if error_rows:
        row = error_rows[0]
        # Dq/wrb.fr is a sentinel-prefixed proto: field 5 is row[5].
        status = decode_rpc_status(row[5]) if row[2] is None and len(row) > 5 else {}
        diagnostic = {**diagnostics, 'parse_reason': 'rpc_error_status',
                      'wire_shape': wire_shape(row), **status}
        # DEADLINE_EXCEEDED, UNKNOWN, INTERNAL, UNAVAILABLE and ALREADY_EXISTS
        # can describe an accepted operation. Never blindly resubmit those.
        if status.get('grpc_code') in {3, 5, 7, 8, 9, 11, 12, 16} and not status.get('reason_conflict'):
            raise AngularRpcRejected(diagnostic)
        if status:
            raise AngularProtocolError("Flow RPC outcome is unconfirmed", diagnostics=diagnostic)
        fail("payload_not_string", "Flow RPC returned an error envelope", row)
    return payloads[0]


def project_context(project_id, captcha):
    return [None, 22, None, None, None, project_id, None, None, None, None, [captcha, 1]]


def build_create_project_rpc(title):
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise AngularProtocolError("Invalid Flow project title")
    return "jHPbke", ["projects/*", [None, [title.strip()]], [None, 22]]


def created_project(payload):
    try:
        projects = flow_projects([[payload]])
    except AngularProtocolError:
        raise AngularSubmissionUncertain("Flow project creation result is unconfirmed; do not automatically recreate") from None
    if len(projects) != 1:
        raise AngularSubmissionUncertain("Flow project creation result is unconfirmed")
    return projects[0]


def build_image_upload_rpc(project_id, captcha, image_bytes, mime_type, filename):
    """maseQ /FlowService.UploadImage, observed and source-checked 2026-09-11."""
    if not project_id or not captcha:
        raise AngularProtocolError("Image upload requires a project and fresh UPLOAD_IMAGE captcha")
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise AngularProtocolError("Image upload requires nonempty image bytes")
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise AngularProtocolError("Unsupported Flow upload image type")
    if not isinstance(filename, str) or not filename or any(c in filename for c in ("/", "\\", "\x00")):
        raise AngularProtocolError("Invalid Flow upload filename")
    return "maseQ", [project_context(project_id, captcha), base64.b64encode(image_bytes).decode("ascii"),
                     mime_type, 1, None, None, None, None, filename, None,
                     str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()]


def uploaded_image_id(payload, project_id):
    if not isinstance(payload, list) or len(payload) < 2:
        raise AngularSubmissionUncertain("Flow image upload result is unconfirmed")
    media, workflow = payload[:2]
    if (not isinstance(media, list) or len(media) < 7 or not isinstance(media[0], str) or not media[0]
            or media[1] != project_id or not isinstance(workflow, list) or len(workflow) < 5
            or not isinstance(media[2], str) or not media[2] or media[2] != workflow[0]
            or workflow[4] != project_id):
        raise AngularSubmissionUncertain("Flow image upload account/project binding is unconfirmed")
    return media[0]


def video_frame_crop(image_bytes, aspect_ratio):
    """Website-style centered crop, without decoding/re-encoding image pixels."""
    from io import BytesIO
    from PIL import Image

    target = {"VIDEO_ASPECT_RATIO_LANDSCAPE": 16 / 9,
              "VIDEO_ASPECT_RATIO_PORTRAIT": 9 / 16}.get(aspect_ratio)
    if target is None:
        raise AngularProtocolError("Unsupported Angular video aspect ratio")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
            if image.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
    except (OSError, ValueError, TypeError):
        raise AngularProtocolError("Cannot read video frame dimensions") from None
    crop = {"top": 0, "left": 0, "bottom": 1, "right": 1}
    ratio = width / height
    # Generated images can be rounded to latent-grid dimensions (1376x768
    # in the capture); the website keeps the full frame for this small delta.
    if abs(ratio / target - 1) <= 0.01:
        return crop
    if ratio > target:
        crop["left"] = (1 - target / ratio) / 2
        crop["right"] = 1 - crop["left"]
    else:
        crop["top"] = (1 - ratio / target) / 2
        crop["bottom"] = 1 - crop["top"]
    return crop


def video_frame(image):
    """Start/end image message, including normalized crop (field 6)."""
    if (not isinstance(image, dict) or set(image) - {"mediaId", "cropCoordinates"}
            or not isinstance(image.get("mediaId"), str) or not image["mediaId"]):
        raise AngularProtocolError("First/last video requires both frame media IDs")
    crop = image.get("cropCoordinates", {"top": 0, "left": 0, "bottom": 1, "right": 1})
    if not isinstance(crop, dict) or set(crop) != {"top", "left", "bottom", "right"}:
        raise AngularProtocolError("Invalid video frame crop")
    values = [crop[key] for key in ("top", "left", "bottom", "right")]
    if (any(type(v) not in (int, float) or not 0 <= v <= 1 for v in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise AngularProtocolError("Invalid video frame crop")
    return [None, image["mediaId"], None, None, None, [v if v else None for v in values]]


def build_video_rpc(rest):
    requests = rest.get("requests") or []
    if len(requests) != 1:
        raise AngularProtocolError("Angular generation currently requires a single output")
    request = requests[0]
    spec = resolve_video_model(request.get("videoModelKey"))
    if spec is None:
        raise AngularProtocolError("Angular model wire shape is not verified")
    if spec.family == "abra_t2v" and any(request.get(key) for key in
            ("referenceImages", "videoInput", "startImage", "endImage", "imageInputs")):
        raise AngularProtocolError("Text video RPC does not accept image or video inputs")
    output = request.get("outputSpec")
    if output is not None:
        if not isinstance(output, dict) or set(output) - {"resolution"}:
            raise AngularProtocolError("Unsupported Angular output specification")
        resolution = output.get("resolution")
        if resolution is not None and VIDEO_RESOLUTIONS.get(resolution) != spec.resolution:
            raise AngularProtocolError("Angular output resolution does not match the model")
    default_aspect = ("VIDEO_ASPECT_RATIO_PORTRAIT" if "_portrait" in spec.model_key
                      else "VIDEO_ASPECT_RATIO_LANDSCAPE")
    aspect = VIDEO_ASPECT_RATIOS.get(request.get("aspectRatio", default_aspect))
    if aspect is None:
        raise AngularProtocolError("Unsupported Angular video aspect ratio")
    if spec.family == "veo_reference" and "_fast_" in spec.model_key:
        if aspect != (1 if "_portrait" in spec.model_key else 2):
            raise AngularProtocolError("Angular aspect ratio does not match the model")
    context = rest.get("clientContext") or {}
    project = context.get("projectId")
    captcha = (context.get("recaptchaContext") or {}).get("token")
    if not project or not captcha:
        raise AngularProtocolError("Flow project and fresh captcha are required")
    parts = request.get("textInput", {}).get("structuredPrompt", {}).get("parts", [])
    if not parts or any(not isinstance(part, dict) or set(part) != {"text"}
                        or not isinstance(part["text"], str) for part in parts):
        raise AngularProtocolError("Unsupported Angular prompt shape")
    prompt = "".join(part["text"] for part in parts)
    reference_images = request.get("referenceImages", [])
    if not isinstance(reference_images, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("mediaId"), str)
            or not item["mediaId"] for item in reference_images):
        raise AngularProtocolError("Invalid reference image media IDs")
    refs = [[None, item["mediaId"]] for item in reference_images]
    if spec.family == "veo_reference" and len(refs) > 3:
        raise AngularProtocolError("Veo reference video accepts at most three images")
    if spec.family in {"veo_reference", "abra_r2v"} and any(
            request.get(key) for key in ("startImage", "endImage", "videoInput", "imageInputs")):
        raise AngularProtocolError("Reference video RPC does not accept frame or video inputs")
    batch = (rest.get("mediaGenerationContext") or {}).get("batchId") or str(uuid.uuid4())
    ids = [None, None, None, None, str(uuid.uuid4()), str(uuid.uuid4())]
    text_input = [None, None, [[[prompt]]]]
    if spec.family == "abra_t2v":
        # YhhmEf / BatchAsyncGenerateVideoText, captured 2026-09-15.
        # Text/model/aspect/metadata are fields 1/2/3/5; no reference slot.
        item = [text_input, spec.model_key, aspect, None, ids]
        output_index = 7
    elif spec.family == "omni_first_last":
        if refs or request.get("videoInput") or request.get("imageInputs"):
            raise AngularProtocolError("First/last video RPC does not accept reference inputs")
        # nprQif / BatchAsyncGenerateVideoStartAndEndImage. Unlike Abra
        # reference/text requests, even 360p omits OutputSpec; model selects it.
        item = [text_input, spec.model_key, aspect, None,
                video_frame(request.get("startImage")), video_frame(request.get("endImage")), ids]
        output_index = None
    elif spec.family == "abra_edit":
        video = request.get("videoInput") or {}
        if not video.get("mediaId") or not video.get("endFrameIndex"):
            raise AngularProtocolError("Video edit requires media ID and end frame")
        item = [[None, video["mediaId"], video.get("startFrameIndex", 0), video["endFrameIndex"]],
                text_input, spec.model_key, aspect, ids, None, None, None, refs]
        output_index = 12
    else:
        if not refs:
            raise AngularProtocolError("Reference video RPC requires reference images")
        item = [text_input, refs, spec.model_key, aspect, None, ids]
        output_index = 11
    # Current frontend omits OutputSpec for default 720p (enum 1), and sets
    # field 8 (text) / 12 (references) / 13 (edit) to [4] for 360p.
    # See the 2026-09-09 and 2026-09-15 source records.
    if spec.resolution != 1 and output_index is not None:
        item.extend([None] * (output_index - len(item)))
        item.append([spec.resolution])
    return spec.rpc_id, [[item], project_context(project, captcha), [batch, 2]]


def media_rows(payload):
    if isinstance(payload, list):
        if len(payload) >= 8 and payload[3] == "CAE" and isinstance(payload[5], list):
            yield payload
        else:
            for child in payload:
                yield from media_rows(child)


def signed_video_url(media):
    # Observed video rendition URL: media[7][0][8]. Never synthesize the
    # unsigned /video/{id} URL: it returns 403 even after successful generation.
    video = media[7] if len(media) > 7 else None
    renditions = video if isinstance(video, list) else []
    for rendition in renditions:
        if not isinstance(rendition, list) or len(rendition) <= 8 or not isinstance(rendition[8], str):
            continue
        url = urlsplit(rendition[8])
        if (url.scheme == "https" and url.netloc == "flow-content.google"
                and url.path == "/video/" + quote(media[0], safe="")
                and {"Expires", "KeyName", "Signature"} <= {key for key, _ in parse_qsl(url.query)}):
            return rendition[8]
    return None


def video_operations(payload, *, token_id, project_id, expected_ids=None):
    operations = []
    for media in media_rows(payload):
        media_id = media[0]
        if not isinstance(media_id, str) or media[1] != project_id:
            raise AngularProtocolError("Flow response does not match the requested project")
        if expected_ids is not None and media_id not in expected_ids:
            continue
        status = media[5][8] if len(media[5]) > 8 else None
        code = status[0] if isinstance(status, list) and status else None
        # Official frontend nM / vL schema (2026-09-11): 1/2 pending,
        # 3 success, 4/7 failed, 5 canceled, 6 scheduled. Unknown fails closed.
        status_name = {1: "PENDING", 2: "ACTIVE", 3: "SUCCESSFUL", 4: "FAILED",
                       5: "CANCELLED", 6: "PENDING", 7: "FAILED"}.get(code) if type(code) is int else None
        if status_name is None:
            raise AngularProtocolError("Unrecognized Flow media status", diagnostics={
                "parse_reason": "media_status_unrecognized", "wire_shape": wire_shape(status)})
        operation = {"name": media_id}
        if code in {4, 5, 7}:
            # Do not expose arbitrary upstream Status text, which may contain
            # request data. The wire status is sufficient for terminal handling.
            operation["error"] = {"code": 1 if code == 5 else 13,
                                  "message": "视频生成已取消" if code == 5 else "视频生成失败，请稍后重试",
                                  "code_source": "local_normalization", "wire_status": code,
                                  "wire_shape": wire_shape(status)}
            detail = decode_rpc_status(status[1] if len(status) > 1 else None)
            if detail:
                operation["error"].update(detail)
                operation["error"]["code"] = detail['grpc_code']
                operation["error"]["code_source"] = "google_rpc_status"
                if detail.get('public_error'):
                    operation["error"]["message"] = detail['public_error']
        if code == 3:
            operation["metadata"] = {"video": {"mediaGenerationId": media_id}}
            video_url = signed_video_url(media)
            if video_url:
                operation["metadata"]["video"]["fifeUrl"] = video_url
        operations.append({"operation": operation, "mediaName": media_id, "projectId": project_id,
                           "workflowId": media[2], "sceneId": media[3], "status": "MEDIA_GENERATION_STATUS_" + status_name,
                           "transport": "angular", "tokenId": token_id})
    if not operations:
        raise AngularProtocolError("Flow RPC returned no matching media")
    if expected_ids is not None and {op["mediaName"] for op in operations} != set(expected_ids):
        raise AngularProtocolError("Flow polling response is incomplete")
    return {"operations": operations}


def build_image_rpc(rest):
    requests = rest.get("requests") or []
    if len(requests) != 1 or requests[0].get("imageModelName") not in IMAGE_MODELS:
        raise AngularProtocolError("Flow image model wire shape is not verified")
    request = requests[0]
    aspect = IMAGE_ASPECT_RATIOS.get(request.get("imageAspectRatio"))
    if aspect is None:
        raise AngularProtocolError("Unsupported Flow image aspect ratio")
    context = rest.get("clientContext") or {}
    project = context.get("projectId")
    captcha = (context.get("recaptchaContext") or {}).get("token")
    if not project or not captcha:
        raise AngularProtocolError("Fresh Flow project captcha is required")
    parts = request.get("structuredPrompt", {}).get("parts", [])
    if not parts or any(set(p) != {"text"} or not isinstance(p["text"],str) for p in parts):
        raise AngularProtocolError("Unsupported Flow image prompt")
    refs = []
    for item in request.get("imageInputs", []):
        if not item.get("name") or item.get("imageInputType") != "IMAGE_INPUT_TYPE_REFERENCE":
            raise AngularProtocolError("Unsupported Flow image reference")
        refs.append([item["name"],None,None,None,1])
    ctx = project_context(project,captcha)
    entry = [None,None,refs,request.get("seed"),aspect,request["imageModelName"],None,ctx,
             [[["".join(p["text"] for p in parts)]]],None,None,None,str(uuid.uuid4()),str(uuid.uuid4())]
    batch = (rest.get("mediaGenerationContext") or {}).get("batchId") or str(uuid.uuid4())
    return "ogiZ0b", [None,[entry],1,ctx,[batch]]


def image_result(payload, project_id):
    """Translate the captured synchronous image response, preserving signed URLs."""
    if (not isinstance(payload,list) or len(payload) < 2
            or not isinstance(payload[0],list) or not isinstance(payload[1],list)):
        raise AngularSubmissionUncertain("Flow image response is unrecognized; automatic resubmission is disabled")
    workflows = {w[0] for w in payload[1] if isinstance(w,list) and len(w)>4
                 and isinstance(w[0],str) and w[4] == project_id}
    images = []
    for row in payload[0]:
        if (not isinstance(row,list) or len(row)<7 or not isinstance(row[0],str)
                or not row[0] or not isinstance(row[2],str) or row[2] not in workflows):
            raise AngularSubmissionUncertain("Flow image project binding is unconfirmed")
        try:
            url = row[6][0][13]
            if not isinstance(url, str):
                raise ValueError()
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.netloc != "flow-content.google"
                    or parsed.path != "/image/" + quote(row[0],safe="")
                    or not {"Expires","KeyName","Signature"} <= {k for k,_ in parse_qsl(parsed.query)}):
                raise ValueError()
        except (IndexError,TypeError,ValueError):
            raise AngularSubmissionUncertain("Flow image result URL is unconfirmed") from None
        images.append({"name":row[0],"image":{"generatedImage":{"fifeUrl":url}}})
    if not images:
        raise AngularSubmissionUncertain("Flow image response contains no generated images")
    return {"media":images,"transport":"angular"}


def rpc_fetch_expression(rpc_id, payload, timeout):
    if rpc_id not in RPC_IDS:
        raise AngularProtocolError("Unsupported Flow RPC ID")
    # Values stay in the browser. SNlM0e is page XSRF, never OAuth access_token.
    args = json.dumps({"rpc": rpc_id, "payload": payload, "timeout": int(timeout * 1000)}, ensure_ascii=True)
    return """(async () => {
      const args = ARGS;
      if (location.origin !== 'https://flow.google.com') return {preflightError: 'Flow page is not authenticated'};
      const wiz = window.WIZ_global_data || {};
      const recent = performance.getEntriesByType('resource').filter(e => e.name.includes('/data/batchexecute')).slice(-1)[0];
      const observed = recent ? new URL(recent.name).searchParams : new URLSearchParams();
      const at = wiz.SNlM0e, bl = wiz.cfb2h || observed.get('bl'), sid = wiz.FdrFJe || observed.get('f.sid');
      if (!at || !bl || !sid) return {preflightError: 'Flow page bootstrap is unavailable; refresh the account session'};
      window.__flow2apiReqId = Math.max(window.__flow2apiReqId || 0, Number(observed.get('_reqid')) || 0) + 100000;
      const query = new URLSearchParams({rpcids: args.rpc, 'source-path': location.pathname,
        bl: String(bl), 'f.sid': String(sid), hl: document.documentElement.lang || 'en',
        _reqid: String(window.__flow2apiReqId), rt: 'c'});
      const body = new URLSearchParams({'f.req': JSON.stringify([[[args.rpc, JSON.stringify(args.payload), null, 'generic']]]), at: String(at)});
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), args.timeout);
      try {
        const response = await fetch('/_/AiSandboxAngularFrontend/data/batchexecute?' + query,
          {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'X-Same-Domain': '1'},
           body, credentials: 'include', signal: controller.signal, redirect: 'error'});
        return {status: response.status, text: await response.text()};
      } catch (_) { return {fetchError: 'Flow RPC transport interrupted'}; }
      finally { clearTimeout(timer); }
    })()""".replace("ARGS", args)
