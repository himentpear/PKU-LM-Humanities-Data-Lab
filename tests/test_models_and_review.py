import json
from pathlib import Path
import threading

import httpx
import pytest
from sqlalchemy import select

from src.model_router import ModelRouter,ground_entities,ground_surfaces
from src.pipeline import run_document
from src.server import make_server
from src.db.models import Canvas,AltoLine,ReviewTask,Revision,RagChunk
from src.db.session import init_db,get_session

PDF=Path(__file__).resolve().parents[1]/"docs/考古报告/1第一卷-已复.pdf"


def test_verbatim_grounding_rejects_fabrication_and_ambiguity():
    value={"entities":[{"type":"PERIOD","text":"宋代","start":0,"end":2}]}
    assert ground_entities(value,"出土宋代瓷器")["entities"][0]["start"]==2
    with pytest.raises(ValueError):ground_entities(value,"出土明代瓷器")
    with pytest.raises(ValueError):ground_entities({"entities":[{"type":"PERIOD","text":"宋代","start":0,"end":2}]},"出土宋代瓷器及宋代陶器")


def test_model_surfaces_get_deterministic_offsets_for_repeated_mentions():
    result=ground_surfaces({"entities":[{"type":"PERIOD","text":"宋代"},{"type":"PERIOD","text":"宋代"}]},"宋代瓷器与宋代陶器")
    assert [(e["start"],e["end"]) for e in result["entities"]]==[(0,2),(5,7)]
    with pytest.raises(ValueError):ground_surfaces({"entities":[{"type":"PERIOD","text":"唐代"}]},"宋代瓷器")
    wrapped=ground_surfaces({"type":"json_object","entities":[{"type":"PLACE","text":"景德镇"}]},"景德\n镇")
    assert wrapped["entities"]==[{"type":"PLACE","text":"景德\n镇","start":0,"end":4}]


def test_cloud_budget_includes_retries_and_cache(monkeypatch,tmp_path):
    router=ModelRouter(tmp_path,allow_cloud=True,max_calls=1,max_total_tokens=10000)
    router.key="test-only-fake-key"
    def fail(*args,**kwargs):return httpx.Response(429)
    monkeypatch.setattr(httpx,"post",fail)
    monkeypatch.setattr("src.model_router.time.sleep",lambda n:None)
    with pytest.raises(RuntimeError,match="budget"):router.extract_entities("出土宋代瓷器",force_cloud=True)
    usage=json.loads((tmp_path/"usage.json").read_text())
    assert usage["requests"]==1
    assert "test-only-fake-key" not in (tmp_path/"usage.json").read_text()


def test_http_images_search_and_review_consistency(tmp_path):
    result=run_document(PDF,tmp_path,settings_path=tmp_path/"no-model.env",max_pages=5)
    mid=result["stats"]["manifest_id"]
    engine=init_db("sqlite:///"+(tmp_path/"library.db").as_posix())
    with get_session(engine) as session:
        canvas=session.scalar(select(Canvas).where(Canvas.manifest_id==mid,Canvas.scan_index==5))
        line=session.scalar(select(AltoLine).where(AltoLine.canvas_id==canvas.id))
        task=ReviewTask(manifest_id=mid,canvas_id=canvas.id,target_type="ocr",target_id=line.id,
                        risk_type="manual_test",description="测试审校一致性")
        session.add(task);session.flush()
        task_id,line_id,canvas_id,revision,raw=task.id,line.id,canvas.id,canvas.revision_no,line.raw_ocr
    server=make_server(tmp_path,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=base,timeout=30) as client:
            assert client.get("/health").json()["status"]=="ok"
            image=client.get(f"/iiif/image/{mid}/5/full/max/0/default.jpg")
            assert image.status_code==200 and image.headers["content-type"]=="image/jpeg"
            assert client.get(f"/iiif/image/{mid}/5/0,0,50,50/max/0/default.jpg").status_code==200
            response=client.post("/api/review",json={"task_id":task_id,"actor":"测试审校员","note":"对照原页进行一致性测试，当前修改仅用于临时测试数据库",
                "status":"approved","expected_revision":revision,"line_id":line_id,"text":"目录校对一致性标记"})
            assert response.status_code==200,response.text
            hits=client.get("/api/search",params={"manifest":mid,"q":"一致性标记"}).json()
            assert hits["total"]==1
            stale=client.post("/api/review",json={"task_id":task_id,"actor":"测试员","note":"过期版本测试",
                "status":"approved","expected_revision":revision})
            assert stale.status_code==409
        with get_session(engine) as session:
            updated=session.get(AltoLine,line_id)
            assert updated.raw_ocr==raw
            assert updated.approved_text=="目录校对一致性标记"
            assert list(session.scalars(select(Revision).where(Revision.entity_type=="review_task")))
        export=(tmp_path/result["run_id"]/"bundles/00005.json").read_text(encoding="utf-8")
        assert "目录校对一致性标记" in export
    finally:
        server.shutdown();thread.join();server.server_close();server.engine.dispose();engine.dispose()
