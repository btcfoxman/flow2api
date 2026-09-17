"""Opt-in ownership of a server-local authenticated Native profile.

The marker contains no cookies or tokens. It records ownership, NOT login
success: it is installed before interactive login so external imports cannot
overwrite credentials while the operator is authenticating.
"""
import hashlib
import json
import os
import tempfile
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


def claim_local_session(token_id: int, proxy_url: str, profile_dir=None):
    """Persist ownership atomically; never replace an existing proxy binding."""
    folder = Path(profile_dir or native_profile_path(token_id))
    previous = local_session_state(token_id, folder)
    validate_local_session_proxy(previous, proxy_url)
    if previous:
        return previous
    folder.mkdir(parents=True, exist_ok=True)
    state = {'version': 1, 'token_id': int(token_id),
             'proxy_sha256': hashlib.sha256(proxy_url.encode()).hexdigest()}
    fd, filename = tempfile.mkstemp(prefix='.local-session-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, folder / LOCAL_SESSION_MARKER)
    finally:
        Path(filename).unlink(missing_ok=True)
    return state
