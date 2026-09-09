"""Read-only probe of an already-open Flow CDP tab; never exports credentials."""
import argparse
import asyncio
import json
import sys
import tempfile
import urllib.request
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qsl
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.browser_captcha_native_cdp import CdpConnection, CdpProtocolError, FLOW_WEBSITE_KEY
from src.services.flow_angular import parse_rpc_response, rpc_fetch_expression, media_rows, build_video_rpc, video_operations


def select_flow_target(targets):
    pages = [t for t in targets if t.get("type") == "page"
             and urlsplit(t.get("url", "")).netloc == "flow.google.com"
             and urlsplit(t.get("url", "")).scheme == "https"]
    return next((t for t in pages if urlsplit(t["url"]).path.startswith("/project/")), pages[0] if pages else None)


def bound_session_summary(sessions):
    summary = []
    for session in sessions:
        site = urlsplit((session.get("key") or {}).get("site", "")).hostname
        if site not in {"google.com", "flow.google.com", "accounts.google.com", "labs.google"}:
            continue
        refresh = urlsplit(session.get("refreshUrl", ""))
        summary.append({"site":site, "refresh_host":refresh.hostname, "refresh_path":refresh.path,
                        "cookies":[{"name":c.get("name"), "domain":c.get("domain")} for c in session.get("cookieCravings", [])]})
    return {"google_session_count":len(summary), "sessions":summary}


class RpcBindingTrace:
    """Keep only RPC association and public DBSC usage enums, never headers."""
    def __init__(self):
        self.requests = set()
        self.usages = {}

    def request(self, event):
        data = event["params"]
        url = urlsplit(data.get("request", {}).get("url", ""))
        if url.netloc == "flow.google.com" and url.path == "/_/AiSandboxAngularFrontend/data/batchexecute":
            self.requests.add(data["requestId"])

    def extra(self, event):
        data = event["params"]
        if len(self.usages) >= 100:
            return
        self.usages[data["requestId"]] = [
            {"site":urlsplit(item.get("sessionKey", {}).get("site", "")).hostname, "usage":item.get("usage")}
            for item in data.get("deviceBoundSessionUsages", [])
            if urlsplit(item.get("sessionKey", {}).get("site", "")).hostname == "google.com"
        ]

    def summary(self):
        return [self.usages[request] for request in self.requests if request in self.usages]


async def inspect_bound_sessions(connection):
    observed = asyncio.Event()
    summary = {}
    def on_sessions(event):
        summary.update(bound_session_summary(event["params"].get("sessions", [])))
        observed.set()
    connection.add_handler("Network.deviceBoundSessionsAdded", on_sessions)
    try:
        # This enables DevTools event reporting, NOT the browser security feature.
        await connection.send("Network.enableDeviceBoundSessions", {"enable":True}, timeout=5)
        try:
            await asyncio.wait_for(observed.wait(), timeout=5)
        except asyncio.TimeoutError:
            return {"supported":True, "initial_event_received":False}
        return {"supported":True, "initial_event_received":True, **summary}
    except CdpProtocolError:
        return {"supported":False}
    finally:
        try:
            await connection.send("Network.enableDeviceBoundSessions", {"enable":False}, timeout=5)
        except (CdpProtocolError, ConnectionError, asyncio.TimeoutError):
            pass


