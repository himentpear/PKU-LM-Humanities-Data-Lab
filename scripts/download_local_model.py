"""Download a pinned GGUF file with resume and upstream LFS SHA-256 verification."""
import argparse
import json
import sys
import urllib.request
import time
import re
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.ingest.page_pipeline import file_hash,atomic_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--repo",default="Qwen/Qwen3-0.6B-GGUF")
    parser.add_argument("--file",default="Qwen3-0.6B-Q8_0.gguf")
    parser.add_argument("--output",type=Path,default=Path("data/processed/models"))
    args=parser.parse_args()
    metadata=json.load(urllib.request.urlopen("https://huggingface.co/api/models/"+args.repo+"?blobs=true",timeout=60))
    entry=next(f for f in metadata["siblings"] if f["rfilename"]==args.file)
    expected=entry["lfs"]["sha256"]
    args.output.mkdir(parents=True,exist_ok=True)
    target=args.output/Path(args.file).name
    if target.exists() and file_hash(target)==expected:
        print("Model already verified",flush=True)
        return
    partial=target.with_suffix(".gguf.partial")
    url="https://huggingface.co/"+args.repo+"/resolve/"+metadata["sha"]+"/"+args.file
    expected_size=entry["size"]
    failures=0
    last=partial.stat().st_size if partial.exists() else 0
    while not partial.exists() or partial.stat().st_size<expected_size:
        offset=partial.stat().st_size if partial.exists() else 0
        # A CDN may terminate a large response early or cache a prior byte range.
        # Reopen at the persisted byte count and validate Content-Range before append.
        request=urllib.request.Request(url+f"?download=true&resume={offset}",headers={"Range":f"bytes={offset}-"} if offset else {})
        try:
            with urllib.request.urlopen(request,timeout=60) as response:
                if response.status==206:
                    match=re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",response.headers.get("Content-Range",""))
                    if not match or int(match[1])!=offset or int(match[3])!=expected_size:
                        raise ValueError("Server returned a different byte range")
                elif offset:
                    raise ValueError("Server did not honor resume range; partial file preserved")
                with partial.open("ab" if offset else "wb") as stream:
                    written=offset
                    while chunk:=response.read(1024*1024):
                        if written+len(chunk)>expected_size:
                            raise ValueError("Response exceeds model metadata size")
                        stream.write(chunk);written+=len(chunk)
                        if written-last>=32*1024*1024:
                            print(json.dumps({"downloaded_mb":round(written/1024**2,1),"total_mb":round(expected_size/1024**2,1)}),flush=True)
                            last=written
                if written==offset:
                    raise ValueError("No download progress")
                failures=0
        except Exception as error:
            failures+=1
            print(json.dumps({"download_retry":failures,"error_type":type(error).__name__,"partial_bytes":partial.stat().st_size if partial.exists() else 0}),flush=True)
            if failures>=5:raise
            time.sleep(min(2**failures,16))
    if file_hash(partial)!=expected:
        raise ValueError("Downloaded model checksum mismatch")
    partial.replace(target)
    atomic_json(target.with_suffix(".json"),{"repo":args.repo,"revision":metadata["sha"],"file":args.file,"sha256":expected})
    print("Model verified: "+str(target),flush=True)


if __name__=="__main__":main()
