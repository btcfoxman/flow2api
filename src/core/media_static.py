"""Expose generated media only, never browser profiles underneath tmp/."""
from pathlib import Path
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles


class MediaStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        filename = Path(path)
        if ('/' in path or '\\' in path or filename.name.startswith('.')
                or filename.suffix.lower() not in {
                    '.mp4', '.mov', '.webm', '.mkv', '.m4v', '.png', '.jpg', '.jpeg',
                    '.webp', '.gif', '.avif', '.bmp'}
                or (Path(self.directory) / path).is_symlink()):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)
