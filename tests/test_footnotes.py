"""Footnote regressions: actual scan, adverse layouts, persistence and revisions."""
import json
from pathlib import Path
import pymupdf
import pytest
from sqlalchemy import select
from src.ingest.digital_pdf_ingestor import ExtractedLine, ExtractedPageData, DigitalPdfIngestor
from src.ingest.footnotes import detect, reference_offsets
from src.db.models import Canvas, AltoLine, AltoBlock, Footnote, FootnoteReference, ReviewTask
from src.db.session import init_db, get_session
from src.iiif.generator import IiifBuilder
from src.review import resolve_review

ROOT=Path(__file__).resolve().parents[1]
SAMPLE=ROOT/'data/processed/benchmarks/stratified-sample/1793344610008326000/c92f5f329d8be223/pages/00331.json'


def line(text,y,size=12,x=70):
    return ExtractedLine(text,x,y,450,size*1.3,size,.99)


def page(lines):
    return ExtractedPageData(1,0,'1',600,900,0,lines)


def test_code_markers_are_not_references():
    text='标本 T 3 ③ ：31，T19③c∶328，T4⑥：85，与他例相似②。'
    refs=list(reference_offsets(text))
    assert len(refs)==1 and text[refs[0][0]:refs[0][1]]=='②'
    assert list(reference_offsets('③ 地层编号'))==[]
    assert list(reference_offsets('T3③'))==[]


def test_wrapping_duplicates_and_orphans(monkeypatch):
    monkeypatch.setattr('src.ingest.footnotes.separator_above',lambda *a:True)
    p=page([line('正文内容相似①，并且有关编号T3③：31。',180),
        line('① 某某作者：《考古报告》，1980年。',720,8),
        line('续接上行出版社和页码等注释文字。',734,8)])
    plan=detect(p)
    assert len(plan.notes)==1 and plan.notes[0].indices==[1,2]
    assert plan.references[0].note_index==0
    p.lines.append(line('① 另一个同号脚注，需要人工判定。',760,8))
    plan=detect(p)
    assert all(n.status=='needs_review' for n in plan.notes)
    assert all(r.note_index is None for r in plan.references)
    plan=detect(page([line('正文孤立引用②。其他正文内容继续。',180)]))
    assert not plan.notes and plan.references[0].status=='needs_review'


def test_low_page_list_is_not_automatically_a_note(monkeypatch):
    monkeypatch.setattr('src.ingest.footnotes.separator_above',lambda *a:False)
    p=page([line('这是一段字号一致的正文内容。',180),line('③ 此处为正文列举的第三种地层。',720)])
    assert detect(p).notes==[]


def real_page():
    if not SAMPLE.exists():pytest.skip('Local scan sample is not distributed with repository')
    d=json.loads(SAMPLE.read_text(encoding='utf8'))['page']
    d['lines']=[ExtractedLine(**l) for l in d['lines']]
    d.update(scan_index=1,pdf_page_index=0)
    return ExtractedPageData(**d)


def test_actual_scan_preserves_codes_and_links_both_notes():
    p=real_page();plan=detect(p)
    assert [n.marker for n in plan.notes]==['①','②']
    assert [r.marker for r in plan.references]==['①','②']
    assert [r.note_index for r in plan.references]==[0,1]
    assert any('T3③：31' in l.text for l in p.lines)
    assert all(n.evidence['separator'] for n in plan.notes)


def test_persist_export_and_revision_invalidation(tmp_path,monkeypatch):
    monkeypatch.setattr('src.ingest.footnotes.separator_above',lambda *a:True)
    p=page([line('标本T3③：31，与其他标本相似①。',180),line('另一处正文引用②。考古文字继续。',240),
        line('① 作者：《考古报告》，1980年。',720,8),line('② 作者：《另一份报告》，1982年。',750,8)])
    pdf=tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page(width=p.width_pt,height=p.height_pt);doc.save(pdf)
    engine=init_db('sqlite:///'+(tmp_path/'library.db').as_posix())
    with get_session(engine) as session:
        stats=DigitalPdfIngestor(session).ingest_pdf(pdf,'regression','Footnotes',prepared_pages=[p])
        canvas=session.scalar(select(Canvas))
        refs=list(session.scalars(select(FootnoteReference)))
        notes=list(session.scalars(select(Footnote)))
        assert len(refs)==2 and len(notes)==2 and all(r.status=='linked' for r in refs)
        for ref in refs:
            source=session.get(AltoLine,ref.line_id)
            assert source.content_text[ref.char_start:ref.char_end]==ref.marker
            assert session.get(Footnote,ref.footnote_id).canvas_id==canvas.id
        note_blocks=list(session.scalars(select(AltoBlock).where(AltoBlock.block_type=='footnote')))
        assert len(note_blocks)==2
        bundle=IiifBuilder(session).build_page_bundle(canvas.id)
        assert len(bundle['footnotes'])==len(bundle['footnote_references'])==2
        task=ReviewTask(manifest_id=stats['manifest_id'],canvas_id=canvas.id,target_type='ocr',
            target_id=canvas.id,risk_type='test',description='Correction')
        session.add(task);session.flush()
        source=session.get(AltoLine,refs[0].line_id)
        raw=source.raw_ocr
        result=resolve_review(session,task.id,actor='tester',note='对照原图修订文字并验证旧脚注关联失效',
            status='approved',expected_revision=canvas.revision_no,line_id=source.id,text=source.content_text+'修订')
        assert source.raw_ocr==raw
        assert all(r.status=='stale' and r.footnote_id is None for r in refs)
        assert any(t.risk_type=='footnote_text_changed' for t in session.scalars(select(ReviewTask)))
        session.flush()
    with engine.connect() as c:
        assert c.exec_driver_sql('pragma foreign_key_check').fetchall()==[]
    engine.dispose()


