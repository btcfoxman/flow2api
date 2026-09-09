"""Read-only pre restart guard; run inside the current container before replacement.

This is a final precondition check, not an atomic ingress drain. If it refuses,
wait for accepted work / the operator's login session to finish and deploy again.
"""
import json
import sqlite3
import urllib.request
from pathlib import Path


def check_metrics(body):
    expected = {"flow2api_image_inflight_total", "flow2api_video_inflight_total"}
    seen = set()
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, value, *_ = line.split()
        if name in expected:
            seen.add(name)
            if float(value) != 0:
                raise RuntimeError("Generation is still in flight; deployment refused")
    if seen != expected:
        raise RuntimeError("Missing generation metrics; deployment refused")


def main():
    state_path = Path('/app/tmp/native-login-43/state.json')
    if state_path.exists():
        state = json.loads(state_path.read_text())
        for role in ('helper_pid', 'chrome_pid'):
            pid = int(state.get(role, 0))
            path = Path(f'/proc/{pid}/cmdline')
            if pid > 1 and path.exists():
                command = path.read_bytes()
                if b'native-api-canary' in command or b'native-login-43' in command or b'/token-43' in command:
                    raise RuntimeError("Diagnostic browser owner is still active; deployment refused")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:4020/metrics', timeout=10) as response:
        check_metrics(response.read().decode())
    with sqlite3.connect('file:/app/data/flow.db?mode=ro', uri=True) as db:
        submitting = db.execute("SELECT count(*) FROM async_task_queue WHERE status='submitting'").fetchone()[0]
        processing = db.execute("SELECT count(*) FROM tasks WHERE status='processing' AND created_at > datetime('now','-2 hours')").fetchone()[0]
        if submitting or processing:
            raise RuntimeError("Submission or accepted task is still active; deployment refused")
    print('Pre restart checks passed (not a media-generation health verdict)')


if __name__ == '__main__':
    main()
