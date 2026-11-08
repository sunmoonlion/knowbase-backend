"""读文件首页的文字（上传时识别描述，SDD 0044 第 2.3 节）。

只读第一页、只取前几千字：识别只看标题、机构、研究员、评级、日期这些首页上的东西。
正文、按页文字的读法仍由 knowfeed 加工（0043 第 2.3 节），这里不存任何读出来的字。
读不出（扫描版没有文字层、加密、坏文件）就回空串。
"""

from __future__ import annotations

import concurrent.futures
import io
import logging
import threading

from app.domain.recognition import MAX_TEXT_CHARS
from app.domain.user_data import format_view

logger = logging.getLogger(__name__)

# PDF 首页识别的时限（秒）。解析放进一个常驻的小线程池，超时就不等它：线程照样跑完、
# 结果丢掉，上传不被坏 PDF 卡住；池子只有两个线程，卡住的再多也不会无限开线程
# （以前 `with ThreadPoolExecutor` 退出时 shutdown(wait=True)，时限不起作用，账 188）。
# 两个线程都被卡死的 PDF 占着时，后来的不排队等时限，当场跳过识别（名额 `_PDF_SLOTS`，
# 卡住的那份跑完才还回来）；否则每次上传都要白等满时限（opus-qwen 第 8 条）。
PDF_TIMEOUT_SECONDS = 3.0
_PDF_WORKERS = 2
_PDF_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_PDF_WORKERS, thread_name_prefix="first-page-pdf"
)
_PDF_SLOTS = threading.BoundedSemaphore(_PDF_WORKERS)


def read_first_page(media_type: str, content: bytes) -> str:
    try:
        if media_type == "application/pdf":
            if not _PDF_SLOTS.acquire(blocking=False):
                logger.info("first_page_skipped reason=pdf_parsers_busy")
                return ""
            try:
                future = _PDF_POOL.submit(_pdf_first_page, content)
            except BaseException:
                _PDF_SLOTS.release()
                raise
            future.add_done_callback(lambda _: _PDF_SLOTS.release())
            # 超时就不等它：已经在跑的跑完丢掉，名额等它跑完才还
            return future.result(timeout=PDF_TIMEOUT_SECONDS)[:MAX_TEXT_CHARS]
        if media_type.startswith("text/"):
            return content[: MAX_TEXT_CHARS * 4].decode("utf-8", "replace")[
                :MAX_TEXT_CHARS
            ]
        view = format_view(media_type, content)
        return "\n".join(view.sheets)[:MAX_TEXT_CHARS]
    except Exception as exc:  # noqa: BLE001  识别只是建议，读不出不挡上传
        logger.info("first_page_unreadable type=%s", type(exc).__name__)
        return ""


def _pdf_first_page(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not reader.pages:
            return ""
        return reader.pages[0].extract_text() or ""
    except Exception:
        return ""
