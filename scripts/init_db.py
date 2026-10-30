"""Initialize the V2 IIIF + ALTO XML database and run an end-to-end archaeological report demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.alignment.sequence_dp import (
    PageObservation,
    PageSequenceAligner,
    merge_domain_page_ranges,
)
from src.alto.parser import AltoIngestor
from src.db.models import (
    Artifact,
    ArtifactOccurrence,
    Canvas,
    IiifRange,
    IndexEntry,
    Manifest,
    PlateFigure,
    ProvenanceAnchor,
    RagChunk,
    RangeItem,
    Work,
)
from src.db.session import DEFAULT_DB_URL, get_session, init_db
from src.iiif.generator import IiifBuilder

SAMPLE_ALTO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#">
  <Description>
    <MeasurementUnit>pixel</MeasurementUnit>
  </Description>
  <Layout>
    <Page ID="PAGE_086" PHYSICAL_IMG_NR="86" WIDTH="1200" HEIGHT="1600">
      <TopMargin ID="TM_086" HPOS="100" VPOS="40" WIDTH="1000" HEIGHT="60">
        <TextBlock ID="TB_HEADER" TYPE="paragraph" HPOS="420" VPOS="50" WIDTH="360" HEIGHT="36">
          <TextLine ID="TL_HDR" HPOS="420" VPOS="50" WIDTH="360" HEIGHT="36">
            <String ID="S_H0" CONTENT="观台磁州窑址 · 第四章 出土遗物" HPOS="420" VPOS="50" WIDTH="360" HEIGHT="36" WC="0.99"/>
          </TextLine>
        </TextBlock>
      </TopMargin>
      <PrintSpace ID="PS_086" HPOS="100" VPOS="120" WIDTH="1000" HEIGHT="1360">
        <TextBlock ID="TB_HEADING_1" TYPE="heading" HPOS="100" VPOS="140" WIDTH="1000" HEIGHT="60">
          <TextLine ID="TL_H1" HPOS="100" VPOS="140" WIDTH="520" HEIGHT="55">
            <String ID="S_1" CONTENT="第二节" HPOS="100" VPOS="140" WIDTH="150" HEIGHT="55" WC="0.99"/>
            <SP HPOS="250" VPOS="140" WIDTH="20"/>
            <String ID="S_2" CONTENT="白地黑花瓷器" HPOS="270" VPOS="140" WIDTH="350" HEIGHT="55" WC="0.98"/>
          </TextLine>
        </TextBlock>
        <TextBlock ID="TB_PARA_1" TYPE="paragraph" HPOS="100" VPOS="230" WIDTH="480" HEIGHT="320">
          <TextLine ID="TL_P1_1" HPOS="100" VPOS="230" WIDTH="480" HEIGHT="42">
            <String ID="S_3" CONTENT="Ⅰ式梅瓶：标本T14③:12，高28.5cm，小唇口，短颈，丰肩。" HPOS="100" VPOS="230" WIDTH="480" HEIGHT="42" WC="0.97"/>
          </TextLine>
          <TextLine ID="TL_P1_2" HPOS="100" VPOS="280" WIDTH="480" HEIGHT="42">
            <String ID="S_4" CONTENT="腹部绘折枝牡丹纹，笔法潇洒（图一二:1；彩版叁:2）。" HPOS="100" VPOS="280" WIDTH="480" HEIGHT="42" WC="0.96"/>
          </TextLine>
        </TextBlock>
        <GraphicalElement ID="GFX_FIG_12" HPOS="620" VPOS="230" WIDTH="480" HEIGHT="520"/>
        <TextBlock ID="TB_CAPTION_12" TYPE="caption" HPOS="640" VPOS="760" WIDTH="440" HEIGHT="40">
          <TextLine ID="TL_CAP_1" HPOS="640" VPOS="760" WIDTH="440" HEIGHT="40">
            <String ID="S_5" CONTENT="图一二:1 白地黑花牡丹纹梅瓶 (T14③:12)" HPOS="640" VPOS="760" WIDTH="440" HEIGHT="40" WC="0.98"/>
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
"""