def test_split_marker_and_cross_page_citation(monkeypatch):
    monkeypatch.setattr('src.ingest.footnotes.separator_above',lambda *a:True)
    p=page([line('正文引用①。另见第123页注①。',180),
        ExtractedLine('①',70,721,10,10,7,.99),line('某某作者：《考古报告》，1980年。',720,9,x=90)])
    plan=detect(p)
    assert len(plan.notes)==1 and plan.notes[0].indices==[1,2]
    assert plan.references[0].note_index==0
    assert plan.references[1].note_index is None and plan.references[1].kind=='cross_page_citation'


def test_manual_link_resolves_unreadable_marker_and_rejects_wrong_page(tmp_path,monkeypatch):
    monkeypatch.setattr('src.ingest.footnotes.separator_above',lambda *a:True)
    p=page([line('正文引用①。考古报告中的正文内容。',180),line('① 作者：《考古报告》，1980年。',720,8)])
    pdf=tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page(width=600,height=900);doc.save(pdf)
    engine=init_db('sqlite:///'+(tmp_path/'library.db').as_posix())
    with get_session(engine) as session:
        stats=DigitalPdfIngestor(session).ingest_pdf(pdf,'manual','Footnotes',prepared_pages=[p])
        canvas=session.scalar(select(Canvas));ref=session.scalar(select(FootnoteReference));target=session.get(Footnote,ref.footnote_id)
        ref.status='needs_review';ref.footnote_id=None;target.marker='?';target.status='needs_review'
        task=ReviewTask(manifest_id=stats['manifest_id'],canvas_id=canvas.id,target_type='footnote_reference',
            target_id=ref.id,risk_type='footnote_unresolved',description='Uncertain marker')
        session.add(task);session.flush()
        resolve_review(session,task.id,actor='tester',note='对照原图确认此处为带圈数字一',status='approved',
            expected_revision=canvas.revision_no,footnote_id=target.id)
        assert target.marker=='①' and ref.status=='approved' and ref.footnote_id==target.id
        source=session.get(AltoLine,ref.line_id)
        task2=ReviewTask(manifest_id=stats['manifest_id'],canvas_id=canvas.id,target_type='ocr',target_id=canvas.id,
            risk_type='ocr',description='Correction')
        session.add(task2);session.flush()
        resolve_review(session,task2.id,actor='tester',note='核对原文并在前面增加文字检查引用位置变化',status='approved',
            expected_revision=canvas.revision_no,line_id=source.id,text='修订'+source.content_text)
        old_id=ref.id
        stale=session.scalar(select(ReviewTask).where(ReviewTask.risk_type=='footnote_text_changed'))
        resolve_review(session,stale.id,actor='tester',note='根据当前文字重新生成关联候选',status='approved',expected_revision=canvas.revision_no)
        session.flush()
        fresh=session.scalar(select(FootnoteReference))
        assert fresh.id!=old_id and fresh.status=='linked' and fresh.char_start==6
        assert source.raw_ocr.startswith('正文引用')
    engine.dispose()


def test_gutter_shape_recovers_unknown_marker_without_guessing(tmp_path):
    import cv2
    import numpy as np
    image=np.full((900,600),255,dtype=np.uint8)
    cv2.line(image,(70,690),(230,690),0,1)
    cv2.circle(image,(79,754),4,0,1)
    path=tmp_path/'scan.png';cv2.imencode('.png',image)[1].tofile(path)
    p=page([line('正文引用①，另一条引用②。正文继续。',180),line('① 有明确标记的参考文献内容。',720,8),
        line('序号漏识别的参考文献内容。',750,8,x=90)])
    p.image_path=str(path)
    plan=detect(p)
    assert [n.marker for n in plan.notes]==['①','?']
    assert plan.notes[0].indices==[1]
    assert plan.notes[1].status=='needs_review' and plan.notes[1].evidence['unreadable_marker_bbox']
    assert plan.references[1].note_index is None