async def main(port, reference=None, inspect_media=None, download_proxy=None, verify_native_proxy=None, fresh_tab=False, project_id=None, bound_sessions=False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.load(response)
    target = select_flow_target(targets)
    if not target:
        print(json.dumps({"source_session_ready":False, "action":"Open Flow in the source browser"}), flush=True)
        return
    path = urlsplit(target["url"]).path
    project = str(uuid.UUID(project_id)) if project_id else (path.split("/project/", 1)[1].split("/", 1)[0] if path.startswith("/project/") else None)
    if (reference or inspect_media or verify_native_proxy) and not project:
        raise ValueError("A project page or --project-id is required for media/native probes")
    connection = CdpConnection(target["webSocketDebuggerUrl"])
    await connection.connect()
    control = None
    owned_target = None
    if fresh_tab:
        control = connection
        page_url = "https://flow.google.com/project/" + project if project else target["url"]
        owned_target = (await control.send("Target.createTarget", {"url":page_url, "background":True}))["targetId"]
        await asyncio.sleep(5)
        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            target = next(t for t in json.load(response) if t["id"] == owned_target)
        connection = CdpConnection(target["webSocketDebuggerUrl"])
        await connection.connect()
    async def evaluate(expression):
        result = await connection.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, timeout=45)
        if result.get("exceptionDetails"):
            raise RuntimeError("Browser evaluation failed")
        return result["result"].get("value")
    try:
        binding_trace = RpcBindingTrace()
        if bound_sessions:
            connection.add_handler("Network.requestWillBeSent", binding_trace.request)
            connection.add_handler("Network.requestWillBeSentExtraInfo", binding_trace.extra)
            await connection.send("Network.enable", {})
        bootstrap = await evaluate("JSON.stringify({origin:location.origin, keys:Object.keys(window.WIZ_global_data||{}), at:!!(window.WIZ_global_data||{}).SNlM0e, bl:!!(window.WIZ_global_data||{}).cfb2h, sid:!!(window.WIZ_global_data||{}).FdrFJe})")
        print(bootstrap)
        rpc_id = "ngNC2" if project else "UpteDb"
        params = ["tools/PINHOLE/projects/" + project] if project else ["projects/*", 21, None, None, None, None, [1]]
        response = await evaluate(rpc_fetch_expression(rpc_id, params, 30))
        print(json.dumps({"rpc_id":rpc_id, "status": response.get("status"), "preflight_error": response.get("preflightError"), "fetch_error": response.get("fetchError")}))
        if response.get("status") != 200:
            print(json.dumps({"source_session_ready":False, "action":"Complete source Flow login before another probe"}), flush=True)
            return
        if response.get("status") == 200:
            payload = parse_rpc_response(response.get("text", ""), rpc_id)
            print(json.dumps({"source_session_ready":True, "rpc_decoded": True, "payload_type": type(payload).__name__, "media_rows": len(list(media_rows(payload)))}))
        if bound_sessions:
            print(json.dumps({"device_bound_sessions":await inspect_bound_sessions(connection)}), flush=True)
            print(json.dumps({"flow_rpc_device_bound_usage":binding_trace.summary()}), flush=True)
        if verify_native_proxy:
            # Explicit local-only verification of cross-profile session import.
            # Credentials exist only in memory and a disposable Chromium profile.
            import src.services.browser_captcha_native_cdp as native
            from src.core.flow_cookies import normalize_google_cookies, COOKIE_HOSTS
            cookies = (await connection.send("Network.getCookies", {"urls": ["https://labs.google/", "https://flow.google.com/", "https://accounts.google.com/", "https://www.google.com/"]}))["cookies"]
            jar = [c for c in cookies if c["domain"].lstrip(".") in COOKIE_HOSTS and not c.get("partitionKey")]
            labs = {c["name"]:c["value"] for c in cookies if c["domain"].lstrip(".") == "labs.google"}
            name = "__Secure-next-auth.session-token"
            session = labs.get(name, "")
            if not session:
                i = 0
                while f"{name}.{i}" in labs:
                    session += labs[f"{name}.{i}"]
                    i += 1
            token = SimpleNamespace(st=session, google_cookies=normalize_google_cookies(jar), captcha_proxy_url=verify_native_proxy)
            class ProbeDB:
                async def get_token(self, _):
                    return token
            original_popen = native.subprocess.Popen
            def headless_popen(args, **kwargs):
                return original_popen([*args, "--headless=new"], **kwargs)
            with tempfile.TemporaryDirectory(prefix="flow2api-session-probe-", ignore_cleanup_errors=True) as directory:
                worker = native.NativeCdpAccountBrowser(1, ProbeDB())
                worker.profile_dir = Path(directory)
                try:
                    with patch.object(native.subprocess, "Popen", headless_popen):
                        await worker._prepare_profile(for_solve=False)
                        _, native_session = await worker._get_or_create_project_session(project, "angular")
                    await asyncio.sleep(5)
                    diagnostic = await worker._evaluate(native_session, "JSON.stringify({origin:location.origin, path:location.pathname, ua:navigator.userAgent, signIn:/Sign in|登录/.test(document.body.innerText), at:!!window.WIZ_global_data?.SNlM0e})")
                    imported = (await worker.connection.send("Network.getCookies", {"urls":["https://flow.google.com/"]}, session_id=native_session))["cookies"]
                    expected = {(c["name"], c["domain"]):c["value"] for c in jar}
                    print(json.dumps({"native_page":json.loads(diagnostic), "source_cookie_count":len(jar),
                                      "destination_cookie_names":[[c["name"],c["domain"],expected.get((c["name"],c["domain"])) == c["value"]] for c in imported]}), flush=True)
                    result = await worker.fetch_json(project_id=project,
                        url="https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute",
                        json_data={"rpc_id":"ngNC2", "payload":["tools/PINHOLE/projects/" + project]})
                    print(json.dumps({"native_session_import":True, "payload_type":type(result["rpc_payload"]).__name__, "new_generation_submitted":False}), flush=True)
                finally:
                    await worker.stop(reason="read_only_probe_complete")
        if inspect_media:
            response = await evaluate(rpc_fetch_expression("as29s", [inspect_media], 30))
            payload = parse_rpc_response(response.get("text", ""), "as29s")
            def summarize(value, path=""):
                if isinstance(value, list):
                    for i, child in enumerate(value):
                        summarize(child, f"{path}[{i}]")
                elif isinstance(value, str) and value.startswith("https://"):
                    url = urlsplit(value)
                    print(json.dumps({"field":path, "url_host":url.hostname, "url_path":url.path,
                                      "query_keys":[key for key, _ in parse_qsl(url.query)]}))
            summarize(payload)
            if download_proxy:
                from curl_cffi.requests import AsyncSession
                result = video_operations(payload, token_id=0, project_id=project, expected_ids=[inspect_media])
                url = result["operations"][0]["operation"]["metadata"]["video"]["fifeUrl"]
                async with AsyncSession(trust_env=False) as client:
                    downloaded = await client.get(url, headers={"Range":"bytes=0-31"}, proxy=download_proxy, timeout=30)
                print(json.dumps({"download_status":downloaded.status_code, "content_type":downloaded.headers.get("content-type"),
                                  "bytes":len(downloaded.content), "mp4_header":downloaded.content[4:8] == b"ftyp"}))
        if reference:
            # Explicit opt-in: exactly one 360p/4s task, never retry a launch.
            check = await evaluate(rpc_fetch_expression("as29s", [reference], 30))
            reference_payload = parse_rpc_response(check.get("text", ""), "as29s")
            rows = list(media_rows(reference_payload))
            if isinstance(reference_payload, list) and len(reference_payload) > 5 and reference_payload[0] == reference:
                rows = [reference_payload]
            if not rows or rows[0][1] != project:
                print(json.dumps({"reference_header":reference_payload[:4] if isinstance(reference_payload,list) else type(reference_payload).__name__}), flush=True)
                raise RuntimeError("Reference media does not belong to this project")
            captcha = await evaluate("grecaptcha.enterprise.execute(" + json.dumps(FLOW_WEBSITE_KEY) + ", {action:'VIDEO_GENERATION'})")
            rest = {"clientContext":{"projectId":project,"recaptchaContext":{"token":captcha}},
                    "requests":[{"videoModelKey":"abra_r2v_4s_360p","textInput":{"structuredPrompt":{"parts":[{"text":"Gentle camera movement, preserve the reference scene."}]}},"referenceImages":[{"mediaId":reference}]}]}
            rpc, params = build_video_rpc(rest)
            result = await evaluate(rpc_fetch_expression(rpc, params, 35))
            print(json.dumps({"canary_submit_status":result.get("status"), "preflight_error":result.get("preflightError"), "fetch_error":result.get("fetchError")}), flush=True)
            if result.get("status") != 200:
                return
            parsed = parse_rpc_response(result.get("text", ""), rpc)
            operations = video_operations(parsed, token_id=0, project_id=project)["operations"]
            ids = [op["mediaName"] for op in operations]
            print(json.dumps({"canary_media_ids":ids, "remaining_credits":parsed[1] if len(parsed)>1 else None}), flush=True)
            for _ in range(36):
                await asyncio.sleep(5)
                polled = await evaluate(rpc_fetch_expression("jwpduf", [None,None,[[mid] for mid in ids]], 30))
                result = video_operations(parse_rpc_response(polled.get("text", ""), "jwpduf"), token_id=0, project_id=project, expected_ids=ids)
                statuses = [op["status"] for op in result["operations"]]
                print(json.dumps({"canary_status":statuses}), flush=True)
                if all(status.endswith("SUCCESSFUL") for status in statuses):
                    print(json.dumps({"canary_completed":True}), flush=True)
                    return
            raise RuntimeError("Canary remains pending; do not resubmit")
    finally:
        await connection.close()
        if control:
            try:
                await control.send("Target.closeTarget", {"targetId":owned_target})
            finally:
                await control.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--canary-reference", help="Explicitly submit one 360p/4s task using this same-project reference image")
    parser.add_argument("--inspect-media", help="Read existing media URL structure without printing signed credentials")
    parser.add_argument("--download-proxy", help="With --inspect-media, verify a 32-byte download using the account's proxy")
    parser.add_argument("--verify-native-proxy", help="Import the source session into a disposable headless native profile using this same-egress proxy; read-only RPC, no generation")
    parser.add_argument("--fresh-tab", action="store_true", help="Use a temporary background tab in the source profile; do not navigate the user's existing tab")
    parser.add_argument("--project-id", help="Existing project UUID to query, including when the current tab is the Flow home page")
    parser.add_argument("--inspect-bound-sessions", action="store_true", help="Read Google device-bound-session metadata only; does not export keys, challenges or session IDs")
    args = parser.parse_args()
    asyncio.run(main(args.port, args.canary_reference, args.inspect_media, args.download_proxy, args.verify_native_proxy, args.fresh_tab, args.project_id, args.inspect_bound_sessions))
