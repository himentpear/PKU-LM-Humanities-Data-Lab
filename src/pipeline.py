"""Document orchestration with page checkpoints and atomic final database commit."""
from __future__ import annotations

import hashlib
import json
import time
import sqlite3
from pathlib import Path

from sqlalchemy import select, delete

from src.db.models import Manifest, Work, ReviewTask, Canvas, AltoLine
from src.db.session import init_db, get_session
from src.iiif.generator import IiifBuilder
from src.ingest.digital_pdf_ingestor import DigitalPdfIngestor
from src.ingest.page_pipeline import PageExtractor, PIPELINE_VERSION, atomic_json, file_hash, job_lock


def index_fingerprint() -> str:
    root=Path(__file__).resolve().parents[1]
    paths=[root/"configs/heading_rules.yaml",root/"requirements.txt"]
    for folder in ["src/ingest","src/alignment","src/alto","src/iiif","src/db"]:
        paths.extend((root/folder).glob("*.py"))
    paths.extend(root/p for p in ["src/enrichment.py","src/pipeline.py","src/io_utils.py"])
    digest=hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n",b"\n"))
    return digest.hexdigest()


def _backup_database(output: Path) -> Path:
    backup=output/"backups"/f"before-reindex-{time.time_ns()}.db"
    backup.parent.mkdir(parents=True,exist_ok=True)
    source=sqlite3.connect((output/"library.db").as_uri()+"?mode=ro",uri=True)
    target=sqlite3.connect(backup)
    try:
        source.backup(target)
        if target.execute("pragma integrity_check").fetchone()[0]!="ok":
            raise RuntimeError("Pre-reindex backup did not pass integrity check")
    finally:
        target.close();source.close()
    return backup


