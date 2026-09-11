import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from src.api import admin
from src.core.config import config
from src.core.database import Database
from src.core.models import Token
from src.core.generation_errors import NativeSessionError
from src.services.flow_angular import (AngularProtocolError, AngularSubmissionUncertain, flow_credits,
    flow_projects, verified_flow_email, build_image_rpc, image_result)
from src.services.token_manager import TokenManager

COOKIES = json.dumps([{"name":"SID","value":"test-root","domain":".google.com","path":"/"},
                      {"name":"OSID","value":"test-flow","domain":"flow.google.com","path":"/"}])
SNAPSHOT = {"email":"person@example.com","credits":50,"userPaygateTier":"PAYGATE_TIER_ONE",
            "projects":[{"project_id":"project-id","project_name":"Test"}],"google_cookies":COOKIES}


class FlowNativeAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = config.captcha_method
        config.set_captcha_method("native_cdp")
        self.addCleanup(config.set_captcha_method, self.previous)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Database(self.folder.name + "/test.db")
        await self.db.init_db()
        self.client = SimpleNamespace(st_to_at=AsyncMock(side_effect=AssertionError("Labs must not run")),
                                      get_credits=AsyncMock(side_effect=AssertionError("OAuth must not run")))
        self.manager = TokenManager(self.db, self.client)
        self.service = SimpleNamespace(_workers={}, verify_flow_import=AsyncMock(return_value=SNAPSHOT),
                                       flow_account_snapshot=AsyncMock(return_value=SNAPSHOT))
        self.service_patch = patch("src.services.browser_captcha_native_cdp.BrowserCaptchaService.get_instance",
                                   AsyncMock(return_value=self.service))
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)

    async def add_account(self, **kwargs):
        data = dict(st="legacy-expired",at="expired-at",at_expires=datetime(2000,1,1,tzinfo=timezone.utc),
                    email="person@example.com",google_cookies=COOKIES,captcha_proxy_url="socks5://127.0.0.1:20022",
                    is_active=False)
        data.update(kwargs)
        token = Token(**data)
        token.id = await self.db.add_token(token)
        return token

    async def test_new_account_sync_does_not_need_labs_st_or_at(self):
        result = await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022",expected_email="person@example.com")
        token = await self.db.get_token(result["token_id"])
        self.assertEqual(token.auth_mode,"flow")
        self.assertIsNone(token.at)
        self.assertIsNone(token.at_expires)
        self.assertTrue(token.is_active)
        self.client.st_to_at.assert_not_awaited()
        self.client.get_credits.assert_not_awaited()
        self.service.verify_flow_import.assert_awaited_once_with(COOKIES,"socks5://127.0.0.1:20022","person@example.com")

    async def test_failed_identity_verification_makes_no_account_write(self):
        token = await self.add_account()
        self.service.verify_flow_import.side_effect = NativeSessionError("flow_identity_mismatch",protocol="angular")
        with self.assertRaises(NativeSessionError):
            await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        current = await self.db.get_token(token.id)
        self.assertEqual(current.auth_mode,"labs")
        self.assertEqual(current.at,"expired-at")
        self.assertFalse(current.is_active)

    async def test_independent_login_is_never_overwritten(self):
        token = await self.add_account()
        with patch("src.core.native_session_state.local_session_state",return_value={"version":1}):
            with self.assertRaises(NativeSessionError):
                await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertEqual((await self.db.get_token(token.id)).auth_mode,"labs")

    async def test_busy_target_cannot_be_resynced(self):
        token = await self.add_account()
        self.service._workers[token.id] = SimpleNamespace(is_busy=True)
        with self.assertRaisesRegex(NativeSessionError,"flow_account_busy"):
            await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertEqual((await self.db.get_token(token.id)).auth_mode,"labs")

    async def test_idle_existing_worker_is_locked_through_credential_write(self):
        from src.services.browser_captcha_native_cdp import NativeCdpAccountBrowser
        token = await self.add_account()
        worker = NativeCdpAccountBrowser(token.id, self.db)
        self.service._workers[token.id] = worker
        real_update = self.db.update_token
        async def update(token_id, **fields):
            if "auth_mode" in fields:
                self.assertTrue(worker.solve_lock.locked())
            return await real_update(token_id, **fields)
        with patch.object(worker, "stop", AsyncMock()), patch.object(self.db, "update_token", update):
            result = await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertTrue(result["account_active"])
        self.assertFalse(worker.solve_lock.locked())

    async def test_credential_change_during_probe_never_enables_account(self):
        token = await self.add_account(auth_mode="flow", at=None)
        async def changed(*args):
            await self.db.update_token(token.id, captcha_proxy_url="socks5://127.0.0.1:20023")
            return SNAPSHOT
        self.service.flow_account_snapshot.side_effect = changed
        self.assertFalse(await self.manager.verify_native_session(token.id))
        self.assertNotIn(token.id, self.manager._flow_verified)

    async def test_flow_image_does_not_send_oauth_to_legacy_endpoint(self):
        from src.services.flow_client import FlowClient
        token = await self.add_account(auth_mode="flow", at=None)
        client = FlowClient(None, self.db)
        client._request_fingerprint_ctx.set({"native_token_id":token.id})
        self.service.fetch_json = AsyncMock(return_value={"rpc_payload":[[],[]]})
        with self.assertRaises(AngularSubmissionUncertain):
            await client._make_image_generation_request("https://legacy.invalid", {
                "clientContext":{"projectId":"project-id","recaptchaContext":{"token":"test-only"}},
                "requests":[{"imageModelName":"GEM_PIX_2","imageAspectRatio":"IMAGE_ASPECT_RATIO_SQUARE",
                             "structuredPrompt":{"parts":[{"text":"test"}]}}]}, None)
        sent = self.service.fetch_json.await_args.kwargs
        self.assertEqual(sent["url"], "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute")
        self.assertNotIn("headers", sent)
        self.assertEqual(sent["json_data"]["rpc_id"], "ogiZ0b")

    async def test_failed_target_verification_never_auto_enables(self):
        token = await self.add_account()
        self.service.flow_account_snapshot.side_effect = NativeSessionError("flow_login_unavailable",protocol="angular")
        result = await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertFalse(result["native_session_verified"])
        self.assertFalse((await self.db.get_token(token.id)).is_active)

    async def test_empty_project_list_cannot_claim_ready(self):
        self.service.flow_account_snapshot.return_value = {**SNAPSHOT,"projects":[]}
        result = await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertFalse(result["native_session_verified"])
        self.assertFalse(result["account_active"])

    async def test_empty_flow_account_creates_project_without_labs(self):
        token = await self.add_account(auth_mode="flow", at=None)
        self.service.flow_account_snapshot.return_value = {**SNAPSHOT,"projects":[]}
        self.client.create_flow_project=AsyncMock(return_value={"project_id":"new-project","project_name":"Created"})
        project_id=await self.manager.ensure_project_exists(token.id)
        self.assertEqual(project_id,"new-project")
        self.client.create_flow_project.assert_awaited_once()
        self.assertEqual((await self.db.get_token(token.id)).current_project_id,"new-project")
        self.assertEqual(len(await self.db.get_projects_by_token(token.id)),1)
        self.client.st_to_at.assert_not_awaited()

    async def test_project_creation_can_complete_target_preflight(self):
        token = await self.add_account(auth_mode="flow", at=None)
        self.service.flow_account_snapshot.return_value = {**SNAPSHOT,"projects":[]}
        self.client.create_flow_project=AsyncMock(return_value={"project_id":"new-project","project_name":"Created"})
        self.assertTrue(await self.manager.verify_native_session(token.id))
        self.client.create_flow_project.assert_awaited_once()

    async def test_project_creation_failure_never_enables_account(self):
        token = await self.add_account(auth_mode="flow", at=None)
        self.service.flow_account_snapshot.return_value = {**SNAPSHOT,"projects":[]}
        self.client.create_flow_project=AsyncMock(side_effect=AngularSubmissionUncertain("unknown"))
        self.assertFalse(await self.manager.verify_native_session(token.id))
        self.assertFalse((await self.db.get_token(token.id)).is_active)
        self.assertEqual(await self.db.get_projects_by_token(token.id),[])
        self.client.create_flow_project.assert_awaited_once()

    async def test_concurrent_empty_account_initialization_creates_only_once(self):
        token = await self.add_account(auth_mode="flow", at=None)
        created = []
        async def snapshot(*args):
            return {**SNAPSHOT,"projects":list(created)}
        async def create(*args):
            await asyncio.sleep(0)
            project={"project_id":"one-project","project_name":"Created"}
            created.append(project)
            return project
        self.service.flow_account_snapshot.side_effect=snapshot
        self.client.create_flow_project=AsyncMock(side_effect=create)
        self.assertEqual(await asyncio.gather(self.manager.ensure_project_exists(token.id),
            self.manager.ensure_project_exists(token.id)),["one-project","one-project"])
        self.client.create_flow_project.assert_awaited_once()
        self.assertEqual(len(await self.db.get_projects_by_token(token.id)),1)

    async def test_case_insensitive_identity_updates_same_account(self):
        token = await self.add_account(email="Person@Example.com")
        result = await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertEqual(result["token_id"],token.id)
        self.assertEqual(len(await self.db.get_all_tokens()),1)
        self.assertTrue((await self.db.get_token(token.id)).st.startswith("flow:"))

    async def test_ambiguous_email_never_overwrites_an_account(self):
        await self.add_account()
        await self.add_account(st="different-session",email="Person@Example.com")
        with self.assertRaisesRegex(NativeSessionError,"flow_identity_ambiguous"):
            await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022")
        self.assertTrue(all(t.auth_mode=="labs" for t in await self.db.get_all_tokens()))

    async def test_admin_metadata_edit_does_not_convert_session_token(self):
        token = await self.add_account(auth_mode="flow",at=None)
        with patch.object(admin,"token_manager",self.manager), patch.object(admin,"concurrency_manager",None):
            result=await admin.update_token(token.id,admin.UpdateTokenRequest(remark="updated"),"admin-test")
        self.assertTrue(result["success"])
        self.assertEqual((await self.db.get_token(token.id)).remark,"updated")
        self.client.st_to_at.assert_not_awaited()

    async def test_disabled_auto_enable_setting_is_honored(self):
        result = await self.manager.sync_flow_session(COOKIES,"socks5://127.0.0.1:20022",auto_enable=False)
        self.assertTrue(result["native_session_verified"])
        self.assertFalse(result["account_active"])

    async def test_native_account_without_oauth_remains_usable_and_cached(self):
        token = await self.add_account(auth_mode="flow",at=None,at_expires=None,is_active=True)
        self.assertIsNotNone(await self.manager.ensure_valid_token(token))
        self.assertIsNotNone(await self.manager.ensure_valid_token(token))
        self.service.flow_account_snapshot.assert_awaited_once_with(token.id,token.email)
        self.client.st_to_at.assert_not_awaited()

    async def test_native_balance_and_projects_do_not_call_oauth_or_legacy_project_creation(self):
        token = await self.add_account(auth_mode="flow",at=None)
        self.assertEqual(await self.manager._refresh_credits_inner(token.id),(True,50))
        self.assertEqual(await self.manager.ensure_project_exists(token.id),"project-id")
        self.client.st_to_at.assert_not_awaited()

    async def test_explicit_native_refresh_never_renews_labs(self):
        token = await self.add_account(auth_mode="flow",at=None)
        self.assertTrue(await self.manager._refresh_at(token.id))
        self.client.st_to_at.assert_not_awaited()

    async def test_public_new_sync_payload_without_session_token(self):
        database=SimpleNamespace(get_plugin_config=AsyncMock(return_value=SimpleNamespace(connection_token="test-key",auto_enable_on_update=True)))
        with patch.object(admin,"db",database),patch.object(admin,"token_manager",self.manager), \
             patch.object(admin,"proxy_manager",SimpleNamespace(normalize_proxy_url=lambda p:p)):
            result = await admin.plugin_update_token({"auth_mode":"flow","email":"person@example.com",
                "google_cookies":json.loads(COOKIES),"captcha_proxy_url":"socks5://127.0.0.1:20022"},"Bearer test-key")
        self.assertTrue(result["flow_identity_verified"])
        self.assertTrue(result["native_session_verified"])
        self.assertFalse(result["oauth_verified"])
        self.assertNotIn("google_cookies",result)

    async def test_database_migration_preserves_legacy_auth_mode(self):
        token = await self.add_account()
        await self.db.check_and_migrate_db()
        self.assertEqual((await self.db.get_token(token.id)).auth_mode,"labs")


