"""Source-preserving, conservative page-local footnote detection and linkage."""
from __future__ import annotations
import re
import statistics
from functools import lru_cache
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, field

VERSION = '1.0'
MARKERS = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
START = re.compile(r'^\s*([' + MARKERS + r'])\s*(.*)')
CODE = re.compile(r'[A-Za-z]\d+[A-Za-z0-9扩采' + MARKERS + r']*[:：∶︰]\d+(?:[-—]\d+)?')

@dataclass
class Note:
    marker: str
    indices: list[int]
    confidence: float
    status: str
    evidence: dict = field(default_factory=dict)

@dataclass
class Reference:
    index: int
    start: int
    end: int
    marker: str
    note_index: int | None
    status: str
    kind: str = "same_page"

@dataclass
class Plan:
    notes: list[Note]
    references: list[Reference]

    @property
    def groups(self):
        return {i: n for n, note in enumerate(self.notes) for i in note.indices}


def reference_offsets(text: str):
    # Match compact text, but return offsets into the unmodified source string.
    positions = [i for i, c in enumerate(text) if not c.isspace()]
    compact = ''.join(text[i] for i in positions)
    protected = [(m.start(), m.end()) for m in CODE.finditer(compact)]
    # Unit-part strata: Y3火① / Y8风②, and enumerated T3②、③、④层.
    # These are archaeological notation even without a specimen colon.
    layer_list=re.compile(r'[A-Za-z]\d+[A-Za-z0-9扩采火烟风道窑室炉膛填土灰坑' + MARKERS
        + r']*(?:[、，,及和至~～—-]['+MARKERS+r'])+')
    protected.extend((m.start(),m.end()) for m in layer_list.finditer(compact))
    bare_layers=re.compile(r'['+MARKERS+r'](?:[、，,及和至~～—-]['+MARKERS+r'])+(?:[a-z]?层)')
    protected.extend((m.start(),m.end()) for m in bare_layers.finditer(compact))
    for j, c in enumerate(compact):
        if c not in MARKERS or any(a <= j < b for a, b in protected):
            continue
        before, after = compact[:j], compact[j+1:]
        if re.search(r'[A-Za-z]\d+[A-Za-z0-9扩采火烟风道窑室炉膛填土灰坑' + MARKERS + r']*$', before):
            continue
        if before.endswith('第') or re.match(r'[a-z]?层',after):
            continue
        if after.startswith((':', '：', '∶', '︰')) or not before:
            continue
        yield positions[j], positions[j]+1, c


