"""Non-destructive PDF Inspector for archaeological reports.

Determines:
1. File SHA-256 hash & byte size (streaming)
2. Total pages, internal PDF PageLabels, and embedded Outline/Bookmark tree
3. Document type classification: 'digital' | 'scanned' | 'hybrid_ocr'
4. Page geometry distribution & foldout map detection (pages wider than 1.35x median width)
5. Header/footer page-number text extraction on sampled pages when a text layer exists
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from pypdf import PdfReader


@dataclass
class SamplePageInfo:
    """Diagnostic snapshot of a single sampled PDF page."""

    pdf_page_index: int  # 0-based
    scan_index: int  # 1-based
    source_page_label: Optional[str]
    width_pt: float
    height_pt: float
    is_foldout: bool
    char_count: int
    image_count: int
    first_line_snippet: str
    last_line_snippet: str


@dataclass
class PdfInspectionReport:
    """Comprehensive inspection report for an archaeological report PDF."""

    file_name: str
    file_path: str
    file_size_mb: float
    sha256: str
    total_pages: int
    pdf_type: str  # 'digital' | 'scanned' | 'hybrid_ocr'
    has_outline_bookmarks: bool
    outline_count: int
    distinct_page_labels_count: int
    median_width_pt: float
    median_height_pt: float
    foldout_page_indices: list[int] = field(default_factory=list)
    sampled_pages: list[SamplePageInfo] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PdfInspector:
    """Inspects PDF files to guide ingestion, OCR routing, and page sequence alignment."""

    def __init__(self, sample_count: int = 12) -> None:
        self.sample_count = sample_count

    @staticmethod
    def compute_sha256(file_path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
        """Compute SHA-256 digest in streaming chunks."""
        hasher = hashlib.sha256()
        with file_path.open("rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()

    def inspect_pdf(
        self, pdf_path: str | Path, compute_hash: bool = True
    ) -> PdfInspectionReport:
        """Inspect a single PDF and classify its text/image layers and page geometry."""
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")

        size_mb = round(path.stat().st_size / (1024 * 1024), 2)
        sha256_hex = self.compute_sha256(path) if compute_hash else ""

        reader = PdfReader(str(path), strict=False)
        total_pages = len(reader.pages)

        # Extract PDF PageLabels if defined
        try:
            page_labels = list(reader.page_labels)
        except Exception:
            page_labels = [str(i + 1) for i in range(total_pages)]

        # Extract Outline/Bookmarks count
        outline_count = self._count_outline_items(reader)

        # Measure dimensions across evenly spaced pages (or all pages if <= 400)
        dimension_step = 1 if total_pages <= 400 else max(1, total_pages // 200)
        widths: list[float] = []
        heights: list[float] = []
        page_dims: dict[int, tuple[float, float]] = {}

        for idx in range(0, total_pages, dimension_step):
            box = reader.pages[idx].mediabox
            w = float(box.width)
            h = float(box.height)
            widths.append(w)
            heights.append(h)
            page_dims[idx] = (w, h)

        med_w = round(statistics.median(widths), 2) if widths else 595.0
        med_h = round(statistics.median(heights), 2) if heights else 842.0

        foldout_indices = [
            idx + 1 for idx, (w, _h) in page_dims.items() if w > med_w * 1.35
        ]

        # Choose strategic sample page indices (front matter, early body, middle, late, plates)
        sample_indices = self._select_sample_indices(total_pages, self.sample_count)
        sampled_pages: list[SamplePageInfo] = []
        pages_with_text = 0
        pages_with_images = 0

        for idx in sample_indices:
            page = reader.pages[idx]
            box = page.mediabox
            w = round(float(box.width), 2)
            h = round(float(box.height), 2)
            label = page_labels[idx] if idx < len(page_labels) else str(idx + 1)

            try:
                raw_text = (page.extract_text() or "").strip()
            except Exception:
                raw_text = ""

            img_count = self._count_page_images(page)
            if len(raw_text) >= 15:
                pages_with_text += 1
            if img_count >= 1:
                pages_with_images += 1

            lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            first_line = lines[0][:60] if lines else ""
            last_line = lines[-1][:60] if lines else ""

            sampled_pages.append(
                SamplePageInfo(
                    pdf_page_index=idx,
                    scan_index=idx + 1,
                    source_page_label=label,
                    width_pt=w,
                    height_pt=h,
                    is_foldout=(w > med_w * 1.35),
                    char_count=len(raw_text),
                    image_count=img_count,
                    first_line_snippet=first_line,
                    last_line_snippet=last_line,
                )
            )

        # Classify PDF type
        if pages_with_text == 0:
            pdf_type = "scanned"
        elif pages_with_images >= max(1, len(sample_indices) // 2) and pages_with_text > 0:
            pdf_type = "hybrid_ocr"
        else:
            pdf_type = "digital"

        # Distinguish non-trivial page labels (where label != str(index + 1))
        non_trivial_labels = sum(
            1 for i, lbl in enumerate(page_labels) if str(lbl) != str(i + 1)
        )

        raw_meta = reader.metadata or {}
        clean_meta = {
            str(k).lstrip("/"): str(v)
            for k, v in raw_meta.items()
            if v is not None and str(v).strip()
        }

        return PdfInspectionReport(
            file_name=path.name,
            file_path=path.as_posix(),
            file_size_mb=size_mb,
            sha256=sha256_hex,
            total_pages=total_pages,
            pdf_type=pdf_type,
            has_outline_bookmarks=(outline_count > 0),
            outline_count=outline_count,
            distinct_page_labels_count=non_trivial_labels,
            median_width_pt=med_w,
            median_height_pt=med_h,
            foldout_page_indices=foldout_indices,
            sampled_pages=sampled_pages,
            metadata=clean_meta,
        )

    @staticmethod
    def _select_sample_indices(total_pages: int, count: int) -> list[int]:
        if total_pages <= count:
            return list(range(total_pages))
        # Always include first 3 pages (cover/title/preface), middle pages, and last 2 pages (plates)
        fixed = {0, min(1, total_pages - 1), min(4, total_pages - 1), total_pages - 1}
        step = max(1, total_pages // max(1, count - len(fixed)))
        for idx in range(0, total_pages, step):
            if len(fixed) >= count:
                break
            fixed.add(idx)
        return sorted(fixed)

    @staticmethod
    def _count_page_images(page: Any) -> int:
        try:
            resources = page.get("/Resources")
            if not resources:
                return 0
            xobj = resources.get("/XObject")
            if not xobj:
                return 0
            xobj = xobj.get_object()
            return sum(
                1
                for key in xobj
                if xobj[key].get_object().get("/Subtype") == "/Image"
            )
        except Exception:
            return 0

    def _count_outline_items(self, reader: PdfReader) -> int:
        try:
            outline = reader.outline
            if not outline:
                return 0
            return self._flatten_outline_len(outline)
        except Exception:
            return 0

    def _flatten_outline_len(self, items: list[Any]) -> int:
        total = 0
        for item in items:
            if isinstance(item, list):
                total += self._flatten_outline_len(item)
            else:
                total += 1
        return total
