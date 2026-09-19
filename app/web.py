"""Serve the two built web entries without turning API errors into SPA pages."""

from __future__ import annotations

from pathlib import Path
import re

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from app.config import Settings


RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
HASHED_ASSET = re.compile(r"[-.][A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+\Z")
REVALIDATE = "public, max-age=0, must-revalidate"


def register_web(application: FastAPI, settings: Settings) -> None:
    """HTML is a public login shell; all case/staff data remains API-authorized."""
    root = settings.frontend_dist.resolve()
    static = StaticFiles(directory=root, check_dir=False)

    def checked_path(directory: str, filename: str) -> Path:
        # The build directory is the entire public boundary, never the repo or /data.
        relative = Path(filename)
        if relative.is_absolute() or any(part.startswith(".") for part in relative.parts):
            raise HTTPException(404, "找不到檔案。")
        base = (root / directory).resolve()
        target = (base / relative).resolve()
        if not base.is_relative_to(root) or not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(404, "找不到檔案。")
        return target

    def entry(filename: str):
        try:
            target = checked_path("", filename)
        except HTTPException:
            # API-only local development still starts normally before npm run build.
            raise HTTPException(503, "前端尚未建置，請先在 frontend 執行 npm ci 與 npm run build。")
        return FileResponse(target, media_type="text/html", headers={"Cache-Control": "no-store"})

    def asset(request: Request, directory: str, filename: str, cache_control: str = REVALIDATE):
        target = checked_path(directory, filename)
        # Reuse Starlette's conditional ETag/Last-Modified handling for large OCR models.
        response = static.file_response(target, target.stat(), request.scope)
        response.headers["Cache-Control"] = cache_control
        if response.status_code != 304 and target.suffix == ".wasm":
            response.headers["Content-Type"] = "application/wasm"
        return response

    @application.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def public_entry():
        return entry("index.html")

    @application.api_route("/admin", methods=["GET", "HEAD"], include_in_schema=False)
    def admin_redirect(request: Request):
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/admin/{query}", status_code=308, headers={"Cache-Control": "no-store"})

    @application.api_route("/admin/", methods=["GET", "HEAD"], include_in_schema=False)
    def admin_entry():
        return entry("admin/index.html")

    @application.api_route("/cases/{resource_id}", methods=["GET", "HEAD"], include_in_schema=False)
    @application.api_route("/tasks/{resource_id}", methods=["GET", "HEAD"], include_in_schema=False)
    def notification_entry(resource_id: str):
        # The browser retains the original URL; the frontend resolves it after login.
        if not RESOURCE_ID.fullmatch(resource_id):
            raise HTTPException(404, "找不到頁面。")
        return entry("index.html")

    @application.api_route("/assets/{filename:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def built_asset(request: Request, filename: str):
        cache = "public, max-age=31536000, immutable" if HASHED_ASSET.search(Path(filename).name) else REVALIDATE
        return asset(request, "assets", filename, cache)

    @application.api_route("/ocr/{filename:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def ocr_asset(request: Request, filename: str):
        # Stable worker/core/model filenames must revalidate together after deployment.
        return asset(request, "ocr", filename)

    @application.api_route("/ai-safety-card.html", methods=["GET", "HEAD"], include_in_schema=False)
    @application.api_route("/ai-safety-card.pdf", methods=["GET", "HEAD"], include_in_schema=False)
    @application.api_route("/ai-safety-card.png", methods=["GET", "HEAD"], include_in_schema=False)
    def safety_card(request: Request):
        return asset(request, "", request.url.path.lstrip("/"))
