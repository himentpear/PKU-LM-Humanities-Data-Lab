# 观台磁州窑报告处理验收（2026-10-30）

整份报告已写入 `data/processed/library/library.db`。文档 ID：`3f6e2ea2-3baa-4462-91d1-f0051bd91d5d`。

阅读服务：http://127.0.0.1:8001 。选择“文献逐页阅览”→“3观台磁州窑址”，页码表第331页为编号与脚注共存示例。

## 入库结果

- 793页：791页走本地 RapidOCR，2页提取原有文字层。
- 25,826条原始文字行；逐行文字、坐标与缓存一致，原 PDF 和全部页图哈希通过核验。
- 725条脚注及候选：558条自动识别、6条经 Codex 对照原图确认、161条待校对。
- 823处正文引用及候选：503处自动关联、6处经原图确认、314处待校对。
- 第331页 `T3③：31` 保持完整，正文①、②分别关联到本页两条注释。
- 来源命名空间 `guantai-cizhou` 防止同号器物与落马桥报告合并。

## 工作流与验证

详见 `docs/guantai_footnote_workflow.md`。脚注与引用分别存表，保留源行、原文偏移、目标、判定证据及修订号。重复编号、漏识序号、跨页引注和弱证据不猜测关联。支持确认、拒绝、文字修订失效与候选重建；CAS 防止并发覆盖。

本轮只使用本地 OCR、规则和 OpenCV，没有调用外部 API。原图确认记录标识核验者为 Codex visual QA，不冒充人工审校。

- `reports/guantai_footnote_audit.json`：整份报告的原文、坐标、页图哈希、引用目标、偏移与导出一致性通过，错误数0。
- `reports/guantai_library_verification.json`：SQLite integrity、外键、793页图、Bundle、ALTO 和全文检索数量通过；越界文字框0。
- `reports/guantai_http_verification.json`：实际服务的793页列表、第331页脚注、编号保护、原图与全文检索通过。
- `reports/guantai_visual_qa/`：10页页底原图抽查、6条漏识序号确认记录。浏览器实际验证第331页点击①可定位脚注，编号中的③没有变成脚注按钮。

回归测试：`python -m pytest -q`，20项通过（含跨页拒绝、修订号 API、不同遗址同号隔离）。

## 实际限制

以上是结构与来源一致性验收，不代表整本 OCR 逐字无误。314处引用候选包含漏序号、跨页引注和其他带圈数字；161条脚注候选仍需校对，跨页链接不自动猜测。

页码对齐、目录、图表仍沿用现有候选流程，尚未完成全书人工验收。当前待校对任务分布：

```json
{
  "figure_crop_candidate": 62,
  "figure_crop_unresolved": 29,
  "footnote_ambiguous": 219,
  "footnote_unresolved": 314,
  "low_mapping_confidence": 198,
  "missing_page_gap": 7,
  "table_structure_review": 42
}
```

备份保存在 `data/processed/library/backups/`。原 PDF、页图、数据库、模型权重和外部凭据均未加入 Git。