class FlowAccountWireTests(unittest.TestCase):
    def test_image_shape_and_signed_result_match_capture(self):
        request = {"clientContext":{"projectId":"project","recaptchaContext":{"token":"captcha-test"}},
            "requests":[{"imageModelName":"GEM_PIX_2","seed":12,"imageAspectRatio":"IMAGE_ASPECT_RATIO_LANDSCAPE",
                "structuredPrompt":{"parts":[{"text":"test"}]}}]}
        rpc, payload = build_image_rpc(request)
        self.assertEqual(rpc,"ogiZ0b")
        self.assertEqual(payload[1][0][3:6],[12,3,"GEM_PIX_2"])
        self.assertEqual(payload[1][0][8],[[["test"]]])
        rendition = [None]*14
        rendition[13]="https://flow-content.google/image/media?Expires=x&KeyName=y&Signature=z"
        reply = [[["media",None,"workflow",None,None,None,[rendition]]],[["workflow",None,None,None,"project"]]]
        result = image_result(reply,"project")
        self.assertEqual(result["media"][0]["image"]["generatedImage"]["fifeUrl"],rendition[13])
        with self.assertRaises(AngularSubmissionUncertain):
            image_result(reply,"foreign-project")

    def test_unknown_image_response_is_uncertain_not_retryable(self):
        for reply in ([[], None], [[[]],[]], [[[[],None,[]]],[]], {}, []):
            with self.assertRaises(AngularSubmissionUncertain):
                image_result(reply,"project")

    def test_identity_needs_authenticated_bootstrap_and_exact_email(self):
        page={"origin":"https://flow.google.com","path":"/","bootstrap":True,"email":"person@example.com"}
        self.assertEqual(verified_flow_email(page,"PERSON@example.com"),"person@example.com")
        for changed in ({"origin":"https://evil.test"},{"path":"/about"},{"bootstrap":False},{"email":""}):
            with self.assertRaises(AngularProtocolError):
                verified_flow_email({**page,**changed})
        with self.assertRaises(AngularProtocolError):
            verified_flow_email(page,"other@example.com")

    def test_zero_credits_requires_recognized_authenticated_tier(self):
        self.assertEqual(flow_credits([None,1])["credits"],0)
        self.assertEqual(flow_credits([50,2])["userPaygateTier"],"PAYGATE_TIER_TWO")
        for payload in ([],{},[0,999],[-1,1],[True,1],[float("inf"),1]):
            with self.assertRaises(AngularProtocolError):
                flow_credits(payload)

    def test_projects_validate_shape_and_do_not_invent_ids(self):
        self.assertEqual(flow_projects([]),[])
        self.assertEqual(flow_projects([[["project-1",["Name"]]]]),[{"project_id":"project-1","project_name":"Name"}])
        with self.assertRaises(AngularProtocolError):
            flow_projects([[["../foreign",["Name"]]]])
