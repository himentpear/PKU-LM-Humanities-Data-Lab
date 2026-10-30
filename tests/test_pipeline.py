"""Regression checks against real PDF geometry and destructive/restart risks."""
import json
from pathlib import Path
import sqlite3

import pymupdf
import pytest
from sqlalchemy import select

from src.db.models import Canvas, Manifest
from src.db.session import get_session, init_db
from src.ingest.digital_pdf_ingestor import DigitalPdfIngestor
from src.ingest.page_pipeline import PageExtractor, file_hash
from src.pipeline import run_document

PDF=Path(__file__).resolve().parents[1]/"docs/考古报告/1第一卷-已复.pdf"


def test_real_geometry_and_body_domain():
    with pymupdf.open(PDF) as document:
        lines=DigitalPdfIngestor.extract_native_lines(document[136])
        line=next(l for l in lines if "F12∶109" in l.text and "失口" in l.text)
        assert 420<line.w_pt<450
        assert 110<line.y_top_pt<122
        lines=DigitalPdfIngestor.extract_native_lines(document[68])
        label,domain=DigitalPdfIngestor._detect_page_number_and_domain_hint(68,lines,document[68].rect.height)
        assert (label,domain)==("11","body")


def test_resume_idempotence_and_source_preservation(tmp_path):
    before=file_hash(PDF)
    interrupted=run_document(PDF,tmp_path,settings_path=tmp_path/"no-model.env",max_pages=5,stop_after=2)
    assert interrupted["status"]=="interrupted_test"
    run_dir=tmp_path/interrupted["run_id"]
    checkpoint=run_dir/"pages/00001.json"
    cache_time=checkpoint.stat().st_mtime_ns
    completed=run_document(PDF,tmp_path,settings_path=tmp_path/"no-model.env",max_pages=5)
    assert completed["status"]=="completed"
    assert checkpoint.stat().st_mtime_ns==cache_time
    repeated=run_document(PDF,tmp_path,settings_path=tmp_path/"no-model.env",max_pages=5)
    assert repeated["stats"]["reused"]
    conn=sqlite3.connect(tmp_path/"library.db")
    assert conn.execute("select count(*) from manifests").fetchone()[0]==1
    assert conn.execute("select count(*) from canvases").fetchone()[0]==5
    assert conn.execute("pragma foreign_key_check").fetchall()==[]
    conn.close()
    assert file_hash(PDF)==before
    assert len(list((run_dir/"alto").glob("*.xml")))==5
    assert len(json.loads((run_dir/"manifest.json").read_text(encoding="utf-8"))["items"])==5


def test_legacy_reingest_refuses_without_deleting(tmp_path):
    engine=init_db("sqlite:///"+(tmp_path/"safe.db").as_posix())
    try:
        with get_session(engine) as session:
            DigitalPdfIngestor(session).ingest_pdf(PDF,"safety","安全检查",max_pages=1)
        with pytest.raises(ValueError,match="already exists"):
            with get_session(engine) as session:
                DigitalPdfIngestor(session).ingest_pdf(PDF,"safety","安全检查",max_pages=1)
        with get_session(engine) as session:
            assert len(list(session.scalars(select(Manifest))))==1
            assert len(list(session.scalars(select(Canvas))))==1
    finally:
        engine.dispose()


def test_reindex_is_atomic_and_cache_is_reused(tmp_path,monkeypatch):
    first=run_document(PDF,tmp_path,settings_path=tmp_path/"no-model.env",max_pages=5)
    old_manifest=first["stats"]["manifest_id"]
    cache=tmp_path/first["run_id"]/"pages/00001.json"
    cache_time=cache.stat().st_mtime_ns
    monkeypatch.setattr("src.pipeline.index_fingerprint",lambda:"new-index-rules")
    with pytest.raises(ValueError,match="rebuild-index"):
        run_document(PDF,tmp_path,max_pages=5,skip_models=True)
    original=DigitalPdfIngestor.ingest_pdf
    def fail(*args,**kwargs):raise RuntimeError("injected ingestion failure")
    monkeypatch.setattr(DigitalPdfIngestor,"ingest_pdf",fail)
    with pytest.raises(RuntimeError,match="injected"):
        run_document(PDF,tmp_path,max_pages=5,rebuild_index=True,skip_models=True)
    connection=sqlite3.connect(tmp_path/"library.db")
    assert connection.execute("select id from manifests").fetchone()[0]==old_manifest
    connection.close()
    monkeypatch.setattr(DigitalPdfIngestor,"ingest_pdf",original)
    rebuilt=run_document(PDF,tmp_path,max_pages=5,rebuild_index=True,skip_models=True)
    assert rebuilt["stats"]["manifest_id"]!=old_manifest
    assert cache.stat().st_mtime_ns==cache_time
    connection=sqlite3.connect(tmp_path/"library.db")
    assert connection.execute("select count(*) from canvases").fetchone()[0]==5
    assert connection.execute("pragma foreign_key_check").fetchall()==[]
    connection.close()
    assert list((tmp_path/"backups").glob("*.db"))
