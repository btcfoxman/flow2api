"""Credential-free google.rpc.Status decoding from the Flow frontend schema.

Source: Flow cJ/uQa/PublicAitkError decoders captured on 2026-09-11.
Only allowlisted public reasons are retained; status messages/metadata are not.
"""
import base64
import binascii


PUBLIC_ERRORS = {
    0: "PUBLIC_ERROR_UNSPECIFIED", 1: "PUBLIC_ERROR_UNSAFE_GENERATION",
    2: "PUBLIC_ERROR_USER_QUOTA_REACHED", 3: "PUBLIC_ERROR_USER_REQUESTS_THROTTLED",
    4: "PUBLIC_ERROR_HIGH_TRAFFIC", 5: "PUBLIC_ERROR_MODEL_DISABLED_DUE_TO_TRAFFIC",
    7: "PUBLIC_ERROR_NON_ENGLISH_PROMPT", 8: "PUBLIC_ERROR_UNSAFE_IMAGE_UPLOAD",
    9: "PUBLIC_ERROR_GENERATION_ALREADY_IN_PROGRESS", 11: "PUBLIC_ERROR_MODEL_ACCESS_DENIED",
    12: "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED", 14: "PUBLIC_ERROR_VIOLENCE_FILTER",
    15: "PUBLIC_ERROR_DANGER_FILTER", 16: "PUBLIC_ERROR_SEXUAL", 17: "PUBLIC_ERROR_MINOR_UPLOAD",
    18: "PUBLIC_ERROR_SEXUAL_UPLOAD", 19: "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD",
    20: "PUBLIC_ERROR_PHOTOREAL_UPLOAD", 21: "PUBLIC_ERROR_MINOR", 22: "PUBLIC_ERROR_MINOR_HARM_UPLOAD",
    23: "PUBLIC_ERROR_PHOTOREAL_INPUT_IMAGE", 24: "PUBLIC_ERROR_PROMINENT_PEOPLE_INPUT_IMAGE",
    25: "PUBLIC_ERROR_MINOR_INPUT_IMAGE", 26: "PUBLIC_ERROR_UNKNOWN_IMAGE_FILE_FORMAT",
    27: "PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT", 28: "PUBLIC_ERROR_MODEL_OVERLOADED",
    29: "PUBLIC_ERROR_HANDLE_INVALID", 30: "PUBLIC_ERROR_HANDLE_TAKEN", 31: "PUBLIC_ERROR_IMAGE_TOO_LARGE",
    32: "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED", 33: "PUBLIC_ERROR_AUDIO_FILTERED",
    34: "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED_UPGRADEABLE", 35: "PUBLIC_ERROR_SOMETHING_WENT_WRONG",
    36: "PUBLIC_ERROR_COLLECTION_INVALID", 39: "PUBLIC_ERROR_COLLECTION_FULL",
    40: "PUBLIC_ERROR_COLLECTION_DEPTH_EXCEEDED", 41: "PUBLIC_ERROR_IP_INPUT_IMAGE",
    42: "PUBLIC_ERROR_APP_UNAVAILABLE", 43: "PUBLIC_BOTTLE_CONTENT_WARNING",
    44: "PUBLIC_ERROR_IMAGE_TOO_SMALL", 45: "PUBLIC_ERROR_USER_REGION_DISALLOWED",
    46: "PUBLIC_ERROR_USER_MINOR", 47: "PUBLIC_ERROR_IMAGE_OUTPUT_IP_FILTER",
    48: "PUBLIC_ERROR_WORKSPACE_ACCOUNT_QUOTA_REACHED", 49: "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
    50: "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC", 51: "PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED",
    52: "PUBLIC_ERROR_MEDIA_GENERATION_CANNOT_BE_CANCELED", 53: "PUBLIC_ERROR_UNSAFE_VIDEO_UPLOAD",
    54: "PUBLIC_ERROR_VIDEO_UPLOAD_TIMEOUT", 55: "PUBLIC_ERROR_VIDEO_DURATION_TOO_LONG",
    56: "PUBLIC_ERROR_APPLET_SIZE_LIMIT_EXCEEDED", 57: "PUBLIC_ERROR_APPLET_STORAGE_QUOTA_EXCEEDED",
    58: "PUBLIC_ERROR_VIDEO_UPLOAD_FAILED", 59: "PUBLIC_ERROR_SPEECH_EDIT", 60: "PUBLIC_ERROR_VIDEO_EDIT",
    61: "PUBLIC_ERROR_REPUTATIONAL", 63: "PUBLIC_ERROR_UNDERSPECIFIED_ANIMAL",
    64: "PUBLIC_ERROR_RESPONSE_TOO_LONG", 65: "PUBLIC_ERROR_PROJECT_NOT_ALLOWLISTED",
    66: "PUBLIC_ERROR_USER_ROLE_NOT_GRANTED", 67: "PUBLIC_ERROR_PROJECT_FLOW_ENTERPRISE_NOT_ENABLED",
    68: "PUBLIC_ERROR_VERTEX_SAFETY_FILTER_TRIGGERED", 69: "PUBLIC_ERROR_VERTEX_UNSUPPORTED_INPUT",
    70: "PUBLIC_ERROR_VERTEX_QUOTA_EXHAUSTED", 71: "PUBLIC_ERROR_VERTEX_QUOTA_EXHAUSTED_ADMIN",
    72: "PUBLIC_ERROR_VERTEX_SERVICE_UNAVAILABLE", 73: "PUBLIC_ERROR_UNSAFE_SEARCH",
}
PUBLIC_ERROR_NAMES = frozenset(PUBLIC_ERRORS.values())
POLICY_ERRORS = frozenset(PUBLIC_ERRORS[code] for code in
                         {1, 8, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 33, 41, 47, 53, 68})
