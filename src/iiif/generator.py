"""IIIF Presentation 3.0 Manifest, Range TOC, AnnotationPage, V2 Page Bundle, and Provenance builder."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    AltoLine,
    ArtifactOccurrence,
    Canvas,
    IiifAnnotation,
    IiifRange,
    Manifest,
    PlateFigure,
    ProvenanceAnchor,
    ReviewTask,
)

IIIF_PRESENTATION_3_CONTEXT = "http://iiif.io/api/presentation/3/context.json"


class IiifBuilder:
    """Generates IIIF Presentation 3.0 resources, V2 Page Bundles, and coordinate-aware queries."""

    def __init__(self, session: Session, base_url: str = "https://lab.pku.edu.cn/iiif") -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    def canvas_uri(self, manifest_id: str, canvas_id: str) -> str:
        return f"{self.base_url}/manifest/{manifest_id}/canvas/{canvas_id}"

    def build_manifest(self, manifest_id: str) -> dict[str, Any]:
        """Build a complete IIIF Presentation 3.0 Manifest including Canvases and Range TOC."""
        manifest = self.session.get(Manifest, manifest_id)
        if manifest is None:
            raise ValueError(f"Manifest not found: {manifest_id}")

        work = manifest.work
        manifest_uri = f"{self.base_url}/manifest/{manifest.id}/manifest.json"

        items = [self._build_canvas_dict(manifest.id, canvas) for canvas in manifest.canvases]

        top_ranges = [r for r in manifest.ranges if r.parent_id is None]
        structures = [self._build_range_dict(manifest.id, r) for r in top_ranges]

        manifest_doc: dict[str, Any] = {
            "@context": IIIF_PRESENTATION_3_CONTEXT,
            "id": manifest_uri,
            "type": "Manifest",
            "label": {"zh-CN": [f"{work.title} · {manifest.label}"]},
            "viewingDirection": manifest.viewing_direction,
            "behavior": manifest.behavior,
            "metadata": [
                {"label": {"zh-CN": ["题名"]}, "value": {"zh-CN": [work.title]}},
                {"label": {"zh-CN": ["分卷"]}, "value": {"zh-CN": [manifest.label]}},
            ],
            "items": items,
        }
        if work.site_name:
            manifest_doc["metadata"].append(
                {"label": {"zh-CN": ["考古遗址"]}, "value": {"zh-CN": [work.site_name]}}
            )
        if structures:
            manifest_doc["structures"] = structures

        return manifest_doc

    def _build_canvas_dict(self, manifest_id: str, canvas: Canvas) -> dict[str, Any]:
        c_uri = self.canvas_uri(manifest_id, canvas.id)
        img_service = canvas.iiif_image_service_id.rstrip("/")
        label_str = canvas.printed_page_label or f"第 {canvas.sequence_index} 页"

        return {
            "id": c_uri,
            "type": "Canvas",
            "label": {"zh-CN": [label_str]},
            "width": canvas.width,
            "height": canvas.height,
            "items": [
                {
                    "id": f"{c_uri}/page",
                    "type": "AnnotationPage",
                    "items": [
                        {
                            "id": f"{c_uri}/page/image",
                            "type": "Annotation",
                            "motivation": "painting",
                            "body": {
                                "id": f"{img_service}/full/max/0/default.jpg",
                                "type": "Image",
                                "format": "image/jpeg",
                                "width": canvas.width,
                                "height": canvas.height,
                                "service": [
                                    {
                                        "id": img_service,
                                        "type": "ImageService3",
                                        "profile": "level0",
                                    }
                                ],
                            },
                            "target": c_uri,
                        }
                    ],
                }
            ],
            "annotations": [
                {
                    "id": f"{c_uri}/annotations/ocr.json",
                    "type": "AnnotationPage",
                    "label": {"zh-CN": ["ALTO OCR 文本与坐标层"]},
                }
            ],
        }

    def _build_range_dict(self, manifest_id: str, range_node: IiifRange) -> dict[str, Any]:
        range_uri = f"{self.base_url}/manifest/{manifest_id}/range/{range_node.id}"
        display_title = (
            f"{range_node.section_number} {range_node.title}".strip()
            if range_node.section_number
            else range_node.title
        )

        range_items: list[dict[str, Any]] = []
        for item in range_node.items:
            target = self.canvas_uri(manifest_id, item.canvas_id)
            if item.fragment_xywh:
                target = f"{target}#xywh={item.fragment_xywh}"
            range_items.append({"id": target, "type": "Canvas"})

        if not range_items and range_node.start_canvas_id:
            target = self.canvas_uri(manifest_id, range_node.start_canvas_id)
            if range_node.start_xywh:
                target = f"{target}#xywh={range_node.start_xywh}"
            range_items.append({"id": target, "type": "Canvas"})

        for child in range_node.children:
            range_items.append(self._build_range_dict(manifest_id, child))

        return {
            "id": range_uri,
            "type": "Range",
            "label": {"zh-CN": [display_title]},
            "items": range_items,
        }

    def build_page_bundle(self, canvas_id: str) -> dict[str, Any]:
        """Build a V2 Page Bundle for the frontend Page Reconstruction Viewer & Review Workbench."""
        canvas = self.session.get(Canvas, canvas_id)
        if canvas is None:
            raise ValueError(f"Canvas not found: {canvas_id}")

        alto = canvas.alto_page
        canonical_w = alto.canonical_width if alto else 10000
        canonical_h = (
            alto.canonical_height
            if alto
            else int(round(10000 * (canvas.height / max(1, canvas.width))))
        )

        artifact_occs = list(
            self.session.scalars(
                select(ArtifactOccurrence).where(ArtifactOccurrence.canvas_id == canvas.id)
            )
        )
        review_tasks = list(
            self.session.scalars(
                select(ReviewTask)
                .where(ReviewTask.canvas_id == canvas.id)
                .order_by(ReviewTask.priority_score.desc())
            )
        )

        from src.ingest.footnotes import page_bundle as footnote_bundle
        return {
            **footnote_bundle(self.session, canvas.id),
            "page_id": canvas.id,
            "manifest_id": canvas.manifest_id,
            "revision": canvas.revision_no,
            "revision_label": f"revision-{canvas.revision_no}",
            "identity": {
                "sequence_no": canvas.sequence_index,
                "scan_index": canvas.scan_index,
                "pdf_page_index": canvas.pdf_page_index,
                "source_page_label": canvas.source_page_label,
                "original_page_label": canvas.printed_page_label,
                "page_domain": canvas.page_domain,
                "page_ordinal": canvas.page_ordinal,
                "mapping": {
                    "confidence": canvas.mapping_confidence,
                    "status": canvas.mapping_status,
                    "signals": canvas.alignment_signals,
                    "candidates": canvas.field_candidates,
                },
            },
            "geometry": {
                "source": [
                    alto.alto_width if alto else canvas.width,
                    alto.alto_height if alto else canvas.height,
                ],
                "canvas": [canvas.width, canvas.height],
                "canonical": [canonical_w, canonical_h],
                "svg_view_box": f"0 0 {canonical_w} {canonical_h}",
                "transform_type": alto.transform_type if alto else "linear",
                "source_to_canonical": alto.source_to_canonical if alto else {},
            },
            "scan_image": {
                "iiif_service_id": canvas.iiif_image_service_id,
                "full_image_url": f"{canvas.iiif_image_service_id.rstrip('/')}/full/max/0/default.jpg",
            },
            "layout_elements": [
                {
                    "id": b.id,
                    "type": b.block_type,
                    "page_zone": b.page_zone,
                    "reading_order": b.reading_order,
                    "column_index": b.column_index,
                    "polygon": b.canonical_polygon,
                    "canvas_xywh": b.xywh,
                    "confidence": b.raw_confidence,
                    "review_status": b.review_status,
                    "text": {
                        "resolved": b.resolve_display_text(),
                        "raw_ocr": b.raw_ocr,
                        "corrected_text": b.corrected_text,
                        "approved_text": b.approved_text,
                        "protected_spans": b.protected_spans,
                    },
                    "continuation": {
                        "logical_group_id": b.logical_group_id,
                        "part_index": b.part_index,
                        "continues_from": b.continues_from_id,
                        "continues_to": b.continues_to_id,
                    },
                }
                for b in canvas.blocks
            ],
            "artifact_occurrences": [
                {
                    "artifact_id": occ.artifact_id,
                    "artifact_no": occ.artifact.artifact_no,
                    "artifact_type": occ.artifact.artifact_type,
                    "role": occ.role,
                    "confidence": occ.confidence,
                    "target_xywh": occ.target_xywh,
                }
                for occ in artifact_occs
            ],
            "review_tasks": [
                {
                    "task_id": rt.id,
                    "risk_type": rt.risk_type,
                    "target_type": rt.target_type,
                    "target_id": rt.target_id,
                    "priority_score": rt.priority_score,
                    "status": rt.status,
                    "description": rt.description,
                }
                for rt in review_tasks
            ],
        }

    def build_canvas_ocr_annotation_page(self, canvas_id: str) -> dict[str, Any]:
        """Build an IIIF 3.0 AnnotationPage for all OCR annotations on a Canvas."""
        canvas = self.session.get(Canvas, canvas_id)
        if canvas is None:
            raise ValueError(f"Canvas not found: {canvas_id}")

        c_uri = self.canvas_uri(canvas.manifest_id, canvas.id)
        annotations = list(
            self.session.scalars(
                select(IiifAnnotation).where(IiifAnnotation.canvas_id == canvas_id)
            )
        )

        items = [
            {
                "id": f"{c_uri}/annotations/{anno.id}",
                "type": "Annotation",
                "motivation": anno.motivation,
                "body": {
                    "type": "TextualBody",
                    "value": anno.body_text,
                    "format": anno.body_format,
                    "language": "zh-CN",
                },
                "target": f"{c_uri}#xywh={anno.target_xywh}",
            }
            for anno in annotations
        ]

        return {
            "@context": IIIF_PRESENTATION_3_CONTEXT,
            "id": f"{c_uri}/annotations/ocr.json",
            "type": "AnnotationPage",
            "items": items,
        }

    def search_text_coordinates(self, manifest_id: str, query: str) -> dict[str, Any]:
        """Search OCR text lines and return IIIF Content Search 2.0 AnnotationPage with #xywh= targets."""
        stmt = (
            select(AltoLine, Canvas)
            .join(Canvas, AltoLine.canvas_id == Canvas.id)
            .where(Canvas.manifest_id == manifest_id, AltoLine.content_text.contains(query))
            .order_by(Canvas.sequence_index, AltoLine.line_index)
        )
        rows = self.session.execute(stmt).all()

        items = []
        for line, canvas in rows:
            c_uri = self.canvas_uri(manifest_id, canvas.id)
            items.append(
                {
                    "id": f"{c_uri}/search-hit/{line.id}",
                    "type": "Annotation",
                    "motivation": "supplementing",
                    "body": {
                        "type": "TextualBody",
                        "value": line.content_text,
                        "format": "text/plain",
                    },
                    "target": f"{c_uri}#xywh={line.xywh}",
                    "printed_page": canvas.printed_page_label,
                    "page_domain": canvas.page_domain,
                    "confidence": line.line_confidence,
                }
            )

        return {
            "@context": IIIF_PRESENTATION_3_CONTEXT,
            "id": f"{self.base_url}/manifest/{manifest_id}/search?q={query}",
            "type": "AnnotationPage",
            "total": len(items),
            "items": items,
        }

    def search_plates_figures(
        self,
        manifest_id: Optional[str] = None,
        keyword: Optional[str] = None,
        artifact_code: Optional[str] = None,
        excavation_unit: Optional[str] = None,
        vessel_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Query plates & figures with IIIF Image API crop URLs and linked body text mentions."""
        stmt = select(PlateFigure)
        if manifest_id:
            stmt = stmt.where(PlateFigure.manifest_id == manifest_id)
        if keyword:
            stmt = stmt.where(PlateFigure.caption_text.contains(keyword))
        if excavation_unit:
            stmt = stmt.where(PlateFigure.excavation_unit == excavation_unit)
        if vessel_type:
            stmt = stmt.where(PlateFigure.vessel_type == vessel_type)

        figures = list(self.session.scalars(stmt))
        results: list[dict[str, Any]] = []
        for fig in figures:
            if artifact_code and artifact_code not in (fig.artifact_codes or []):
                continue

            results.append(
                {
                    "figure_id": fig.id,
                    "figure_number": fig.figure_number,
                    "category": fig.figure_category,
                    "caption": fig.caption_text,
                    "artifact_codes": fig.artifact_codes,
                    "excavation_unit": fig.excavation_unit,
                    "vessel_type": fig.vessel_type,
                    "iiif_cropped_image_url": fig.iiif_crop_url,
                    "canvas_target": f"{self.canvas_uri(fig.manifest_id, fig.canvas_id)}#xywh={fig.crop_xywh}",
                    "body_mentions": [
                        {
                            "mention_text": m.mention_text,
                            "context_snippet": m.context_snippet,
                            "printed_page": m.mention_canvas.printed_page_label,
                            "target": f"{self.canvas_uri(fig.manifest_id, m.mention_canvas_id)}#xywh={m.mention_xywh}",
                        }
                        for m in fig.mentions
                    ],
                }
            )
        return results

    def trace_provenance(
        self, source_entity_type: str, source_entity_id: str
    ) -> list[dict[str, Any]]:
        """Trace an extracted entity or RAG chunk back to its primary source page, section, and coordinates."""
        stmt = select(ProvenanceAnchor).where(
            ProvenanceAnchor.source_entity_type == source_entity_type,
            ProvenanceAnchor.source_entity_id == source_entity_id,
        )
        anchors = list(self.session.scalars(stmt))

        citations: list[dict[str, Any]] = []
        for anchor in anchors:
            canvas = anchor.canvas
            manifest = canvas.manifest
            work = manifest.work
            section_title = anchor.range_node.title if anchor.range_node else "正文"
            section_num = (
                f"{anchor.range_node.section_number} "
                if (anchor.range_node and anchor.range_node.section_number)
                else ""
            )
            citation_str = (
                f"《{work.title}》（{manifest.label}），"
                f"{section_num}{section_title}，第 {anchor.printed_page or canvas.sequence_index} 页"
            )
            citations.append(
                {
                    "anchor_id": anchor.id,
                    "academic_citation": citation_str,
                    "verbatim_quote": anchor.verbatim_quote,
                    "iiif_target_uri": anchor.iiif_target_uri,
                    "highlight_boxes": anchor.highlight_boxes,
                }
            )
        return citations