def test_http_footnote_bundle_can_submit_review_revision(tmp_path,monkeypatch):
    import threading
    import httpx
    from src.server import make_server
    monkeypatch.setattr('src.ingest.footnotes.separator_above',lambda *a:True)
    p=page([line('正文引用①。正文继续。考古报告。',180),line('① 作者：《考古报告》，1980年。',720,8)])
    pdf=tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page(width=600,height=900);doc.save(pdf)
    engine=init_db('sqlite:///'+(tmp_path/'library.db').as_posix())
    with get_session(engine) as session:
        stats=DigitalPdfIngestor(session).ingest_pdf(pdf,'api-test','Footnotes',prepared_pages=[p])
        canvas=session.scalar(select(Canvas));ref=session.scalar(select(FootnoteReference))
        task=ReviewTask(manifest_id=stats['manifest_id'],canvas_id=canvas.id,target_type='footnote_reference',
            target_id=ref.id,risk_type='footnote_unresolved',description='Verify endpoint')
        session.add(task);session.flush();task_id,cid=task.id,canvas.id
    server=make_server(tmp_path,port=0)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=30) as client:
            bundle=client.get('/api/page',params={'id':cid}).json()
            assert isinstance(bundle['revision'],int)
            assert bundle['scan_image']['full_image_url'].startswith(f'http://127.0.0.1:{server.server_port}/')
            assert len(bundle['footnotes'])==len(bundle['footnote_references'])==1
            assert [l['text'] for l in bundle['lines']]==[l.text for l in p.lines]
            response=client.post('/api/review',json={'task_id':task_id,'actor':'tester','note':'对照原图确认引用与注释一致',
                'status':'approved','expected_revision':bundle['revision'],'footnote_id':bundle['footnotes'][0]['id']})
            assert response.status_code==200,response.text
            updated=client.get('/api/page',params={'id':cid}).json()
            assert updated['footnote_references'][0]['status']=='approved'
    finally:
        server.shutdown();worker.join();server.server_close();server.engine.dispose();engine.dispose()


def test_chinese_unit_parts_and_enumerated_strata_never_become_notes():
    text='黑瓷碗③，分别同Y8火③和Y8火②出土物。T3②、③、④、⑤层，以及③、④层，参见另例②。'
    refs=list(reference_offsets(text))
    assert [m for _,_,m in refs]==['③','②']
    assert refs[0][0]==3 and refs[1][0]==text.rindex('②')
    assert list(reference_offsets('例如，Y3火①，指三号窑火膛内堆积1层。'))==[]
    assert list(reference_offsets('简称Y3火①、Y3火②和Y3火③。'))==[]

def test_same_specimen_code_in_different_sites_does_not_merge(tmp_path):
    from src.server import _build_index_cache
    from src.db.models import Manifest
    p=page([line('标本T3③：31，残。宽3.5厘米。',180)])
    pdf=tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page(width=600,height=900);doc.save(pdf)
    engine=init_db('sqlite:///'+(tmp_path/'library.db').as_posix())
    with get_session(engine) as session:
        for slug in ['site-a','site-b']:
            stats=DigitalPdfIngestor(session).ingest_pdf(pdf,slug,slug,prepared_pages=[p])
            manifest=session.get(Manifest,stats['manifest_id'])
            manifest.work.metadata_json={'source_namespace':slug}
    cache=_build_index_cache(engine)
    entries=[e for e in cache['cross_artifacts'] if e['code']=='T3③∶31']
    assert len(entries)==2
    assert all(len(e['manifest_ids'])==1 for e in entries)
    assert {e['source_namespace'] for e in entries}=={'site-a','site-b'}
    engine.dispose()

def test_review_rejects_target_on_another_page_without_changing_revision(tmp_path):
    from src.db.models import Manifest
    p=page([line('正文引用①。考古报告正文继续。',180)])
    pdf=tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page(width=600,height=900);doc.save(pdf)
    engine=init_db('sqlite:///'+(tmp_path/'library.db').as_posix())
    with get_session(engine) as s:
        a=DigitalPdfIngestor(s).ingest_pdf(pdf,'a','a',prepared_pages=[p])
        b=DigitalPdfIngestor(s).ingest_pdf(pdf,'b','b',prepared_pages=[p])
        ca=s.scalar(select(Canvas).where(Canvas.manifest_id==a['manifest_id']))
        cb=s.scalar(select(Canvas).where(Canvas.manifest_id==b['manifest_id']))
        ref=s.scalar(select(FootnoteReference).where(FootnoteReference.canvas_id==ca.id))
        wrong=Footnote(canvas_id=cb.id,marker='①',content_text='another page',source_line_ids=[],status='approved',confidence=1)
        s.add(wrong);s.flush()
        task=s.scalar(select(ReviewTask).where(ReviewTask.target_id==ref.id))
        task_id,wrong_id,canvas_id,revision=task.id,wrong.id,ca.id,ca.revision_no
    with pytest.raises(ValueError,match='same-page'):
        with get_session(engine) as s:
            resolve_review(s,task_id,actor='tester',note='错误跨页目标测试',status='approved',expected_revision=revision,footnote_id=wrong_id)
    with get_session(engine) as s:
        assert s.get(Canvas,canvas_id).revision_no==revision
        assert s.get(ReviewTask,task_id).status=='pending'
    engine.dispose()
