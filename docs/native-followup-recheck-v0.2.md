# 本机跟进：3987193 测试修订与部分释放现场诊断

本次仅跟进测试及诊断覆盖。先核验同次交付的完整提交、产品 ZIP 外部 SHA、BUILD 与包内清单，使用全新隔离安装、配置和合成库。正式 Skill/课程、旧 C04、旧报告及封包保持。不得修改 ACL、关闭安全软件或提权来获得通过。

## 1. 身份与输入

按产品包的 `scripts/install_skill.py --destination <新Skill> --library <新库> --python-executable <测试Python>` 安装，显式配置运行 doctor。使用之前一致的 Python 3.12.10，记录原始输出/退出码和 UTC 起止。逐文件比较新包的 Skill、安装器及存储/恢复/性能脚本与 3987193；本轮这些文件应相同。新包身份不得用 3987193 或文档 HEAD 代替。

从已核验的 `3987193` 本机证据 ZIP 取得此次真实失败归档：

```text
regression-failed-fixtures/test_archives/test_archives.ArchiveTests.test_release_failure_before_first_unlink_can_restore_matching_residue/0/archives/course.zip
```

其 SHA-256 必须为 `ede14f3b361e3f148dc63111598daf79151edce4662ed926061565b645494ea4`。它不是旧 F06 或 S05 ZIP。原证据 ZIP SHA-256 为 `4352fbad3b8b613b385fce0e94e43561795fe8a26a224a7d9400f087ee74de06`。只读取原件，所有诊断输出另建目录。

## 2. 受影响测试

在候选源码目录执行以下三组，各自保存命令、stdout/stderr、退出码、实际测试数和 SKIP 原因。不要用共享 subprocess.run mock 包裹这些命令：

```powershell
$candidate = "<新候选解压根目录>"
$testPython = "<本次隔离venv的Scripts\python.exe>"
$skill = "<本次新Skill目录>"
$runRoot = "<本次新证据工作目录>"
$failedArchive = "<上面的原失败course.zip路径>"
& $testPython -m unittest discover -s "$candidate\tests" -p test_benchmark_restore.py -v
& $testPython -m unittest discover -s "$candidate\tests" -p test_archives.py -v
& $testPython -m unittest discover -s "$candidate\tests" -p test_archive_diagnostic.py -v
```

mock 修订必须通过冷缓存辅助子进程用例，不能靠先跑其他套件预热平台信息。硬链接能力不足只影响独立硬链接子项；旧 ERROR 保持，新的 SKIP 不算产品通过或覆盖完成。如果能力探测成功、实际归档硬链接仍失败，保留 ERROR，不自行改成 SKIP。

部分释放用例现在叫 `before_course_manifest_unlink`：验证清单前缀已删除而 course.json 尚在；新 `before_first_manifest_member_unlink` 验证所有成员仍在。任意提前错误不得冒充预期注入。归档回归若出现自然失败，保存实际取得的原始输出和 traceback，仍继续独立可执行的现场诊断；不要覆盖失败重跑求通过。标准 unittest 不保证保留结构化异常和临时夹具；持久夹具及探针保证仅属于下方诊断脚本，后续诊断现场不能归给先前回归中的失败。

## 3. 固定数量的定向诊断

三个输出目录必须不存在。每个样本的新合成夹具均持久保留，无需另写清理钩子：

```powershell
# 原真实测试：包括新建归档的全过程，5个独立诊断。
& $testPython "$candidate\scripts\diagnose_archive_regression.py" --scripts-root "$skill\scripts" --output-dir "$runRoot\partial-test-flow" --case partial --repeat 5

# 固定失败ZIP：同一业务中断点，5个独立诊断，明确跳过新建归档。
& $testPython "$candidate\scripts\diagnose_archive_regression.py" --scripts-root "$skill\scripts" --output-dir "$runRoot\partial-original-archive" --case partial --archive $failedArchive --repeat 5

# 真正首成员删除前的完整残留分支，1个独立诊断。
& $testPython "$candidate\scripts\diagnose_archive_regression.py" --scripts-root "$skill\scripts" --output-dir "$runRoot\full-residue" --case full --repeat 1
```

这 11 个样本是预先确定的诊断，不是重试或首调验收。失败不覆盖，输出目录不复用，不临时追加次数追求通过或捕获事件。若出现超时/中断，保留实际进度与未知状态，不能由没有完成回执断言产品从未进入。

检查 fixture_origin、archive_creation_expected/attempted/call_completed、实际产品模块/测试源码/驱动哈希、原错误/cause、probe 事件与采集错误。原失败归档 replay 应验证 39 个成员，course.json 前缀的 15 个正文删除，剩余 24 个成员一致，再执行恢复；39/15/24 仅绑定该固定归档。full 命令新建归档，应验证删除 0、剩余数量等于该样本完整清单，不能套用固定 ZIP 的成员数。

新断言的文件核验和探针改变时序；snapshot 是顺序观测，不是原子现场。默认不运行 ACL 子进程；只有已取得事件且确有必要补充权限观测时，再单列预先确定的诊断并显式启用 `--collect-acl`。不查询或关闭外部进程句柄，不把 UNKNOWN 写成无占用。

## 4. 保留已通过证据与性能门槛

本轮产品字节不变时，不自动重跑 3987193 的 60 首调、原 S05 精确链或另 60 个正常释放诊断。引用其原始身份/结果并注明新包未重复执行；工具更新和产品等价证明分别保存，不能把历史运行改写成新包运行。

本轮不执行正式性能或 8 样本性能诊断，不用 mock 修复、环境 SKIP 或无自然事件来解除恢复正确性前置条件。自然 WinError 5 根因仍需实际事件和区分性证据。固定诊断无事件且 event_count_complete=true 时结束为“未捕获”；采集错误、超时或提前失败按实际结果保留 UNKNOWN/NOT_STARTED，不能一律改成“未捕获”。不再盲刷已通过场景。OCR、真实面板、Drive、Docling、完整进程组采样和正式升级不在此次范围。

## 5. 交付

交付中文报告、结构化结果、全部原始命令及样本、保留夹具、实际保护对照、逐文件长度/SHA 清单和证据 ZIP 外部校验值。分列测试 mock 修复、硬链接能力/真实覆盖、部分与完整残留、原ZIP replay、自然事件采集、根因、性能门槛及原件保护。任何此前 FAIL 保留；新工具通过不等于恢复机制已修复或0.2.0整体交付通过。
