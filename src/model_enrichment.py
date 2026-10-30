"""Optional bounded model stage for existing or newly ingested documents."""
from pathlib import Path
from sqlalchemy import select
from src.db.session import get_session
from src.db.models import (ArchEntity, EntityMention, AltoBlock, Canvas, Manifest,
                           ProvenanceAnchor, ReviewTask, AltoLine, AltoPage)
from src.model_router import ModelRouter
from src.settings import load_settings
from src.ingest.page_pipeline import atomic_json


def enrich_with_models(engine, manifest_id: str, run_dir: Path, base_url: str, settings_path: Path | None = None):
    settings=load_settings(settings_path)
    router=ModelRouter.from_settings(run_dir/"model_cache",settings)
    if router.local_model is None and not (router.allow_cloud and router.key):
        result={"status":"unavailable","reason":"No installed local language model or enabled cloud credential"}
        atomic_json(run_dir/"model_stage.json",result)
        return result
    budget=int(settings.get("MAX_LOCAL_BLOCKS_PER_DOCUMENT","30"))
    vision_result=_vision_fallback(engine,manifest_id,run_dir,router) if router.allow_cloud and router.key else {"status":"disabled"}
    with get_session(engine) as session:
        # Restrict costly language reasoning to blocks with domain entities or
        # ambiguous OCR; deterministic extraction already covers the other blocks.
        candidates=list(session.scalars(select(AltoBlock).join(Canvas).where(Canvas.manifest_id==manifest_id,
            AltoBlock.page_zone=="print_space",AltoBlock.block_type.in_(["paragraph","heading"])).order_by(Canvas.scan_index,AltoBlock.reading_order)))
        queue=[(b.id,b.resolve_display_text() or "") for b in candidates if 10<=len(b.resolve_display_text() or "")<=1000
               and any(word in (b.resolve_display_text() or "") for word in ["遗址","考古","地层","时期","窑址","编著","出土"])]
    result={"status":"running","candidates":len(queue),"processed":0,"entities_added":0,"errors":[],"providers":{},"vision":vision_result}
    atomic_json(run_dir/"model_stage.json",result)
    for block_id,source in queue[:budget]:
        try:
            answer=router.extract_entities(source)
            with get_session(engine) as session:
                block=session.get(AltoBlock,block_id)
                if block.resolve_display_text()!=source:
                    raise ValueError("Source was reviewed while model extraction was running")
                canvas=session.get(Canvas,block.canvas_id)
                manifest=session.get(Manifest,manifest_id)
                for item in answer["result"]["entities"]:
                    entity=session.scalar(select(ArchEntity).where(ArchEntity.work_id==manifest.work_id,
                        ArchEntity.entity_type==item["type"],ArchEntity.canonical_name==item["text"]))
                    if entity is None:
                        entity=ArchEntity(work_id=manifest.work_id,entity_type=item["type"],canonical_name=item["text"],
                            metadata_json={"method":answer["provider"],"model":answer["model"]})
                        session.add(entity);session.flush()
                    exists=session.scalar(select(EntityMention).where(EntityMention.entity_id==entity.id,
                        EntityMention.block_id==block_id,EntityMention.char_start==item["start"],EntityMention.line_id.is_(None)))
                    if exists:continue
                    mention=EntityMention(entity_id=entity.id,canvas_id=canvas.id,block_id=block_id,
                        surface_text=item["text"],char_start=item["start"],char_end=item["end"],confidence=0.5)
                    session.add(mention);session.flush()
                    session.add(ProvenanceAnchor(source_entity_type="entity_mention",source_entity_id=str(mention.id),
                        manifest_id=manifest_id,canvas_id=canvas.id,printed_page=canvas.printed_page_label,
                        verbatim_quote=source,highlight_boxes=[{"xywh":block.xywh,"char_start":item["start"],"char_end":item["end"]}],
                        iiif_target_uri=f"{base_url}/manifest/{manifest_id}/canvas/{canvas.id}#xywh={block.xywh}"))
                    result["entities_added"]+=1
                block.provenance_meta={**block.provenance_meta,"entity_model":answer["model"],"entity_provider":answer["provider"]}
                if answer["result"]["entities"] and session.scalar(select(ReviewTask).where(
                        ReviewTask.target_id==block_id,ReviewTask.risk_type=="model_entity_candidate")) is None:
                    session.add(ReviewTask(manifest_id=manifest_id,canvas_id=canvas.id,target_type="entity",
                        target_id=block_id,risk_type="model_entity_candidate",priority_score=0.4,
                        description="模型实体已通过逐字出处校验，但类型与完整性仍需抽查。"))
            result["processed"]+=1
            provider=answer["provider"];result["providers"][provider]=result["providers"].get(provider,0)+1
        except Exception as error:
            result["errors"].append({"block_id":block_id,"type":type(error).__name__})
            with get_session(engine) as session:
                block=session.get(AltoBlock,block_id)
                existing=session.scalar(select(ReviewTask).where(ReviewTask.target_id==block_id,ReviewTask.risk_type=="model_validation_failed"))
                if existing is None:
                    session.add(ReviewTask(manifest_id=manifest_id,canvas_id=block.canvas_id,target_type="entity",
                        target_id=block_id,risk_type="model_validation_failed",priority_score=0.5,
                        description="模型未能返回通过原文证据校验的实体，保留规则结果，需审校。"))
        atomic_json(run_dir/"model_stage.json",result)
    result["remaining_candidates"]=max(0,len(queue)-budget)
    result["status"]="completed_with_review" if result["errors"] or result["remaining_candidates"] else "completed"
    atomic_json(run_dir/"model_stage.json",result)
    return result