TRAFFIC_ERRORS = frozenset(PUBLIC_ERRORS[code] for code in {3, 4, 5, 9, 28, 49, 50, 51})
ERROR_INFO = "google.rpc.ErrorInfo"
PUBLIC_AITK_ERROR = "google.internal.labs.aisandbox.proto.common.v1.PublicAitkError"


def _proto_first_field(encoded, expected_wire):
    """Read just field 1 of an Any; reject malformed/duplicate encodings."""
    if not isinstance(encoded, str) or len(encoded) > 65536:
        return None
    try:
        data = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        pos = 0
        values = []

        def varint():
            nonlocal pos
            value = 0
            for shift in range(0, 70, 7):
                byte = data[pos]
                pos += 1
                value |= (byte & 127) << shift
                if not byte & 128:
                    return value
            raise ValueError('invalid varint')

        while pos < len(data):
            tag = varint()
            field, wire = tag >> 3, tag & 7
            if field < 1:
                return None
            if wire == 0:
                value = varint()
            elif wire in {1, 2, 5}:
                size = varint() if wire == 2 else (8 if wire == 1 else 4)
                if size > len(data) - pos:
                    return None
                value = data[pos:pos + size]
                pos += size
            else:
                return None
            if field == 1:
                if wire != expected_wire:
                    return None
                values.append(value)
        return values[0] if len(values) == 1 else None
    except (ValueError, IndexError, binascii.Error):
        return None


def decode_rpc_status(status):
    """Return safe fields, not a verdict about whether a launch was accepted."""
    if (not isinstance(status, list) or not status or type(status[0]) is not int
            or not 1 <= status[0] <= 16):
        return {}
    if len(status) > 1 and status[1] is not None and not isinstance(status[1], str):
        return {}
    if len(status) > 2 and status[2] is not None and not isinstance(status[2], list):
        return {}
    result = {"grpc_code": status[0]}
    details = status[2] if len(status) > 2 else None
    reasons = set()
    if isinstance(details, list):
        for detail in details[:32]:
            if not isinstance(detail, list) or len(detail) != 2 or not isinstance(detail[0], str):
                continue
            name = detail[0].rsplit('/', 1)[-1]
            if name not in {ERROR_INFO, PUBLIC_AITK_ERROR}:
                continue
            value = detail[1]
            if isinstance(value, list):
                first = value[0] if value else None
            else:
                first = _proto_first_field(value, 2 if name == ERROR_INFO else 0)
                if isinstance(first, bytes):
                    try:
                        first = first.decode('utf-8')
                    except UnicodeError:
                        first = None
            if name == PUBLIC_AITK_ERROR:
                first = PUBLIC_ERRORS.get(first) if type(first) is int else None
            if isinstance(first, str) and first in PUBLIC_ERROR_NAMES:
                reasons.add(first)
    # Some media Status protos put the public reason in message field 2.
    # Retain only exact enum names, never the rest of an upstream message.
    if len(status) > 1 and isinstance(status[1], str):
        import re
        reasons.update(name for name in re.findall(r'\bPUBLIC_ERROR_[A-Z_]+\b', status[1])
                       if name in PUBLIC_ERROR_NAMES)
    if len(reasons) == 1:
        result['public_error'] = reasons.pop()
    elif reasons:
        result['reason_conflict'] = True
    return result
