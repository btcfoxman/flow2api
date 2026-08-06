import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from src.api.admin import UpdateAdminConfigRequest, update_admin_config
from src.core.config import config
from src.core.database import Database


class MinimumGenerationCreditsConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_minimum_credits = config.minimum_generation_credits
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self.temp_dir.name}/flow.db")
        await self.db.init_db()
        await self.db.init_config_from_toml(
            config.get_raw_config(),
            is_first_startup=True,
        )

    async def asyncTearDown(self):
        config.set_minimum_generation_credits(self.original_minimum_credits)
        self.temp_dir.cleanup()

    async def test_new_database_defaults_to_fifteen(self):
        admin_config = await self.db.get_admin_config()

        self.assertIsNotNone(admin_config)
        self.assertEqual(admin_config.minimum_generation_credits, 15)

    async def test_database_value_hot_reloads_to_runtime_config(self):
        await self.db.update_admin_config(minimum_generation_credits=9)
        await self.db.reload_config_to_memory()

        self.assertEqual(config.minimum_generation_credits, 9)

    async def test_admin_update_persists_and_hot_applies_threshold(self):
        fake_db = SimpleNamespace(update_admin_config=AsyncMock())

        with patch("src.api.admin.db", fake_db):
            result = await update_admin_config(
                UpdateAdminConfigRequest(
                    error_ban_threshold=3,
                    minimum_generation_credits=11,
                ),
                token="admin-session",
            )

        self.assertTrue(result["success"])
        fake_db.update_admin_config.assert_awaited_once_with(
            error_ban_threshold=3,
            minimum_generation_credits=11,
        )
        self.assertEqual(config.minimum_generation_credits, 11)

    async def test_existing_admin_config_table_is_migrated_with_default(self):
        legacy_dir = tempfile.TemporaryDirectory()
        legacy_path = f"{legacy_dir.name}/legacy.db"
        try:
            async with aiosqlite.connect(legacy_path) as connection:
                await connection.execute(
                    """
                    CREATE TABLE admin_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        username TEXT DEFAULT 'admin',
                        password TEXT DEFAULT 'admin',
                        api_key TEXT DEFAULT 'han1234',
                        error_ban_threshold INTEGER DEFAULT 3
                    )
                    """
                )
                await connection.execute(
                    """
                    INSERT INTO admin_config (
                        id, username, password, api_key, error_ban_threshold
                    ) VALUES (1, 'admin', 'admin', 'key', 3)
                    """
                )
                await connection.commit()

            legacy_db = Database(db_path=legacy_path)
            await legacy_db.init_db()
            await legacy_db.check_and_migrate_db(config.get_raw_config())

            admin_config = await legacy_db.get_admin_config()
            self.assertIsNotNone(admin_config)
            self.assertEqual(admin_config.minimum_generation_credits, 15)
        finally:
            legacy_dir.cleanup()
