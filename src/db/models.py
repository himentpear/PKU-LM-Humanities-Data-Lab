"""SQLAlchemy 2.0 ORM models (V2) for Archaeological Report Page Reconstruction,
Layout Recovery, Automatic Heading Indexing, and IIIF + ALTO XML Interoperability.

Core abstraction:
Document (Work/Manifest) -> Physical Page & Canonical Page (Canvas + AltoPage)
-> Layout Element (AltoBlock/AltoLine/AltoToken) -> Semantic Entity (Artifact/ArchEntity)
-> Index Entry (IndexEntry/IiifRange) -> Revision & Review Queue (Revision/ReviewTask)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all V2 tables."""


# ==============================================================================
# Module 1: Document, Multi-Domain Page Identity & Three-Space Geometry
# ==============================================================================


class Work(Base):
    """Top-level archaeological report or multi-volume series (IIIF Collection)."""

    __tablename__ = "works"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    edition: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    publisher: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    publish_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    site_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="2.0")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    manifests: Mapped[list[Manifest]] = relationship(
        back_populates="work", cascade="all, delete-orphan", order_by="Manifest.volume_number"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )
    image_assets: Mapped[list[ImageAsset]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )


class Manifest(Base):
    """Single volume/tome corresponding to an IIIF Presentation 3.0 Manifest."""

    __tablename__ = "manifests"
    __table_args__ = (UniqueConstraint("work_id", "volume_number", name="uq_manifest_work_vol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    work_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("works.id", ondelete="CASCADE"), nullable=False
    )
    volume_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    viewing_direction: Mapped[str] = mapped_column(String(32), default="left-to-right")
    behavior: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["paged"])
    source_pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_pdf_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pdf_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )  # 'digital' | 'scanned' | 'hybrid_ocr'
    total_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    iiif_manifest_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    work: Mapped[Work] = relationship(back_populates="manifests")
    canvases: Mapped[list[Canvas]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan", order_by="Canvas.sequence_index"
    )
    ranges: Mapped[list[IiifRange]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan", order_by="IiifRange.sort_order"
    )
    plates_figures: Mapped[list[PlateFigure]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan"
    )
    index_entries: Mapped[list[IndexEntry]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan"
    )


class Canvas(Base):
    """Page entity with V2 Multi-Domain Page Identity and DP Alignment provenance."""

    __tablename__ = "canvases"
    __table_args__ = (
        UniqueConstraint("manifest_id", "sequence_index", name="uq_canvas_manifest_seq"),
        Index("idx_canvases_manifest_seq", "manifest_id", "sequence_index"),
        Index("idx_canvases_domain_ord", "manifest_id", "page_domain", "page_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )

    # --- V2 Multi-Domain Page Identity ---
    sequence_index: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # Unified 1-based logical sequence_no
    scan_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # 1-based physical order in scan/file
    pdf_page_index: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # 0-based PDF page index
    source_page_label: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )  # PDF PageLabel or filename label
    printed_page_label: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )  # Original printed label ("37", "iv", "图版八")
    page_domain: Mapped[str] = mapped_column(
        String(32), default="body"
    )  # 'cover' | 'front_matter' | 'toc' | 'body' | 'plate' | 'appendix' | 'insert'
    page_ordinal: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # Numeric order within page_domain (e.g. 8 for "图版八")
    page_type: Mapped[str] = mapped_column(
        String(32), default="text"
    )  # 'cover' | 'toc' | 'text' | 'plate' | 'foldout_map'

    # --- DP/Viterbi Page Mapping Confidence & Multi-Source Candidates ---
    mapping_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    mapping_status: Mapped[str] = mapped_column(
        String(32), default="auto_accepted"
    )  # 'auto_accepted' | 'needs_review' | 'manual_locked' | 'unaligned'
    alignment_signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    field_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    # --- IIIF Canvas Dimensions & Image Service ---
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    iiif_image_service_id: Mapped[str] = mapped_column(Text, nullable=False)
    image_storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reading_behavior: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)

    manifest: Mapped[Manifest] = relationship(back_populates="canvases")
    alto_page: Mapped[Optional[AltoPage]] = relationship(
        back_populates="canvas", uselist=False, cascade="all, delete-orphan"
    )
    blocks: Mapped[list[AltoBlock]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan", order_by="AltoBlock.reading_order"
    )
    lines: Mapped[list[AltoLine]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan"
    )
    tokens: Mapped[list[AltoToken]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan"
    )
    annotations: Mapped[list[IiifAnnotation]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan"
    )
    plates_figures: Mapped[list[PlateFigure]] = relationship(
        back_populates="canvas", cascade="all, delete-orphan"
    )

    @property
    def original_page_label(self) -> Optional[str]:
        """Alias matching V2 specification terminology."""
        return self.printed_page_label

    @original_page_label.setter
    def original_page_label(self, value: Optional[str]) -> None:
        self.printed_page_label = value