@lru_cache(maxsize=2)
def _gray(path, mtime):
    import cv2
    import numpy as np
    return cv2.imdecode(np.fromfile(path,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)


def gutter_marker(page,line):
    """Detect a tiny ring left of a note even when OCR missed its digit.

    A detected shape is only an unreadable marker candidate, never a guessed
    circled number. This also prevents absorbing a new note into the previous one.
    """
    if not page.image_path or not Path(page.image_path).exists():return None
    import cv2
    image=_gray(page.image_path,Path(page.image_path).stat().st_mtime_ns)
    scale=image.shape[1]/page.width_pt
    x0=max(0,int((line.x_pt-page.width_pt*.045)*scale))
    x1=max(x0,int((line.x_pt-1)*scale))
    y0=max(0,int((line.y_top_pt-line.h_pt*.2)*scale))
    y1=min(image.shape[0],int((line.y_top_pt+line.h_pt*1.2)*scale))
    crop=image[y0:y1,x0:x1]
    if not crop.size:return None
    binary=cv2.threshold(crop,170,255,cv2.THRESH_BINARY_INV)[1]
    contours,hierarchy=cv2.findContours(binary,cv2.RETR_TREE,cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:return None
    for i,c in enumerate(contours):
        x,y,w,h=cv2.boundingRect(c)
        if (hierarchy[0][i][2]>=0 and .65<w/max(h,1)<1.45
            and line.h_pt*scale*.45<h<line.h_pt*scale*1.35
            and cv2.contourArea(c)>.35*w*h):
            return [(x0+x)/scale,(y0+y)/scale,w/scale,h/scale]
    return None


def separator_above(page, line) -> bool:
    """Find a short thin horizontal rule; ignore page borders and printed text."""
    if not page.image_path:
        return False
    import cv2
    import numpy as np
    image = _gray(page.image_path,Path(page.image_path).stat().st_mtime_ns)
    if image is None:
        return False
    scale = image.shape[1] / page.width_pt
    top = max(0, int((line.y_top_pt-max(line.h_pt*4, page.height_pt*.04))*scale))
    bottom = max(top, int((line.y_top_pt-line.h_pt*.25)*scale))
    x0 = max(0, int((line.x_pt-page.width_pt*.04)*scale))
    x1 = min(image.shape[1], int((line.x_pt+page.width_pt*.65)*scale))
    roi = image[top:bottom, x0:x1]
    if not roi.size:
        return False
    binary = cv2.threshold(roi, 170, 255, cv2.THRESH_BINARY_INV)[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, int(page.width_pt*.09*scale)), 1))
    rules = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(rules, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (.09*page.width_pt*scale <= w <= .6*page.width_pt*scale
                and h <= max(4, 2.5*scale) and x < .12*page.width_pt*scale):
            return True
    return False


def detect(page) -> Plan:
    lines = page.lines
    body_sizes = [l.font_size for l in lines if .12 < l.y_top_pt/page.height_pt < .72 and len(l.text)>12]
    body_size = statistics.median(body_sizes) if body_sizes else 0
    # OCR often emits a tiny marker as a separate box to the LEFT of its
    # note. Associate by baseline and proximity, preserving both source lines.
    starts = []
    for i, line in enumerate(lines):
        if line.y_top_pt <= page.height_pt*.55:
            continue
        match = START.match(line.text)
        if not match:
            continue
        if len(match[2]) >= 3 and not match[2].lstrip().startswith((':','：','∶')):
            starts.append((i,match[1],[i],i))
        elif not match[2].strip():
            neighbors = [j for j,other in enumerate(lines) if j!=i and len(other.text.strip())>=3
                and other.x_pt >= line.x_pt+line.w_pt*.6
                and other.x_pt-(line.x_pt+line.w_pt)<page.width_pt*.06
                and abs((other.y_top_pt+other.h_pt/2)-(line.y_top_pt+line.h_pt/2))<max(other.h_pt,line.h_pt)*.65]
            if neighbors:
                j=min(neighbors,key=lambda j:lines[j].x_pt)
                starts.append((i,match[1],[i,j],j))
    # After finding a ruled note region, recover the LOCATION of unreadable
    # marker glyphs. Keep their marker unknown and require explicit review.
    unreadable = {}
    ruled_top = min((lines[i].y_top_pt for i,_,_,j in starts if separator_above(page,lines[j])),default=page.height_pt)
    initial_ids = {k for _,_,ids,_ in starts for k in ids}
    for i,line in enumerate(lines):
        if i in initial_ids or line.y_top_pt<ruled_top or len(line.text.strip())<3:
            continue
        bbox=gutter_marker(page,line)
        if bbox is not None:
            ids=[j for j,other in enumerate(lines) if len(other.text.strip())<=2
                and other.x_pt>=bbox[0]-3 and other.x_pt<=bbox[0]+bbox[2]
                and abs(other.y_top_pt-bbox[1])<line.h_pt*.7]
            starts.append((i,'?',ids+[i],i));unreadable[i]=bbox
    candidate_ids = {k for _,_,ids,_ in starts for k in ids}
    raw_refs = [(i,a,b,m) for i,l in enumerate(lines) if i not in candidate_ids
                for a,b,m in reference_offsets(l.text)]
    notes = []
    ruled_regions = []
    for i,marker,initial,content_index in sorted(starts,key=lambda t:lines[t[0]].y_top_pt):
        line,content = lines[i],lines[content_index]
        small = bool(body_size and line.font_size < body_size*.85)
        paired = any(m==marker and lines[j].y_top_pt < line.y_top_pt for j,_,_,m in raw_refs)
        above = [l.y_top_pt+l.h_pt for j,l in enumerate(lines) if j not in initial
            and l.y_top_pt+l.h_pt <= min(line.y_top_pt,content.y_top_pt)]
        gap = min(line.y_top_pt,content.y_top_pt)-max(above) if above else 0
        own_rule = separator_above(page,content)
        inherited_rule = any(y<line.y_top_pt and abs(x-line.x_pt)<page.width_pt*.05
            for y,x in ruled_regions)
        separated = own_rule or inherited_rule
        if own_rule:
            ruled_regions.append((line.y_top_pt,line.x_pt))
        strong = marker!='?' and ((small and separated) or (paired and separated) or (paired and small and gap > line.h_pt*.8))
        if not (paired or small or separated):
            continue
        indices = list(initial)
        next_start = min((min(lines[k].y_top_pt for k in ids) for a,_,ids,_ in starts
            if lines[a].y_top_pt>line.y_top_pt+line.h_pt*.5),default=page.height_pt)
        for j in sorted(range(len(lines)),key=lambda j:(lines[j].y_top_pt,lines[j].x_pt)):
            other = lines[j]
            if j in candidate_ids or other.y_top_pt<=content.y_top_pt or other.y_top_pt>=next_start:
                continue
            previous = lines[indices[-1]]
            if (other.y_top_pt-previous.y_top_pt>previous.h_pt*2.3
                    or abs(other.x_pt-content.x_pt)>page.width_pt*.06
                    or other.font_size>content.font_size*1.25 or len(other.text.strip())<3):
                continue
            indices.append(j)
        notes.append(Note(marker,indices,.96 if strong else .55,
            'auto_linkable' if strong else 'needs_review',
            {'small_type':small,'body_font_size':body_size,'separator':separated,
             'separator_inherited':inherited_rule and not own_rule,'split_marker':len(initial)>1,
             'body_reference':paired,'gap_pt':round(gap,2),'unreadable_marker_bbox':unreadable.get(i),'version':VERSION}))
    counts = Counter(n.marker for n in notes)
    for note in notes:
        if counts[note.marker]>1:
            note.status='needs_review'
    groups = {i for n in notes for i in n.indices}
    references = []
    for i, line in enumerate(lines):
        if i in groups:
            continue
        for a, b, marker in reference_offsets(line.text):
            targets = [n for n,note in enumerate(notes) if note.marker==marker]
            cross_page = bool(re.search(r'第[0-9一二三四五六七八九十百]+页注?\s*$',line.text[max(0,a-20):a]))
            target = targets[0] if not cross_page and len(targets)==1 and notes[targets[0]].status=='auto_linkable' else None
            references.append(Reference(i,a,b,marker,target,'linked' if target is not None else 'needs_review',
                'cross_page_citation' if cross_page else 'same_page'))
    return Plan(notes,references)


def persist(session, canvas, page, plan, line_map):
    from src.db.models import Footnote, FootnoteReference, ReviewTask
    rows = []
    for note in plan.notes:
        source_lines = [line_map[i] for i in note.indices]
        row = Footnote(canvas_id=canvas.id, marker=note.marker,
            content_text='\n'.join(l.content_text for l in source_lines),
            source_line_ids=[l.id for l in source_lines],
            status=note.status, confidence=note.confidence, evidence=note.evidence)
        session.add(row);session.flush();rows.append(row)
        for l in source_lines:
            block = l.block
            block.block_type = 'footnote' if note.status=='auto_linkable' else 'footnote_candidate'
            block.style_meta={**block.style_meta,'footnote_id':row.id,'evidence':note.evidence}
        if note.status=='needs_review' or not any(r.note_index==len(rows)-1 for r in plan.references):
            session.add(ReviewTask(manifest_id=canvas.manifest_id,canvas_id=canvas.id,
                target_type='footnote',target_id=row.id,risk_type='footnote_ambiguous',priority_score=.8,
                description='脚注证据不足、编号重复或没有正文引用；请核对原图。'))
    for ref in plan.references:
        line = line_map[ref.index]
        row = FootnoteReference(canvas_id=canvas.id,line_id=line.id,marker=ref.marker,
            char_start=ref.start,char_end=ref.end,footnote_id=rows[ref.note_index].id if ref.note_index is not None else None,
            status=ref.status,source_revision=canvas.revision_no,
            context_text=line.content_text,evidence={'version':VERSION,'reference_kind':ref.kind,'same_page':ref.kind=='same_page','code_protected':True})
        session.add(row);session.flush()
        if ref.status!='linked':
            session.add(ReviewTask(manifest_id=canvas.manifest_id,canvas_id=canvas.id,
                target_type='footnote_reference',target_id=row.id,risk_type='footnote_unresolved',priority_score=.75,
                description='正文带圈标记没有唯一可靠的同页脚注；保留候选，不猜测跨页关联。'))
    return len(rows),len(plan.references)


def page_bundle(session, canvas_id):
    from sqlalchemy import select
    from src.db.models import Footnote, FootnoteReference, AltoLine
    notes = list(session.scalars(select(Footnote).where(Footnote.canvas_id==canvas_id)))
    refs = list(session.scalars(select(FootnoteReference).where(FootnoteReference.canvas_id==canvas_id)))
    lines = {l.id:l for l in session.scalars(select(AltoLine).where(AltoLine.canvas_id==canvas_id))}
    return {
        "footnotes":[{"id":n.id,"marker":n.marker,"text":n.content_text,"status":n.status,
            "confidence":n.confidence,"source_line_ids":n.source_line_ids,"evidence":n.evidence,
            "targets":[{"line_id":i,"xywh":lines[i].xywh} for i in n.source_line_ids if i in lines]}
            for n in notes],
        "footnote_references":[{"id":r.id,"line_id":r.line_id,"marker":r.marker,
            "start":r.char_start,"end":r.char_end,"footnote_id":r.footnote_id,
            "status":r.status,"source_revision":r.source_revision} for r in refs]}


def invalidate_after_correction(session, canvas):
    """Never expose old offsets/links after an approved source-text change."""
    from sqlalchemy import select
    from src.db.models import Footnote, FootnoteReference, AltoLine, ReviewTask
    refs = list(session.scalars(select(FootnoteReference).where(FootnoteReference.canvas_id==canvas.id)))
    notes = list(session.scalars(select(Footnote).where(Footnote.canvas_id==canvas.id)))
    if not refs and not notes:
        return
    for ref in refs:
        ref.status='stale';ref.footnote_id=None
    for note in notes:
        note.status='needs_review'
        note.evidence={**note.evidence,'text_changed':True}
        note.content_text='\n'.join(session.get(AltoLine,i).content_text for i in note.source_line_ids)
    session.add(ReviewTask(manifest_id=canvas.manifest_id,canvas_id=canvas.id,
        target_type='footnote_page',target_id=canvas.id,risk_type='footnote_text_changed',priority_score=.9,
        description='文字修订已使本页旧脚注关联失效；请重新核对标记和原文位置。'))


def recompute_page(session,canvas,exclude_task_id=None):
    """Rebuild stale candidates from active text; raw OCR and audit trail survive."""
    from sqlalchemy import select,delete
    from src.db.models import Footnote,FootnoteReference,AltoLine,AltoBlock,ReviewTask
    from src.ingest.digital_pdf_ingestor import ExtractedLine,ExtractedPageData
    source=list(session.scalars(select(AltoLine).join(AltoBlock,AltoBlock.id==AltoLine.block_id)
        .where(AltoLine.canvas_id==canvas.id).order_by(AltoBlock.reading_order,AltoLine.line_index)))
    page=ExtractedPageData(canvas.scan_index,canvas.pdf_page_index,canvas.source_page_label or '',
        canvas.width/2,canvas.height/2,0,
        [ExtractedLine(l.content_text,l.x/2,l.y/2,l.w/2,l.h/2,l.h*.375,l.line_confidence) for l in source],
        image_path=canvas.image_storage_path)
    snapshot=page_bundle(session,canvas.id)
    session.execute(delete(FootnoteReference).where(FootnoteReference.canvas_id==canvas.id))
    session.execute(delete(Footnote).where(Footnote.canvas_id==canvas.id))
    for l in source:
        if l.block.block_type in {'footnote','footnote_candidate'}:
            l.block.block_type='paragraph';l.block.style_meta={}
    for t in session.scalars(select(ReviewTask).where(ReviewTask.canvas_id==canvas.id,
            ReviewTask.target_type.in_(['footnote','footnote_reference','footnote_page']),ReviewTask.status=='pending')):
        if t.id!=exclude_task_id:t.status='superseded'
    counts=persist(session,canvas,page,detect(page),dict(enumerate(source)))
    return {'previous':snapshot,'notes':counts[0],'references':counts[1]}
