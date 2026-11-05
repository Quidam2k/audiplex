"""Where an item's audio lives on the server, for Todd's long-press sheet (#5485).

Admin only. Items are looked up by their database id; this never takes a path from
the client, so it can only tell you about files the library already holds.
"""

import ntpath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from audiplex.auth import get_admin_user
from audiplex.database import get_db
from audiplex.models import Album, Book, Track

router = APIRouter(prefix="/api/files", tags=["files"])


class FileInfoSchema(BaseModel):
    folder: str
    file_name: str | None = None  # None for an album, which is a folder


@router.get("/{kind}/{item_id}", response_model=FileInfoSchema)
def get_file_info(kind: str, item_id: int, db: Session = Depends(get_db),
                  user=Depends(get_admin_user)):
    model = {"book": Book, "track": Track, "album": Album}.get(kind)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown kind")
    item = db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{kind} not found")
    if kind == "album":
        return FileInfoSchema(folder=item.folder_path)
    folder, name = ntpath.split(item.file_path)  # Windows paths; ntpath splits \ and /
    return FileInfoSchema(folder=folder, file_name=name)
