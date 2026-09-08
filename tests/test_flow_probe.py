import unittest
import json

from tools.probe_flow_angular import select_flow_target, bound_session_summary, RpcBindingTrace


class ProbeTargetTests(unittest.TestCase):
    def test_accepts_home_page_after_login(self):
        page = {"type":"page", "url":"https://flow.google.com/", "id":"home"}
        self.assertEqual(select_flow_target([page]), page)

    def test_prefers_existing_project_page(self):
        home = {"type":"page", "url":"https://flow.google.com/"}
        project = {"type":"page", "url":"https://flow.google.com/project/p"}
        self.assertEqual(select_flow_target([home, project]), project)

    def test_rejects_foreign_targets(self):
        self.assertIsNone(select_flow_target([
            {"type":"page", "url":"https://evil.test/?next=https://flow.google.com/"},
            {"type":"service_worker", "url":"https://flow.google.com/"},
            {"type":"page", "url":"https://flow.google.com.evil.test/"},
        ]))

    def test_bound_session_metadata_excludes_credentials_and_other_sites(self):
        result = bound_session_summary([
            {"key":{"site":"https://google.com", "id":"secret-session-id"},
             "refreshUrl":"https://accounts.google.com/refresh?secret=private-token",
             "cachedChallenge":"secret-challenge",
             "cookieCravings":[{"name":"SID", "domain":".google.com", "value":"never-export"}]},
            {"key":{"site":"https://unrelated.test", "id":"outside-scope"}},
        ])
        self.assertEqual(result["google_session_count"], 1)
        text = json.dumps(result)
        for secret in ["secret-session-id", "private-token", "secret-challenge", "never-export", "outside-scope", "unrelated.test"]:
            self.assertNotIn(secret, text)

    def test_rpc_usage_trace_handles_extra_event_before_request_without_logging_headers(self):
        trace = RpcBindingTrace()
        trace.extra({"params":{"requestId":"1", "headers":{"cookie":"secret-cookie"},
                                "deviceBoundSessionUsages":[{"sessionKey":{"site":"https://google.com", "id":"secret-id"}, "usage":"InScopeRefreshNotYetNeeded"}]}})
        trace.request({"params":{"requestId":"1", "request":{"url":"https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute?f.sid=private"}}})
        self.assertEqual(trace.summary(), [[{"site":"google.com", "usage":"InScopeRefreshNotYetNeeded"}]])
        self.assertNotIn("secret", json.dumps(trace.__dict__, default=list))
