from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings


class SafeRootBrowserError(ValueError):
    pass


class SafeRootBrowserService:
    def list_roots(self, roots: list[str] | None = None) -> list[str]:
        return [str(root) for root in self._resolve_roots(roots)]

    def browse(
        self,
        *,
        target_path: str,
        recursive: bool = False,
        limit: int = 100,
        roots: list[str] | None = None,
    ) -> dict:
        resolved_roots = self._resolve_roots(roots)
        candidate = self._resolve_candidate(target_path, resolved_roots)
        if not candidate.exists():
            raise SafeRootBrowserError(f"Path does not exist: {target_path}")
        if not candidate.is_dir():
            raise SafeRootBrowserError(f"Path is not a directory: {target_path}")

        entries = self._collect_entries(candidate, recursive=recursive, limit=limit)
        return {
            "target_path": str(candidate),
            "recursive": recursive,
            "limit": limit,
            "count": len(entries),
            "entries": entries,
        }

    def _resolve_roots(self, roots: list[str] | None) -> list[Path]:
        configured_roots = roots or settings.ODDESY_SAFE_ROOTS
        resolved = [Path(root).resolve() for root in configured_roots if root]
        if not resolved:
            raise SafeRootBrowserError("No safe roots are configured.")
        return resolved

    def _resolve_candidate(self, target_path: str, roots: list[Path]) -> Path:
        try:
            candidate = Path(target_path).resolve()
        except OSError as exc:
            raise SafeRootBrowserError(f"Invalid path: {target_path}") from exc
        if not any(candidate.is_relative_to(root) for root in roots):
            raise SafeRootBrowserError("Requested path is outside configured safe roots.")
        return candidate

    def _collect_entries(self, directory: Path, *, recursive: bool, limit: int) -> list[dict]:
        entries: list[dict] = []
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        for item in iterator:
            if len(entries) >= limit:
                break
            stat = item.stat()
            entries.append(
                {
                    "path": str(item),
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size_bytes": None if item.is_dir() else stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                }
            )
        return entries
