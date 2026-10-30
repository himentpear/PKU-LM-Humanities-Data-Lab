"""ALTO XML (v3/v4) parser and V2 Three-Space Coordinate Normalizer for IIIF Canvases."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    AltoBlock,
    AltoLine,
    AltoPage,
    AltoToken,
    Canvas,
    FigureMention,
    IiifAnnotation,
    PlateFigure,
)

# Regex for archaeological report figure/plate citations, e.g. "图一二", "彩版叁", "图版贰拾:2", "图版3.2.482"
FIGURE_CITATION_PATTERN = re.compile(
    r"(?P<prefix>彩版|图版|插图|附图|图)\s*"
    r"(?P<num>[一二三四五六七八九十百零壹贰叁肆伍陆柒捌玖拾0-9]+(?:\.[0-9]+)*)"
    r"(?:[\s,，:：∶\-—·]+(?P<sub>[0-9a-zA-Z一二三四五六七八九十～~]+))?"
)

# Regex for protected archaeological identifiers (including 景德镇落马桥遗址 formats like T3扩G3∶310, T19③c∶328)
PROTECTED_ARCH_ID_PATTERN = re.compile(
    r"(?P<ARTIFACT_ID>[THYMJGFCQ]\d+(?:[扩采a-zA-Z0-9①②③④⑤⑥⑦⑧⑨⑩]+)*[:：∶]\d+(?:-\d+)?)"
    r"|(?P<EXCAVATION_UNIT>\b(?:[IVXⅠⅡⅢⅣⅤ]+)?[THYMJGFCQ]\d+(?:[扩采a-zA-Z0-9①②③④⑤⑥⑦⑧⑨⑩]+)*\b)"
    r"|(?P<MEASUREMENT>\d+(?:\.\d+)?\s*(?:cm|mm|m|厘米|毫米|米))"
)


def _strip_ns(tag: str) -> str:
    """Strip XML namespace URI from element tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _rect_to_polygon(x: int, y: int, w: int, h: int) -> list[list[int]]:
    """Convert rectangular bbox (x, y, w, h) to 4-vertex clockwise polygon."""
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


