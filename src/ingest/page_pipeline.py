"""Resumable PDF page extraction; native text first, local CPU OCR when needed."""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from src.ingest.digital_pdf_ingestor import DigitalPdfIngestor, ExtractedLine, ExtractedPageData
from src.ingest.layout import order_lines
from src.io_utils import atomic_write

PIPELINE_VERSION = "3.0.0"


def atomic_json(path: Path, value: object) -> None:
    atomic_write(path,(json.dumps(value,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refine_graphic_page(page: ExtractedPageData) -> ExtractedPageData:
    area=page.width_pt*page.height_pt
    graphic_area=sum(r[2]*r[3] for r in page.graphics)
    if (page.layout_hint not in {"cover","toc"} and len(page.graphics)>=2
            and graphic_area>area*0.2 and sum(len(l.text) for l in page.lines)<700):
        page.layout_hint="plate"
    return page


@contextmanager
def job_lock(path: Path):
    """OS-released advisory lock, including when the process is killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class LocalOcr:
    def __init__(self, threads: int = 4):
        from rapidocr import RapidOCR
        self.engine = RapidOCR(params={"EngineConfig.onnxruntime.intra_op_num_threads": threads,
                                       "EngineConfig.onnxruntime.inter_op_num_threads": 1})

    def recognize(self, image_path: Path, scale: float) -> list[ExtractedLine]:
        result = self.engine(str(image_path))
        if result.boxes is None:
            return []
        lines = []
        for box, text, confidence in zip(result.boxes, result.txts, result.scores):
            x0, y0 = box.min(axis=0) / scale
            x1, y1 = box.max(axis=0) / scale
            lines.append(ExtractedLine(text, float(x0), float(y0), float(x1-x0),
                                       float(y1-y0), float((y1-y0)*0.75), float(confidence)))
        return order_lines(lines)


def native_quality(page, lines: list[ExtractedLine]) -> tuple[str, list[str]]:
    text = "".join(l.text for l in lines)
    if text and (text.count("\ufffd")/len(text) > 0.01 or
                 sum(ord(c) < 32 and not c.isspace() for c in text)/len(text) > 0.01):
        return "ocr", ["damaged_text_layer"]
    # An image covering most of a page can contain unrecognized text even if a
    # footer or a small part of a previous OCR layer remains selectable.
    area = page.rect.width*page.rect.height
    import pymupdf
    image_area = max((pymupdf.Rect(i["bbox"]).get_area() for i in page.get_image_info()), default=0)
    if image_area > area*0.65 and len(text) < 80:
        return "ocr", ["full_page_image_with_sparse_text"]
    if not text and image_area > area*0.02:
        return "ocr", ["image_without_text"]
    if not text:
        # Vector outlines may represent text; a render is needed to tell from blank.
        pix = page.get_pixmap(matrix=pymupdf.Matrix(0.3, 0.3), colorspace=pymupdf.csGRAY)
        import numpy as np
        ink = float((np.frombuffer(pix.samples, dtype=np.uint8)<220).mean())
        return ("blank", []) if ink < 0.0003 else ("ocr", ["visible_content_without_text"])
    return "native", []


class PageExtractor:
    def __init__(self, run_dir: Path, dpi: int = 180, threads: int = 4):
        self.run_dir = run_dir
        self.dpi = dpi
        self.threads = threads
        self.ocr = None

    def extract(self, document, index: int, source_sha256: str) -> ExtractedPageData:
        page = document[index]
        cache = self.run_dir / "pages" / f"{index+1:05d}.json"
        image = self.run_dir / "images" / f"{index+1:05d}.jpg"
        signature = {"version": PIPELINE_VERSION, "sha256": source_sha256, "dpi": self.dpi}
        if cache.exists() and image.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if saved["signature"] == signature and saved["image_sha256"] == file_hash(image):
                    payload = saved["page"]
                    payload["lines"] = order_lines([ExtractedLine(**line) for line in payload["lines"]])
                    # Layout rules may be refined without rerunning OCR. The cached
                    # source lines remain authoritative; derive hints again.
                    payload["detected_page_no_raw"], payload["layout_hint"] = DigitalPdfIngestor._detect_page_number_and_domain_hint(
                        index, payload["lines"], payload["height_pt"])
                    return refine_graphic_page(ExtractedPageData(**payload))
            except (KeyError, ValueError, TypeError):
                pass
        import pymupdf
        started = time.perf_counter()
        image.parent.mkdir(parents=True, exist_ok=True)
        scale = self.dpi / 72
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(image, jpg_quality=88)
        lines = DigitalPdfIngestor.extract_native_lines(page)
        route, issues = native_quality(page, lines)
        if route == "ocr":
            if self.ocr is None:
                self.ocr = LocalOcr(self.threads)
            lines = self.ocr.recognize(image, scale)
            if not lines:
                issues.append("ocr_no_text_review_image")
            method = "rapidocr-onnx"
        else:
            method = "pymupdf" if route == "native" else "blank"
        # Normal page/figure images are useful graphic regions. A full-page scan
        # is not itself a figure: never use its container rectangle as a figure.
        graphics = []
        for info in page.get_image_info():
            rect = pymupdf.Rect(info["bbox"]) * page.rotation_matrix
            rect &= page.rect
            if not rect.is_empty and 0.01 < rect.get_area()/page.rect.get_area() < 0.65:
                graphics.append([rect.x0, rect.y0, rect.width, rect.height])
        label, hint = DigitalPdfIngestor._detect_page_number_and_domain_hint(index, lines, page.rect.height)
        result = ExtractedPageData(index+1, index, page.get_label(), page.rect.width,
                                   page.rect.height, len(page.get_images()), lines,
                                   label, hint, method, str(image.resolve()), graphics, issues)
        result=refine_graphic_page(result)
        atomic_json(cache, {"signature": signature, "page": asdict(result), "route": route,
                            "image_sha256": file_hash(image), "elapsed_seconds": round(time.perf_counter()-started,3)})
        return result
