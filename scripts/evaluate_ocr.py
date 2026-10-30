"""Compare local OCR with embedded native text on stratified real document pages."""
import argparse
import importlib.metadata
import json
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.ingest.page_pipeline import LocalOcr,PageExtractor,file_hash,atomic_json
from src.ingest.digital_pdf_ingestor import DigitalPdfIngestor
from src.alto.parser import AltoIngestor


def normalize(text):
    return "".join(unicodedata.normalize("NFKC",text).split())


def protected_metrics(row):
    spans=AltoIngestor.extract_protected_spans(row["reference"])
    compact=lambda s:"".join(s.split())
    # Normalize punctuation only. Circled stratum numbers must remain distinct
    # from ordinary digits, unlike NFKC normalization used for general text CER.
    canonical=lambda s:compact(s).translate(str.maketrans({"∶":":","：":":"}))
    row["protected_spans"]=len(spans)
    row["protected_verbatim_found"]=sum(compact(s["text"]) in compact(row["ocr"]) for s in spans)
    row["protected_spans_found"]=sum(canonical(s["text"]) in canonical(row["ocr"]) for s in spans)
    return row


def distance(a,b):
    if len(a)<len(b):a,b=b,a
    previous=list(range(len(b)+1))
    for i,ca in enumerate(a,1):
        current=[i]
        for j,cb in enumerate(b,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(ca!=cb)))
        previous=current
    return previous[-1]


def main():
    import pymupdf
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--pdf-dir",type=Path,default=Path("docs/考古报告"))
    parser.add_argument("--output",type=Path,default=Path("data/processed/benchmarks/ocr-evaluation"))
    parser.add_argument("--pages-per-document",type=int,default=6)
    args=parser.parse_args()
    ocr=LocalOcr(4);rows=[]
    for pdf in sorted(args.pdf_dir.glob("*.pdf")):
        sha=file_hash(pdf)
        folder=args.output/sha[:16]
        extractor=PageExtractor(folder,dpi=180)
        with pymupdf.open(pdf) as document:
            indices=sorted({min(len(document)-1,round(len(document)*(i+1)/(args.pages_per_document+1)))
                            for i in range(args.pages_per_document)})
            for index in indices:
                native=DigitalPdfIngestor.extract_native_lines(document[index])
                reference="\n".join(line.text for line in native)
                if len(normalize(reference))<30:
                    continue
                cache=folder/"evaluation"/f"{index+1:05d}.json"
                if cache.exists():
                    rows.append(protected_metrics(json.loads(cache.read_text(encoding="utf-8"))))
                    continue
                page=extractor.extract(document,index,sha)
                started=time.time()
                recognized=ocr.recognize(Path(page.image_path),180/72)
                text="\n".join(line.text for line in recognized)
                a,b=normalize(reference),normalize(text)
                edits=distance(a,b)
                protected=AltoIngestor.extract_protected_spans(reference)
                recalled=sum(normalize(span["text"]) in b for span in protected)
                row={"source":pdf.name,"scan_index":index+1,"reference_characters":len(a),
                    "edit_distance":edits,"character_error_rate":round(edits/max(1,len(a)),4),
                    "protected_spans":len(protected),"protected_spans_found":recalled,
                    "ocr_seconds":round(time.time()-started,3),"reference":reference,"ocr":text,
                    "reference_kind":"embedded_text_not_manually_verified_ground_truth"}
                row=protected_metrics(row)
                atomic_json(cache,row);rows.append(row)
                print(json.dumps({k:v for k,v in row.items() if k not in {"reference","ocr"}},ensure_ascii=False),flush=True)
    chars=sum(r["reference_characters"] for r in rows);spans=sum(r["protected_spans"] for r in rows)
    summary={"samples":len(rows),"documents":len({r["source"] for r in rows}),
             "reference_characters":chars,"character_error_rate":round(sum(r["edit_distance"] for r in rows)/max(1,chars),4),
             "protected_spans":spans,"protected_span_recall":round(sum(r["protected_spans_found"] for r in rows)/max(1,spans),4),
             "protected_verbatim_recall":round(sum(r["protected_verbatim_found"] for r in rows)/max(1,spans),4),
             "protected_normalization":"Whitespace and colon variants only; circled numbers are preserved",
             "rapidocr_version":importlib.metadata.version("rapidocr"),"dpi":180,
             "limitation":"Native text is a reference, not manually verified truth. Reading-order differences count as character edits; this does not measure accuracy on historical scans."}
    atomic_json(Path("reports/local_ocr_evaluation.json"),{"summary":summary,"pages":rows})
    print(json.dumps(summary,ensure_ascii=False))


if __name__=="__main__":main()