def _vision_fallback(engine,manifest_id: str,run_dir: Path,router) -> dict:
    """Inspect difficult OCR lines, preserving the original and requiring review."""
    with get_session(engine) as session:
        rows=session.execute(select(AltoLine,Canvas).join(Canvas,AltoLine.canvas_id==Canvas.id)
            .where(Canvas.manifest_id==manifest_id,AltoLine.line_confidence<0.8)
            .order_by(AltoLine.line_confidence).limit(5)).all()
        targets=[{"line_id":line.id,"canvas_id":canvas.id,"image":canvas.image_storage_path,
            "width":canvas.width,"height":canvas.height,"box":[line.x,line.y,line.w,line.h],
            "raw":line.raw_ocr} for line,canvas in rows if canvas.image_storage_path]
    result={"status":"completed","candidates":len(targets),"processed":0,"errors":[]}
    for target in targets:
        try:
            from PIL import Image
            with Image.open(target["image"]) as image:
                sx,sy=image.width/target["width"],image.height/target["height"]
                x,y,w,h=target["box"]
                rect=(max(0,int((x-8)*sx)),max(0,int((y-5)*sy)),min(image.width,int((x+w+8)*sx)),min(image.height,int((y+h+5)*sy)))
                path=run_dir/"model_cache"/("ocr-"+target["line_id"]+".jpg")
                image.crop(rect).convert("RGB").save(path)
            answer=router.inspect_image(path,"逐字抄录图中考古文献文字，尤其保留数字、圈号、单位。"
                "返回字段transcription（字符串或null）和uncertain（难辨位置说明的字符串数组），不猜测补字")
            with get_session(engine) as session:
                line=session.get(AltoLine,target["line_id"])
                block=session.get(AltoBlock,line.block_id)
                block.provenance_meta={**block.provenance_meta,"ocr_api_candidate":answer}
                existing=session.scalar(select(ReviewTask).where(ReviewTask.target_id==line.id,ReviewTask.risk_type=="api_ocr_candidate"))
                if existing is None:
                    session.add(ReviewTask(manifest_id=manifest_id,canvas_id=target["canvas_id"],target_type="ocr",
                        target_id=line.id,risk_type="api_ocr_candidate",priority_score=0.8,
                        description="本地识别置信度较低，API 给出的未审候选："+str(answer["result"]["transcription"])))
            result["processed"]+=1
        except Exception as error:
            result["errors"].append({"line_id":target["line_id"],"type":type(error).__name__})
    if result["errors"]:result["status"]="completed_with_review"
    return result
