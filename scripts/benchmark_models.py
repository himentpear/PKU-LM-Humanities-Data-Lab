"""Time local model candidates and the configured API on real source excerpts."""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.model_router import ModelRouter
from src.settings import load_settings
from src.ingest.page_pipeline import atomic_json


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    root=Path("data/processed/benchmarks/model-comparison")/str(time.time_ns())
    root.mkdir(parents=True,exist_ok=True)
    source_dir=Path("data/processed/library/618d4b7d24cf24d4-40cf08553706/pages")
    page61=json.loads((source_dir/"00061.json").read_text(encoding="utf-8"))["page"]
    page137=json.loads((source_dir/"00137.json").read_text(encoding="utf-8"))["page"]
    p61="\n".join(l["text"] for l in page61["lines"])
    p137="\n".join(l["text"] for l in page137["lines"])
    sources=[p61[40:200],p61[300:650],p137[40:500]]
    settings=load_settings();rows=[]
    models=[("local-0.6B",Path("data/processed/models/Qwen3-0.6B-Q8_0.gguf")),
            ("local-4B",Path("data/processed/models/Qwen3-4B-Q4_K_M.gguf")),("api",None)]
    for name,model in models:
        if model and not model.exists():continue
        router=ModelRouter(root/name,local_model=model,allow_cloud=False,threads=4) if model else ModelRouter.from_settings(root/name,settings)
        for index,source in enumerate(sources):
            started=time.perf_counter()
            row={"provider_test":name,"sample":index+1,"source_characters":len(source),"source":source}
            try:
                answer=router.extract_entities(source,force_cloud=model is None)
                row.update(status="source_validation_passed",model=answer["model"],entities=answer["result"]["entities"])
            except Exception as error:
                row.update(status="failed",error_type=type(error).__name__,local_error_type=getattr(router,"last_local_error_type",None))
            row["seconds"]=round(time.perf_counter()-started,3)
            rows.append(row)
            atomic_json(Path("reports/model_comparison.json"),{"status":"running","rows":rows})
            print(json.dumps({k:v for k,v in row.items() if k not in {"source","entities"}},ensure_ascii=False),flush=True)
        # Explicitly release model weights between candidates.
        if router.llm is not None:router.llm.close();router.llm=None
    summary=[]
    for name in {r["provider_test"] for r in rows}:
        group=[r for r in rows if r["provider_test"]==name]
        summary.append({"provider_test":name,"samples":len(group),"source_validation_passes":sum(r["status"]=="source_validation_passed" for r in group),
            "mean_seconds":round(statistics.mean(r["seconds"] for r in group),3),"max_seconds":max(r["seconds"] for r in group)})
    result={"status":"completed","summary":summary,"rows":rows,"limitations":"Three source excerpts measure latency and source-span validation, not independently annotated semantic accuracy."}
    atomic_json(Path("reports/model_comparison.json"),result)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=="__main__":main()
