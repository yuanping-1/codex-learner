# 本机任务：路径校验优化及恢复现场诊断

本任务在收到新候选产品 ZIP、包外 SHA-256 和完整源提交后执行。它替代本轮入口，不重跑 OCR、Docling、面板、Drive 或教学验收。正式 Skill、配置、课程、C04、旧报告及旧封包保持。全部写入本次新隔离目录，不能用重试成功改写首次失败。

## 1. 固定输入与安装

1. 核验包外候选完整提交/ZIP SHA、包内 `BUILD.json` 与全部清单成员。不能将 `02c922d` 或文档 HEAD 当成新候选。
2. 从已核验旧证据的 `inputs/` 取得下列原合成归档和固定基线。归档驱动硬编码这两份 SHA；输入不符必须停止相关运行，不能换新造归档。

| 输入 | SHA-256 |
| --- | --- |
| `F06-original-synthetic.zip` | `3470e1d590dc1b75e38fe2dc8e0dc709f7013ae240da0842bc11241be6d71a7e` |
| `S05-original-synthetic.zip` | `1466225d47ad3a83f04d9b3f485b8465a9965fe5cb00299c0d5bc420d691b70a` |
| 基线 `1c2565465dbdb43a10fc25a93b034011613d71bd` 产品 ZIP | `1eb92400cc2858e86c529813c4eedf50099c17ab6083ca2fed79e06161de0df2` |

3. 用与旧本机比较一致的 Python 3.12.10 创建新 venv，安装新包 `requirements-base.txt`。保存 Python/OS/可用内存和所有命令 UTC 起止、stdout、stderr、退出码。安装到新目标目录，显式指定新配置和新合成库：

```powershell
# 以下变量由本机 Codex 按本次新工作区实际位置设定，不沿用正式安装。
$candidate = "<新包解压后的 codex-learner 根目录>"
$baseline = "<固定基线解压根目录>"
$runRoot = "<尚不存在的新工作目录>"
$testPython = "<本次新 venv 的 Scripts\python.exe>"
$f06 = "<已核验 inputs\F06-original-synthetic.zip>"
$s05 = "<已核验 inputs\S05-original-synthetic.zip>"
$skill = Join-Path $runRoot "installation\codex-learner"
& $testPython "$candidate\scripts\install_skill.py" --destination $skill --library "$runRoot\installation-library" --python-executable $testPython
& $testPython "$skill\scripts\learner.py" --config "$skill\config.json" doctor
```

逐文件比对安装清单与候选产品，记录实际模块哈希。源码测试与隔离安装 API 的身份分别记录。

## 2. 先做受影响回归

在候选源码根目录，以新 Python 运行下列测试，各自保留日志/退出码，不因调度器 exit 0 把失败子命令计为通过：

```powershell
$patterns = @("test_blob_paths.py", "test_blob_integrity.py", "test_v2_storage.py", "test_archives.py", "test_archive_restore_failures.py", "test_restore_probe.py", "test_benchmark_restore.py", "test_history_profile.py", "test_benchmark_history.py", "test_resource_limits.py")
foreach ($pattern in $patterns) { & $testPython -m unittest discover -s "$candidate\tests" -p $pattern -v }
```

确认实际匹配到测试，不能把 0 tests 当通过。保留 junction/symlink 权限不足等真实 SKIP，不改变 ACL 或提权以获得通过。清理/lstat 双故障时 null 表示未知；解锁注入测试必须记录是否到达注入点和完整原错。出现影响正确性的失败后，后续性能验收保持 BLOCKED，独立错误采集仍可继续。

## 3. 原 S05 精确故障链与独立首调

以下命令的输出目录都必须不存在。驱动先校验固定归档身份，再建新合成库；不对正式库执行释放。它使用隔离安装 Python API，不是 CLI/原生宿主/真实 Drive 验收。

```powershell
& $testPython "$candidate\scripts\benchmark_restore.py" --scripts-root "$skill\scripts" --f06-archive $f06 --s05-archive $s05 --output-dir "$runRoot\fault-chain" --mode fault-chain
& $testPython "$candidate\scripts\benchmark_restore.py" --scripts-root "$skill\scripts" --f06-archive $f06 --s05-archive $s05 --output-dir "$runRoot\first-calls" --mode first --repeat 30
```

