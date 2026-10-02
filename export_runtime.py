from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from streaming_excel_export import export_streaming_xlsx

RENDER_TIMEOUT_SECONDS = 120


def ensure_artifact_tool(project_dir: Path) -> None:
    """Use an explicitly installed optional renderer, without searching private caches."""
    package = project_dir / "node_modules" / "@oai" / "artifact-tool" / "package.json"
    if package.is_file():
        return
    raise RuntimeError("Optional Excel renderer is not installed; use the Python exporter")


def export_xlsx_atomic(node: str | None, script: Path, payload: Path, output: Path,
                       project_dir: Path) -> Path:
    from export_names import distinguished_workbook_path
    output = distinguished_workbook_path(output, project_dir)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.xlsx")
    # v1.43: presentation-only schema; the native CSV and ranking JSON are unchanged.
    if script.name == "excel_export.mjs":
        import json
        from ranking_view import build_view
        view = build_view(json.loads(payload.read_text("utf-8-sig")), payload.parent)
        # Large category exports stream on ordinary PCs. The optional renderer
        # must not become a hard dependency or prevent portable export on failure.
        total_rows = sum(len(rows) for rows in view.get("分类", {}).values())
        try:
            if total_rows > 7000:
                raise ImportError("大榜单使用低内存流式导出")
            import artifact_tool
            from artifact_rank_export import export_artifact_view
            export_artifact_view(view, temporary)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            from portable_rank_export import export_portable_view
            export_portable_view(view, temporary)
        actual = output
        try:
            os.replace(temporary, output)
        except PermissionError:
            actual = output.with_name(f"{output.stem}_本次_{time.strftime('%Y%m%d_%H%M%S')}{output.suffix}")
            os.replace(temporary, actual)
        return actual
    if not node or not script.is_file() or payload.stat().st_size >= 8 * 1024 * 1024:
        export_streaming_xlsx(payload, temporary)
    else:
        # 小文件优先使用 artifact-tool；普通用户机器没有 Codex 运行时组件时，
        # 自动回退到 Python streaming exporter，避免 UI 导出候选 Excel 直接失败。
        try:
            ensure_artifact_tool(project_dir)
            result = subprocess.run(
                [node, "--max-old-space-size=8192", str(script), str(payload), str(temporary)], cwd=project_dir,
                text=True, encoding="utf-8", errors="replace", capture_output=True,
                timeout=RENDER_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            export_streaming_xlsx(payload, temporary)
    actual = output
    try:
        os.replace(temporary, output)
    except PermissionError:
        actual = output.with_name(f"{output.stem}_本次_{time.strftime('%Y%m%d_%H%M%S')}{output.suffix}")
        os.replace(temporary, actual)
    return actual
