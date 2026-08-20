---
name: tech-spec-pdf-diff
description: 通用技术协议 PDF 差异比对。This skill should be used when the user asks to compare/diff two technical specification PDFs (设备技术规格书/技术协议), for any equipment type (激光/卷绕/碟片/涂布/模切/分切等). It handles DLP-encrypted PDFs (Sangfor ztsm %TSD-Header-###%), extracts clean page text, strips headers/footers, aligns chapter structure, compares clauses and table rows (including cross-page table merging), and outputs diff reports in Markdown and/or HTML (diff-only, readable version). Triggers： "比对两个技术协议/规格书差异", "compare two spec PDFs", "输出差异报告 md/html".
agent_created: true
---

# 技术协议 PDF 差异比对 (tech-spec-pdf-diff)

## 强制执行规则（违反即任务失败）

本 skill 的提取、机械 diff、报告生成已封装为确定性工具，**禁止自写替代代码**：

1. **必须调用 `run_pdf_diff_stage1`** 完成 DLP 解密（如需要）、PDF 提取、结构对齐、差异页机械定位。禁止自己编写 PDF 解析/文本提取/比对代码替代，即使你认为自己写的更简单更快。
2. 基于 stage1 返回的候选差异页做**语义判定**，手工整理 `diff.json`（结构见下方"diff.json 结构"）。
3. **必须调用 `run_pdf_diff_stage3`** 生成报告。禁止自写报告生成代码。
4. 用 `download_from_sandbox` 把报告与 `diff_pages.json` 拉回 `/reports/{user_id}/{session_id}/`（stage1/stage3 产物都在沙箱 `/home/user/` 下）。
5. 任何工具报错时，把原始错误信息返回用户，禁止静默改用其他方法或跳过步骤。

## 流程

### Step 1：阶段1（确定性，工具执行）

调用 `run_pdf_diff_stage1(doc_a_path, doc_b_path, sandbox_id)`：

- 入参：两个 PDF 的路径（`/uploads/{user_id}/{session_id}/{文件名}` 虚拟路径即可，工具会解析真实路径）
- 工具内部固定执行：DLP 检测→解密→上传 skill 脚本→`extract_pdf.py` 提取两份文档（`--chapters`）→`diff_structures.py` 结构粗筛→`diff_pages.py` 定位差异页（`--out diff_pages.json`）
- 返回：`docA.json / docB.json / diff_pages.json` 沙箱路径 + `diff_summary`（差异页摘要）+ `structure_summary`

### Step 2：语义判定（唯一需要你判断的步骤）

基于 stage1 返回的差异页清单，用 `diff_pages.py --full --pages <N-M>`（或直接读沙箱内 JSON 文本）核对完整文本，**逐条判定实质差异后手工整理 `diff.json`**。

**排版噪声（忽略，不写入 diff.json）**：
- 纯空格/标点差异（如 `Φ1000mm 设计` vs `Φ 1000mm 设计`、`（DCM平台）` vs `（DCM 平台）`）
- 换行/分页偏移（同一段文本出现在不同页，行切分位置不同）
- pdfplumber 表格提取顺序差异（单元格行序/列序不同但内容相同）——需人工确认内容一致后才可判为噪声

**实质差异（必须写入 diff.json）**：
- 条款增/删（含编号顺延——删一条后后续编号 7)→6) 顺延，是强信号）
- 条款内容变更（含措辞增强，如"数显气压表"→"带模拟量输出的数显气压表"）
- 数值/单位变化（精确给出旧值/新值，如"≥15m/s vs ≥12m/s"）
- 模块表新增/删除模块（常伴随后续章节页码偏移）
- 附图增删（目录中"错误!未定义书签"通常是删除了对应内容）
- 修订履历表（版本 A/B/C 修订记录）条目数差异与增删条目

