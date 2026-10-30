"""Stratified cold-path samples and transparent full-corpus runtime estimates."""
import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.ingest.page_pipeline import PageExtractor,atomic_json,file_hash
from src.ingest.digital_pdf_ingestor import DigitalPdfIngestor
from src.db.session import init_db,get_session
from src.enrichment import enrich_document
from src.pipeline import _export


def main():
    import pymupdf
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--pdf-dir",type=Path,default=Path("docs/考古报告"))
    parser.add_argument("--output",type=Path,default=Path("data/processed/benchmarks/stratified-sample"))
    parser.add_argument("--strata",type=int,default=6)
    args=parser.parse_args()
    # A fresh sample directory bypasses extraction cache. Existing full-corpus
    # artifacts remain untouched; OS filesystem caches may still be warm.
    run=args.output/str(time.time_ns());run.mkdir(parents=True,exist_ok=True)
    rows=[];started=time.time()
    for pdf in sorted(args.pdf_dir.glob("*.pdf")):
        sha=file_hash(pdf);folder=run/sha[:16]
        extractor=PageExtractor(folder,dpi=180,threads=4)
        samples=[];prepared=[]
        with pymupdf.open(pdf) as document:
            total=len(document)
            selection=[(0,1)]
            for group in range(args.strata):
                start=1+(total-1)*group//args.strata
                end=1+(total-1)*(group+1)//args.strata
                if end>start:selection.append(((start+end-1)//2,end-start))
            for index,weight in selection:
                t=time.perf_counter()
                page=extractor.extract(document,index,sha)
                seconds=time.perf_counter()-t
                low=sum(l.confidence is not None and l.confidence<0.8 for l in page.lines)
                geometry_errors=sum(l.x_pt<0 or l.y_top_pt<0 or l.w_pt<=0 or l.h_pt<=0 or
                    l.x_pt+l.w_pt>page.width_pt+1 or l.y_top_pt+l.h_pt>page.height_pt+1 for l in page.lines)
                sample={"physical_page":index+1,"stratum_weight_pages":weight,"method":page.method,
                    "characters":sum(len(l.text) for l in page.lines),"lines":len(page.lines),
                    "low_confidence_lines":low,"geometry_errors":geometry_errors,
                    "seconds":round(seconds,4),"quality_issues":page.quality_issues}
                samples.append(sample)
                prepared.append(replace(page,scan_index=len(prepared)+1))
                print(json.dumps({"source":pdf.name,**sample},ensure_ascii=False),flush=True)
        engine=init_db("sqlite:///"+(folder/"sample.db").resolve().as_posix())
        t=time.perf_counter()
        with get_session(engine) as session:
            stats=DigitalPdfIngestor(session).ingest_pdf(pdf,sha[:16],pdf.stem,max_pages=len(prepared),
                prepared_pages=prepared,source_sha256=sha)
            stats.update(enrich_document(session,stats["manifest_id"],prepared,folder,"http://127.0.0.1:8000/iiif"))
        indexing=time.perf_counter()-t
        t=time.perf_counter()
        with get_session(engine) as session:_export(session,stats["manifest_id"],folder,"http://127.0.0.1:8000/iiif")
        exporting=time.perf_counter()-t;engine.dispose()
        row={"source":pdf.name,"total_pages":total,"sample_pages":len(samples),"samples":samples,
            "sample_indexing_seconds":round(indexing,3),"sample_export_seconds":round(exporting,3),
            "estimated_extraction_seconds":round(sum(s["seconds"]*s["stratum_weight_pages"] for s in samples),2),
            "estimated_indexing_seconds":round(indexing*total/len(samples),2),
            "estimated_export_seconds":round(exporting*total/len(samples),2),"sample_stats":stats}
        rows.append(row)
        atomic_json(Path("reports/stratified_sample_timing.json"),{"status":"running","sample_run":str(run.resolve()),"documents":rows})
    base=sum(r["estimated_extraction_seconds"]+r["estimated_indexing_seconds"]+r["estimated_export_seconds"] for r in rows)
    result={"status":"completed","sample_run":str(run.resolve()),"dpi":180,"ocr_threads":4,
        "total_pages":sum(r["total_pages"] for r in rows),"sample_pages":sum(r["sample_pages"] for r in rows),
        "sample_wall_seconds":round(time.time()-started,2),"estimated_base_seconds":round(base,2),
        "engineering_range_seconds":[round(base*0.8),round(base*1.5)],"documents":rows,
        "limitations":["Engineering range is not a statistical confidence interval.",
            "Extraction bypassed application cache, but operating-system disk cache may be warm.",
            "Sample indexing renumbers selected pages; alignment outputs are not full-document accuracy measurements.",
            "Full-document model calls, downloads, manual review, defect fixing and final validation are estimated separately."]}
    atomic_json(Path("reports/stratified_sample_timing.json"),result)
    print(json.dumps({k:v for k,v in result.items() if k!="documents"},ensure_ascii=False),flush=True)


if __name__=="__main__":main()