class AltoPage(Base):
    """Page Geometry & ALTO XML metadata bridging Source Space (S) and Canonical Space (C)."""

    __tablename__ = "alto_pages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    alto_xml_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    measurement_unit: Mapped[str] = mapped_column(String(16), default="pixel")

    # Source Space (S) dimensions
    alto_width: Mapped[float] = mapped_column(Float, nullable=False)
    alto_height: Mapped[float] = mapped_column(Float, nullable=False)

    # Canonical Page Space (C): default normalized width = 10000
    canonical_width: Mapped[int] = mapped_column(Integer, default=10000)
    canonical_height: Mapped[int] = mapped_column(Integer, default=14142)
    transform_type: Mapped[str] = mapped_column(
        String(32), default="linear"
    )  # 'linear' | 'homography' | 'mesh'
    source_to_canonical: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # PrintSpace (版心) bounding box in Canvas pixels
    print_space_x: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    print_space_y: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    print_space_w: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    print_space_h: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    ocr_engine: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ocr_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    avg_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    canvas: Mapped[Canvas] = relationship(back_populates="alto_page")


# ==============================================================================
# Module 2: Layout Elements & Four-State Text Lifecycle (Block / Line / Token)
# ==============================================================================


class Footnote(Base):
    """Page-local note with immutable source-line provenance and explicit uncertainty."""
    __tablename__ = "footnotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    canvas_id: Mapped[str] = mapped_column(ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False)
    marker: Mapped[str] = mapped_column(String(16), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_line_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class FootnoteReference(Base):
    """Original line offsets; missing targets stay unresolved instead of guessed."""
    __tablename__ = "footnote_references"
    __table_args__ = (UniqueConstraint("line_id", "char_start", "char_end"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    canvas_id: Mapped[str] = mapped_column(ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False)
    line_id: Mapped[str] = mapped_column(ForeignKey("alto_lines.id", ondelete="CASCADE"), nullable=False)
    footnote_id: Mapped[Optional[str]] = mapped_column(ForeignKey("footnotes.id", ondelete="SET NULL"))
    marker: Mapped[str] = mapped_column(String(16), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    context_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class AltoBlock(Base):
    """Layout Element (<ComposedBlock>/<TextBlock>/<GraphicalElement>) with V2 polygon,
    page_zone (margin vs print_space), four-state text, and cross-page continuation.
    """

    __tablename__ = "alto_blocks"
    __table_args__ = (
        Index("idx_alto_blocks_canvas_order", "canvas_id", "reading_order"),
        Index("idx_alto_blocks_logical_group", "logical_group_id", "part_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    parent_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="CASCADE"), nullable=True
    )
    alto_element_id: Mapped[str] = mapped_column(String(128), nullable=False)
    block_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="paragraph"
    )  # 'paragraph' | 'heading' | 'running_header' | 'page_number' | 'table' | 'caption' | 'illustration' | 'footnote'
    page_zone: Mapped[str] = mapped_column(
        String(32), default="print_space"
    )  # 'margin_top' | 'margin_bottom' | 'margin_left' | 'margin_right' | 'print_space'
    reading_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    column_index: Mapped[int] = mapped_column(Integer, default=1)

    # Canvas pixel bbox (#xywh) + Canonical Space polygon [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    w: Mapped[int] = mapped_column(Integer, nullable=False)
    h: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_polygon: Mapped[list[list[int]]] = mapped_column(JSON, default=list)
    polygon_svg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_direction: Mapped[str] = mapped_column(String(16), default="ltr")
    style_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # --- V2 Four-State Text Lifecycle ---
    raw_ocr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    normalized_ocr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    corrected_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approved_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_text: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # Active resolved text
    raw_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    correction_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), default="unreviewed"
    )  # 'unreviewed' | 'auto_accepted' | 'needs_review' | 'approved'
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    protected_spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provenance_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # --- V2 Cross-Page Continuation ---
    logical_group_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    part_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    continues_from_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    continues_to_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)

    canvas: Mapped[Canvas] = relationship(back_populates="blocks")
    parent_block: Mapped[Optional[AltoBlock]] = relationship(
        remote_side=[id], back_populates="child_blocks"
    )
    child_blocks: Mapped[list[AltoBlock]] = relationship(
        back_populates="parent_block", cascade="all, delete-orphan"
    )
    lines: Mapped[list[AltoLine]] = relationship(
        back_populates="block", cascade="all, delete-orphan", order_by="AltoLine.line_index"
    )

    @property
    def xywh(self) -> str:
        return f"{self.x},{self.y},{self.w},{self.h}"

    def resolve_display_text(self) -> Optional[str]:
        """Priority: approved_text > corrected_text > normalized_ocr > raw_ocr > content_text."""
        for candidate in (
            self.approved_text,
            self.corrected_text,
            self.normalized_ocr,
            self.raw_ocr,
            self.content_text,
        ):
            if candidate:
                return candidate
        return None


class AltoLine(Base):
    """ALTO <TextLine> with V2 four-state text preservation and protected archaeological IDs."""

    __tablename__ = "alto_lines"
    __table_args__ = (Index("idx_alto_lines_canvas_block", "canvas_id", "block_id", "line_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    block_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    alto_element_id: Mapped[str] = mapped_column(String(128), nullable=False)
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    w: Mapped[int] = mapped_column(Integer, nullable=False)
    h: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_polygon: Mapped[list[list[int]]] = mapped_column(JSON, default=list)
    baseline_points: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Four-state text lifecycle
    raw_ocr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    normalized_ocr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    corrected_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approved_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)

    line_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    correction_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), default="unreviewed")
    protected_spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    char_start_in_block: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end_in_block: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    block: Mapped[AltoBlock] = relationship(back_populates="lines")
    canvas: Mapped[Canvas] = relationship(back_populates="lines")
    tokens: Mapped[list[AltoToken]] = relationship(
        back_populates="line", cascade="all, delete-orphan", order_by="AltoToken.token_index"
    )

    @property
    def xywh(self) -> str:
        return f"{self.x},{self.y},{self.w},{self.h}"


