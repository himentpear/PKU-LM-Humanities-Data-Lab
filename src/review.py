"""Optimistic, audited text corrections with synchronized search/annotations."""
from sqlalchemy import select, update
from src.db.models import AltoBlock, AltoLine, Canvas, IiifAnnotation, ReviewTask, Revision
from src.alto.parser import AltoIngestor


def resolve_review(session, task_id: str, *, actor: str, note: str, status: str,
                   expected_revision: int, line_id: str | None = None, text: str | None = None,
                   footnote_id: str | None = None):
    if status not in {"approved","rejected"} or not actor.strip() or not note.strip():
        raise ValueError("Reviewer, reason and valid resolution are required")
    task=session.get(ReviewTask,task_id)
    if task is None:
        raise LookupError("Review task not found")
    canvas=session.get(Canvas,task.canvas_id)
    if canvas is None or canvas.revision_no!=expected_revision:
        raise ValueError("Page revision conflict; reload before reviewing")
    claimed=session.execute(update(Canvas).where(Canvas.id==canvas.id,Canvas.revision_no==expected_revision)
        .values(revision_no=expected_revision+1).execution_options(synchronize_session=False))
    if claimed.rowcount!=1:
        raise ValueError("Page revision conflict; reload before reviewing")
    before={"task_status":task.status,"revision":canvas.revision_no}
    after={"task_status":status,"note":note}
    if text is not None:
        if status!="approved" or line_id is None or not text.strip() or len(text)>20000:
            raise ValueError("An approved correction requires a nonempty line")
        line=session.get(AltoLine,line_id)
        if line is None or line.canvas_id!=canvas.id:
            raise ValueError("Correction line must belong to the review page")
        before.update(line_id=line.id,text=line.content_text)
        protected=AltoIngestor.extract_protected_spans(line.content_text)
        changed=[s["text"] for s in protected if s["text"] not in text]
        if changed and len(note.strip())<12:
            raise ValueError("Changing a protected number/code requires a detailed evidence note")
        line.approved_text=text;line.content_text=text;line.review_status="approved"
        block=session.get(AltoBlock,line.block_id)
        session.flush()
        lines=list(session.scalars(select(AltoLine).where(AltoLine.block_id==block.id).order_by(AltoLine.line_index)))
        block.approved_text="\n".join(l.approved_text or l.corrected_text or l.content_text for l in lines)
        block.content_text=block.approved_text;block.review_status="approved"
        for annotation in session.scalars(select(IiifAnnotation).where(IiifAnnotation.canvas_id==canvas.id,
                IiifAnnotation.target_xywh==line.xywh)):
            annotation.body_text=text
        after.update(line_id=line.id,text=text,protected_fields_changed=changed)
        _reindex_block(session,block,canvas)
        from src.ingest.footnotes import invalidate_after_correction
        invalidate_after_correction(session,canvas)
    if text is None and task.target_type in {"footnote", "footnote_reference", "footnote_page"}:
        from src.db.models import Footnote, FootnoteReference
        if task.target_type == "footnote_page":
            if status!="approved":
                raise ValueError("Recompute the stale page before closing its review")
            from src.ingest.footnotes import recompute_page
            after["footnotes_recomputed"]=recompute_page(session,canvas,task.id)
        elif task.target_type == "footnote_reference":
            ref=session.get(FootnoteReference,task.target_id)
            if ref is None or ref.canvas_id!=canvas.id:
                raise ValueError("Footnote reference does not belong to this page")
            before["footnote_reference"]={"status":ref.status,"target":ref.footnote_id}
            if status=="approved":
                target=session.get(Footnote,footnote_id or ref.footnote_id)
                line=session.get(AltoLine,ref.line_id)
                if target is None or target.canvas_id!=canvas.id or target.marker not in {ref.marker,"?"} or target.status=="rejected":
                    raise ValueError("Select a same-page footnote with the same marker")
                from src.ingest.footnotes import reference_offsets
                if (ref.char_start,ref.char_end,ref.marker) not in list(reference_offsets(line.content_text)):
                    raise ValueError("Reference offsets changed or are inside an archaeological code")
                if target.marker=="?":
                    before["unreadable_marker"]={"footnote_id":target.id,"marker":target.marker}
                    target.marker=ref.marker;target.status="approved"
                    target.evidence={**target.evidence,"marker_resolved_by":actor,"resolution_note":note}
                    for source_id in target.source_line_ids:
                        session.get(AltoLine,source_id).block.block_type="footnote"
                ref.footnote_id=target.id;ref.status="approved";ref.context_text=line.content_text
                ref.source_revision=expected_revision+1
            else:
                ref.footnote_id=None;ref.status="rejected"
            after["footnote_reference"]={"status":ref.status,"target":ref.footnote_id}
        else:
            target=session.get(Footnote,task.target_id)
            if target is None or target.canvas_id!=canvas.id:
                raise ValueError("Footnote does not belong to this page")
            if status=="approved" and target.marker=="?":
                raise ValueError("Unreadable marker: approve a body reference with this footnote selected to confirm its number")
            before["footnote"]={"status":target.status}
            target.status=status
            if status=="rejected":
                for ref in session.scalars(select(FootnoteReference).where(FootnoteReference.footnote_id==target.id)):
                    ref.footnote_id=None;ref.status="needs_review"
            for source_id in target.source_line_ids:
                source=session.get(AltoLine,source_id)
                source.block.block_type="footnote" if status=="approved" else "paragraph"
            after["footnote"]={"status":target.status}
    task.status=status;task.reviewer=actor;task.resolution_note=note
    canvas.revision_no=expected_revision+1
    session.add(Revision(entity_type="review_task",entity_id=task.id,revision_no=canvas.revision_no,
        actor=actor,reason=note,before_state=before,after_state=after))
    session.flush()
    return {"task_id":task.id,"status":task.status,"revision":canvas.revision_no}


