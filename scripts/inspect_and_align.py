"""Inspect archaeological report PDFs in `docs/考古报告/` and test DP Page Sequence Alignment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.alignment.sequence_dp import PageObservation, PageSequenceAligner
from src.ingest.pdf_inspector import PdfInspector


def run_pdf_inspection(pdf_dir: Path, output_json: Path, compute_hash: bool = False) -> None:
    inspector = PdfInspector(sample_count=10)
    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"未在目录中找到 PDF 文件: {pdf_dir}")
        return

    print(f"=== 正在探查 {pdf_dir} 下的 {len(pdf_files)} 份考古报告 PDF ===")
    reports = []
    for pdf_path in pdf_files:
        rep = inspector.inspect_pdf(pdf_path, compute_hash=compute_hash)
        reports.append(rep.to_dict())
        print(
            f"- {rep.file_name:<18} | 页数: {rep.total_pages:>4} | 大小: {rep.file_size_mb:>6.1f} MB "
            f"| 类型: {rep.pdf_type:<10} | 书签数: {rep.outline_count:>3} "
            f"| 尺寸(pt): {rep.median_width_pt}x{rep.median_height_pt} "
            f"| 折页/大图数: {len(rep.foldout_page_indices)}"
        )

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n探查报告已保存至: {output_json}")


def run_alignment_simulation() -> None:
    """Demonstrate Viterbi DP alignment handling preface, body, foldouts, smudged page numbers,
    duplicate scans, missing page gaps, and back-of-book plates.
    """
    print("\n=== 页码序列动态规划 (Viterbi DP) 复杂场景对齐测试 ===")
    simulated_scans = [
        PageObservation(scan_index=1, pdf_page_index=0, source_page_label="1", ocr_page_label_raw=None, ocr_confidence=0.0, layout_hint="cover"),
        PageObservation(scan_index=2, pdf_page_index=1, source_page_label="2", ocr_page_label_raw="I", ocr_confidence=0.96, layout_hint="front_matter"),
        PageObservation(scan_index=3, pdf_page_index=2, source_page_label="3", ocr_page_label_raw="II", ocr_confidence=0.95, layout_hint="front_matter"),
        # Body starts at scan #4 -> printed page 1
        PageObservation(scan_index=4, pdf_page_index=3, source_page_label="4", ocr_page_label_raw="1", ocr_confidence=0.98, layout_hint="body"),
        # Scan #5 has smudged/illegible page number (ocr_page_label_raw=None), recovered as '2' by DP!
        PageObservation(scan_index=5, pdf_page_index=4, source_page_label="5", ocr_page_label_raw=None, ocr_confidence=0.0, layout_hint="body"),
        PageObservation(scan_index=6, pdf_page_index=5, source_page_label="6", ocr_page_label_raw="3", ocr_confidence=0.99, layout_hint="body"),
        # Scan #7 is a duplicate scan of printed page 3
        PageObservation(scan_index=7, pdf_page_index=6, source_page_label="7", ocr_page_label_raw="3", ocr_confidence=0.97, layout_hint="body", visual_similarity_to_prev=0.96),
        # Scan #8 is an unnumbered foldout map (插页)
        PageObservation(scan_index=8, pdf_page_index=7, source_page_label="8", ocr_page_label_raw=None, ocr_confidence=0.0, layout_hint="insert", is_foldout=True),
        # Scan #9 resumes body at page 4 (remembering active body ordinal 3 across the foldout!)
        PageObservation(scan_index=9, pdf_page_index=8, source_page_label="9", ocr_page_label_raw="4", ocr_confidence=0.98, layout_hint="body"),
        # Scan #10 jumps to page 6 (page 5 missing!)
        PageObservation(scan_index=10, pdf_page_index=9, source_page_label="10", ocr_page_label_raw="6", ocr_confidence=0.99, layout_hint="body"),
        # Scan #11 transitions to Plates
        PageObservation(scan_index=11, pdf_page_index=10, source_page_label="11", ocr_page_label_raw="图版一", ocr_confidence=0.97, layout_hint="plate"),
        PageObservation(scan_index=12, pdf_page_index=11, source_page_label="12", ocr_page_label_raw="图版二", ocr_confidence=0.98, layout_hint="plate"),
    ]

    aligner = PageSequenceAligner()
    results, anomalies = aligner.align_sequence(simulated_scans)

    for r in results:
        print(
            f"  Scan #{r.scan_index:02d} (PDF p.{r.pdf_page_index:<2}) -> "
            f"[{r.page_domain:<12}] ord={str(r.page_ordinal):<4} "
            f"label='{r.original_page_label:<4}' | conf={r.mapping_confidence:.3f} | {r.mapping_status}"
        )

    print(f"\n检测到异常并生成的审校任务 ({len(anomalies)} 项):")
    for a in anomalies:
        print(f"  * [Scan #{a.scan_index:02d} | {a.risk_type} | 优先级 {a.priority_score}] {a.description}")


def run_real_pdf_alignment_sample(pdf_dir: Path) -> None:
    """Run DP Page Sequence Alignment on real pages from `2第二卷-已复.pdf` (景德镇落马桥遗址)."""
    vol2_path = pdf_dir / "2第二卷-已复.pdf"
    if not vol2_path.exists():
        return

    from pypdf import PdfReader

    reader = PdfReader(str(vol2_path), strict=False)
    page_labels = list(reader.page_labels)
    observations: list[PageObservation] = []

    # Inspect the first 10 pages of Volume 2 to show how DP reconciles PDF PageLabel ("355".."364")
    # vs actual printed running header page numbers ("· 355 ·" starting on scan_index=3!)
    for idx in range(min(10, len(reader.pages))):
        page = reader.pages[idx]
        text = (page.extract_text() or "").strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        first_line = lines[0] if lines else ""
        last_line = lines[-1] if lines else ""

        # Check if first or last line starts with "· 357 ·" pattern
        header_candidate = None
        for ln in (first_line, last_line):
            if ln.startswith("·") and "·" in ln[1:]:
                header_candidate = ln.split("·")[1].strip()
                break

        layout_hint = "front_matter" if idx < 2 else "body"
        observations.append(
            PageObservation(
                scan_index=idx + 1,
                pdf_page_index=idx,
                source_page_label=page_labels[idx] if idx < len(page_labels) else str(idx + 1),
                ocr_page_label_raw=header_candidate,
                ocr_confidence=0.98 if header_candidate else 0.0,
                layout_hint=layout_hint,
            )
        )

    aligner = PageSequenceAligner()
    results, _ = aligner.align_sequence(observations)
    print("\n=== 真实文献实测：《景德镇落马桥遗址（第二卷）》前 10 页 PageLabel 偏移校准 ===")
    for r in results:
        print(
            f"  Scan #{r.scan_index:02d} | PDF内置PageLabel='{r.source_page_label}' "
            f"-> 还原印刷页码='{r.original_page_label}' (domain={r.page_domain}, conf={r.mapping_confidence:.3f})"
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Inspect PDFs and run DP Page Sequence Alignment.")
    parser.add_argument(
        "--pdf-dir",
        default="docs/考古报告",
        help="Directory containing archaeological report PDFs.",
    )
    parser.add_argument(
        "--output",
        default="reports/pdf_inspection_report.json",
        help="Output JSON path for PDF inspection report.",
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        help="Compute full SHA-256 hash for each PDF file.",
    )
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    run_pdf_inspection(pdf_dir, Path(args.output), compute_hash=args.hash)
    run_alignment_simulation()
    run_real_pdf_alignment_sample(pdf_dir)


if __name__ == "__main__":
    main()
