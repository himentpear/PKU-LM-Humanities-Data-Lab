"""Audit all persisted pages and exports; produce machine-readable evidence."""
import argparse
import json
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.ingest.page_pipeline import atomic_json,file_hash


def verify(library: Path) -> dict:
    connection=sqlite3.connect((library.resolve()/"library.db").as_uri()+"?mode=ro",uri=True)
    connection.row_factory=sqlite3.Row
    result={"integrity":connection.execute("pragma integrity_check").fetchone()[0],
            "foreign_key_errors":[tuple(r) for r in connection.execute("pragma foreign_key_check")],"documents":[]}
    for manifest in connection.execute("select m.*,w.slug from manifests m join works w on w.id=m.work_id").fetchall():
        run_dir=library/manifest["slug"]
        canvases=connection.execute("select * from canvases where manifest_id=? order by scan_index",(manifest["id"],)).fetchall()
        errors=[];methods=Counter();out_of_bounds=0
        for page in canvases:
            image=Path(page["image_storage_path"] or "")
            if not image.is_file():errors.append({"page":page["scan_index"],"error":"missing_image"})
            for suffix,folder in [("xml","alto"),("json","bundles")]:
                file=run_dir/folder/f"{page['scan_index']:05d}.{suffix}"
                try:
                    if suffix=="xml":ET.parse(file)
                    else:json.loads(file.read_text(encoding="utf-8"))
                except Exception:errors.append({"page":page["scan_index"],"error":"invalid_"+folder})
            alto=connection.execute("select ocr_engine from alto_pages where canvas_id=?",(page["id"],)).fetchone()
            methods[alto[0] if alto else "missing"]+=1
            for line in connection.execute("select x,y,w,h from alto_lines where canvas_id=?",(page["id"],)):
                if line[0]<0 or line[1]<0 or line[2]<1 or line[3]<1 or line[0]+line[2]>page["width"]+1 or line[1]+line[3]>page["height"]+1:
                    out_of_bounds+=1
        if len(canvases)!=manifest["total_pages"]:errors.append({"error":"page_count_mismatch"})
        if [p["scan_index"] for p in canvases]!=list(range(1,len(canvases)+1)):errors.append({"error":"nonconsecutive_physical_pages"})
        original_hash=file_hash(Path(manifest["source_pdf_path"]))
        if original_hash!=manifest["source_pdf_sha256"]:errors.append({"error":"source_hash_changed"})
        data={"manifest_id":manifest["id"],"source":Path(manifest["source_pdf_path"]).name,"pages":len(canvases),
              "source_preserved":original_hash==manifest["source_pdf_sha256"],"page_methods":dict(methods),
              "out_of_canvas_lines":out_of_bounds,"errors":errors}
        for table in ["alto_blocks","alto_lines","plates_figures","iiif_ranges","rag_chunks","provenance_anchors","review_tasks"]:
            if table in {"alto_blocks","alto_lines"}:
                n=connection.execute(f"select count(*) from {table} t join canvases c on c.id=t.canvas_id where c.manifest_id=?",(manifest["id"],)).fetchone()[0]
            elif table=="plates_figures" or table in {"iiif_ranges","rag_chunks","provenance_anchors","review_tasks"}:
                n=connection.execute(f"select count(*) from {table} where manifest_id=?",(manifest["id"],)).fetchone()[0]
            data[table]=n
        result["documents"].append(data)
    result["fts_line_count"]=connection.execute("select count(*) from fts_alto_lines").fetchone()[0]
    result["line_count"]=connection.execute("select count(*) from alto_lines").fetchone()[0]
    result["passed"]=(result["integrity"]=="ok" and not result["foreign_key_errors"] and
                      all(not d["errors"] and d["out_of_canvas_lines"]==0 for d in result["documents"]) and
                      result["fts_line_count"]==result["line_count"])
    connection.close()
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--library",type=Path,default=Path("data/processed/library"))
    parser.add_argument("--output",type=Path,default=Path("reports/library_verification.json"))
    args=parser.parse_args()
    result=verify(args.library)
    atomic_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result["passed"] else 1)
