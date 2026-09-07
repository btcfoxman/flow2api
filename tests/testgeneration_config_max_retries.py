import tempfile
import unittest

import aiosqlite

from src.core.config import config
from src.core.database import Database


class GenerationConfigMaxRetriesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        self._original_image_timeout = config.image_timeout
        self._original_video_timeout = config.video_timeout
        self._original_max_retries = config.flow_max_retries
        self._original_async_task_queue_capacity = config.async_task_queue_capacity
        self._original_captcha_max_retries = config.captcha_max_retries
        await self.db.init_db()

    async def asyncTearDown(self):
        config.set_image_timeout(self._original_image_timeout)
        config.set_video_timeout(self._original_video_timeout)
        config.set_flow_max_retries(self._original_max_retries)
        config.set_async_task_queue_capacity(self._original_async_task_queue_capacity)
        config.set_captcha_max_retries(self._original_captcha_max_retries)
        self._temp_dir.cleanup()

    async def test_init_config_from_toml_persists_flow_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 321,
                    "video_timeout": 654,
                    "async_task_queue_capacity": 17,
                },
                "flow": {
                    "max_retries": 7,
                },
            },
            is_first_startup=True,
        )

        generation_config = await self.db.get_generation_config()

        self.assertIsNotNone(generation_config)
        self.assertEqual(generation_config.image_timeout, 321)
        self.assertEqual(generation_config.video_timeout, 654)
        self.assertEqual(generation_config.max_retries, 7)
        self.assertEqual(generation_config.async_task_queue_capacity, 17)

    async def test_reload_config_to_memory_syncs_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 300,
                    "video_timeout": 1500,
                },
                "flow": {
                    "max_retries": 3,
                },
            },
            is_first_startup=True,
        )

        await self.db.update_generation_config(max_retries=9)
        await self.db.reload_config_to_memory()

        self.assertEqual(config.flow_max_retries, 9)

    async def test_reload_config_to_memory_syncs_async_queue_capacity(self):
        await self.db.init_config_from_toml(
            {
                "generation": {
                    "image_timeout": 300,
                    "video_timeout": 1500,
                    "async_task_queue_capacity": 50,
                },
            },
            is_first_startup=True,
        )

        await self.db.update_generation_config(async_task_queue_capacity=73)
        await self.db.reload_config_to_memory()

        self.assertEqual(config.async_task_queue_capacity, 73)

    async def test_existing_generation_config_gets_queue_capacity_column(self):
        legacy_dir = tempfile.TemporaryDirectory()
        legacy_path = f"{legacy_dir.name}/legacy.db"
        try:
            async with aiosqlite.connect(legacy_path) as connection:
                await connection.execute(
                    """
                    CREATE TABLE generation_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        image_timeout INTEGER DEFAULT 300,
                        video_timeout INTEGER DEFAULT 1500,
                        max_retries INTEGER DEFAULT 3
                    )
                    """
                )
                await connection.execute(
                    """
                    INSERT INTO generation_config (
                        id, image_timeout, video_timeout, max_retries
                    ) VALUES (1, 321, 654, 7)
                    """
                )
                await connection.commit()

            legacy_db = Database(db_path=legacy_path)
            await legacy_db.init_db()
            await legacy_db.check_and_migrate_db(config.get_raw_config())

            generation_config = await legacy_db.get_generation_config()
            self.assertEqual(generation_config.image_timeout, 321)
            self.assertEqual(generation_config.max_retries, 7)
            self.assertEqual(generation_config.async_task_queue_capacity, 50)
        finally:
            legacy_dir.cleanup()

    async def test_init_config_from_toml_persists_captcha_max_retries(self):
        await self.db.init_config_from_toml(
            {
                "captcha": {
                    "captcha_method": "adspower",
                    "captcha_max_retries": 6,
                },
            },
            is_first_startup=True,
        )

        captcha_config = await self.db.get_captcha_config()

        self.assertEqual(captcha_config.captcha_max_retries, 6)

    async def test_reload_config_to_memory_syncs_captcha_max_retries(self):
        await self.db.update_captcha_config(captcha_max_retries=7)
        await self.db.reload_config_to_memory()

        self.assertEqual(config.captcha_max_retries, 7)


if __name__ == "__main__":
    unittest.main()