**diff.json 结构**：以 `generate_report.py` 头部注释定义的字段为准（章节/条款/表格/模块表逐条列出，无差异内容记入 no_diff 清单）。**禁止用脚本自动生成 diff.json**——机械文本 diff 会把排版噪声灌进报告，语义判定必须由你完成。**必须确保 diff.json 是合法 JSON**：数组/对象最后一项后禁止尾随逗号（trailing comma），写完自查 `json.loads` 能解析；若 run_pdf_diff_stage3 返回 `diff_json_validate` 错误，按其中的行/列信息修复后重试，不要盲目重写。

### Step 3：阶段3（确定性，工具执行）

调用 `run_pdf_diff_stage3(diff_json_path, out_prefix, sandbox_id)`：

- `diff_json_path`：diff.json 来源，二选一——(a) 宿主路径：write_file 写到 `/reports/{user_id}/{session_id}/diff.json` 后传入该路径；(b) 沙箱内路径：若 diff.json 已在沙箱（如 /home/user/diff.json），直接传沙箱路径。工具自动识别，两种都接受。
- `out_prefix`：报告名前缀，如 `技术协议差异对比报告_阴极vs阳极_20260819_1`
- 工具内部固定执行：上传 diff.json → `generate_report.py --format md` + `--format html` → 产物校验
- 返回沙箱内报告路径

之后 `download_from_sandbox` 拉回两份报告到 `/reports/{user_id}/{session_id}/`，用 `present_files` 展示。

**输出约定**：HTML 只含差异内容，无差异章节以"✓ 清单"收尾；红色=文档 A 原值，绿色=文档 B 新值；用户只要一种格式则按需生成，默认 md + html 都生成。

## 关键经验（语义判定时务必遵守）

1. **中间文件 DLP 加密**：workspace 下批量中间文本会被 DLP 钩子截短为 8192 字节。脚本已在沙箱/内存中处理，你无需持久化中间产物；write_file 只写最终 diff.json 和报告。
2. **页眉页脚**：`extract_pdf.py` 默认剔除页眉（文件名称/公司抬头/标识码/版本号）与页脚（机密标记/第 X 页 共 N 页）。新文档格式不同时，可先用 `--print` 查看再调脚本（需修改 skill 脚本，走上传更新）。
3. **页码偏移**：两文档正文相同但页码整体偏移（如 A 31 页 / B 32 页），通常是新增模块/排版导致，报告说明"每章顺延 N 页"即可，不算内容差异。
4. **同族不同型号**（阴极/阳极、A 型/B 型）常有"设备极性/加工对象"字段差异，属正常差异，如实列出。
5. **报告交叉核对**：有《技术协议偏离项》xlsx 需求变更台账时，把"需求变更/确定事项"列逐条与 PDF 差异报告核对：已落地变更应 100% 覆盖；台账注明"待定/未实施/按原方案"或 A 版已含（B 未改）的，不会出现在 PDF 差异报告中。
6. **DLP 加密 xlsx 解密后可能损坏**（flag 0x80 完整加密实测）：zip 内部条目损坏时，`xl/sharedStrings.xml` 通常完整可解压（文本单元格全存这里），可结合表头语义重建文本列；判定损坏用 `zipfile.testzip()` + `zf.read('xl/worksheets/sheet1.xml')` 抛 `zlib.error`；损坏文件在 Excel 大概率也打不开，需用户 Office/WPS 另存触发重加密后重解。

## Resources

### scripts/（由 stage1/stage3 工具内部调用，你不直接运行；排障时参考）

- `extract_pdf.py` — DLP 检测 + 内存解密 + 分页文本提取 + 页眉页脚剔除 + 章节识别
- `diff_structures.py` — 章节级对齐与差异初筛
- `diff_pages.py` — 逐页 diff 初筛：定位差异页 + 行级 diff 摘要（仅标准库）
- `generate_report.py` — 从 diff.json 生成 Markdown / HTML 报告

### references/

- `REQUIREMENTS.md` — **需求基线**（背景、需求清单、边界场景 S1-S4 设计决策、待加固项 ①-⑥、变更记录）。需求变更或加固开发前先读此文档，变更后追加记录。
