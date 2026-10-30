"""Export the preserved source text and real line geometry as ALTO 4 XML."""
from pathlib import Path
import xml.etree.ElementTree as ET
from sqlalchemy import select
from src.db.models import AltoBlock, AltoLine
from src.io_utils import atomic_write

NS = "http://www.loc.gov/standards/alto/ns-v4#"
ET.register_namespace("", NS)


def export_canvas(session, canvas, path: Path):
    def node(parent, name, attrs=None):
        return ET.SubElement(parent, f"{{{NS}}}{name}", attrs or {})
    root = ET.Element(f"{{{NS}}}alto")
    description = node(root,"Description")
    node(description,"MeasurementUnit").text = "pixel"
    source = node(description,"sourceImageInformation")
    node(source,"fileName").text = canvas.image_storage_path or ""
    types=list(session.scalars(select(AltoBlock.block_type).where(AltoBlock.canvas_id==canvas.id).distinct()))
    tags=node(root,"Tags")
    for kind in types:
        node(tags,"OtherTag",{"ID":"type_"+kind,"LABEL":kind})
    layout = node(root,"Layout")
    page = node(layout,"Page", {"ID": "P"+canvas.id, "WIDTH":str(canvas.width),
                                "HEIGHT":str(canvas.height), "PHYSICAL_IMG_NR":str(canvas.scan_index)})
    space = node(page,"PrintSpace",{"HPOS":"0","VPOS":"0","WIDTH":str(canvas.width),"HEIGHT":str(canvas.height)})
    for block in session.scalars(select(AltoBlock).where(AltoBlock.canvas_id==canvas.id).order_by(AltoBlock.reading_order)):
        tb = node(space,"TextBlock",{"ID":"B"+block.id,"HPOS":str(block.x),"VPOS":str(block.y),
                    "WIDTH":str(block.w),"HEIGHT":str(block.h),"TAGREFS":"type_"+block.block_type})
        for line in session.scalars(select(AltoLine).where(AltoLine.block_id==block.id).order_by(AltoLine.line_index)):
            attrs={"HPOS":str(line.x),"VPOS":str(line.y),"WIDTH":str(line.w),"HEIGHT":str(line.h)}
            tl=node(tb,"TextLine", {"ID":"L"+line.id,**attrs})
            # A String is a whole OCR line here; do not invent character boxes.
            text_attrs={"ID":"S"+line.id,"CONTENT":line.raw_ocr or line.content_text,**attrs}
            if line.line_confidence is not None:
                text_attrs["WC"]=str(line.line_confidence)
            node(tl,"String",text_attrs)
    atomic_write(path,ET.tostring(root,encoding="utf-8",xml_declaration=True))