class AltoToken(Base):
    """ALTO <String> representing character/word-level bounding boxes and OCR alternatives."""

    __tablename__ = "alto_tokens"
    __table_args__ = (Index("idx_alto_tokens_canvas_surface", "canvas_id", "surface_form"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    line_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alto_lines.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    token_index: Mapped[int] = mapped_column(Integer, nullable=False)
    alto_element_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    surface_form: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_form: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    w: Mapped[int] = mapped_column(Integer, nullable=False)
    h: Mapped[int] = mapped_column(Integer, nullable=False)
    word_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ocr_alternatives: Mapped[list[str]] = mapped_column(JSON, default=list)

    line: Mapped[AltoLine] = relationship(back_populates="tokens")
    canvas: Mapped[Canvas] = relationship(back_populates="tokens")

    @property
    def xywh(self) -> str:
        return f"{self.x},{self.y},{self.w},{self.h}"


class IiifAnnotation(Base):
    """W3C Web Annotation linking OCR results or scholarly comments to Canvas regions."""

    __tablename__ = "iiif_annotations"
    __table_args__ = (Index("idx_iiif_annotations_canvas", "canvas_id", "motivation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    alto_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="SET NULL"), nullable=True
    )
    alto_line_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_lines.id", ondelete="SET NULL"), nullable=True
    )
    motivation: Mapped[str] = mapped_column(String(32), nullable=False, default="supplementing")
    target_xywh: Mapped[str] = mapped_column(String(64), nullable=False)
    target_svg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_format: Mapped[str] = mapped_column(String(32), default="text/plain")
    creator_type: Mapped[str] = mapped_column(String(16), default="machine")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    canvas: Mapped[Canvas] = relationship(back_populates="annotations")


# ==============================================================================
# Module 3: Automatic Table of Contents (IIIF Range) & Unified Index Entries
# ==============================================================================


class IiifRange(Base):
    """Hierarchical table of contents node corresponding to an IIIF Range."""

    __tablename__ = "iiif_ranges"
    __table_args__ = (
        Index("idx_iiif_ranges_manifest_parent", "manifest_id", "parent_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("iiif_ranges.id", ondelete="CASCADE"), nullable=True
    )
    range_type: Mapped[str] = mapped_column(String(32), default="toc")
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_method: Mapped[str] = mapped_column(String(32), default="auto_heading")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    heading_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="SET NULL"), nullable=True
    )
    start_canvas_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True
    )
    end_canvas_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="SET NULL"), nullable=True
    )
    start_label: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    end_label: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    start_xywh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    manifest: Mapped[Manifest] = relationship(back_populates="ranges")
    parent: Mapped[Optional[IiifRange]] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list[IiifRange]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", order_by="IiifRange.sort_order"
    )
    items: Mapped[list[RangeItem]] = relationship(
        back_populates="range_node", cascade="all, delete-orphan", order_by="RangeItem.sequence_order"
    )