def run_document(pdf: Path, output: Path, *, max_pages: int | None = None,
                 dpi: int = 180, threads: int = 4, base_url: str = "http://127.0.0.1:8000/iiif",
                 stop_after: int | None = None, settings_path: Path | None = None,
                 rebuild_index: bool = False, skip_models: bool = False) -> dict:
    import pymupdf
    pdf, output = pdf.resolve(), output.resolve()
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    if not 72 <= dpi <= 400:
        raise ValueError("dpi must be between 72 and 400")
    sha256 = file_hash(pdf)
    config = {"version": PIPELINE_VERSION, "dpi": dpi, "max_pages": max_pages}
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    run_id = f"{sha256[:16]}-{config_hash}"
    run_dir = output / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state_file = run_dir / "job.json"
    with job_lock(run_dir / "job.lock"):
        previous=json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        fingerprint=index_fingerprint()
        state = {"run_id": run_id, "source_name": pdf.name, "source_sha256": sha256,
                 "config": config, "status": "extracting", "completed_pages": 0,
                 "index_fingerprint":fingerprint,"errors": [], "started_at": time.time()}
        atomic_json(state_file, state)
        engine = init_db("sqlite:///" + (output / "library.db").as_posix())
        try:
            with get_session(engine) as session:
                existing = session.scalar(select(Manifest).join(Work).where(Work.slug == run_id))
                if existing:
                    stale=(existing.work.metadata_json.get("index_fingerprint")!=fingerprint)
                    if stale and not rebuild_index:
                        raise ValueError("Index code/config changed. Back up and use --rebuild-index to regenerate from cached pages.")
                    existing_id,existing_pages,existing_work=existing.id,existing.total_pages,existing.work_id
                    if stale:
                        human_review=session.scalar(select(ReviewTask.id).where(ReviewTask.manifest_id==existing.id,
                            ReviewTask.status.in_(["approved","rejected","in_review"])))
                        if human_review:
                            raise ValueError("Document contains human review work; reindex into a separate library to preserve it")
                        reviewed_line=session.scalar(select(AltoLine.id).join(Canvas).where(Canvas.manifest_id==existing.id,
                            AltoLine.approved_text.is_not(None)))
                        locked_page=session.scalar(select(Canvas.id).where(Canvas.manifest_id==existing.id,Canvas.mapping_status=="manual_locked"))
                        if reviewed_line or locked_page:
                            raise ValueError("Document contains approved text or locked pages; preserve them in a separate library")
                    else:
                        existing_work=None
                else:
                    existing_id=None;existing_work=None
            if existing_id and existing_work is None:
                stats={**previous.get("stats",{}),"manifest_id":existing_id,"pages_ingested":existing_pages,"reused":True}
                state.update(status="model_enrichment",completed_pages=existing_pages,total_pages=existing_pages)
                atomic_json(state_file,state)
                from src.model_enrichment import enrich_with_models
                state["model_stage"]={"status":"skipped"} if skip_models else enrich_with_models(engine,existing_id,run_dir,base_url,settings_path)
                with get_session(engine) as session:
                    _export(session,existing_id,run_dir,base_url)
                state.update(status="completed",stats=stats,finished_at=time.time())
                atomic_json(state_file,state)
                return state
            if existing_work:
                state["pre_reindex_backup"]=str(_backup_database(output))
            pages = []
            extractor = PageExtractor(run_dir, dpi, threads)
            with pymupdf.open(pdf) as document:
                total = min(len(document), max_pages) if max_pages else len(document)
                state["total_pages"] = total
                for index in range(total):
                    try:
                        pages.append(extractor.extract(document, index, sha256))
                    except Exception as error:
                        # Do not log exception bodies: a third-party client can include credentials.
                        state["errors"].append({"scan_index": index+1, "error_type": type(error).__name__})
                        raise
                    state["completed_pages"] = index+1
                    atomic_json(state_file, state)
                    if (index+1) % 10 == 0 or index == total-1:
                        print(json.dumps({"run_id": run_id, "stage": "extracting", "pages": index+1,
                                          "total": total, "method": pages[-1].method}), flush=True)
                    if stop_after and index+1 >= stop_after:
                        state["status"] = "interrupted_test"
                        atomic_json(state_file, state)
                        return state
            state["status"] = "ingesting"
            atomic_json(state_file, state)
            # A failed commit leaves no partially indexed document. Page OCR caches
            # remain available, so retries do not repeat costly page recognition.
            with get_session(engine) as session:
                if existing_work:
                    # Remove only the unreviewed derived document, within the same
                    # transaction as its replacement. Failure restores the old index.
                    session.execute(delete(Work).where(Work.id==existing_work))
                    session.flush()
                stats = DigitalPdfIngestor(session, base_url=base_url).ingest_pdf(
                    pdf, run_id, pdf.stem, max_pages=max_pages, prepared_pages=pages,
                    source_sha256=sha256)
                canvases = list(session.scalars(select(Canvas).where(
                    Canvas.manifest_id == stats["manifest_id"]).order_by(Canvas.scan_index)))
                for page, canvas in zip(pages, canvases):
                    for issue in page.quality_issues:
                        # Routing reasons aren't necessarily errors after successful OCR.
                        if issue in {"damaged_text_layer", "ocr_no_text_review_image"}:
                            session.add(ReviewTask(manifest_id=stats["manifest_id"], canvas_id=canvas.id,
                                target_type="ocr", target_id=canvas.id, risk_type=issue,
                                description="请对照原页核对文字层", priority_score=0.8))
                from src.enrichment import enrich_document
                stats.update(enrich_document(session, stats["manifest_id"], pages, run_dir, base_url))
                manifest=session.get(Manifest,stats["manifest_id"])
                manifest.work.metadata_json={**manifest.work.metadata_json,"index_fingerprint":fingerprint}
            state["status"] = "exporting"
            state["stats"] = stats
            atomic_json(state_file, state)
            from src.model_enrichment import enrich_with_models
            state["model_stage"] = {"status":"skipped"} if skip_models else enrich_with_models(engine,stats["manifest_id"],run_dir,base_url,settings_path)
            with get_session(engine) as session:
                _export(session, stats["manifest_id"], run_dir, base_url)
            state.update(status="completed", finished_at=time.time())
            atomic_json(state_file, state)
            return state
        except Exception as error:
            state.update(status="failed", error_type=type(error).__name__, updated_at=time.time())
            atomic_json(state_file, state)
            raise
        finally:
            engine.dispose()


def _export(session, manifest_id: str, run_dir: Path, base_url: str):
    from src.alto.exporter import export_canvas
    builder = IiifBuilder(session, base_url)
    atomic_json(run_dir / "manifest.json", builder.build_manifest(manifest_id))
    canvases = list(session.scalars(select(Canvas).where(Canvas.manifest_id == manifest_id).order_by(Canvas.scan_index)))
    for canvas in canvases:
        atomic_json(run_dir / "bundles" / f"{canvas.scan_index:05d}.json", builder.build_page_bundle(canvas.id))
        export_canvas(session, canvas, run_dir / "alto" / f"{canvas.scan_index:05d}.xml")
    reviews = list(session.scalars(select(ReviewTask).where(ReviewTask.manifest_id == manifest_id)))
    atomic_json(run_dir / "quality.json", {"manifest_id":manifest_id, "pages":len(canvases),
        "pending_review_count":sum(r.status == "pending" for r in reviews),
        "review_tasks":[{"id":r.id,"canvas_id":r.canvas_id,"risk":r.risk_type,
                          "status":r.status,"description":r.description} for r in reviews]})
