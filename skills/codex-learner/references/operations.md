# 记录接口

Python 3.10+。核心使用标准库；PDF 需要 pypdf 或 pdftotext。配置文件在 Skill 根目录，包含 schema_version=1、绝对 library_root 和可选 python_executable。

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

完整 JSON 写入文件再调用 apply；不要把 answer 放进 shell 命令。面板重试同一次作答保持同一 ID。重新作答使用新 ID。

results 按题目覆盖的全部 check_id 组织，每项必须含 outcome（meets/needs_work/uncertain）、reason、source_refs。assess 的 feedback_help 必须明确为 none/hint/solution；它影响后续同题作答，不反向改变原答帮助快照。脚本不评价语义，只校验和保存 Codex 的判断。

## 读取与恢复

show 返回正式状态、原答、完整评价历史和来源摘录；review 返回当前章节、检查点、各项证据状态、待处理记录。课程列表可重建；损坏条目会显示错误，不静默隐藏。

所有写入均在短时本地文件锁内重读合并，保存上一有效版本，通过临时文件替换后回读。Linux/macOS 使用 flock，Windows 使用 msvcrt；不支持多设备并发共享目录。

    python /absolute/skill/scripts/learner.py backup-info --course COURSE_ID
    python /absolute/skill/scripts/learner.py restore --course COURSE_ID --backup-sha256 HASH --confirm

第二条命令必须在用户选择该备份后执行。恢复保留现有原文件，返回备份时间与可能缺失范围。commit_uncertain 表示写入可能已成功，重试同一请求；id_conflict 表示同 ID 数据不同，不能直接覆盖。
