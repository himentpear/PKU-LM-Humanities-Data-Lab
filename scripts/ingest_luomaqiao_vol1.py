"""Ingest 《景德镇落马桥遗址》第一卷 (`docs/考古报告/1第一卷-已复.pdf`) into V2 database and verify queries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select

from src.db.models import Canvas, IiifRange, IndexEntry
from src.db.session import DEFAULT_DB_URL, get_session, init_db
from src.iiif.generator import IiifBuilder
from src.ingest.digital_pdf_ingestor import DigitalPdfIngestor


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Test ingestion of 《景德镇落马桥遗址（第一卷）》 into V2 SQLite database."
    )
    parser.add_argument(
        "--pdf",
        default="docs/考古报告/1第一卷-已复.pdf",
        help="Path to Volume 1 PDF.",
    )
    parser.add_argument(
        "--db-url",
        default=DEFAULT_DB_URL,
        help=f"Database URL (default: {DEFAULT_DB_URL})",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional page limit (default: ingest all 412 pages).",
    )
    args = parser.parse_args()

    engine = init_db(db_url=args.db_url)
    with get_session(engine) as session:
        ingestor = DigitalPdfIngestor(session, include_annotations=True)
        stats = ingestor.ingest_pdf(
            pdf_path=args.pdf,
            work_slug="jingdezhen-luomaqiao",
            work_title="景德镇落马桥遗址2012～2017年考古发掘报告",
            volume_number=1,
            volume_label="第一卷",
            site_name="江西省景德镇市落马桥窑址",
            max_pages=args.max_pages,
        )

        manifest_id = stats["manifest_id"]
        pages_per_sec = round(
            stats["pages_ingested"] / max(0.01, stats["elapsed_seconds"]), 2
        )

        print("=== 1. 《景德镇落马桥遗址（第一卷）》入库性能与规模统计 ===")
        print(f"  处理页数          : {stats['pages_ingested']} 页")
        print(f"  总耗时            : {stats['elapsed_seconds']} 秒 (速度: {pages_per_sec} 页/秒)")
        print(f"  版面区块 (Blocks) : {stats['blocks_count']} 个 (过滤跨页重复页眉 {stats['running_headers_filtered']} 处)")
        print(f"  文本行 (Lines)    : {stats['lines_count']} 行")
        print(f"  自动目录 (Ranges) : {stats['toc_ranges_count']} 个章节节点")
        print(f"  随文插图 (Figures): {stats['figures_count']} 幅")
        print(f"  器物标本 (Artifacts): {stats['artifacts_count']} 件 (共 {stats['artifact_occurrences_count']} 次出现)")
        print(f"  合并索引词条      : {stats['index_entries_count']} 条")

        # 2. Check multi-domain page alignment distribution
        domain_counts = session.execute(
            select(Canvas.page_domain, func.count(Canvas.id))
            .where(Canvas.manifest_id == manifest_id)
            .group_by(Canvas.page_domain)
        ).all()
        print("\n=== 2. 多域页码 (Page Domain) 分布与典型页码对齐结果 ===")
        for dom, cnt in domain_counts:
            print(f"  - {dom:<14}: {cnt} 页")

        sample_scans = [1, 5, 58, 59, 60, 69, 137, 205, 412]
        sample_canvases = list(
            session.scalars(
                select(Canvas)
                .where(Canvas.manifest_id == manifest_id, Canvas.scan_index.in_(sample_scans))
                .order_by(Canvas.scan_index)
            )
        )
        for c in sample_canvases:
            print(
                f"  Scan #{c.scan_index:03d} (PDF p.{c.pdf_page_index:<3}) -> "
                f"domain={c.page_domain:<12} ord={str(c.page_ordinal):<4} "
                f"印刷页码='{c.printed_page_label}' (conf={c.mapping_confidence})"
            )

        # 3. Show extracted top-level and chapter-level TOC ranges in page order
        top_ranges = list(
            session.scalars(
                select(IiifRange)
                .join(Canvas, IiifRange.start_canvas_id == Canvas.id)
                .where(IiifRange.manifest_id == manifest_id, IiifRange.level <= 3)
                .order_by(Canvas.scan_index, IiifRange.sort_order)
                .limit(20)
            )
        )
        print("\n=== 3. 自动抽取的目录树结构 (前 18 个章/节节点) ===")
        for r in top_ranges:
            indent = "  " * r.level
            print(
                f"{indent}[L{r.level}] {r.normalized_title} "
                f"(起始页码: {r.start_label}, 坐标: #xywh={r.start_xywh})"
            )

        # 4. Show sample multi-page artifact index entries
        all_idx_entries = list(
            session.scalars(
                select(IndexEntry).where(IndexEntry.manifest_id == manifest_id)
            )
        )
        multi_page_entries = [
            e for e in all_idx_entries if "," in e.formatted_pages or "–" in e.formatted_pages
        ][:10]
        if not multi_page_entries:
            multi_page_entries = all_idx_entries[:10]

        print("\n=== 4. 器物标本索引示例 (自动提取标本号 + 分域页码合并) ===")
        for entry in multi_page_entries:
            print(f"  标本 {entry.display_text:<18} -> 出处页码: {entry.formatted_pages}")

        # 5. Test IIIF Content Search & Page Bundle on Scan #137 (Printed Page 79)
        builder = IiifBuilder(session)
        search_res = builder.search_text_coordinates(manifest_id, "T15H2∶17")
        print(f"\n=== 5. IIIF 坐标级全文检索测试 (检索标本号 'T15H2∶17'，共命中 {search_res['total']} 处) ===")
        for hit in search_res["items"][:3]:
            print(
                f"  - 印刷页码: 第 {hit['printed_page']} 页 | Target: {hit['target']}\n"
                f"    原文行: {hit['body']['value']}"
            )

        canvas_137 = next((c for c in sample_canvases if c.scan_index == 137), sample_canvases[-1])
        bundle = builder.build_page_bundle(canvas_137.id)
        print(f"\n=== 6. 单页 Page Bundle 结构抽检 (Scan #{canvas_137.scan_index} / 印刷页码 {canvas_137.printed_page_label}) ===")
        print(
            json.dumps(
                {
                    "page_id": bundle["page_id"],
                    "identity": bundle["identity"],
                    "geometry": bundle["geometry"],
                    "layout_elements_count": len(bundle["layout_elements"]),
                    "sample_layout_elements": bundle["layout_elements"][:3],
                    "artifact_occurrences": bundle["artifact_occurrences"][:5],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