def _reindex_block(session,block,canvas):
    from sqlalchemy import delete
    from src.db.models import (ArchEntity,EntityMention,ProvenanceAnchor,RagChunk,Manifest,
        Artifact,ArtifactOccurrence,IndexEntry,PlateFigure,IiifRange,FigureMention)
    from src.alignment.sequence_dp import merge_domain_page_ranges
    manifest=session.get(Manifest,canvas.manifest_id)
    old_mentions=list(session.scalars(select(EntityMention).where(EntityMention.block_id==block.id)))
    for mention in old_mentions:
        session.execute(delete(ProvenanceAnchor).where(ProvenanceAnchor.source_entity_type=="entity_mention",
            ProvenanceAnchor.source_entity_id==str(mention.id)))
        session.delete(mention)
    session.execute(delete(ArtifactOccurrence).where(ArtifactOccurrence.layout_block_id==block.id))
    for line in session.scalars(select(AltoLine).where(AltoLine.block_id==block.id)):
        if block.block_type in {"footnote","footnote_candidate"}:
            continue
        import re
        from src.enrichment import VOCAB
        spans=AltoIngestor.extract_protected_spans(line.content_text)
        for kind,words in VOCAB.items():
            for word in words:
                spans.extend({"type":kind,"text":word,"start":m.start(),"end":m.end()}
                    for m in re.finditer(re.escape(word),line.content_text))
        for span in spans:
            entity=session.scalar(select(ArchEntity).where(ArchEntity.work_id==manifest.work_id,
                ArchEntity.entity_type==span["type"],ArchEntity.canonical_name==span["text"]))
            if entity is None:
                entity=ArchEntity(work_id=manifest.work_id,entity_type=span["type"],canonical_name=span["text"])
                session.add(entity);session.flush()
            session.add(EntityMention(entity_id=entity.id,canvas_id=canvas.id,block_id=block.id,line_id=line.id,
                surface_text=span["text"],char_start=span["start"],char_end=span["end"],confidence=1.0))
            if span["type"]=="ARTIFACT_ID":
                artifact=session.scalar(select(Artifact).where(Artifact.work_id==manifest.work_id,Artifact.artifact_no==span["text"]))
                if artifact is None:
                    artifact=Artifact(work_id=manifest.work_id,artifact_no=span["text"])
                    session.add(artifact);session.flush()
                session.add(ArtifactOccurrence(artifact_id=artifact.id,canvas_id=canvas.id,layout_block_id=block.id,
                    role="mentioned",context_snippet=line.content_text,target_xywh=line.xywh,confidence=1.0))
    for anchor in session.scalars(select(ProvenanceAnchor).where(ProvenanceAnchor.canvas_id==canvas.id,
            ProvenanceAnchor.source_entity_type=="rag_chunk")):
        if any(box.get("xywh")==block.xywh for box in anchor.highlight_boxes):
            chunk=session.get(RagChunk,anchor.source_entity_id)
            if chunk:chunk.content_text=block.content_text;chunk.text_embedding=None
            anchor.verbatim_quote=block.content_text
    for figure in session.scalars(select(PlateFigure).where(PlateFigure.caption_block_id==block.id)):
        figure.caption_text=block.content_text
        figure.artifact_codes=AltoIngestor.extract_artifact_codes(block.content_text)
    for heading in session.scalars(select(IiifRange).where(IiifRange.heading_block_id==block.id)):
        heading.title=block.content_text.splitlines()[0]
        heading.normalized_title=heading.title
    session.execute(delete(FigureMention).where(FigureMention.mention_canvas_id==canvas.id))
    session.flush()
    AltoIngestor(session).link_figure_mentions_on_canvas(canvas.id)
    # Rebuild this document's specimen index from occurrences, preserving domains.
    session.execute(delete(IndexEntry).where(IndexEntry.manifest_id==canvas.manifest_id,IndexEntry.index_type=="artifact"))
    rows=session.execute(select(Artifact,ArtifactOccurrence,Canvas).join(ArtifactOccurrence,
        ArtifactOccurrence.artifact_id==Artifact.id).join(Canvas,Canvas.id==ArtifactOccurrence.canvas_id).where(Canvas.manifest_id==canvas.manifest_id)).all()
    groups={}
    for artifact,occurrence,page in rows:
        groups.setdefault(artifact.id,(artifact,[]))[1].append((page.page_domain,page.page_ordinal,page.printed_page_label or "未识别页码"))
    for artifact,occurrences in groups.values():
        ranges=merge_domain_page_ranges(occurrences)
        session.add(IndexEntry(manifest_id=canvas.manifest_id,index_type="artifact",key=artifact.artifact_no,
            display_text=artifact.artifact_no,target_type="artifact",target_id=artifact.id,page_ranges=ranges,
            formatted_pages=", ".join(r["display"] for r in ranges)))
