from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from apps.core.models import GenerationJob, MediaAsset
from apps.core.services.media_library_report import MediaLibraryReportService


class MediaLibraryCleanupService:
    def __init__(self, report_service: MediaLibraryReportService | None = None) -> None:
        self.report_service = report_service or MediaLibraryReportService()

    def execute(
        self,
        *,
        older_than_days: int = 0,
        asset_types: list[str] | None = None,
        include_missing_files: bool = True,
        limit: int = 100,
        safe_roots: list[str] | None = None,
    ) -> dict:
        report = self.report_service.generated_media_cleanup_report(
            older_than_days=older_than_days,
            asset_types=asset_types,
            include_missing_files=include_missing_files,
            limit=limit,
            safe_roots=safe_roots,
        )

        deleted_asset_ids: list[int] = []
        deleted_paths: list[str] = []
        skipped_asset_ids: list[int] = []
        skipped_paths: list[str] = []
        freed_bytes = 0

        for candidate in report["candidates"]:
            asset = MediaAsset.objects.get(id=candidate["media_asset_id"])
            file_path = Path(candidate["file_path"])
            file_exists = file_path.exists()
            size_bytes = int(candidate.get("size_bytes") or 0)

            if file_exists:
                file_path.unlink()
                deleted_paths.append(str(file_path))
                freed_bytes += size_bytes
            else:
                skipped_paths.append(str(file_path))

            asset.metadata["cleanup"] = {
                "cleaned_at": datetime.now(UTC).isoformat(),
                "file_path": str(file_path),
                "file_existed": file_exists,
                "output_job_ids": candidate["output_job_ids"],
                "size_bytes": size_bytes if file_exists else None,
                "removed_from_library": True,
            }
            asset.save(update_fields=["metadata"])
            GenerationJob.objects.filter(output_media_id=asset.id).update(output_media=None)

            if file_exists:
                deleted_asset_ids.append(asset.id)
            else:
                skipped_asset_ids.append(asset.id)

        return {
            "candidate_count": report["candidate_count"],
            "older_than_days": report["older_than_days"],
            "asset_types": report["asset_types"],
            "include_missing_files": report["include_missing_files"],
            "deleted_count": len(deleted_asset_ids),
            "deleted_asset_ids": deleted_asset_ids,
            "deleted_paths": deleted_paths,
            "skipped_asset_ids": skipped_asset_ids,
            "skipped_paths": skipped_paths,
            "freed_bytes": freed_bytes,
            "candidates": report["candidates"],
        }
