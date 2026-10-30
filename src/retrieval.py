"""Local lexical retrieval with exact source citations and optional grounded answers."""
import math
import re
from collections import Counter
from sqlalchemy import select,or_
from src.db.models import RagChunk,ProvenanceAnchor


def terms(text: str):
    output=[]
    for token in re.findall(r"[A-Za-z0-9①②③④⑤⑥⑦⑧⑨⑩:∶：._-]+|[\u3400-\u9fff]+",text):
        if re.fullmatch(r"[\u3400-\u9fff]+",token):
            output.extend(token[i:i+2] for i in range(len(token)-1))
        else:output.append(token.lower())
    return output


def retrieve(session,manifest_id: str,question: str,limit: int=5):
    tokens=list(dict.fromkeys(terms(question)))[:40]
    if not tokens:return []
    candidates=list(session.scalars(select(RagChunk).where(RagChunk.manifest_id==manifest_id,
        or_(*(RagChunk.content_text.contains(token,autoescape=True) for token in tokens))).order_by(RagChunk.chunk_index)))
    if not candidates:return []
    counts=[Counter(terms(c.content_text)) for c in candidates]
    df=Counter(token for token in tokens for count in counts if token in count)
    average=sum(sum(c.values()) for c in counts)/len(counts)
    scored=[]
    for chunk,count in zip(candidates,counts):
        length=sum(count.values());score=0
        for token in tokens:
            frequency=count[token]
            if frequency:
                idf=math.log(1+(len(counts)-df[token]+0.5)/(df[token]+0.5))
                score+=idf*frequency*2.2/(frequency+1.2*(0.25+0.75*length/max(1,average)))
        scored.append((score,chunk))
    hits=[]
    for score,chunk in sorted(scored,key=lambda item:item[0],reverse=True)[:limit]:
        anchor=session.scalar(select(ProvenanceAnchor).where(ProvenanceAnchor.source_entity_type=="rag_chunk",
            ProvenanceAnchor.source_entity_id==chunk.id))
        if anchor:
            hits.append({"chunk_id":chunk.id,"text":chunk.content_text,"score":round(score,4),
                "printed_page":anchor.printed_page,"canvas_id":anchor.canvas_id,
                "target":anchor.iiif_target_uri,"boxes":anchor.highlight_boxes})
    return hits


def validate_answer(answer: dict,hits: list[dict]):
    from jsonschema import validate
    validate(answer,{"type":"object","additionalProperties":False,"required":["answer","citations"],
        "properties":{"answer":{"type":["string","null"]},"citations":{"type":"array","maxItems":10,
            "items":{"type":"object","additionalProperties":False,"required":["chunk_id","quote"],
                "properties":{"chunk_id":{"type":"string"},"quote":{"type":"string","minLength":1}}}}}})
    by_id={h["chunk_id"]:h for h in hits}
    if answer["answer"] and not answer["citations"]:
        raise ValueError("An answer requires source quotations")
    for citation in answer["citations"]:
        if citation["chunk_id"] not in by_id or citation["quote"] not in by_id[citation["chunk_id"]]["text"]:
            raise ValueError("Answer quotation is absent from the retrieved source")
    return {**answer,"requires_review":True,"sources":[by_id[c["chunk_id"]] for c in answer["citations"]]}
