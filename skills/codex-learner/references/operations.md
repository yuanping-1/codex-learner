# 记录接口

Python 3.10–3.12。基础环境安装 requirements-base.txt；OCR 使用独立 Python 3.12 环境。配置文件在 Skill 根目录，包含 schema_version=1、绝对 library_root 和可选 python_executable。

从任意目录调用（把路径换成本 Skill 的实际绝对路径）：

    python /absolute/skill/scripts/learner.py doctor
    python /absolute/skill/scripts/learner.py list
    python /absolute/skill/scripts/learner.py show --course COURSE_ID
    python /absolute/skill/scripts/learner.py review --course COURSE_ID
    python /absolute/skill/scripts/learner.py apply --request /absolute/request.json
    python /absolute/skill/scripts/learner.py render --course COURSE_ID --output /absolute/new-panel.html --fragment

开发或隔离验收使用全局参数 --library /absolute/sample-library。只有 init 命令显式创建空库。其他命令发现库缺失会失败，不创建替代库。成功 stdout 为 JSON {ok:true,result:...}；失败 stderr 为 JSON，进程退出码为 2。输入 JSON 拒绝重复键和 NaN。

## 标识与版本

course_id、chapter_id、content_version、question_id、question_version、rubric_version 和 operation_id 使用 1–96 位 ASCII 字母、数字、下划线或连字符，首位为字母/数字。可用 UUID。标题和内容可以用中文。

所有写操作都有 action、operation_id、course_id。operation_id 在课程中唯一；原请求重试须保持全部数据相同。正文与状态版本不同：

- state_revision：每次新事务增加；旧面板携带它仅供说明，不能凭它拒绝另一题的答案。
- content_version：来源、实质讲义或目标结构版本。
- question_version：同一题的题意版本；新复测题使用新的 question_id。
- rubric_version：同一题意下的评分依据版本。

新内容发布不自动替换已采用内容，使用 adopt_content。题目当前版本按 content_version 分开保存；未采用章节的新题不能提前作废正在学习的旧章题目。

## 写操作

| action | 其他必需字段 | 行为 |
|---|---|---|
| create_course | title、goals；可选 outline | 创建课程；outline 是 chapter_id/title/goals 对象列表 |
| import_source | source_id、绝对 path；HTML 还需 url | 保存完整原件副本和结构化摘录；可选 captured_at |
| verify_source | source_id、locators、evidence | 实际视觉核对后登记；不能核验空文本 |
| publish_content | chapter_id、content_version、title、goals、parts | 发布不可变内容；首章自动成为初始位置 |
| adopt_content | chapter_id、content_version | 显式采用已发布新版；旧证据不自动继承 |
| navigate | chapter_id、content_version | 显式切换已采用章节 |
| prepare_part | chapter_id、content_version、part_id | 返回下一未确认片段；不确认已读 |
| confirm_part | 同上 | 只确认当前待讲片段；旧确认不回退/越级 |
| begin_exercises | chapter_id、content_version | 可附 confirm_last=true 或 skip_remaining=true，二者语义不同 |
| publish_question | 章节与题目标识、rubric_version、prompt、checks、rubric | 发布核对过评分依据的题目 |
| record_help | 章节与题目标识、event_id、level、source | level=none/hint/solution；事后补报还需 target_submission_id、reason |
| submit_answer | 章节与题目标识、submission_id、answer | 完整原答先落盘；可附 replaces_submission_id |
| resolve_history | submission_id | 选择仅按旧版评价；不进入当前证据 |
| assess | submission_id、assessment_id、rubric_version、results、feedback、feedback_help | 保存逐项评价；更正需 supersedes、correction_reason |
| replace_rubric | 章节与题目标识、rubric_version、rubric、affected_checks、reason | 原子地发布新依据并让受影响证据待重评 |

所有定位字段必须明确；题目不能借相同标题或编号跨课程匹配。review 回传是只读操作，只需 course_id。

### 内容与来源

