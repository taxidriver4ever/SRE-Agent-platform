"""用户附件的预签名上传 API。"""

from app.uploads.router import router as upload_router
from app.uploads.service import UploadService

__all__ = ["UploadService", "upload_router"]