- `fault-chain`：原 S05 归档直接建立活跃夹具，本地核验模拟后显式 release；初始化不调用 restore。先注入 `_write_index`，必须确实命中、捕获指定原错且 `phase=write_index`，回滚/索引正确后，同进程、同库、同 ZIP 解除注入正常恢复。提前发布失败计 FAIL，下游链 BLOCKED；不能重建夹具冒充完成。
- `first`：F06 空库与 S05 释放后各 30 个新子进程/新目录，每个只有一次首调，无重试。已失败样本保留，后续独立样本仍计数。每组必须全部 30 次 PASS 才能关闭该组在新候选上的本轮观察；不能外推长期可靠性。
- 成功不仅需要回执，还需逐文件字节数/SHA、当前与上一快照全部传递引用、展开正文/回执及来源读回一致。socket 拒绝只覆盖工具内本地读回，不证明全系统断网。
- worker 命令、UTC、PID、解释器、输入和产品模块哈希、调用前后快照、产品原异常/回溯与验收器错误分列。进程超时/中断或准备失败单列，不能当成执行过一次产品首调。

## 4. 发布失败的现场采集单独进行

如果无探针样本发生发布失败，保留原目录与失败，另建诊断目录运行：

```powershell
& $testPython "$candidate\scripts\benchmark_restore.py" --scripts-root "$skill\scripts" --f06-archive $f06 --s05-archive $s05 --output-dir "$runRoot\rename-diagnostics" --mode diagnostic --repeat 30
```

默认轻量采集发生在真实 rename 抛错后、产品回滚前。只有需要补充 ACL 且环境允许时，另建目录运行同命令并追加 `--collect-acl`。ACL 只读、限时；无需授权提升、修改权限或关闭安全软件。若没有自然失败事件，结论是“未捕获”，不能伪造或用模拟错误定位自然根因。`PermissionError` 注入测试仅验证探针行为。

诊断样本不能加入首调统计。采集本身延长回滚前窗口，可能改变时序；源/目标属性是非原子、顺序观测。Python 流仅覆盖本上下文观察到的 open；外部/原生句柄及锁拥有者 UNKNOWN 不等于不存在。不得用 sleep/自动重试或换目录后成功直接认定原因。

## 5. 新性能路径的正式比较

本轮 `blobs.py` 实际改变了提交路径，需新正式样本。回归和其他资源任务结束后，先做无探针对比，再单独诊断；不得与恢复/测试/打包并发。

```powershell
& $testPython "$candidate\scripts\benchmark_history.py" --records 1000 --repeat 30 --warmup 3 --operations submit,retry --baseline-root $baseline --output "$runRoot\performance-comparison.json"
& $testPython "$candidate\scripts\benchmark_history.py" --records 1000 --operations submit,retry --profile-only --baseline-root $baseline --output "$runRoot\performance-profile.json"
```

基线/候选使用同解释器、相同合成起始快照。分别保存每版本每操作 3 个预热与 30 个正式尝试，所有失败和中断保留，不补样覆盖。诊断是独立 8 样本，`summaries=[]`、`operations=[]`、`passed=null`，不能混入正式分位数。目标仍为新提交中位数 ≤2 秒/P95 ≤5 秒、峰值 <256 MiB；不提高目标。每次写入完整字节/传递引用校验、原答保真与同 ID 幂等必须保持。

如果条件阻止正式运行，给出 BLOCKED 和实际原因。单个有探针样本变快或云端 Linux 通过不能关闭目标机时延 FAIL。嵌套路径/元数据计时不相加，应用读取不当作物理磁盘统计。

## 6. 交付与结论

交付中文报告、结构化结果、全部原始命令/样本/失败现场、保护前后对照、逐文件路径/长度/SHA 清单和证据 ZIP 外部 SHA。分别报告：恢复异常分支、无探针目标机稳定性、原 S05 精确故障链、诊断完整性、正式性能、原件保护。新旧结果分列，旧 `02c922d` 的历史 FAIL 永久保留。

正式安装不升级。未取得新证据的 OCR、面板、Drive、Docling、完整进程组采样仍 BLOCKED，0.2.0 整体不标 PASS。详细修订依据见 [工程审阅](native-repair-02c922d.md)。
