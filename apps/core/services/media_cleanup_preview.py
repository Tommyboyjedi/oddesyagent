from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.core.services.safe_root_browser import SafeRootBrowserError, SafeRootBrowserService


class MediaCleanupPreviewService:
    DEFAULT_EXTENSIONS = [".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp"]

    def __init__(self, browser: SafeRootBrowserService | None = None) -> None:
        self.browser = browser or SafeRootBrowserService()

    def preview(
        self,
        *,
        target_path: str,
        recursive: bool = True,
        limit: int = 100,
        older_than_days: int = 0,
        extensions: list[str] | None = None,
        roots: list[str] | None = None,
    ) -> dict:
        payload = self.browser.browse(
            target_path=target_path,
            recursive=recursive,
            limit=limit,
            roots=roots,
        )
        allowed_extensions = self._normalize_extensions(extensions or self.DEFAULT_EXTENSIONS)
        cutoff = datetime.now(UTC) - timedelta(days=max(0, older_than_days))

        candidates: list[dict] = []
        total_size_bytes = 0
        for entry in payload["entries"]:
            if entry["is_dir"]:
                continue
            suffix = Path(entry["name"]).suffix.lower()
            if suffix not in allowed_extensions:
                continue
            modified_at = datetime.fromisoformat(entry["modified_at"])
            if older_than_days > 0 and modified_at > cutoff:
                continue
            total_size_bytes += int(entry["size_bytes"] or 0)
            candidates.append(entry)

        return {
            "target_path": payload["target_path"],
            "recursive": recursive,
            "limit": limit,
            "older_than_days": older_than_days,
            "extensions": sorted(allowed_extensions),
            "candidate_count": len(candidates),
            "total_size_bytes": total_size_bytes,
            "candidates": candidates,
        }

    def _normalize_extensions(self, extensions: list[str]) -> set[str]:
        normalized = set()
        for extension in extensions:
            value = extension.strip().lower()
            if not value:
                continue
            if not value.startswith("."):
                value = f".{value}"
            normalized.add(value)
        if not normalized:
            raise SafeRootBrowserError("At least one extension must be provided for cleanup preview.")
        return normalized