source_refs 是对象列表，每项含 source_id 和 locator。locator 从导入结果读取：MD/TXT 的 line:N，DOCX 的 paragraph:N / table:N，PDF 的 page:N，归档 HTML 的 block:N。不要凭想象生成定位。来源 versions 通过不同 source_id 表达。

goals 示例：

~~~json
[{"goal_id":"explain","label":"解释并使用平均速率","checks":[
  {"check_id":"compute","label":"计算并解释单位"},
  {"check_id":"boundary","label":"说明时间范围"}
]}]
~~~

parts 示例：

~~~json
[{"part_id":"part-1","text":"保存的完整讲义正文。原文与补充逐处标识。",
  "source_refs":[{"source_id":"source-v1","locator":"line:2"}]}]
~~~

### 题目与评分

checks 为本内容版本中必要 check_id 列表。rubric 示例：

~~~json
{
  "verified":true,
  "criteria":{"compute":{
    "required":"总路程除以总时间，给出正确单位。",
    "alternatives":"等价分数、公式变形及合理表述。",
    "critical_errors":"混淆位移、遗漏停留时间或错误单位。"
  }},
  "reference_answer":"完整参考解释。",
  "source_refs":[{"source_id":"source-v1","locator":"line:2"}]
}
~~~

只有实际核对资料后才使用 verified=true。讨论性问题不要假装已经确定标准。

完整作答请求示例：

~~~json
{
  "action":"submit_answer",
  "operation_id":"submission-unique-id",
  "submission_id":"submission-unique-id",
  "course_id":"course-unique-id",
  "chapter_id":"chapter-1",
  "content_version":"v1",
  "question_id":"question-1",
  "question_version":"qv1",
  "answer":"逐字保存的原答，可含中文、换行、引号与代码。",
  "state_revision":12
}
~~~

完整 JSON 写入文件再调用 apply；不要把 answer 放进 shell 命令。面板重试同一次作答保持同一 ID 和负载。重新作答使用新 ID，replaces_submission_id 关联同题已有原答，帮助历史保留。讲解阶段的聊天提前作答不会推进阶段。

results 按题目覆盖的全部 check_id 组织，每项必须含 outcome（meets/needs_work/uncertain）、reason、source_refs。assess 的 feedback_help 必须明确为 none/hint/solution；它影响后续同题作答，不反向改变原答帮助快照。脚本不评价语义，只校验和保存 Codex 的判断。

## 读取与恢复

schema 2 的 show 只返回正式紧凑清单与正文引用；read 按提交 ID/来源定位/讲义片段读取正文，history/retests 分页读取；review 返回章节、检查点、证据状态和待处理记录。课程列表可重建；损坏条目会显示错误，不静默隐藏。

所有写入均在短时本地文件锁内重读合并，保存上一有效版本，通过临时文件替换后回读。Linux/macOS 使用 flock，Windows 使用 msvcrt；不支持多设备并发共享目录。

    python /absolute/skill/scripts/learner.py backup-info --course COURSE_ID
    python /absolute/skill/scripts/learner.py restore --course COURSE_ID --backup-sha256 HASH --confirm

第二条命令必须在用户选择该备份后执行。恢复保留现有原文件，返回备份时间与可能缺失范围。commit_uncertain 表示写入可能已成功，重试同一请求；id_conflict 表示同 ID 数据不同，不能直接覆盖。


## 0.2.0 读取、核验与资源控制

所有正文引用含 path、sha256、byte_length 和 encoding；course.json 不再重复嵌入正文。读取失败时返回具体错误，不能用空内容继续教学。列表和面板只读取摘要/当前章，history 默认元数据分页，include-bodies 明确读取当前页。

    learner.py read --course COURSE_ID --submission SUBMISSION_ID
    learner.py read --course COURSE_ID --source SOURCE_ID --locator page:3
    learner.py read --course COURSE_ID --chapter CHAPTER_ID --part PART_ID
    learner.py history --course COURSE_ID --page 1 --page-size 20
    learner.py history --course COURSE_ID --page 1 --include-bodies
    learner.py retests --course COURSE_ID --kind transfer
    learner.py export --course COURSE_ID --output /absolute/full.json

