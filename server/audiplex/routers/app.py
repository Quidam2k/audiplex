"""APK distribution + version info for the Android client."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from audiplex.auth import get_current_user
from audiplex.config import get_settings
from audiplex.models import User

router = APIRouter(prefix="/api/app", tags=["app"])


def _resolve_apk_dir() -> Path:
    settings = get_settings()
    p = Path(settings.apk_output_dir)
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def _read_metadata(apk_dir: Path) -> dict:
    metadata_path = apk_dir / "output-metadata.json"
    if not metadata_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"APK output-metadata.json not found at {metadata_path}",
        )
    with open(metadata_path) as f:
        meta = json.load(f)
    elements = meta.get("elements") or []
    if not elements:
        raise HTTPException(status_code=500, detail="output-metadata.json has no elements")
    el = elements[0]
    return {
        "versionCode": el["versionCode"],
        "versionName": el["versionName"],
        "outputFile": el["outputFile"],
    }


@router.get("/version")
def get_version():
    apk_dir = _resolve_apk_dir()
    info = _read_metadata(apk_dir)
    apk_path = apk_dir / info["outputFile"]
    if not apk_path.exists():
        raise HTTPException(status_code=404, detail=f"APK file missing: {apk_path}")
    stat = apk_path.stat()
    return {
        **info,
        "sizeBytes": stat.st_size,
        "mtime": int(stat.st_mtime),
    }


@router.get("/download")
def download_apk():
    apk_dir = _resolve_apk_dir()
    info = _read_metadata(apk_dir)
    apk_path = apk_dir / info["outputFile"]
    if not apk_path.exists():
        raise HTTPException(status_code=404, detail=f"APK file missing: {apk_path}")
    return FileResponse(
        apk_path,
        media_type="application/vnd.android.package-archive",
        filename=f"audiplex-{info['versionName']}-{info['versionCode']}.apk",
    )
