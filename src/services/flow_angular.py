"""Flow Angular wire adapter, based on the 2026-09-08 browser capture.

Generation is opt-in per model or verified model family.
Page bootstrap and credentials are read in the same browser that submits the RPC.
"""
from dataclasses import dataclass
import json
import re
import uuid
from urllib.parse import quote, urlsplit, parse_qsl


class AngularProtocolError(RuntimeError):
    pass


class AngularSubmissionUncertain(AngularProtocolError):
    """A launch may exist upstream; do not retry or switch transports."""


RPC_IDS = {"MZZa6b", "jIps6", "ogiZ0b", "jwpduf", "as29s", "ngNC2", "UpteDb"}
VIDEO_FAMILIES = frozenset({"abra_r2v", "abra_edit"})
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
    match = re.fullmatch(r"(abra_r2v_(?:4|6|8|10)s|abra_edit)(?:_(360p|720p))?", model)
    if match:
        base, resolution = match.groups()
        family = "abra_edit" if base == "abra_edit" else "abra_r2v"
        return VideoModel(
            model_key=base + ("_360p" if resolution == "360p" else ""),
            family=family,
            rpc_id="jIps6" if family == "abra_edit" else "MZZa6b",
            resolution=4 if resolution == "360p" else 1,
        )
    if model == "veo_3_1_r2v_fast_portrait":
        return VideoModel(model, "veo_reference", "MZZa6b", 1)
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


def parse_rpc_response(text: str, rpc_id: str):
    # Decode JSON rather than slicing by character count: frame lengths count
    # UTF-8 bytes, and captured/redacted fixtures can change the declared length.
    raw = text.lstrip()
    if raw.startswith(")]}'"):
        raw = raw[4:]
    decoder = json.JSONDecoder()
    payloads = []
    while raw.strip():
        raw = raw.lstrip()
        length = re.match(r"\d+\r?\n", raw)
        if length:
            raw = raw[length.end():].lstrip()
        try:
            frame, end = decoder.raw_decode(raw)
        except ValueError:
            raise AngularProtocolError("Malformed Flow RPC response") from None
        raw = raw[end:]
        if not isinstance(frame, list):
            raise AngularProtocolError("Invalid Flow RPC frame")
        for row in frame:
            if not isinstance(row, list) or len(row) < 3 or row[:2] != ["wrb.fr", rpc_id]:
                continue
            if not isinstance(row[2], str):
                raise AngularProtocolError(f"Flow RPC {rpc_id} returned an error envelope")
            try:
                payloads.append(json.loads(row[2]))
            except ValueError:
                raise AngularProtocolError("Malformed Flow RPC payload") from None
    if len(payloads) != 1:
        raise AngularProtocolError(f"Flow RPC {rpc_id} response missing or ambiguous")
    return payloads[0]


def project_context(project_id, captcha):
    return [None, 22, None, None, None, project_id, None, None, None, None, [captcha, 1]]


def build_video_rpc(rest):
    requests = rest.get("requests") or []
    if len(requests) != 1:
        raise AngularProtocolError("Angular generation currently requires a single output")
    request = requests[0]
    spec = resolve_video_model(request.get("videoModelKey"))
    if spec is None:
        raise AngularProtocolError("Angular model wire shape is not verified")
    output = request.get("outputSpec")
    if output is not None:
        if not isinstance(output, dict) or set(output) - {"resolution"}:
            raise AngularProtocolError("Unsupported Angular output specification")
        resolution = output.get("resolution")
        if resolution is not None and VIDEO_RESOLUTIONS.get(resolution) != spec.resolution:
            raise AngularProtocolError("Angular output resolution does not match the model")
    default_aspect = ("VIDEO_ASPECT_RATIO_PORTRAIT" if spec.family == "veo_reference"
                      else "VIDEO_ASPECT_RATIO_LANDSCAPE")
    aspect = VIDEO_ASPECT_RATIOS.get(request.get("aspectRatio", default_aspect))
    if aspect is None:
        raise AngularProtocolError("Unsupported Angular video aspect ratio")
    context = rest.get("clientContext") or {}
    project = context.get("projectId")
    captcha = (context.get("recaptchaContext") or {}).get("token")
    if not project or not captcha:
        raise AngularProtocolError("Flow project and fresh captcha are required")
    parts = request.get("textInput", {}).get("structuredPrompt", {}).get("parts", [])
    if not parts or any(set(part) != {"text"} for part in parts):
        raise AngularProtocolError("Unsupported Angular prompt shape")
    prompt = "".join(part["text"] for part in parts)
    refs = [[None, item["mediaId"]] for item in request.get("referenceImages", [])]
    batch = (rest.get("mediaGenerationContext") or {}).get("batchId") or str(uuid.uuid4())
    ids = [None, None, None, None, str(uuid.uuid4()), str(uuid.uuid4())]
    text_input = [None, None, [[[prompt]]]]
    if spec.family == "abra_edit":
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
    # field 12 (references) / 13 (edit) to [4] for 360p. Do not copy the
    # captured 360p tail to every resolution. See the 2026-09-09 source record.
    if spec.resolution != 1:
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
        # Unknown failure/cancellation enums must never be counted as success.
        status_name = {1: "SUCCESSFUL", 2: "ACTIVE", 3: "SUCCESSFUL", 6: "PENDING"}.get(code)
        if status_name is None:
            raise AngularProtocolError(f"Unrecognized Flow media status {code}")
        if code == 1 and expected_ids is None:
            continue  # User uploads are not newly submitted generation tasks.
        operation = {"name": media_id}
        if code in {1, 3}:
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
