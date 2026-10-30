"""Evidence-backed image, entity, table and retrieval enrichment."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import select
from src.db.models import (AltoBlock, AltoLine, ArchEntity, Canvas, EntityMention,
    ImageAsset, ImageOccurrence, Manifest, PlateFigure, ProvenanceAnchor, RagChunk, ReviewTask)
from src.alto.parser import AltoIngestor
from src.ingest.page_pipeline import atomic_json, file_hash


def graphic_regions(image_path: str, lines, width_pt: float) -> list[list[float]]:
    """Detect connected visible regions after masking text; never fabricate a crop."""
    import cv2
    import numpy as np
    image=cv2.imdecode(np.fromfile(image_path,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    scale=image.shape[1]/width_pt
    mask=cv2.threshold(image,210,255,cv2.THRESH_BINARY_INV)[1]
    for line in lines:
        x,y,w,h=[int(v*scale) for v in (line.x_pt,line.y_top_pt,line.w_pt,line.h_pt)]
        cv2.rectangle(mask,(max(0,x-2),max(0,y-2)),(x+w+2,y+h+2),0,-1)
    # Ignore thin page borders and scan edge shadows.
    border=max(2,int(mask.shape[1]*0.025))
    mask[:border,:]=mask[-border:,:]=0
    mask[:,:border]=mask[:,-border:]=0
    dilated=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_RECT,(max(3,int(8*scale)),max(3,int(6*scale)))))
    contours,_=cv2.findContours(dilated,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    regions=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if w>25*scale and h>25*scale and w*h>mask.size*0.002 and w*h<mask.size*0.65:
            regions.append([x/scale,y/scale,w/scale,h/scale])
    return regions


def extract_tables(pdf_page, page, run_dir: Path) -> list[dict]:
    """Native ruled/borderless tables plus scanned ruled-cell recovery."""
    tables=[]
    if page.method == "pymupdf":
        for table in pdf_page.find_tables().tables:
            tables.append({"method":"pymupdf-table","bbox_pt":list(table.bbox),
                           "rows":table.extract(),"needs_review":True})
    else:
        import cv2
        import numpy as np
        gray=cv2.imdecode(np.fromfile(page.image_path,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
        scale=gray.shape[1]/page.width_pt
        binary=cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_MEAN_C,cv2.THRESH_BINARY_INV,25,15)
        horizontal=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(25,gray.shape[1]//20),1)))
        vertical=cv2.morphologyEx(binary,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(25,gray.shape[0]//25))))
        grid=cv2.add(horizontal,vertical)
        contours,hierarchy=cv2.findContours(grid,cv2.RETR_TREE,cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is not None:
            cells=[]
            for i,contour in enumerate(contours):
                x,y,w,h=cv2.boundingRect(contour)
                if hierarchy[0][i][3]>=0 and w>15*scale and h>8*scale and w*h<gray.size*0.25:
                    rect=[x/scale,y/scale,w/scale,h/scale]
                    contents=[l.text for l in page.lines if x/scale<=l.x_pt+l.w_pt/2<=(x+w)/scale and y/scale<=l.y_top_pt+l.h_pt/2<=(y+h)/scale]
                    cells.append({"bbox_pt":rect,"text":"\n".join(contents)})
            if len(cells)>=4:
                tables.append({"method":"opencv-ruled-cells","cells":sorted(cells,key=lambda c:(round(c["bbox_pt"][1]/5),c["bbox_pt"][0])),"needs_review":True})
    if tables:
        atomic_json(run_dir/"tables"/f"{page.scan_index:05d}.json",{"scan_index":page.scan_index,"tables":tables})
    return tables


VOCAB={"PERIOD":["新石器时代","旧石器时代","宋代","元代","明代","清代","唐代","汉代"],
       "MATERIAL":["青铜","白瓷","青瓷","陶器","瓷器","石器","骨器"],
       "ARTIFACT_TYPE":["高足杯","瓷碗","瓷盘","陶罐"]}


def enrich_document(session, manifest_id: str, pages, run_dir: Path, base_url: str) -> dict:
    import pymupdf
    manifest=session.get(Manifest,manifest_id)
    canvases=list(session.scalars(select(Canvas).where(Canvas.manifest_id==manifest_id).order_by(Canvas.scan_index)))
    entities={}; entity_count=0; image_count=0; table_count=0; chunk_count=0
    with pymupdf.open(manifest.source_pdf_path) as document:
        for canvas,page in zip(canvases,pages):
            tables=extract_tables(document[page.pdf_page_index],page,run_dir)
            table_count+=len(tables)
            if tables:
                session.add(ReviewTask(manifest_id=manifest_id,canvas_id=canvas.id,target_type="table",
                    target_id=canvas.id,risk_type="table_structure_review",priority_score=0.6,
                    description="已提取表格候选，请核对行列、合并单元格和单位；表格不是已审定事实。"))
            figures=list(session.scalars(select(PlateFigure).where(PlateFigure.canvas_id==canvas.id)))
            regions=page.graphics
            if figures and not regions:
                regions=graphic_regions(page.image_path,page.lines,page.width_pt)
            from PIL import Image
            image=None
            for figure in figures:
                caption=session.get(AltoBlock,figure.caption_block_id)
                cap_y=caption.y/2
                candidates=[r for r in regions if r[1]+r[3]<=cap_y+8 and cap_y-(r[1]+r[3])<page.height_pt*0.65]
                if not candidates:
                    session.add(ReviewTask(manifest_id=manifest_id,canvas_id=canvas.id,target_type="figure",
                        target_id=figure.id,risk_type="figure_crop_unresolved",priority_score=0.7,
                        description="未能可靠定位图像边界；当前框仅为图题，需审校。"))
                    figure.crop_x,figure.crop_y,figure.crop_w,figure.crop_h=caption.x,caption.y,caption.w,caption.h
                    continue
                # Cluster the nearest row and contiguous rows above it. Multi-panel
                # plates can have several disconnected images under one caption.
                bottom=max(r[1]+r[3] for r in candidates)
                selected=[r for r in candidates if bottom-(r[1]+r[3])<page.height_pt*0.35]
                x0=min(r[0] for r in selected);y0=min(r[1] for r in selected)
                x1=max(r[0]+r[2] for r in selected);y1=max(r[1]+r[3] for r in selected)
                figure.crop_x=max(0,round(x0*2));figure.crop_y=max(0,round(y0*2))
                figure.crop_w=min(canvas.width-figure.crop_x,round((x1-x0)*2))
                figure.crop_h=min(canvas.height-figure.crop_y,round((y1-y0)*2))
                if image is None:
                    image=Image.open(page.image_path)
                scale=image.width/page.width_pt
                crop_path=run_dir/"figures"/f"{figure.id}.jpg";crop_path.parent.mkdir(exist_ok=True)
                image.crop((int(x0*scale),int(y0*scale),int(x1*scale),int(y1*scale))).save(crop_path)
                asset=ImageAsset(work_id=manifest.work_id,sha256=file_hash(crop_path),uri=str(crop_path.resolve()),
                    width=int((x1-x0)*scale),height=int((y1-y0)*scale),source_type="cropped_plate")
                session.add(asset);session.flush()
                session.add(ImageOccurrence(image_id=asset.id,canvas_id=canvas.id,plate_figure_id=figure.id,
                    transform_json={"method":"visible_region_candidate","bbox_canvas":figure.crop_xywh},match_score=0.5))
                session.add(ReviewTask(manifest_id=manifest_id,canvas_id=canvas.id,target_type="figure",target_id=figure.id,
                    risk_type="figure_crop_candidate",priority_score=0.4,description="自动区域检测生成图版裁切候选，需核对是否包含全部子图。"))
                image_count+=1
            if image is not None:
                image.close()
            blocks=list(session.scalars(select(AltoBlock).where(AltoBlock.canvas_id==canvas.id).order_by(AltoBlock.reading_order)))
            for block in blocks:
                text=block.resolve_display_text() or ""
                if block.page_zone != "print_space" or not text or block.block_type=="footnote_candidate":
                    continue
                chunk=RagChunk(manifest_id=manifest_id,chunk_index=chunk_count,breadcrumb_path=canvas.page_domain+("/footnote" if block.block_type=="footnote" else ""),
                    content_text=text,token_count=len(text))
                session.add(chunk);session.flush();chunk_count+=1
                target=f"{base_url}/manifest/{manifest_id}/canvas/{canvas.id}#xywh={block.xywh}"
                session.add(ProvenanceAnchor(source_entity_type="rag_chunk",source_entity_id=chunk.id,
                    manifest_id=manifest_id,canvas_id=canvas.id,printed_page=canvas.printed_page_label,
                    verbatim_quote=text,highlight_boxes=[{"xywh":block.xywh}],iiif_target_uri=target))
                if block.block_type=="footnote":
                    continue
                # Exact spans, not paraphrased names: deterministic baseline before LLM.
                for line in session.scalars(select(AltoLine).where(AltoLine.block_id==block.id)):
                    spans=AltoIngestor.extract_protected_spans(line.content_text)
                    for kind,words in VOCAB.items():
                        for word in words:
                            spans.extend({"type":kind,"text":word,"start":m.start(),"end":m.end()}
                                         for m in re.finditer(re.escape(word),line.content_text))
                    for span in spans:
                        kind,name=span["type"],span["text"]
                        key=(kind,name)
                        if key not in entities:
                            entity=ArchEntity(work_id=manifest.work_id,entity_type=kind,canonical_name=name,
                                metadata_json={"method":"exact_span_rule"})
                            session.add(entity);session.flush();entities[key]=entity
                        session.add(EntityMention(entity_id=entities[key].id,canvas_id=canvas.id,block_id=block.id,
                            line_id=line.id,surface_text=name,char_start=span["start"],char_end=span["end"],confidence=1.0))
                        entity_count+=1
    return {"image_assets_count":image_count,"table_candidates_count":table_count,
            "entity_mentions_count":entity_count,"rag_chunks_count":chunk_count}