def seed_demo_data(db_url: str) -> None:
    """Seed a V2 archaeological report workflow and verify DP alignment + Page Bundle."""
    engine = init_db(db_url=db_url)
    with get_session(engine) as session:
        work = Work(
            slug="guantai-cizhou-kiln",
            title="观台磁州窑址",
            publisher="文物出版社",
            authors=["北京大学考古学系", "河北省文物研究所"],
            publish_year=1997,
            site_name="河北省邯郸市磁县观台镇磁州窑遗址",
            schema_version="2.0",
        )
        session.add(work)
        session.flush()

        manifest = Manifest(
            work_id=work.id,
            volume_number=1,
            label="全一册",
            viewing_direction="left-to-right",
            source_pdf_path="docs/考古报告/3观台磁州窑址.pdf",
            pdf_type="hybrid_ocr",
        )
        session.add(manifest)
        session.flush()

        # Create sample Canvases representing a sequence with Roman preface, body, foldout, missing page 87, and plate
        demo_scan_specs = [
            (1, 0, "1", "I", "front_matter", 1, "text", 2400, 3200),
            (2, 1, "2", "II", "front_matter", 2, "text", 2400, 3200),
            (3, 84, "85", "85", "body", 85, "text", 2400, 3200),
            (4, 85, "86", "86", "body", 86, "text", 2400, 3200),
            (5, 86, "87", None, "insert", None, "foldout_map", 4200, 3200),  # Foldout map between 86 & 88 (87 missing!)
            (6, 87, "88", "88", "body", 88, "text", 2400, 3200),
            (7, 410, "411", "彩版叁", "plate", 3, "plate", 2400, 3200),
        ]

        canvases_by_scan: dict[int, Canvas] = {}
        for seq_idx, pdf_idx, src_lbl, prt_lbl, dom, ord_val, p_type, w, h in demo_scan_specs:
            c = Canvas(
                manifest_id=manifest.id,
                sequence_index=seq_idx,
                scan_index=seq_idx,
                pdf_page_index=pdf_idx,
                source_page_label=src_lbl,
                printed_page_label=prt_lbl,
                page_domain=dom,
                page_ordinal=ord_val,
                page_type=p_type,
                width=w,
                height=h,
                iiif_image_service_id=f"https://lab.pku.edu.cn/iiif/image/guantai_scan_{seq_idx:03d}",
            )
            session.add(c)
            canvases_by_scan[seq_idx] = c
        session.flush()

        # Run V2 PageSequenceAligner over simulated OCR observations
        observations = [
            PageObservation(scan_index=1, pdf_page_index=0, source_page_label="1", ocr_page_label_raw="I", ocr_confidence=0.98, layout_hint="front_matter"),
            PageObservation(scan_index=2, pdf_page_index=1, source_page_label="2", ocr_page_label_raw="II", ocr_confidence=0.97, layout_hint="front_matter"),
            PageObservation(scan_index=3, pdf_page_index=84, source_page_label="85", ocr_page_label_raw="85", ocr_confidence=0.99, layout_hint="body"),
            PageObservation(scan_index=4, pdf_page_index=85, source_page_label="86", ocr_page_label_raw="86", ocr_confidence=0.99, layout_hint="body"),
            PageObservation(scan_index=5, pdf_page_index=86, source_page_label="87", ocr_page_label_raw=None, ocr_confidence=0.0, layout_hint="insert", is_foldout=True),
            PageObservation(scan_index=6, pdf_page_index=87, source_page_label="88", ocr_page_label_raw="88", ocr_confidence=0.98, layout_hint="body"),
            PageObservation(scan_index=7, pdf_page_index=410, source_page_label="411", ocr_page_label_raw="彩版叁", ocr_confidence=0.96, layout_hint="plate"),
        ]
        aligner = PageSequenceAligner()
        aligned_results, anomalies = aligner.align_sequence(observations)
        aligner.apply_alignment_to_db(session, manifest.id, aligned_results, anomalies)

        canvas_86 = canvases_by_scan[4]
        canvas_plate3 = canvases_by_scan[7]

        # Register PlateFigure on Page 86
        plate_fig = PlateFigure(
            manifest_id=manifest.id,
            canvas_id=canvas_86.id,
            figure_category="line_drawing",
            figure_number="图一二:1",
            sub_index="1",
            caption_text="图一二:1 白地黑花牡丹纹梅瓶 (T14③:12)",
            artifact_codes=["T14③:12"],
            excavation_unit="T14",
            period_stratum="第三期（金代）",
            vessel_type="梅瓶",
            decoration_motif=["白地黑花", "折枝牡丹纹"],
            crop_x=1240,
            crop_y=460,
            crop_w=960,
            crop_h=1040,
        )
        session.add(plate_fig)
        session.flush()

        # Ingest ALTO XML into Page 86 (tests V2 Canonical Space 10000x13333, TopMargin, Four-State Text & Protected Spans)
        ingestor = AltoIngestor(session, include_tokens=True)
        ingestor.ingest_alto_string(
            canvas_id=canvas_86.id,
            xml_content=SAMPLE_ALTO_XML,
            ocr_engine="PP-StructureV3",
            ocr_version="3.0",
        )

        # Create V2 Artifact + Multi-Role ArtifactOccurrences across Body Page 86 & Color Plate 3
        meiping = Artifact(
            work_id=work.id,
            artifact_no="T14③:12",
            name="白地黑花牡丹纹梅瓶",
            artifact_type="梅瓶",
            typology="Ⅰ式",
            material="白釉黑花瓷",
            period="金代（第三期）",
            excavation_unit="T14",
            stratum="③",
            decoration=["白地黑花", "折枝牡丹纹"],
            measurements={"height_cm": 28.5},
            review_status="approved",
        )
        session.add(meiping)
        session.flush()

        session.add_all(
            [
                ArtifactOccurrence(
                    artifact_id=meiping.id,
                    canvas_id=canvas_86.id,
                    role="mentioned",
                    confidence=0.99,
                    context_snippet="Ⅰ式梅瓶：标本T14③:12，高28.5cm，小唇口，短颈，丰肩。",
                    target_xywh="200,460,960,84",
                ),
                ArtifactOccurrence(
                    artifact_id=meiping.id,
                    canvas_id=canvas_86.id,
                    plate_figure_id=plate_fig.id,
                    role="illustrated",
                    confidence=0.98,
                    context_snippet="图一二:1 白地黑花牡丹纹梅瓶 (T14③:12)",
                    target_xywh=plate_fig.crop_xywh,
                ),
                ArtifactOccurrence(
                    artifact_id=meiping.id,
                    canvas_id=canvas_plate3.id,
                    role="photographed",
                    confidence=0.96,
                    context_snippet="彩版叁:2 白地黑花牡丹纹梅瓶",
                    target_xywh="300,400,1800,2200",
                ),
            ]
        )

        # Test domain-safe continuous page range merging for IndexEntry
        merged_ranges = merge_domain_page_ranges(
            [
                ("body", 85, "85"),
                ("body", 86, "86"),
                ("insert", None, "插页"),
                ("body", 88, "88"),
                ("plate", 3, "彩版叁"),
            ]
        )
        idx_entry = IndexEntry(
            manifest_id=manifest.id,
            index_type="artifact",
            key="T14③:12",
            display_text="T14③:12（Ⅰ式白地黑花梅瓶）",
            target_type="artifact",
            target_id=meiping.id,
            page_ranges=merged_ranges,
            formatted_pages=", ".join(r["display"] for r in merged_ranges),
        )
        session.add(idx_entry)

        # Create hierarchical IIIF Range (Table of Contents)
        ch_range = IiifRange(
            manifest_id=manifest.id,
            level=1,
            sort_order=1,
            section_number="第四章",
            title="出土遗物",
            start_canvas_id=canvas_86.id,
            start_label="85",
            end_label="88",
        )
        session.add(ch_range)
        session.flush()

        sec_range = IiifRange(
            manifest_id=manifest.id,
            parent_id=ch_range.id,
            level=2,
            sort_order=1,
            section_number="第二节",
            title="白地黑花瓷器",
            start_canvas_id=canvas_86.id,
            start_label="86",
            end_label="88",
            start_xywh="200,280,1040,110",
        )
        session.add(sec_range)
        session.flush()

        session.add(
            RangeItem(
                range_id=sec_range.id,
                canvas_id=canvas_86.id,
                sequence_order=1,
                fragment_xywh="200,280,2000,1400",
            )
        )

        # Create RAG Chunk + ProvenanceAnchor
        rag_chunk = RagChunk(
            manifest_id=manifest.id,
            range_id=sec_range.id,
            breadcrumb_path="观台磁州窑址 > 第四章 出土遗物 > 第二节 白地黑花瓷器",
            chunk_index=1,
            content_text="Ⅰ式梅瓶：标本T14③:12，高28.5cm，小唇口，短颈，丰肩。腹部绘折枝牡丹纹，笔法潇洒（图一二:1；彩版叁:2）。",
            token_count=52,
        )
        session.add(rag_chunk)
        session.flush()

        builder = IiifBuilder(session)
        anchor = ProvenanceAnchor(
            source_entity_type="rag_chunk",
            source_entity_id=rag_chunk.id,
            manifest_id=manifest.id,
            canvas_id=canvas_86.id,
            range_id=sec_range.id,
            printed_page="86",
            verbatim_quote=rag_chunk.content_text,
            highlight_boxes=[
                {"xywh": "200,460,960,84"},
                {"xywh": "200,560,960,84"},
            ],
            iiif_target_uri=f"{builder.canvas_uri(manifest.id, canvas_86.id)}#xywh=200,460,960,184",
        )
        session.add(anchor)
        session.flush()

        page_bundle = builder.build_page_bundle(canvas_86.id)

        print("=== 1. V2 多域页码 DP 序列对齐与异常检测 ===")
        for res in aligned_results:
            print(
                f"  Scan #{res.scan_index:02d} -> domain={res.page_domain:<12} "
                f"ordinal={str(res.page_ordinal):<4} label='{res.original_page_label}' "
                f"conf={res.mapping_confidence} status={res.mapping_status}"
            )
        print(f"  自动入队的审校任务 (ReviewTasks): {len(anomalies)} 条")
        for anom in anomalies:
            print(f"    [优先级 {anom.priority_score}] ({anom.risk_type}) {anom.description}")

        print("\n=== 2. 分域安全页码合并索引 (IndexEntry) ===")
        print(f"  词条: {idx_entry.display_text}")
        print(f"  合并页码: {idx_entry.formatted_pages}")

        print("\n=== 3. V2 Page Bundle (含三坐标系、四态文本、编号保护、器物多角色关联) ===")
        print(json.dumps(page_bundle, ensure_ascii=False, indent=2))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Initialize V2 IIIF + ALTO SQLite/PostgreSQL database.")
    parser.add_argument(
        "--db-url",
        default=DEFAULT_DB_URL,
        help=f"Database connection URL (default: {DEFAULT_DB_URL})",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Seed V2 archaeological report data (观台磁州窑址) and run verification.",
    )
    args = parser.parse_args()

    if args.demo:
        db_file = Path(args.db_url.replace("sqlite:///", "", 1))
        if args.db_url.startswith("sqlite:///") and db_file.exists():
            db_file.unlink()
        seed_demo_data(args.db_url)
    else:
        init_db(db_url=args.db_url)
        print(f"V2 Database initialized at: {args.db_url}")


if __name__ == "__main__":
    main()
