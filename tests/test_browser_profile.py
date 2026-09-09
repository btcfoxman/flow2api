import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from src.core.browser_profile import configure_web_only_profile


class WebOnlyProfileTests(unittest.TestCase):
    def test_existing_browser_refresh_tokens_protect_profile_even_without_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            default=Path(root)/"Default";default.mkdir()
            with closing(sqlite3.connect(default/"Web Data")) as db:
                db.execute("CREATE TABLE token_service (service TEXT)")
                db.execute("INSERT INTO token_service VALUES ('existing')")
                db.commit()
            self.assertEqual(configure_web_only_profile(root), "preserved_browser_account")
            self.assertFalse((default/"Preferences").exists())

    def test_preserves_other_preferences_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Default" / "Preferences"
            path.parent.mkdir()
            path.write_text(json.dumps({"signin":{"allowed":True,"other":123},"profile":{"name":"Original"}}))
            self.assertEqual(configure_web_only_profile(root), "configured")
            result = json.loads(path.read_text())
            self.assertEqual(result["profile"], {"name":"Original"})
            self.assertEqual(result["signin"], {"allowed":False,"allowed_on_next_startup":False,"other":123})
            self.assertEqual(configure_web_only_profile(root), "unchanged")
            self.assertEqual(json.loads((path.parent / ".flow2api-browser-signin-backup.json").read_text()), {"allowed":True})

    def test_never_logs_existing_chrome_account_out(self):
        for state in ({"account_info":[{"email":"user@example.invalid"}]},
                      {"google":{"services":{"account_id":"existing"}}}):
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "Default" / "Preferences"; path.parent.mkdir()
                original = json.dumps(state); path.write_text(original)
                self.assertEqual(configure_web_only_profile(root), "preserved_browser_account")
                self.assertEqual(path.read_text(), original)

    def test_corrupt_preferences_remain_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Default" / "Preferences"; path.parent.mkdir()
            path.write_text('{broken')
            with self.assertRaises(ValueError):configure_web_only_profile(root)
            self.assertEqual(path.read_text(), '{broken')

    def test_new_profile_keeps_web_cookies_and_security_settings_default(self):
        with tempfile.TemporaryDirectory() as root:
            configure_web_only_profile(root)
            state=json.loads((Path(root)/"Default/Preferences").read_text())
            self.assertEqual(state, {"signin":{"allowed":False,"allowed_on_next_startup":False}})
