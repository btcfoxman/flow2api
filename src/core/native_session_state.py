"""Opt-in ownership of a server-local authenticated Native profile.

The marker contains no cookies or tokens. It is installed by an operator only
after independently logging into this exact profile; imported cookies must not
overwrite that profile's device-bound credentials.
"""
import hashlib
import json
import os
from pathlib import Path
from .generation_errors import NativeSessionError

LOCAL_SESSION_MARKER = '.flow2api-local-session.json'


def native_profile_path(token_id: int) -> Path:
    root = Path(os.environ.get('NATIVE_CDP_PROFILE_ROOT') or 'tmp/native_cdp_profiles')
    return root.resolve() / f'token-{int(token_id)}'


def local_session_state(token_id: int, profile_dir=None):
    marker = Path(profile_dir or native_profile_path(token_id)) / LOCAL_SESSION_MARKER
    if not marker.exists():
        return None
    try:
        state = json.loads(marker.read_text(encoding='utf-8'))
        if (state.get('version') != 1 or state.get('token_id') != int(token_id)
                or not isinstance(state.get('proxy_sha256'), str)
                or len(state['proxy_sha256']) != 64):
            raise ValueError('invalid marker')
        return state
    except (OSError, ValueError, TypeError, AttributeError):
        # A corrupt marker must not silently re-enable external Cookie imports.
        raise NativeSessionError('local_session_state_invalid', protocol='angular') from None


def validate_local_session_proxy(state, proxy_url: str):
    if state and state['proxy_sha256'] != hashlib.sha256(proxy_url.encode()).hexdigest():
        raise NativeSessionError('local_session_proxy_changed', protocol='angular')
