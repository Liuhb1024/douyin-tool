from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.douyin import (
    DouyinError,
    content_disposition,
    download_images,
    fetch_cover,
    fetch_post,
    filename_for,
    open_video,
    parse_text,
)

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

app = FastAPI(title="抖音分享下载")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ParseBody(BaseModel):
    text: str = Field(..., max_length=4000)
    quality: str = "1080p"


def _http(exc: DouyinError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/parse")
def parse_link(body: ParseBody) -> dict:
    try:
        post = parse_text(body.text, body.quality)
    except DouyinError as exc:
        raise _http(exc) from exc
    return post.public()


@app.get("/api/cover/{aweme_id}")
def cover(aweme_id: str) -> Response:
    try:
        post = fetch_post(aweme_id)
    except DouyinError as exc:
        raise _http(exc) from exc
    try:
        content, media = fetch_cover(post)
    except DouyinError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=content, media_type=media)


@app.get("/api/download/{aweme_id}")
def download(aweme_id: str, quality: str = "1080p") -> StreamingResponse:
    try:
        post = fetch_post(aweme_id, quality, force=True)
    except DouyinError as exc:
        raise _http(exc) from exc

    if post.is_photo:
        try:
            files = download_images(post)
        except DouyinError as exc:
            raise _http(exc) from exc
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in files:
                archive.writestr(name, data)
        payload = buffer.getvalue()
        name = filename_for(post, "zip")
        return StreamingResponse(
            iter([payload]),
            media_type="application/zip",
            headers={
                "Content-Disposition": content_disposition(name),
                "Content-Length": str(len(payload)),
            },
        )

    try:
        upstream = open_video(post)
    except DouyinError as exc:
        raise _http(exc) from exc

    name = filename_for(post, "mp4")
    headers = {"Content-Disposition": content_disposition(name)}
    if upstream.content_length.isdigit() and not upstream.response.headers.get("Content-Encoding"):
        headers["Content-Length"] = upstream.content_length
    return StreamingResponse(upstream, media_type="video/mp4", headers=headers)