`verify_source` 可附 expected_source_sha256 与 expected_segment_hashes（locator→SHA-256），防止用旧页面印象核验新正文。它追加 source_verifications 事件，不覆盖旧核验记录。迁移后的 legacy_claim 来源须逐定位复核，旧 verified 声明不自动升为可信证据。

复测题使用新 question_id，retest_context 结构如下，不接受人为指定已过天数：

```json
{"baseline_submission_id":"first-answer","kind":"delayed","context":"隔日用新数据解释同一原理"}
```

kind 可为 delayed 或 transfer；基线必须来自同章、同内容版本且覆盖相同检验项。提交时根据基线与新原答的 received_at 计算 elapsed_seconds。旧的任意 retest_context 保留为 legacy_unclassified，不伪造时间证据。

### 解析参数

import_source 的普通来源字段不变，另支持：

- profile：auto/native/rapidocr/docling。已配置的默认轻量 OCR 在扫描页运行，文字层优先；Docling 需明确选页。
- page_range：[first,last]（1-based，含首尾）；或 page_numbers：[1,3] 显式页列表，ocr_pages 为兼容别名，与范围互斥。原页号不因过滤而重排。
- memory_budget_mb：不超过配置值及 2048 MiB；配置默认为 2048。预算不能自动提高。
- resume：沿用匹配原件/模型/参数哈希的完成页检查点；变化后的输入不能复用旧结果。
- crop：{page_no,rect:[left,bottom,right,top],dpi}，原 PDF 坐标，仅 RapidOCR 单页，dpi=72–300（默认150）；从原 PDF 局部重新渲染，仍受像素上限限制。
- 旧参数 ocr=true、force_ocr 和 ocr_pages 保持兼容。页号必须为正整数，resume/force_ocr 必须为布尔值。

OCR Python、离线模型、缓存位置来自运行配置，不混入操作指纹。运行结束退出模型进程；失败保留检查点、停止原因和资源数据，不能把部分结果声明为全文完成。普通导入也有提取量、页面/段落和时间上限，超限明确报错。

### 显式迁移与历史归档

    learner.py migrate --course COURSE_ID --preview
    learner.py migrate --course COURSE_ID --expected-sha256 HASH --confirm
    learner.py archive-create --course COURSE_ID --output /absolute/course-r42.zip
    learner.py archive-copy --archive /absolute/course-r42.zip --drive-directory /absolute/private-drive
    learner.py archive-confirm-download --archive /absolute/course-r42.zip --download /absolute/web-download/course-r42.zip --sha256 HASH --source-declaration web_download --output /absolute/verification.json
    learner.py archive-release --course COURSE_ID --archive /absolute/course-r42.zip --verification /absolute/verification.json --confirm
    learner.py archive-restore --archive /absolute/web-download/course-r42.zip --sha256 HASH --confirm

迁移/释放/恢复仅对用户明确选择的课程执行。复制到 Drive 不是云端同步证明；没有网络观测能力时，网页重新下载的来源由用户声明，程序只验证独立文件、哈希和内容。归档释放拒绝待评/待重评/运行任务/未归档文件，保留轻量索引。详情见发布包 docs/v0.2-upgrade.md。

归档恢复异常保留 `phase`、`original_error`（包括可用的 `errno/winerror/filename/filename2`）、源/目标路径、`rollback` 及 `stage_path/staging_preserved`。收到 `commit_uncertain` 时保留现场并核对状态，不自动删除暂存目录或覆盖目标；`restore_failed` 的回滚结果按回执读取。已启用课程的清理失败在完整回读确认后仍返回 `course_restored`，附 `warning/diagnostics`，不能再次导入或向学习者报告课程尚未恢复。
