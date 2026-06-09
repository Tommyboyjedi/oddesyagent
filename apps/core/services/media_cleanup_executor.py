from __future__ import annotations

from pathlib import Path

from apps.core.services.media_cleanup_preview import MediaCleanupPreviewService


class MediaCleanupExecutorService:
    def __init__(self, preview_service: MediaCleanupPreviewService | None = None) -> None:
        self.preview_service = preview_service or MediaCleanupPreviewService()

    def execute(
        self,
        *,
        target_path: str,
        recursive: bool = True,
        limit: int = 100,
        older_than_days: int = 0,
        extensions: list[str] | None = None,
        roots: list[str] | None = None,
    ) -> dict:
        preview = self.preview_service.preview(
            target_path=target_path,
            recursive=recursive,
            limit=limit,
            older_than_days=older_than_days,
            extensions=extensions,
            roots=roots,
        )

        deleted_paths: list[str] = []
        skipped_paths: list[str] = []
        freed_bytes = 0
        for candidate in preview["candidates"]:
            candidate_path = Path(candidate["path"])
            if not candidate_path.exists():
                skipped_paths.append(str(candidate_path))
                continue
            candidate_path.unlink()
            deleted_paths.append(str(candidate_path))
            freed_bytes += int(candidate.get("size_bytes") or 0)

        return {
            "target_path": preview["target_path"],
            "recursive": preview["recursive"],
            "limit": preview["limit"],
            "older_than_days": preview["older_than_days"],
            "extensions": preview["extensions"],
            "candidate_count": preview["candidate_count"],
            "candidates": preview["candidates"],
            "deleted_count": len(deleted_paths),
            "deleted_paths": deleted_paths,
            "skipped_paths": skipped_paths,
            "freed_bytes": freed_bytes,
        }
