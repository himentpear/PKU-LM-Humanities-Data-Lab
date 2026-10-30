"""Audit one report's footnote links against source OCR, database and exports."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.ingest.footnotes import reference_offsets
from src.ingest.page_pipeline import file_hash,atomic_json


def audit(library,manifest_id):
    db=sqlite3.connect((library.resolve()/'library.db').as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    manifest=db.execute('select m.*,w.slug,w.title from manifests m join works w on w.id=m.work_id where m.id=?',(manifest_id,)).fetchone()
    if manifest is None:raise ValueError('Manifest not found')
    run=library/manifest['slug'];errors=[];notes=[];refs=[];raw_count=0
    canvases=db.execute('select * from canvases where manifest_id=? order by scan_index',(manifest_id,)).fetchall()
    def error(kind,page,**data):errors.append({'kind':kind,'page':page,**data})
    for canvas in canvases:
        page=canvas['scan_index'];cid=canvas['id']
        lines={l['id']:l for l in db.execute('select * from alto_lines where canvas_id=?',(cid,))}
        ns=db.execute('select * from footnotes where canvas_id=?',(cid,)).fetchall()
        rs=db.execute('select * from footnote_references where canvas_id=?',(cid,)).fetchall()
        by_note={n['id']:n for n in ns}
        for note in ns:
            ids=json.loads(note['source_line_ids'])
            if not ids or any(i not in lines for i in ids):error('invalid_note_source',page,id=note['id'])
            elif '\n'.join(lines[i]['content_text'] for i in ids)!=note['content_text']:error('note_text_drift',page,id=note['id'])
            notes.append({'id':note['id'],'page':page,'marker':note['marker'],'status':note['status'],
                'text':note['content_text'],'evidence':json.loads(note['evidence'])})
        linked=Counter()
        for ref in rs:
            line=lines.get(ref['line_id']);target=by_note.get(ref['footnote_id'])
            if line is None:error('missing_ref_line',page,id=ref['id']);continue
            if ref['status'] in {'linked','approved'}:
                if line['content_text'][ref['char_start']:ref['char_end']]!=ref['marker']:error('offset_drift',page,id=ref['id'])
                if (ref['char_start'],ref['char_end'],ref['marker']) not in list(reference_offsets(line['content_text'])):error('code_misclassified',page,id=ref['id'])
                if target is None or target['marker']!=ref['marker'] or target['status'] not in {'auto_linkable','approved'}:error('invalid_target',page,id=ref['id'])
                if ref['status']=='linked':
                    if sum(n['marker']==ref['marker'] for n in ns)!=1:error('ambiguous_auto_target',page,id=ref['id'])
                    if json.loads(ref['evidence']).get('reference_kind')!='same_page':error('cross_page_auto_link',page,id=ref['id'])
                linked[ref['marker']]+=1
            refs.append({'id':ref['id'],'page':page,'marker':ref['marker'],'status':ref['status'],
                'target':ref['footnote_id'],'context':line['content_text'],'evidence':json.loads(ref['evidence'])})
        try:
            cache=json.loads((run/'pages'/f'{page:05d}.json').read_text(encoding='utf8'))
            cached=cache['page']
            actual=Counter((l['raw_ocr'],l['x'],l['y'],l['w'],l['h']) for l in lines.values())
            expected=Counter((l['text'],round(l['x_pt']*2),round(l['y_top_pt']*2),max(1,round(l['w_pt']*2)),max(1,round(l['h_pt']*2))) for l in cached['lines'])
            if actual!=expected:error('raw_ocr_or_geometry_changed',page)
            if file_hash(Path(cached['image_path']))!=cache['image_sha256']:error('page_image_changed',page)
            raw_count+=len(cached['lines'])
            bundle=json.loads((run/'bundles'/f'{page:05d}.json').read_text(encoding='utf8'))
            if {n['id'] for n in bundle['footnotes']}!=set(by_note):error('export_note_mismatch',page)
            if {r['id'] for r in bundle['footnote_references']}!={r['id'] for r in rs}:error('export_ref_mismatch',page)
            ET.parse(run/'alto'/f'{page:05d}.xml')
        except (OSError,ValueError,KeyError,ET.ParseError) as exc:error('invalid_export_or_cache',page,error_type=type(exc).__name__)
    integrity=db.execute('pragma integrity_check').fetchone()[0]
    fk=[tuple(r) for r in db.execute('pragma foreign_key_check')]
    if len(canvases)!=manifest['total_pages']:errors.append({'kind':'page_count_mismatch'})
    if [c['scan_index'] for c in canvases]!=list(range(1,manifest['total_pages']+1)):errors.append({'kind':'physical_page_sequence'})
    source_ok=file_hash(Path(manifest['source_pdf_path']))==manifest['source_pdf_sha256']
    if not source_ok:errors.append({'kind':'source_pdf_changed'})
    result={'manifest_id':manifest_id,'title':manifest['title'],'source_sha256':manifest['source_pdf_sha256'],
        'pages':len(canvases),'raw_lines_preserved':raw_count,'source_preserved':source_ok,'integrity':integrity,
        'foreign_key_errors':fk,'notes_by_status':dict(Counter(n['status'] for n in notes)),
        'references_by_status':dict(Counter(r['status'] for r in refs)),
        'unresolved_pages':sorted({r['page'] for r in refs if r['status']=='needs_review'}),
        'errors':errors,'notes':notes,'references':refs,
        'passed':not errors and not fk and integrity=='ok'}
    db.close();return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path,default=Path('data/processed/library'))
    parser.add_argument('--manifest-id',required=True)
    parser.add_argument('--output',type=Path,default=Path('reports/guantai_footnote_audit.json'))
    args=parser.parse_args();result=audit(args.library,args.manifest_id);atomic_json(args.output,result)
    print(json.dumps({k:v for k,v in result.items() if k not in {'notes','references'}},ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