class AltoIngestor:
    """Ingests ALTO XML into V2 Three-Space coordinates (Source -> Canonical & Canvas)
    with Four-State Text preservation and Protected Archaeological ID detection.
    """

    CANONICAL_BASE_WIDTH = 10000

    def __init__(self, session: Session, include_tokens: bool = True) -> None:
        self.session = session
        self.include_tokens = include_tokens

    def ingest_alto_file(
        self,
        canvas_id: str,
        alto_xml_path: str | Path,
        ocr_engine: str = "ALTO-Importer",
        ocr_version: str = "4.0",
    ) -> AltoPage:
        """Parse an ALTO XML file from disk and populate V2 ALTO + IIIF Annotation tables."""
        xml_path = Path(alto_xml_path)
        xml_content = xml_path.read_text(encoding="utf-8")
        return self.ingest_alto_string(
            canvas_id=canvas_id,
            xml_content=xml_content,
            alto_xml_path=xml_path.as_posix(),
            ocr_engine=ocr_engine,
            ocr_version=ocr_version,
        )

    def ingest_alto_string(
        self,
        canvas_id: str,
        xml_content: str,
        alto_xml_path: Optional[str] = None,
        ocr_engine: str = "ALTO-Importer",
        ocr_version: str = "4.0",
    ) -> AltoPage:
        """Parse ALTO XML string, map Source -> Canonical (10000xH) & Canvas pixels, and persist."""
        canvas = self.session.get(Canvas, canvas_id)
        if canvas is None:
            raise ValueError(f"Canvas not found: {canvas_id}")

        root = ET.fromstring(xml_content)

        measurement_unit = "pixel"
        for elem in root.iter():
            if _strip_ns(elem.tag) == "MeasurementUnit" and elem.text:
                measurement_unit = elem.text.strip()
                break

        page_elem = next((el for el in root.iter() if _strip_ns(el.tag) == "Page"), None)
        if page_elem is None:
            raise ValueError("ALTO XML is missing a <Page> element.")

        alto_w = float(page_elem.attrib.get("WIDTH", canvas.width))
        alto_h = float(page_elem.attrib.get("HEIGHT", canvas.height))

        # Source -> Canvas pixel scaling
        scale_x = canvas.width / alto_w if alto_w > 0 else 1.0
        scale_y = canvas.height / alto_h if alto_h > 0 else 1.0

        # Canonical Page Space: width = 10000, height = 10000 * (H / W)
        canonical_w = self.CANONICAL_BASE_WIDTH
        aspect_ratio = (canvas.height / canvas.width) if canvas.width > 0 else (alto_h / max(1.0, alto_w))
        canonical_h = int(round(canonical_w * aspect_ratio))
        canon_scale_x = canonical_w / alto_w if alto_w > 0 else 1.0
        canon_scale_y = canonical_h / alto_h if alto_h > 0 else 1.0

        def norm_box(elem: ET.Element) -> tuple[int, int, int, int]:
            hx = float(elem.attrib.get("HPOS", 0))
            vy = float(elem.attrib.get("VPOS", 0))
            bw = float(elem.attrib.get("WIDTH", 0))
            bh = float(elem.attrib.get("HEIGHT", 0))
            return (
                int(round(hx * scale_x)),
                int(round(vy * scale_y)),
                max(1, int(round(bw * scale_x))),
                max(1, int(round(bh * scale_y))),
            )

        def canonical_poly(elem: ET.Element) -> list[list[int]]:
            hx = float(elem.attrib.get("HPOS", 0))
            vy = float(elem.attrib.get("VPOS", 0))
            bw = float(elem.attrib.get("WIDTH", 0))
            bh = float(elem.attrib.get("HEIGHT", 0))
            cx = int(round(hx * canon_scale_x))
            cy = int(round(vy * canon_scale_y))
            cw = max(1, int(round(bw * canon_scale_x)))
            ch = max(1, int(round(bh * canon_scale_y)))
            return _rect_to_polygon(cx, cy, cw, ch)

        print_space_elem = next(
            (el for el in page_elem.iter() if _strip_ns(el.tag) == "PrintSpace"), page_elem
        )
        ps_x, ps_y, ps_w, ps_h = norm_box(print_space_elem)

        alto_page = AltoPage(
            canvas_id=canvas.id,
            alto_xml_path=alto_xml_path,
            measurement_unit=measurement_unit,
            alto_width=alto_w,
            alto_height=alto_h,
            canonical_width=canonical_w,
            canonical_height=canonical_h,
            transform_type="linear",
            source_to_canonical={
                "type": "homography",
                "matrix": [canon_scale_x, 0.0, 0.0, 0.0, canon_scale_y, 0.0, 0.0, 0.0, 1.0],
            },
            print_space_x=ps_x,
            print_space_y=ps_y,
            print_space_w=ps_w,
            print_space_h=ps_h,
            ocr_engine=ocr_engine,
            ocr_version=ocr_version,
        )
        self.session.add(alto_page)
        self.session.flush()

        all_confidences: list[float] = []
        reading_order_counter = 0

        def process_block_container(
            container: ET.Element,
            parent_block_id: Optional[str] = None,
            default_zone: str = "print_space",
        ) -> None:
            nonlocal reading_order_counter
            for child in container:
                tag = _strip_ns(child.tag)
                if tag in ("TopMargin", "BottomMargin", "LeftMargin", "RightMargin"):
                    zone_map = {
                        "TopMargin": "margin_top",
                        "BottomMargin": "margin_bottom",
                        "LeftMargin": "margin_left",
                        "RightMargin": "margin_right",
                    }
                    process_block_container(child, parent_block_id=None, default_zone=zone_map[tag])
                elif tag == "PrintSpace":
                    process_block_container(child, parent_block_id=None, default_zone="print_space")
                elif tag == "ComposedBlock":
                    reading_order_counter += 1
                    bx, by, bw, bh = norm_box(child)
                    comp_block = AltoBlock(
                        canvas_id=canvas.id,
                        parent_block_id=parent_block_id,
                        alto_element_id=child.attrib.get("ID", f"CB_{reading_order_counter}"),
                        block_type=child.attrib.get("TYPE", "composed"),
                        page_zone=default_zone,
                        reading_order=reading_order_counter,
                        x=bx,
                        y=by,
                        w=bw,
                        h=bh,
                        canonical_polygon=canonical_poly(child),
                    )
                    self.session.add(comp_block)
                    self.session.flush()
                    process_block_container(child, parent_block_id=comp_block.id, default_zone=default_zone)

                elif tag in ("GraphicalElement", "Illustration"):
                    reading_order_counter += 1
                    bx, by, bw, bh = norm_box(child)
                    gfx_block = AltoBlock(
                        canvas_id=canvas.id,
                        parent_block_id=parent_block_id,
                        alto_element_id=child.attrib.get("ID", f"GFX_{reading_order_counter}"),
                        block_type="illustration",
                        page_zone=default_zone,
                        reading_order=reading_order_counter,
                        x=bx,
                        y=by,
                        w=bw,
                        h=bh,
                        canonical_polygon=canonical_poly(child),
                    )
                    self.session.add(gfx_block)

                elif tag == "TextBlock":
                    reading_order_counter += 1
                    bx, by, bw, bh = norm_box(child)
                    c_poly = canonical_poly(child)

                    # Infer margin zone if y is in top 8% or bottom 8% of canonical page
                    inferred_zone = default_zone
                    if default_zone == "print_space":
                        if c_poly[0][1] < canonical_h * 0.08:
                            inferred_zone = "margin_top"
                        elif c_poly[2][1] > canonical_h * 0.92:
                            inferred_zone = "margin_bottom"

                    col_idx = 1 if (bx + bw / 2) < (canvas.width / 2) else 2
                    if bw > canvas.width * 0.65:
                        col_idx = 0

                    block_type = child.attrib.get("TYPE", "paragraph").lower()
                    text_block = AltoBlock(
                        canvas_id=canvas.id,
                        parent_block_id=parent_block_id,
                        alto_element_id=child.attrib.get("ID", f"TB_{reading_order_counter}"),
                        block_type=block_type,
                        page_zone=inferred_zone,
                        reading_order=reading_order_counter,
                        column_index=col_idx,
                        x=bx,
                        y=by,
                        w=bw,
                        h=bh,
                        canonical_polygon=c_poly,
                        provenance_meta={"method": ocr_engine, "version": ocr_version},
                    )
                    self.session.add(text_block)
                    self.session.flush()

                    block_lines_text: list[str] = []
                    block_wcs: list[float] = []
                    char_cursor = 0
                    line_idx = 0

                    for line_elem in child:
                        if _strip_ns(line_elem.tag) != "TextLine":
                            continue
                        line_idx += 1
                        lx, ly, lw, lh = norm_box(line_elem)

                        token_records: list[AltoToken] = []
                        line_str_parts: list[str] = []
                        line_wcs: list[float] = []
                        tok_idx = 0

                        for tok_elem in line_elem:
                            tok_tag = _strip_ns(tok_elem.tag)
                            if tok_tag == "String":
                                tok_idx += 1
                                content = tok_elem.attrib.get("CONTENT", "")
                                subs_content = tok_elem.attrib.get("SUBS_CONTENT")
                                wc_raw = tok_elem.attrib.get("WC")
                                wc = float(wc_raw) if wc_raw is not None else None
                                if wc is not None:
                                    line_wcs.append(wc)
                                    block_wcs.append(wc)
                                    all_confidences.append(wc)

                                tx, ty, tw, th = norm_box(tok_elem)
                                alts = [
                                    alt.text.strip()
                                    for alt in tok_elem
                                    if _strip_ns(alt.tag) == "ALTERNATIVE" and alt.text
                                ]
                                line_str_parts.append(content)
                                if self.include_tokens and content:
                                    token_records.append(
                                        AltoToken(
                                            canvas_id=canvas.id,
                                            token_index=tok_idx,
                                            alto_element_id=tok_elem.attrib.get("ID"),
                                            surface_form=content,
                                            normalized_form=subs_content,
                                            x=tx,
                                            y=ty,
                                            w=tw,
                                            h=th,
                                            word_confidence=wc,
                                            ocr_alternatives=alts,
                                        )
                                    )
                            elif tok_tag == "SP":
                                line_str_parts.append(" ")

                        line_text = "".join(line_str_parts).strip()
                        if not line_text:
                            continue

                        line_conf = round(sum(line_wcs) / len(line_wcs), 4) if line_wcs else None
                        start_pos = char_cursor
                        end_pos = start_pos + len(line_text)
                        char_cursor = end_pos + 1
                        prot_spans = self.extract_protected_spans(line_text)

                        alto_line = AltoLine(
                            block_id=text_block.id,
                            canvas_id=canvas.id,
                            alto_element_id=line_elem.attrib.get(
                                "ID", f"TL_{reading_order_counter}_{line_idx}"
                            ),
                            line_index=line_idx,
                            x=lx,
                            y=ly,
                            w=lw,
                            h=lh,
                            canonical_polygon=canonical_poly(line_elem),
                            baseline_points=line_elem.attrib.get("BASELINE"),
                            raw_ocr=line_text,
                            normalized_ocr=line_text,
                            content_text=line_text,
                            line_confidence=line_conf,
                            protected_spans=prot_spans,
                            char_start_in_block=start_pos,
                            char_end_in_block=end_pos,
                        )
                        self.session.add(alto_line)
                        self.session.flush()

                        for tr in token_records:
                            tr.line_id = alto_line.id
                            self.session.add(tr)

                        anno = IiifAnnotation(
                            canvas_id=canvas.id,
                            alto_block_id=text_block.id,
                            alto_line_id=alto_line.id,
                            motivation="supplementing",
                            target_xywh=alto_line.xywh,
                            body_text=line_text,
                            creator_type="machine",
                        )
                        self.session.add(anno)
                        block_lines_text.append(line_text)

                    joined_block_text = "\n".join(block_lines_text)
                    text_block.raw_ocr = joined_block_text
                    text_block.normalized_ocr = joined_block_text
                    text_block.content_text = joined_block_text
                    text_block.raw_confidence = (
                        round(sum(block_wcs) / len(block_wcs), 4) if block_wcs else None
                    )
                    text_block.protected_spans = self.extract_protected_spans(joined_block_text)

        # If page_elem has direct children (TopMargin/PrintSpace/BottomMargin), process from page_elem
        has_sub_spaces = any(
            _strip_ns(c.tag) in ("PrintSpace", "TopMargin", "BottomMargin") for c in page_elem
        )
        process_block_container(page_elem if has_sub_spaces else print_space_elem)

        if all_confidences:
            alto_page.avg_confidence = round(sum(all_confidences) / len(all_confidences), 4)

        self.session.flush()
        self.link_figure_mentions_on_canvas(canvas.id)
        return alto_page

    def link_figure_mentions_on_canvas(self, canvas_id: str) -> int:
        """Scan body text lines on a Canvas for figure citations (e.g. '见图一二') and link to PlateFigure."""
        canvas = self.session.get(Canvas, canvas_id)
        if canvas is None:
            return 0

        figures = list(
            self.session.scalars(
                select(PlateFigure).where(PlateFigure.manifest_id == canvas.manifest_id)
            )
        )
        if not figures:
            return 0

        fig_by_number: dict[str, PlateFigure] = {}
        for fig in figures:
            if fig.figure_number:
                norm_key = re.sub(r"\s+", "", fig.figure_number)
                fig_by_number[norm_key] = fig

        lines = list(
            self.session.scalars(select(AltoLine).where(AltoLine.canvas_id == canvas_id))
        )
        linked_count = 0
        for line in lines:
            for match in FIGURE_CITATION_PATTERN.finditer(line.content_text):
                prefix = match.group("prefix")
                num = match.group("num")
                sub = match.group("sub")
                candidate_keys = [f"{prefix}{num}"]
                if sub:
                    candidate_keys.insert(0, f"{prefix}{num}:{sub}")

                matched_fig = next(
                    (fig_by_number[k] for k in candidate_keys if k in fig_by_number), None
                )
                if matched_fig is not None:
                    mention = FigureMention(
                        figure_id=matched_fig.id,
                        mention_canvas_id=canvas.id,
                        mention_block_id=line.block_id,
                        mention_line_id=line.id,
                        mention_text=match.group(0),
                        context_snippet=line.content_text,
                        mention_xywh=line.xywh,
                    )
                    self.session.add(mention)
                    linked_count += 1

        self.session.flush()
        return linked_count

    @staticmethod
    def extract_protected_spans(text: str) -> list[dict[str, Any]]:
        """Identify high-risk archaeological codes (specimen IDs, trench/tomb IDs, measurements)
        that post-correction models must not arbitrarily rewrite.
        """
        spans: list[dict[str, Any]] = []
        for match in PROTECTED_ARCH_ID_PATTERN.finditer(text):
            kind = match.lastgroup or "ARCH_CODE"
            spans.append(
                {
                    "text": match.group(0),
                    "type": kind,
                    "start": match.start(),
                    "end": match.end(),
                }
            )
        return spans

    @staticmethod
    def extract_artifact_codes(text: str) -> list[str]:
        """Extract archaeological specimen codes (e.g., 'T14③:12', 'Y1:2') from caption or body text."""
        return [
            s["text"]
            for s in AltoIngestor.extract_protected_spans(text)
            if s["type"] == "ARTIFACT_ID"
        ]
