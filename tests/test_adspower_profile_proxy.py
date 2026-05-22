import unittest
from unittest.mock import patch

from src.services.browser_captcha import _adspower_profile_proxy_url, _stop_adspower_profile


class AdsPowerProfileProxyTests(unittest.TestCase):
    def test_resolves_profile_proxy_from_v1_user_list(self):
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "user_id": "kwrxc3b",
                        "user_proxy_config": {
                            "proxy_type": "socks5",
                            "proxy_host": "127.0.0.1",
                            "proxy_port": "20021",
                            "proxy_user": "user name",
                            "proxy_password": "p@ss",
                        },
                    }
                ]
            },
        }

        with patch("src.services.browser_captcha._adspower_request_json", return_value=payload):
            self.assertEqual(
                _adspower_profile_proxy_url("kwrxc3b"),
                "socks5://user%20name:p%40ss@127.0.0.1:20021",
            )

    def test_rewrites_loopback_host_when_adspower_api_is_remote(self):
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "profile_id": "profile-1",
                        "user_proxy_config": {
                            "proxy_type": "http",
                            "proxy_host": "127.0.0.1",
                            "proxy_port": "8080",
                        },
                    }
                ]
            },
        }

        with patch("src.services.browser_captcha._adspower_request_json", return_value=payload), patch(
            "src.services.browser_captcha._adspower_api_host_for_cdp",
            return_value="adspower-cli",
        ):
            self.assertEqual(
                _adspower_profile_proxy_url("profile-1"),
                "http://adspower-cli:8080",
            )

    def test_stop_profile_tries_legacy_stop_params(self):
        calls = []

        def fake_request(method, path, params=None, body=None):
            calls.append((method, path, params, body))
            if params == {"id": "kwrxc3b"}:
                return {"code": 0}
            return {"code": 1, "msg": "not found"}

        with patch("src.services.browser_captcha._adspower_request_json", side_effect=fake_request):
            self.assertTrue(_stop_adspower_profile("kwrxc3b"))

        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/browser/stop", {"user_id": "kwrxc3b"}, None),
                ("GET", "/api/v1/browser/stop", {"id": "kwrxc3b"}, None),
            ],
        )
