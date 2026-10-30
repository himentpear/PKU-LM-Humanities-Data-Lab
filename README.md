# 大模型与人文数据处理

北京大学课程“大模型与人文数据处理”的考古方向小组仓库。

本仓库用于保存课程作业、文本数据处理实验、可复用的 Python 工具、Jupyter Notebook、报告与课程笔记。探索性分析放在 Notebook 中；可复用的数据处理逻辑应整理到 `src/`，并尽量通过 `scripts/` 中的脚本重复执行。

## 环境要求

- Python 3.11 或更高版本
- Git

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

目前已实现考古 PDF 的本地数字化流程，包含嵌字提取、逐页扫描 OCR、断点缓存、结构化入库、ALTO/IIIF 导出及本地阅览审校。部署、模型和外部 API 配置见 [操作说明](docs/digitization_runbook.md)。

## 工作流架构

本项目针对考古报告与人文历史文献的高保真数字化、版面还原、知识抽取与交互审校，设计了全流程、可追溯、原子化保障的流水线：

```mermaid
flowchart TD
    subgraph S1["1. 文档摄入与页级分流 (Ingest & Routing)"]
        PDF["原始 PDF 文档<br/>docs/考古报告/*.pdf"] --> Inspect["PDF 探查器<br/>src/ingest/pdf_inspector.py"]
        Inspect --> Route{"逐页判断页面类型<br/>文字层/损坏/纯图扫描"}
        Route -->|"原生文字层可用"| PyMuPDF["PyMuPDF 文本与字符坐标抽取"]
        Route -->|"扫描图/文字损坏"| OCR["RapidOCR (PP-OCRv6)<br/>ONNX Runtime CPU"]
        PyMuPDF --> Checkpoint["逐页断点缓存<br/>pages/*.json, images/*.jpg"]
        OCR --> Checkpoint
    end

    subgraph S2["2. 版面分析与坐标对齐 (Layout & Alignment)"]
        Checkpoint --> Layout["版面规则引擎<br/>src/ingest/layout.py"]
        Layout --> Clean["页眉/页脚剔除 & 阅读顺序整理"]
        Clean --> Align["字符与行包围盒对齐<br/>src/alignment/sequence_dp.py"]
        Align --> Structure["版面语义识别<br/>目录域 / 图版与图题 / 表格候选"]
    end

    subgraph S3["3. 关系数据库与全文索引 (Atomic Ingestion)"]
        Structure --> IngestTx["原子事务入库<br/>src/ingest/digital_pdf_ingestor.py"]
        IngestTx --> DB[("SQLite 数据库<br/>library.db<br/>SQLAlchemy 2.0 ORM + FTS5")]
        DB --- Tables["14 张核心模型表:<br/>• Work / Manifest / Canvas<br/>• AltoPage / Block / Line / Token<br/>• PlateFigure / ImageAsset / ImageOccurrence<br/>• ArchEntity / EntityMention<br/>• RagChunk / ProvenanceAnchor<br/>• ReviewTask / IiifRange"]
    end

    subgraph S4["4. 证据闭环丰富与大模型抽取 (Enrichment & Models)"]
        IngestTx --> EnrichRule["规则丰富阶段<br/>src/enrichment.py"]
        EnrichRule --> GraphicCrop["OpenCV 图像连通域检测 & 图版裁切"]
        EnrichRule --> TableParse["表格结构候选抽取 (Rule/Grid)"]
        EnrichRule --> RAGChunk["构建 RAG 切片与 #xywh 物理坐标溯源锚点"]

        IngestTx -.-> ModelOpt["可选语言模型阶段<br/>src/model_enrichment.py"]
        ModelOpt --> LocalLLM["本地轻量模型 (Qwen GGUF / llama-cpp)"]
        ModelOpt --> CloudAPI["云端模型回退 (外部配置 deepseek-flash)"]
        LocalLLM --> SourceVerify["字面证据校验<br/>validate_entities: 精确原文位置匹配"]
        CloudAPI --> SourceVerify
        SourceVerify -->|"校验通过"| EntityCand["存入 ArchEntity / EntityMention 候选"]
    end

    subgraph S5["5. 标准开放格式导出 (Export)"]
        DB --> Exporter["导出生成器<br/>src/alto/exporter.py & src/iiif/generator.py"]
        Exporter --> IIIF["IIIF Presentation 3.0<br/>manifest.json, bundles/*.json"]
        Exporter --> ALTO["ALTO XML v4<br/>alto/*.xml, 含 PrintSpace 与块/行坐标"]
        Exporter --> QualityRep["质量报告与审校任务清单<br/>quality.json"]
    end

    subgraph S6["6. 阅览、搜索与人工审校 (Serve & Review)"]
        IIIF --> Server["本地 HTTP 服务 (src/server.py:8000)"]
        ALTO --> Server
        DB --> Server
        Server --> WebUI["本地阅览审校室<br/>src/web/library.html"]
        WebUI --> Action1["多卷文献切换 & 原页原图对比"]
        WebUI --> Action2["FTS5 中文全文检索 & 器物编号过滤"]
        WebUI --> Action3["点击文字行高亮原图 BBox 坐标"]
        WebUI --> Action4["乐观并发控制审校保存 (src/review.py)<br/>• raw_ocr 保留<br/>• approved_text 增量保存<br/>• Revision 历史追溯"]
    end
```

## 目录结构

```text
.
├── notebooks/        # Jupyter Notebook：探索与演示
├── src/              # 可复用的 Python 源码
├── data/
│   ├── raw/          # 原始数据（默认不提交）
│   └── processed/    # 处理后数据
├── assignments/      # 课程作业
├── experiments/      # 实验代码
├── reports/          # 报告、图表和结果
├── scripts/          # 可重复执行的脚本
└── docs/             # 课程笔记与技术文档
```

## 运行方法

激活虚拟环境后，从项目根目录运行脚本，例如：

```bash
python scripts/example.py
```

后续新增的脚本与实验应在对应文档或作业说明中记录使用方法。

## 数据说明

原始数据存放在 `data/raw/`，默认不纳入 Git；处理后的数据存放在 `data/processed/`。请勿提交 API 密钥、访问令牌、个人信息或大型数据文件。数据来源、获取方式和处理流程请记录在 [data/README.md](data/README.md)。
