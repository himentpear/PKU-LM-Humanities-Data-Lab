"""Direct vector text, coordinate, heading, figure, and artifact ingestor for native digital PDFs.

Designed for Channel A/B archaeological reports (such as 《景德镇落马桥遗址》 Volumes 1-6)
exported from Adobe InDesign, extracting:
- Exact line/block bounding boxes in Canvas Space & Canonical Space (10000 x H)
- Printed page numbers from running headers/footers + global Viterbi DP Page Sequence Alignment
- Running header negative-rule filtering
- Automatic multi-level Table of Contents (IiifRange + RangeItem)
- Archaeological specimen codes (Artifact + ArtifactOccurrence) & Figure citations (PlateFigure + FigureMention)
- Domain-safe merged IndexEntry records
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.ingest.layout import order_lines

from src.alignment.sequence_dp import (
    PageObservation,
    PageSequenceAligner,
    detect_running_headers,
    merge_domain_page_ranges,
)
from src.alto.parser import (
    FIGURE_CITATION_PATTERN,
    AltoIngestor,
    _rect_to_polygon,
)
from src.db.models import (
    AltoBlock,
    AltoLine,
    AltoPage,
    Artifact,
    ArtifactOccurrence,
    Canvas,
    FigureMention,
    IiifAnnotation,
    IiifRange,
    IndexEntry,
    Manifest,
    PlateFigure,
    RangeItem,
    Work,
)

# Running header pattern in 《景德镇落马桥遗址》: e.g. "· 11 ·第一部分·第二章..." or "景德镇落马桥遗址... · 12 ·"
RUNNING_HEADER_PAGE_RE = re.compile(
    r"(?:^[·•\-\s]*(\d{1,4})\s*[·•\-]+)|(?:[·•\-]+\s*(\d{1,4})\s*[·•\-\s]*$)"
)
ROMAN_PAGE_RE = re.compile(r"^[·•\-\s]*([IVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)[·•\-\s]*$", re.IGNORECASE)

# Heading hierarchy patterns for archaeological reports
HEADING_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (1, re.compile(r"^(第[一二三四五六七八九十]+部分)\s*(.*)$")),
    (1, re.compile(r"^(前\s*言|序\s*言|凡\s*例|目\s*录|插图目录|附表目录|结\s*语|后\s*记)$")),
    (2, re.compile(r"^(第[一二三四五六七八九十百]+章)\s*(.*)$")),
    (3, re.compile(r"^(第[一二三四五六七八九十百]+节)\s*(.*)$")),
    (4, re.compile(r"^([一二三四五六七八九十百]+)[、.．]\s*(\S.*)$")),
    (5, re.compile(r"^[（(]([一二三四五六七八九十百]+)[)）]\s*(\S.*)$")),
]

# Figure caption line pattern, e.g. "图1.2.1 落马桥遗址位置示意图" or "图 2.1.3 ..."
FIGURE_CAPTION_LINE_RE = re.compile(
    r"^(?P<fig_no>(?:彩版|图版|插图|附图|图)\s*[一二三四五六七八九十百零0-9]+(?:\.[0-9]+)*(?:[\-—][0-9]+)?)"
    r"(?:\s+(?P<title>\S.*))?$"
)


@dataclass
class ExtractedLine:
    """Single line of vector text reconstructed from PDF text matrix callbacks."""

    text: str
    x_pt: float
    y_top_pt: float
    w_pt: float
    h_pt: float
    font_size: float
    confidence: Optional[float] = None


@dataclass
class ExtractedPageData:
    """Extracted vector lines and page-level cues for one PDF page."""

    scan_index: int
    pdf_page_index: int
    source_page_label: str
    width_pt: float
    height_pt: float
    image_count: int
    lines: list[ExtractedLine] = field(default_factory=list)
    detected_page_no_raw: Optional[str] = None
    layout_hint: str = "body"
    method: str = "pymupdf"
    image_path: Optional[str] = None
    graphics: list[list[float]] = field(default_factory=list)
    quality_issues: list[str] = field(default_factory=list)


class DigitalPdfIngestor:
    """High-speed vector PDF ingestor for native digital archaeological reports."""

    CANONICAL_WIDTH = 10000
    PIXEL_SCALE = 2.0  # Convert PDF pt (72 DPI) to 144 DPI Canvas pixels

    def __init__(self, session: Session, include_annotations: bool = True,
                 base_url: str = "http://127.0.0.1:8000/iiif") -> None:
        self.session = session
        self.include_annotations = include_annotations
        self.base_url = base_url.rstrip("/")
        import yaml
        config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/heading_rules.yaml").read_text(encoding="utf-8"))
        self.heading_rules = [(r["level"], re.compile(r["regex"])) for r in
                              sorted(config["heading_rules"], key=lambda r:r["priority"], reverse=True)]
        self.running_header_rules = config["running_header_rules"]

    def ingest_pdf(
        self,
        pdf_path: str | Path,
        work_slug: str,
        work_title: str,
        volume_number: int = 1,
        volume_label: str = "第一卷",
        site_name: Optional[str] = None,
        max_pages: Optional[int] = None,
        prepared_pages: Optional[list[ExtractedPageData]] = None,
        source_sha256: Optional[str] = None,
    ) -> dict[str, Any]:
        """Extract pages, align multi-domain page numbers via DP, and populate V2 database."""
        t0 = time.perf_counter()
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")

        reader = PdfReader(str(path), strict=False)
        total_pdf_pages = len(reader.pages)
        num_to_process = min(total_pdf_pages, max_pages) if max_pages else total_pdf_pages

        try:
            page_labels = list(reader.page_labels)
        except Exception:
            page_labels = [str(i + 1) for i in range(total_pdf_pages)]

        # 1. Create or retrieve Work & Manifest
        work = self.session.scalar(select(Work).where(Work.slug == work_slug))
        if work is None:
            work = Work(slug=work_slug, title=work_title, site_name=site_name, schema_version="2.0")
        self.session.add(work)
        self.session.flush()

        existing = self.session.scalar(select(Manifest).where(
            Manifest.work_id == work.id, Manifest.volume_number == volume_number))
        if existing is not None:
            raise ValueError("Volume already exists; use the resumable pipeline or a new volume/database.")

        manifest = Manifest(
            work_id=work.id,
            volume_number=volume_number,
            label=volume_label,
            source_pdf_path=path.as_posix(),
            source_pdf_sha256=source_sha256,
            pdf_type="hybrid_ocr" if prepared_pages and any(p.method != "pymupdf" for p in prepared_pages) else "digital",
            total_pages=num_to_process,
        )
        self.session.add(manifest)
        self.session.flush()

        # 2. Pass 1: Extract vector text lines & coordinates from PDF pages
        extracted_pages: list[ExtractedPageData] = []
        observations: list[PageObservation] = []

        if prepared_pages is not None:
            if len(prepared_pages) != num_to_process:
                raise ValueError("Prepared page count does not match requested page count")
            extracted_pages = prepared_pages
        import pymupdf
        native_document = pymupdf.open(path) if prepared_pages is None else None
        for idx in range(0 if prepared_pages is not None else num_to_process):
            page = reader.pages[idx]
            box = native_document[idx].rect
            w_pt = float(box.width)
            h_pt = float(box.height)
            src_label = str(page_labels[idx]) if idx < len(page_labels) else str(idx + 1)

            lines = self.extract_native_lines(native_document[idx])
            img_count = self._count_images(page)
            page_no_raw, layout_hint = self._detect_page_number_and_domain_hint(
                idx, lines, h_pt
            )

            ep = ExtractedPageData(
                scan_index=idx + 1,
                pdf_page_index=idx,
                source_page_label=src_label,
                width_pt=w_pt,
                height_pt=h_pt,
                image_count=img_count,
                lines=lines,
                detected_page_no_raw=page_no_raw,
                layout_hint=layout_hint,
            )
            extracted_pages.append(ep)
        if native_document is not None:
            native_document.close()

        for ep in extracted_pages:
            observations.append(
                PageObservation(
                    scan_index=ep.scan_index,
                    pdf_page_index=ep.pdf_page_index,
                    source_page_label=ep.source_page_label if "/PageLabels" in reader.trailer["/Root"] else None,
                    ocr_page_label_raw=ep.detected_page_no_raw,
                    ocr_confidence=0.98 if ep.detected_page_no_raw else 0.0,
                    layout_hint=ep.layout_hint,
                    is_foldout=(ep.width_pt > statistics.median(p.width_pt for p in extracted_pages) * 1.35),
                )
            )

        # 3. Pass 2: Global Viterbi DP Page Sequence Alignment
        aligner = PageSequenceAligner()
        aligned_results, anomalies = aligner.align_sequence(observations)

        # 4. Pass 3: Populate Canvases, AltoPages, AltoBlocks, AltoLines, Plates, Artifacts
        canvases_by_scan: dict[int, Canvas] = {}
        all_blocks: list[AltoBlock] = []
        artifacts_by_code: dict[str, Artifact] = {}
        artifact_page_tuples: dict[str, list[tuple[str, Optional[int], str]]] = {}
        heading_candidates: list[tuple[Canvas, AltoBlock, int, str, str]] = []

        total_lines_count = 0
        total_figures_count = 0
        total_occurrences_count = 0
        footnote_count = reference_count = 0

        for ep, aligned in zip(extracted_pages, aligned_results):
            canvas_w = max(1, int(round(ep.width_pt * self.PIXEL_SCALE)))
            canvas_h = max(1, int(round(ep.height_pt * self.PIXEL_SCALE)))
            canonical_w = self.CANONICAL_WIDTH
            canonical_h = max(1, int(round(canonical_w * (ep.height_pt / max(1.0, ep.width_pt)))))

            canvas = Canvas(
                manifest_id=manifest.id,
                sequence_index=ep.scan_index,
                scan_index=ep.scan_index,
                pdf_page_index=ep.pdf_page_index,
                source_page_label=ep.source_page_label,
                printed_page_label=aligned.original_page_label,
                page_domain=aligned.page_domain,
                page_ordinal=aligned.page_ordinal,
                page_type="toc" if aligned.page_domain == "toc" else "plate" if aligned.page_domain == "plate" else "blank" if ep.method == "blank" else "text",
                mapping_confidence=aligned.mapping_confidence,
                mapping_status=aligned.mapping_status,
                alignment_signals=aligned.signals,
                field_candidates=aligned.candidates,
                width=canvas_w,
                height=canvas_h,
                iiif_image_service_id=f"{self.base_url}/image/{manifest.id}/{ep.scan_index}",
                image_storage_path=ep.image_path,
            )
            self.session.add(canvas)
            canvases_by_scan[ep.scan_index] = canvas

        self.session.flush()

        # Persist alignment revisions and anomaly review tasks
        aligner.apply_alignment_to_db(self.session, manifest.id, aligned_results, anomalies)

        for ep in extracted_pages:
            canvas = canvases_by_scan[ep.scan_index]
            canonical_w = self.CANONICAL_WIDTH
            canonical_h = max(1, int(round(canonical_w * (ep.height_pt / max(1.0, ep.width_pt)))))
            c_scale_x = canonical_w / max(1.0, ep.width_pt)
            c_scale_y = canonical_h / max(1.0, ep.height_pt)

            alto_page = AltoPage(
                canvas_id=canvas.id,
                measurement_unit="point",
                alto_width=ep.width_pt,
                alto_height=ep.height_pt,
                canonical_width=canonical_w,
                canonical_height=canonical_h,
                transform_type="linear",
                source_to_canonical={
                    "type": "homography",
                    "matrix": [c_scale_x, 0.0, 0.0, 0.0, c_scale_y, 0.0, 0.0, 0.0, 1.0],
                },
                print_space_x=int(round(ep.width_pt * 0.08 * self.PIXEL_SCALE)),
                print_space_y=int(round(ep.height_pt * 0.08 * self.PIXEL_SCALE)),
                print_space_w=int(round(ep.width_pt * 0.84 * self.PIXEL_SCALE)),
                print_space_h=int(round(ep.height_pt * 0.84 * self.PIXEL_SCALE)),
                ocr_engine=ep.method,
                ocr_version="3.0",
                avg_confidence=(sum(l.confidence for l in ep.lines if l.confidence is not None) /
                    max(1, sum(l.confidence is not None for l in ep.lines))) if any(l.confidence is not None for l in ep.lines) else None,
            )
            self.session.add(alto_page)

            from src.ingest.footnotes import detect as detect_footnotes, persist as persist_footnotes
            footnote_plan = detect_footnotes(ep)
            line_indices = {id(line): i for i,line in enumerate(ep.lines)}
            grouped_blocks = self._group_lines_into_blocks(ep.lines, ep.width_pt, ep.height_pt, footnote_plan.groups)
            footnote_lines = {}

            for b_idx, (block_type, page_zone, b_lines) in enumerate(grouped_blocks, start=1):
                min_x = min(ln.x_pt for ln in b_lines)
                min_y = min(ln.y_top_pt for ln in b_lines)
                max_x = max(ln.x_pt + ln.w_pt for ln in b_lines)
                max_y = max(ln.y_top_pt + ln.h_pt for ln in b_lines)

                bx = int(round(min_x * self.PIXEL_SCALE))
                by = int(round(min_y * self.PIXEL_SCALE))
                bw = max(1, int(round((max_x - min_x) * self.PIXEL_SCALE)))
                bh = max(1, int(round((max_y - min_y) * self.PIXEL_SCALE)))

                cx = int(round(min_x * c_scale_x))
                cy = int(round(min_y * c_scale_y))
                cw = max(1, int(round((max_x - min_x) * c_scale_x)))
                ch = max(1, int(round((max_y - min_y) * c_scale_y)))

                block_text = "\n".join(ln.text for ln in b_lines)
                prot_spans = AltoIngestor.extract_protected_spans(block_text)

                alto_block = AltoBlock(
                    canvas_id=canvas.id,
                    alto_element_id=f"P{ep.scan_index:04d}_TB_{b_idx:03d}",
                    block_type=block_type,
                    page_zone=page_zone,
                    reading_order=b_idx,
                    column_index=0,
                    x=bx,
                    y=by,
                    w=bw,
                    h=bh,
                    canonical_polygon=_rect_to_polygon(cx, cy, cw, ch),
                    raw_ocr=block_text,
                    normalized_ocr=block_text,
                    content_text=block_text,
                    raw_confidence=None,
                    review_status="auto_accepted",
                    protected_spans=prot_spans,
                    provenance_meta={"method": ep.method, "source_sha256": source_sha256},
                )
                self.session.add(alto_block)
                self.session.flush()
                all_blocks.append(alto_block)

                # Check if this block is a heading in body/front_matter (excluding TOC dotted lines)
                if block_type != "footnote" and page_zone == "print_space" and canvas.page_domain in {"body", "appendix"}:
                    first_line_str = b_lines[0].text.strip()
                    if "……" not in first_line_str and ".." not in first_line_str:
                        h_match = self._match_heading(first_line_str, b_lines[0].font_size)
                        if h_match is not None:
                            h_level, sec_num, sec_title = h_match
                            alto_block.block_type = "heading"
                            heading_candidates.append(
                                (canvas, alto_block, h_level, sec_num, sec_title)
                            )

                # Check if this block is a figure caption (in body pages, not TOC)
                cap_m = (
                    FIGURE_CAPTION_LINE_RE.match(b_lines[0].text.strip())
                    if block_type != "footnote" and canvas.page_domain in {"body", "plate", "appendix"}
                    else None
                )
                if cap_m and "……" not in block_text:
                    fig_no = re.sub(r"\s+", "", cap_m.group("fig_no"))
                    alto_block.block_type = "caption"
                    spec_codes = AltoIngestor.extract_artifact_codes(block_text)
                    pf = PlateFigure(
                        manifest_id=manifest.id,
                        canvas_id=canvas.id,
                        caption_block_id=alto_block.id,
                        figure_category="line_drawing",
                        figure_number=fig_no,
                        caption_text=block_text,
                        artifact_codes=spec_codes,
                        crop_x=max(0, bx - 40),
                        crop_y=max(0, by - 400),
                        crop_w=min(canvas.width, bw + 80),
                        crop_h=400,
                    )
                    self.session.add(pf)
                    total_figures_count += 1

                # Create AltoLines & extract ArtifactOccurrences
                char_cursor = 0
                for l_idx, ln in enumerate(b_lines, start=1):
                    lx = int(round(ln.x_pt * self.PIXEL_SCALE))
                    ly = int(round(ln.y_top_pt * self.PIXEL_SCALE))
                    lw = max(1, int(round(ln.w_pt * self.PIXEL_SCALE)))
                    lh = max(1, int(round(ln.h_pt * self.PIXEL_SCALE)))

                    lcx = int(round(ln.x_pt * c_scale_x))
                    lcy = int(round(ln.y_top_pt * c_scale_y))
                    lcw = max(1, int(round(ln.w_pt * c_scale_x)))
                    lch = max(1, int(round(ln.h_pt * c_scale_y)))

                    l_spans = AltoIngestor.extract_protected_spans(ln.text)
                    start_pos = char_cursor
                    end_pos = start_pos + len(ln.text)
                    char_cursor = end_pos + 1

                    alto_line = AltoLine(
                        block_id=alto_block.id,
                        canvas_id=canvas.id,
                        alto_element_id=f"P{ep.scan_index:04d}_TL_{b_idx:03d}_{l_idx:02d}",
                        line_index=l_idx,
                        x=lx,
                        y=ly,
                        w=lw,
                        h=lh,
                        canonical_polygon=_rect_to_polygon(lcx, lcy, lcw, lch),
                        raw_ocr=ln.text,
                        normalized_ocr=ln.text,
                        content_text=ln.text,
                        line_confidence=ln.confidence,
                        review_status="auto_accepted",
                        protected_spans=l_spans,
                        char_start_in_block=start_pos,
                        char_end_in_block=end_pos,
                    )
                    self.session.add(alto_line)
                    footnote_lines[line_indices[id(ln)]] = alto_line
                    total_lines_count += 1

                    if self.include_annotations:
                        self.session.add(
                            IiifAnnotation(
                                canvas_id=canvas.id,
                                alto_block_id=alto_block.id,
                                motivation="supplementing",
                                target_xywh=f"{lx},{ly},{lw},{lh}",
                                body_text=ln.text,
                                creator_type="machine",
                            )
                        )

                    # Extract Artifact specimens mentioned in this line (exclude TOC listing pages)
                    if canvas.page_domain == "toc":
                        continue
                    for sp in l_spans:
                        if sp["type"] == "ARTIFACT_ID":
                            code = sp["text"]
                            artifact = artifacts_by_code.get(code)
                            if artifact is None:
                                unit_part = code.split("∶")[0].split(":")[0]
                                artifact = Artifact(
                                    work_id=work.id,
                                    artifact_no=code,
                                    excavation_unit=unit_part,
                                    review_status="auto_accepted",
                                )
                                self.session.add(artifact)
                                self.session.flush()
                                artifacts_by_code[code] = artifact

                            role = "captioned" if alto_block.block_type == "caption" else "mentioned"
                            self.session.add(
                                ArtifactOccurrence(
                                    artifact_id=artifact.id,
                                    canvas_id=canvas.id,
                                    layout_block_id=alto_block.id,
                                    role=role,
                                    confidence=0.99,
                                    context_snippet=ln.text[:160],
                                    target_xywh=f"{lx},{ly},{lw},{lh}",
                                )
                            )
                            total_occurrences_count += 1
                            artifact_page_tuples.setdefault(code, []).append(
                                (
                                    canvas.page_domain,
                                    canvas.page_ordinal,
                                    canvas.printed_page_label or str(canvas.scan_index),
                                )
                            )

            self.session.flush()
            note_n, ref_n = persist_footnotes(self.session, canvas, ep, footnote_plan, footnote_lines)
            footnote_count += note_n
            reference_count += ref_n

        # 5. Apply Running Header negative rule across all pages
        running_headers = detect_running_headers(all_blocks, **self.running_header_rules)

        # 6. Build hierarchical IIIF Ranges (TOC) from non-running-header headings
        toc_count = self._build_toc_ranges(manifest.id, heading_candidates)

        # 7. Build domain-merged IndexEntries for top artifacts
        index_count = 0
        for code, occ_tuples in artifact_page_tuples.items():
            merged = merge_domain_page_ranges(occ_tuples)
            art = artifacts_by_code[code]
            self.session.add(
                IndexEntry(
                    manifest_id=manifest.id,
                    index_type="artifact",
                    key=code,
                    display_text=code,
                    target_type="artifact",
                    target_id=art.id,
                    page_ranges=merged,
                    formatted_pages=", ".join(r["display"] for r in merged),
                )
            )
            index_count += 1

        self.session.flush()
        linker = AltoIngestor(self.session)
        for canvas in canvases_by_scan.values():
            linker.link_figure_mentions_on_canvas(canvas.id)
        elapsed = round(time.perf_counter() - t0, 2)

        return {
            "work_id": work.id,
            "manifest_id": manifest.id,
            "pages_ingested": num_to_process,
            "blocks_count": len(all_blocks),
            "lines_count": total_lines_count,
            "footnotes_count": footnote_count,
            "footnote_references_count": reference_count,
            "running_headers_filtered": len(running_headers),
            "toc_ranges_count": toc_count,
            "figures_count": total_figures_count,
            "artifacts_count": len(artifacts_by_code),
            "artifact_occurrences_count": total_occurrences_count,
            "index_entries_count": index_count,
            "anomalies_count": len(anomalies),
            "elapsed_seconds": elapsed,
        }

    def _extract_page_lines(
        self, page: Any, w_pt: float, h_pt: float
    ) -> list[ExtractedLine]:
        # Resolve the page through MuPDF so CropBox, font scaling and text matrices
        # are handled by the PDF renderer, rather than estimating glyph widths.
        import pymupdf
        stream = page.pdf.stream
        position = stream.tell()
        try:
            stream.seek(0)
            with pymupdf.open(stream=stream.read(), filetype="pdf") as document:
                return self.extract_native_lines(document[page.page_number])
        finally:
            stream.seek(position)

    @staticmethod
    def extract_native_lines(page: Any) -> list[ExtractedLine]:
        lines = []
        for block in page.get_text("dict", sort=True)["blocks"]:
            for line in block.get("lines", []):
                spans = line["spans"]
                text = "".join(span["text"] for span in spans).strip()
                if not text:
                    continue
                # MuPDF text coordinates are unrotated. Match the displayed page.
                import pymupdf
                rect = pymupdf.Rect(line["bbox"]) * page.rotation_matrix
                rect &= page.rect
                if rect.is_empty:
                    continue
                lines.append(ExtractedLine(text, rect.x0, rect.y0, rect.width,
                                           rect.height, max(s["size"] for s in spans)))
        return order_lines(lines)

    @staticmethod
    def _detect_page_number_and_domain_hint(
        pdf_idx: int, lines: list[ExtractedLine], h_pt: float
    ) -> tuple[Optional[str], str]:
        if pdf_idx == 0:
            return None, "cover"
        if not lines:
            return None, "front_matter" if pdf_idx < 4 else "body"

        full_text_top = " ".join(ln.text for ln in lines[:3])
        if pdf_idx < 4 and (
            "图书在版编目" in full_text_top
            or "成果之一" in full_text_top
            or len(lines) <= 4
        ):
            return None, "front_matter"

        # Detect TOC pages by top running header ("· 5 ·目 录", "· 11 ·插图目录") or dotted leader lines
        toc_line_hits = sum(
            1 for ln in lines if re.search(r"(?:…{3,}|\.{5,})\s*[（(]?\d+[)）]?\s*$", ln.text)
        )
        is_toc_header = any(
            re.sub(r"[\s·•\d]", "", ln.text) in {"目录","插图目录","附表目录","彩版目录","图版目录"}
            for ln in lines[:5]
        )
        if is_toc_header or toc_line_hits >= 2:
            for candidate_ln in lines:
                if not (candidate_ln.y_top_pt <= h_pt * 0.14 or candidate_ln.y_top_pt >= h_pt * 0.86):
                    continue
                m_hdr = RUNNING_HEADER_PAGE_RE.search(candidate_ln.text.strip())
                if m_hdr:
                    return m_hdr.group(1) or m_hdr.group(2), "toc"
                m_rom = ROMAN_PAGE_RE.match(candidate_ln.text.strip())
                if m_rom:
                    return m_rom.group(1), "toc"
            return None, "toc"

        # Check top running header or bottom footer for "· 11 ·第一部分..."
        for candidate_ln in lines:
            if candidate_ln.y_top_pt <= h_pt * 0.14 or candidate_ln.y_top_pt >= h_pt * 0.86:
                m_hdr = RUNNING_HEADER_PAGE_RE.search(candidate_ln.text.strip())
                if m_hdr:
                    num_str = m_hdr.group(1) or m_hdr.group(2)
                    return num_str, "body"
                m_rom = ROMAN_PAGE_RE.match(candidate_ln.text.strip())
                if m_rom:
                    return m_rom.group(1), "front_matter"
                if re.fullmatch(r"\d{1,4}", candidate_ln.text.strip()):
                    return candidate_ln.text.strip(), "body"

        return None, "body"

    def _group_lines_into_blocks(
        self, lines: list[ExtractedLine], _w_pt: float, h_pt: float, note_groups: dict | None = None
    ) -> list[tuple[str, str, list[ExtractedLine]]]:
        if not lines:
            return []

        blocks: list[tuple[str, str, list[ExtractedLine]]] = []
        curr_lines: list[ExtractedLine] = []
        curr_zone = "print_space"
        note_groups = note_groups or {}
        line_index = {id(line): i for i,line in enumerate(lines)}

        def block_type():
            if line_index[id(curr_lines[0])] in note_groups:
                return "footnote"
            return "running_header" if curr_zone == "margin_top" else "paragraph"

        def zone_of(ln: ExtractedLine) -> str:
            if ln.y_top_pt < h_pt * 0.10:
                return "margin_top"
            if ln.y_top_pt > h_pt * 0.90:
                return "margin_bottom"
            return "print_space"

        for ln in lines:
            z = zone_of(ln)
            if not curr_lines:
                curr_lines = [ln]
                curr_zone = z
                continue

            prev = curr_lines[-1]
            v_gap = ln.y_top_pt - (prev.y_top_pt + prev.h_pt)
            is_new_para = (
                z != curr_zone
                or note_groups.get(line_index[id(ln)]) != note_groups.get(line_index[id(prev)])
                or v_gap > max(10.0, prev.h_pt * 0.85)
                or abs(ln.font_size - prev.font_size) >= 1.8
                or any(p.match(ln.text.strip()) for _, p in self.heading_rules)
                or abs(ln.x_pt - prev.x_pt) > _w_pt * 0.35
                or FIGURE_CAPTION_LINE_RE.match(ln.text.strip()) is not None
            )

            if is_new_para:
                b_type = block_type()
                blocks.append((b_type, curr_zone, curr_lines))
                curr_lines = [ln]
                curr_zone = z
            else:
                curr_lines.append(ln)

        if curr_lines:
            b_type = block_type()
            blocks.append((b_type, curr_zone, curr_lines))

        return blocks

    def _match_heading(self, text: str, font_size: float) -> Optional[tuple[int, str, str]]:
        cleaned = text.strip()
        if len(cleaned) > 48:
            return None
        for level, pattern in self.heading_rules:
            m = pattern.match(cleaned)
            if m:
                if m.lastindex and m.lastindex >= 2:
                    sec_num = m.group(1).strip()
                    sec_title = m.group(2).strip() or sec_num
                    if level in (4, 5):
                        sec_num = f"{sec_num}、" if level == 4 else f"（{sec_num}）"
                    return level, sec_num, sec_title
                return level, "", cleaned
        return None

    def _build_toc_ranges(
        self,
        manifest_id: str,
        heading_candidates: list[tuple[Canvas, AltoBlock, int, str, str]],
    ) -> int:
        stack: list[IiifRange] = []
        sort_counters: dict[Optional[str], int] = {}
        created_ranges: list[IiifRange] = []

        for canvas, blk, level, sec_num, sec_title in heading_candidates:
            # Skip if running header detector reclassified this block
            if blk.block_type != "heading" or blk.page_zone != "print_space":
                continue

            while stack and stack[-1].level >= level:
                stack.pop()

            parent_id = stack[-1].id if stack else None
            sort_order = sort_counters.get(parent_id, 0) + 1
            sort_counters[parent_id] = sort_order

            r_node = IiifRange(
                manifest_id=manifest_id,
                parent_id=parent_id,
                range_type="toc",
                level=level,
                sort_order=sort_order,
                section_number=sec_num or None,
                title=sec_title,
                normalized_title=f"{sec_num} {sec_title}".strip(),
                source_method="auto_heading",
                heading_block_id=blk.id,
                start_canvas_id=canvas.id,
                start_label=canvas.printed_page_label,
                start_xywh=blk.xywh,
            )
            self.session.add(r_node)
            self.session.flush()

            self.session.add(
                RangeItem(
                    range_id=r_node.id,
                    canvas_id=canvas.id,
                    sequence_order=1,
                    fragment_xywh=blk.xywh,
                )
            )
            stack.append(r_node)
            created_ranges.append(r_node)

        return len(created_ranges)

    @staticmethod
    def _count_images(page: Any) -> int:
        try:
            res = page.get("/Resources")
            if not res or "/XObject" not in res:
                return 0
            xobj = res["/XObject"].get_object()
            return sum(
                1 for k in xobj if xobj[k].get_object().get("/Subtype") == "/Image"
            )
        except Exception:
            return 0
