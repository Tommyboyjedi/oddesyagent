from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings

from apps.core.models import MediaAsset


class MediaLibraryReportService:
    DEFAULT_ASSET_TYPES = [MediaAsset.TYPE_GENERATED_VIDEO, MediaAsset.TYPE_GENERATED_IMAGE]

    def generated_media_cleanup_report(
        self,
        *,
        older_than_days: int = 0,
        asset_types: list[str] | None = None,
        include_missing_files: bool = True,
        limit: int = 100,
        safe_roots: list[str] | None = None,
    ) -> dict:
        asset_types = asset_types or self.DEFAULT_ASSET_TYPES
        safe_root_paths = [Path(root).resolve() for root in (safe_roots or settings.ODDESY_SAFE_ROOTS) if root]
        cutoff = datetime.now(UTC) - timedelta(days=max(0, older_than_days))

        queryset = (
            MediaAsset.objects.filter(asset_type__in=asset_types)
            .prefetch_related("output_generation_jobs")
            .order_by("created_at", "id")
        )

        candidates: list[dict] = []
        total_size_bytes = 0
        for asset in queryset:
            if len(candidates) >= limit:
                break
            created_at = asset.created_at.astimezone(UTC)
            if older_than_days > 0 and created_at > cutoff:
                continue

            file_path = Path(asset.file.path).resolve()
            if safe_root_paths and not any(file_path.is_relative_to(root) for root in safe_root_paths):
                continue

            file_exists = file_path.exists()
            if not file_exists and not include_missing_files:
                continue

            size_bytes = file_path.stat().st_size if file_exists else None
            if size_bytes:
                total_size_bytes += size_bytes

            candidates.append(
                {
                    "media_asset_id": asset.id,
                    "asset_type": asset.asset_type,
                    "original_file_name": asset.original_file_name,
                    "file_name": asset.file.name,
                    "file_path": str(file_path),
                    "file_exists": file_exists,
                    "size_bytes": size_bytes,
                    "created_at": created_at.isoformat(),
                    "output_job_ids": list(asset.output_generation_jobs.values_list("id", flat=True)),
                    "metadata": asset.metadata,
                }
            )

        return {
            "candidate_count": len(candidates),
            "total_size_bytes": total_size_bytes,
            "older_than_days": older_than_days,
            "asset_types": asset_types,
            "include_missing_files": include_missing_files,
            "candidates": candidates,
        }