class RangeItem(Base):
    """Links an IIIF Range to ordered Canvases or Canvas spatial fragments."""

    __tablename__ = "range_items"
    __table_args__ = (
        UniqueConstraint("range_id", "canvas_id", "fragment_xywh", name="uq_range_canvas_frag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    range_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("iiif_ranges.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fragment_xywh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    range_node: Mapped[IiifRange] = relationship(back_populates="items")
    canvas: Mapped[Canvas] = relationship()


class IndexEntry(Base):
    """Domain-aware merged index entry for headings, artifacts, trenches/tombs, and subjects."""

    __tablename__ = "index_entries"
    __table_args__ = (Index("idx_index_entries_manifest_type", "manifest_id", "index_type", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )
    index_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # 'heading' | 'artifact' | 'entity' | 'subject'
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    display_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # Domain-merged page ranges, e.g. [{"domain": "body", "range": "37–39"}, {"domain": "plate", "range": "图版八"}]
    page_ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    formatted_pages: Mapped[str] = mapped_column(Text, default="")

    manifest: Mapped[Manifest] = relationship(back_populates="index_entries")


# ==============================================================================
# Module 4: Archaeological Semantic Layer (Artifact, ImageAsset, ArchEntity, Plates)
# ==============================================================================


class Artifact(Base):
    """First-class archaeological artifact entity decoupled from individual page occurrences."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("work_id", "artifact_no", name="uq_artifact_work_no"),
        Index("idx_artifacts_no", "artifact_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    work_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("works.id", ondelete="CASCADE"), nullable=False
    )
    artifact_no: Mapped[str] = mapped_column(
        String(128), nullable=False
    )  # Specimen code, e.g. 'T14③:12', 'M45:3'
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    artifact_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 器类: 梅瓶, 碗
    typology: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 型式: Ⅰ式
    material: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 材质: 白釉黑花瓷
    period: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 时期: 金代 / 第三期
    excavation_unit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 探方/墓葬: T14
    stratum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 层位: ③
    decoration: Mapped[list[str]] = mapped_column(JSON, default=list)
    measurements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    review_status: Mapped[str] = mapped_column(String(32), default="unreviewed")

    work: Mapped[Work] = relationship(back_populates="artifacts")
    occurrences: Mapped[list[ArtifactOccurrence]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )


class ArtifactOccurrence(Base):
    """Many-to-many occurrence linking an Artifact to every Page/LayoutElement where it appears."""

    __tablename__ = "artifact_occurrences"
    __table_args__ = (Index("idx_artifact_occ_artifact_role", "artifact_id", "role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    layout_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="SET NULL"), nullable=True
    )
    plate_figure_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("plates_figures.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # 'mentioned' | 'illustrated' | 'photographed' | 'tabulated' | 'captioned' | 'excavation_context'
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    context_snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_xywh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    artifact: Mapped[Artifact] = relationship(back_populates="occurrences")
    canvas: Mapped[Canvas] = relationship()


class ImageAsset(Base):
    """High-resolution standalone photograph/drawing asset with perceptual & visual fingerprints."""

    __tablename__ = "image_assets"
    __table_args__ = (Index("idx_image_assets_sha256", "sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    work_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("works.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), default="cropped_plate"
    )  # 'standalone_photo' | 'cropped_plate' | 'vector_drawing'
    phash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    feature_descriptor_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    work: Mapped[Work] = relationship(back_populates="image_assets")
    occurrences: Mapped[list[ImageOccurrence]] = relationship(
        back_populates="image_asset", cascade="all, delete-orphan"
    )


class ImageOccurrence(Base):
    """Links an ImageAsset to its printed region on a Canvas via visual/homography matching."""

    __tablename__ = "image_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("image_assets.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    layout_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="SET NULL"), nullable=True
    )
    plate_figure_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("plates_figures.id", ondelete="SET NULL"), nullable=True
    )
    transform_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_score: Mapped[float] = mapped_column(Float, default=1.0)

    image_asset: Mapped[ImageAsset] = relationship(back_populates="occurrences")
    canvas: Mapped[Canvas] = relationship()


class ArchEntity(Base):
    """Archaeological domain NER entity (18-tag schema: SITE, TRENCH, TOMB, PIT, STRATUM, etc.)."""

    __tablename__ = "arch_entities"
    __table_args__ = (
        UniqueConstraint("work_id", "entity_type", "canonical_name", name="uq_arch_entity"),
        Index("idx_arch_entities_type_name", "entity_type", "canonical_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    work_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("works.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # SITE | AREA | TRENCH | TOMB | PIT | STRATUM | FEATURE | ARTIFACT_ID | ARTIFACT_TYPE | MATERIAL | TYPOLOGY | DECORATION | PERIOD | MEASUREMENT | FIGURE_ID | PLATE_ID | PERSON | PLACE | ORGANIZATION
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    mentions: Mapped[list[EntityMention]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )


class EntityMention(Base):
    """Exact character span and page location where an ArchEntity is mentioned."""

    __tablename__ = "entity_mentions"
    __table_args__ = (Index("idx_entity_mentions_entity", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("arch_entities.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="CASCADE"), nullable=True
    )
    line_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_lines.id", ondelete="CASCADE"), nullable=True
    )
    surface_text: Mapped[str] = mapped_column(String(255), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    entity: Mapped[ArchEntity] = relationship(back_populates="mentions")


class PlateFigure(Base):
    """Archaeological plates, line drawings, rubbings, and site plans cropped via IIIF."""

    __tablename__ = "plates_figures"
    __table_args__ = (
        Index("idx_plates_manifest_num", "manifest_id", "figure_number"),
        Index("idx_plates_excavation_unit", "excavation_unit"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    graphic_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="SET NULL"), nullable=True
    )
    caption_block_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="SET NULL"), nullable=True
    )
    figure_category: Mapped[str] = mapped_column(String(32), nullable=False, default="line_drawing")
    figure_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sub_index: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    caption_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    artifact_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    excavation_unit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    period_stratum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    vessel_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    decoration_motif: Mapped[list[str]] = mapped_column(JSON, default=list)

    crop_x: Mapped[int] = mapped_column(Integer, nullable=False)
    crop_y: Mapped[int] = mapped_column(Integer, nullable=False)
    crop_w: Mapped[int] = mapped_column(Integer, nullable=False)
    crop_h: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_polygon: Mapped[list[list[int]]] = mapped_column(JSON, default=list)

    image_embedding: Mapped[Optional[list[float]]] = mapped_column(JSON, nullable=True)
    caption_embedding: Mapped[Optional[list[float]]] = mapped_column(JSON, nullable=True)

    manifest: Mapped[Manifest] = relationship(back_populates="plates_figures")
    canvas: Mapped[Canvas] = relationship(back_populates="plates_figures")
    mentions: Mapped[list[FigureMention]] = relationship(
        back_populates="figure", cascade="all, delete-orphan"
    )

    @property
    def crop_xywh(self) -> str:
        return f"{self.crop_x},{self.crop_y},{self.crop_w},{self.crop_h}"

    @property
    def iiif_crop_param(self) -> str:
        return f"{self.crop_xywh}/max/0/default.jpg"

    @property
    def iiif_crop_url(self) -> str:
        base = self.canvas.iiif_image_service_id.rstrip("/")
        return f"{base}/{self.iiif_crop_param}"


class FigureMention(Base):
    """Cross-reference linking in-text citations (e.g. '见图一二') to PlateFigure records."""

    __tablename__ = "figure_mentions"
    __table_args__ = (Index("idx_figure_mentions_figure", "figure_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    figure_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plates_figures.id", ondelete="CASCADE"), nullable=False
    )
    mention_canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    mention_block_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alto_blocks.id", ondelete="CASCADE"), nullable=False
    )
    mention_line_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("alto_lines.id", ondelete="SET NULL"), nullable=True
    )
    mention_text: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mention_xywh: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    figure: Mapped[PlateFigure] = relationship(back_populates="mentions")
    mention_canvas: Mapped[Canvas] = relationship()


# ==============================================================================
# Module 5: Provenance, Revisions & Risk-Prioritized Human Review Queue
# ==============================================================================


class RagChunk(Base):
    """Semantic passage chunks with full section breadcrumb and provenance anchors."""

    __tablename__ = "rag_chunks"
    __table_args__ = (Index("idx_rag_chunks_manifest_range", "manifest_id", "range_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )
    range_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("iiif_ranges.id", ondelete="SET NULL"), nullable=True
    )
    breadcrumb_path: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_embedding: Mapped[Optional[list[float]]] = mapped_column(JSON, nullable=True)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ProvenanceAnchor(Base):
    """Exact page, section, quote, and bounding-box provenance for any extracted knowledge."""

    __tablename__ = "provenance_anchors"
    __table_args__ = (Index("idx_provenance_entity", "source_entity_type", "source_entity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source_entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False
    )
    range_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("iiif_ranges.id", ondelete="SET NULL"), nullable=True
    )
    printed_page: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    verbatim_quote: Mapped[str] = mapped_column(Text, nullable=False)
    highlight_boxes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    iiif_target_uri: Mapped[str] = mapped_column(Text, nullable=False)

    canvas: Mapped[Canvas] = relationship()
    range_node: Mapped[Optional[IiifRange]] = relationship()


class Revision(Base):
    """Immutable audit log tracking machine & human edits (supports optimistic concurrency)."""

    __tablename__ = "revisions"
    __table_args__ = (Index("idx_revisions_entity", "entity_type", "entity_id", "revision_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    entity_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # 'canvas' | 'alto_block' | 'alto_line' | 'artifact' | 'iiif_range'
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)  # 'auto_dp' | 'reviewer_zhang'
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ReviewTask(Base):
    """Risk-prioritized human review queue item (missing pages, low mapping score, OCR conflict)."""

    __tablename__ = "review_tasks"
    __table_args__ = (
        Index("idx_review_tasks_status_priority", "manifest_id", "status", "priority_score"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False
    )
    canvas_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canvases.id", ondelete="CASCADE"), nullable=True
    )
    target_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # 'page_mapping' | 'missing_page_gap' | 'ocr_conflict' | 'heading_conflict' | 'artifact_link'
    target_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    risk_type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5
    )  # Higher = more urgent (0.0 ~ 1.0)
    status: Mapped[str] = mapped_column(
        String(32), default="pending"
    )  # 'pending' | 'in_review' | 'approved' | 'rejected'
    confidence_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    candidates_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reviewer: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
