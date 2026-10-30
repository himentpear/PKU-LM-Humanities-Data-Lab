# IIIF + ALTO XML 人文与考古文献数据库架构设计

针对课程仓库中的考古报告（如《观台磁州窑址》及各卷报告）与人文文献数字化需求，本架构以 **IIIF Presentation API 3.0 / Image API 3.0** 为表现与交互骨架，以 **ALTO XML (v4)** 为版面与细粒度文字坐标载体，采用 **SQLAlchemy 2.0（默认单文件 SQLite + FTS5 全文索引，无缝兼容 PostgreSQL + pgvector）** 构建数据库架构。

---

## 1. 总体架构与核心概念映射

| 业务场景 | IIIF 标准对象 | ALTO XML 节点 | 数据库核心模型 (`src/db/models.py`) | 关键能力 |
| :--- | :--- | :--- | :--- | :--- |
| **书页复原** | `Collection` / `Manifest` / `Canvas` | `<Layout>` / `<Page>` / `<PrintSpace>` | `Work`, `Manifest`, `Canvas`, `AltoPage` | 物理页码与印刷页码对齐、版心边界还原、ALTO 单位与 Canvas 像素坐标归一化映射 |
| **自动目录** | `Range` (`structures`) | 标题类 `<TextBlock TYPE="heading">` | `IiifRange`, `RangeItem` | 多级目录树（章/节/图版目录）、精确到页面局部坐标 `#xywh` 的章节锚定 |
| **文字坐标** | `AnnotationPage` / `Annotation` | `<ComposedBlock>` / `<TextBlock>` / `<TextLine>` / `<String>` | `AltoBlock`, `AltoLine`, `AltoToken`, `IiifAnnotation` | 版面阅读顺序（Reading Order）、块/行/字三级包围盒（BBox）、OCR 置信度与校对版本 |
| **图版检索** | `Canvas#xywh=` + IIIF Image API 裁切 | `<GraphicalElement>` / `<Illustration>` + 图题 `<TextBlock>` | `PlateFigure`, `FigureMention` | 线图/拓片/照片裁切 URL、器物标本号索引、正文“见图X”与图版双向联动 |
| **原文溯源** | `Content State` / `FragmentSelector` | `<String ID="...">` 跨行/跨页组合 | `RagChunk`, `ProvenanceAnchor` | 大模型抽取实体/问答结论精确回溯至原书卷次、章节、页码、行号及高亮坐标框 |

---

## 2. 核心目录与代码组织

```text
src/
├── db/
│   ├── models.py       # 14 张核心表的 SQLAlchemy 2.0 ORM 模型定义
│   └── session.py      # 数据库连接管理、WAL 配置与 SQLite FTS5 全文检索表初始化
├── alto/
│   └── parser.py       # ALTO XML 版面解析、坐标缩放归一化、图版引文自动识别与入库
└── iiif/
    └── generator.py    # IIIF Presentation 3.0 Manifest、Range 目录树、OCR AnnotationPage 与溯源查询生成器
scripts/
└── init_db.py          # 数据库初始化与示例数据（含《观台磁州窑址》样板数据）验证脚本
```

---

## 3. 快速使用指南

### 3.1 初始化数据库并运行演示

```bash
# 初始化数据库并写入演示数据（默认生成在 data/processed/humanities_lab.db）
python scripts/init_db.py --demo
```

### 3.2 导入真实 ALTO XML 页面

```python
from src.db.session import get_session, init_db
from src.alto.parser import AltoIngestor

engine = init_db("sqlite:///data/processed/humanities_lab.db")
with get_session(engine) as session:
    ingestor = AltoIngestor(session)
    ingestor.ingest_alto_file(
        canvas_id="<canvas_uuid>",
        alto_xml_path="data/raw/page_001.alto.xml"
    )
```

### 3.3 导出 IIIF 3.0 Manifest 与单页 OCR 标注

```python
from src.db.session import get_session, init_db
from src.iiif.generator import IiifBuilder

engine = init_db("sqlite:///data/processed/humanities_lab.db")
with get_session(engine) as session:
    builder = IiifBuilder(session, base_url="https://lab.pku.edu.cn/iiif")
    manifest_json = builder.build_manifest("<manifest_uuid>")
    anno_page_json = builder.build_canvas_ocr_annotation_page("<canvas_uuid>")
```
