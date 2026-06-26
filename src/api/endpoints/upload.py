import os
from fastapi import APIRouter, UploadFile, File, Form
from src.core.config import settings

router = APIRouter()


@router.post("/upload")
async def upload_file(
    files: list[UploadFile] = File(...),
    user_id: str = Form(...),
    session_id: str = Form(...),
):
    dir_path = f"{settings.upload_root}/{user_id}/{session_id}"
    os.makedirs(dir_path, exist_ok=True)

    results = []
    for f in files:
        file_path = f"{dir_path}/{f.filename}"
        content = await f.read()
        with open(file_path, "wb") as fh:
            fh.write(content)
        results.append(
            {
                "filename": f.filename,
                "path": f"/uploads/{user_id}/{session_id}/{f.filename}",
            }
        )

    return {"uploaded": results}
