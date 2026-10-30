# 考古文献自动处理与本地部署

## 安装

在仓库根目录运行，推荐 Python 3.11—3.13：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-models.txt --only-binary=llama-cpp-python
```

基础流程使用 PyMuPDF、RapidOCR / PP-OCRv6 小模型和 ONNX Runtime CPU，不要求 CUDA。语言模型是可选阶段；模型结果必须通过原文字面证据校验，不能覆盖原始识别结果。PyMuPDF 软件分发需确认相应许可要求。

## 外部配置

默认读取 `~/.config/archaeology-digitization/.env`，位于 Git 仓库之外。当前机器的路径是 `C:/Users/jisub/.config/archaeology-digitization/.env`。也可用 `process_document.py --settings <外部路径>` 指定其他文件。不要把密钥作为命令行参数传入。

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
ALLOW_CLOUD_FALLBACK=false
MAX_CLOUD_CALLS_PER_DOCUMENT=20
MAX_CLOUD_TOKENS_PER_DOCUMENT=50000
LOCAL_MODEL_PATH=D:/path/to/model.gguf
LOCAL_MODEL_THREADS=4
MAX_LOCAL_BLOCKS_PER_DOCUMENT=30
```

第三方兼容服务需填写其实际地址及模型名。填好凭据后，将 `ALLOW_CLOUD_FALLBACK` 设为 `true`。配置中允许的环境变量可覆盖文件值。代码、运行元数据和用量报告不保存密钥。

## 单份文献

```powershell
.venv\Scripts\python.exe scripts/process_document.py "D:\path\report.pdf"
```

输入不需要书名、遗址名或预先标注的正文起始页。默认输出到 `data/processed/library/`：

- `library.db`：独立文献库，不覆盖之前的 `data/processed/humanities_lab.db`。
- `<源哈希-配置哈希>/job.json`：状态、已完成页数和错误类型。
- `pages/*.json`：逐页提取缓存、引擎、耗时及原始文字框。
- `images/*.jpg`：原页渲染；`figures/*.jpg`：图版候选。
- `manifest.json`、`bundles/*.json`、`alto/*.xml`：IIIF、页面数据和 ALTO 4 输出。
- `tables/*.json`：表格候选、单元格文字/坐标及待审标识。
- `quality.json`：质量报告与审校任务。
- `model_stage.json`、`model_cache/`：可选语言模型阶段的结果、缓存和有界 API 用量。

相同文件、相同提取配置使用同一运行目录。失败后重新运行原命令会校验并复用逐页缓存。数据库写入在最终事务内完成，避免留下半卷索引。已经入库的文献再次运行会复用已有记录并重新导出，不清库、不重复导入。

`--max-pages N` 仅用于抽样，其配置哈希与全卷不同，因此会成为独立测试记录。`--stop-after N` 可模拟提取阶段中断；去掉该参数再次运行即可续跑。代码和配置规则改变后，正式重建应先做数据库备份，在新库验证结果，不要把已人工校对的数据直接覆盖。

## 全量实机测试

完整考古报告 PDF 不随 Git 仓库分发。运行真实文献集成测试前，将本地原件放入 `docs/考古报告/`；对应文件名与哈希可在探查报告中核对。数据库、页图缓存和模型权重也不会随代码推送。

```powershell
.venv\Scripts\python.exe scripts/benchmark_corpus.py --output data/processed/library
.venv\Scripts\python.exe scripts/verify_library.py
.venv\Scripts\python.exe -m pytest tests -q
```

批处理将目录中全部 PDF 送入同一条通用流程，使用文件名作为临时显示名，不使用报告专属页码偏移或正文模板。`corpus_results.json` 记录逐份状态和运行时间。`verify_library.py` 检查所有已入库文献的页数、原文件哈希、外键、文字框范围、图像与导出文件，但**不把这些结构检查当作 OCR 或实体语义准确率**。

## 本地语言模型

```powershell
.venv\Scripts\python.exe scripts/download_local_model.py
# 需要更高质量时对比 4B 的 4-bit 量化版本
.venv\Scripts\python.exe scripts/download_local_model.py --repo Qwen/Qwen3-4B-GGUF --file Qwen3-4B-Q4_K_M.gguf
.venv\Scripts\python.exe scripts/enrich_models.py --library data/processed/library
```

下载使用官方模型仓库固定 revision，完成后校验 LFS SHA-256，模型文件不纳入 Git。0.6B 是速度基线，实际抽样已发现实体漏提与类型判断错误，不能把“能输出 JSON”视为质量通过。4B 或 API 仍需要相同来源约束及样本评测。

模型阶段只处理有领域线索的有限数量文本块，配置控制每份文献的上限。其余候选在 `remaining_candidates` 中明确记录；规则结果依然保留。模型实体区分候选与已审结果，保留类型、逐字原文位置及来源锚点。重跑模型阶段使用缓存，避免重复调用。

## 阅览与审校

```powershell
.venv\Scripts\python.exe scripts/serve_library.py --library data/processed/library
```

打开 [本地阅览室](http://127.0.0.1:8000)。可切换文献、跳页、检索中文和器物编号、点击文字行高亮原页，查看审校任务及导出文件。

审校保存要求审校人、依据和当前页面版本；过期版本返回冲突，避免覆盖其他人的修改。原始 `raw_ocr` 保留，校对文本另存；编号/尺寸等受保护内容的修改要求详细说明。文字、全文检索、注释、索引与导出随审校更新，并记录 Revision。

服务默认仅监听本机。当前提供原尺寸图像及像素区域裁切，IIIF Image API 宣告 level0，不宣称完整 level2 能力。默认地址为 `http://127.0.0.1:8000/iiif`；使用其他端口时，生成阶段也需通过 `--base-url` 设置一致地址。

## 备份恢复

```powershell
.venv\Scripts\python.exe scripts/backup_library.py "D:\backup\library-2026-10-30.db"
```

脚本通过 SQLite backup API 生成一致快照并验证完整性，拒绝覆盖已有备份。数据库、运行目录中的原页/图版和外部配置应分别备份；密钥按用户自己的凭据备份方式保存。恢复时先停服务，将备份复制到新的库目录，并恢复对应运行文件，再以 `--library` 指向该目录启动验证。

## 当前边界

图版连接区域与扫描表格网格是候选结果，存在拆分/合并错误，需要抽查；未可靠定位的图版有明确待审标识。无框表格、罕见字、模糊标本号和复杂跨页关系不能以自动成功掩盖。API 不可用、预算耗尽或结果未通过证据校验时，保留本地结果并进入审校，不伪造补全。
