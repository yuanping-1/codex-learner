# Codex 对话学习器

在本机 Codex 中按资料学习、练习、反馈和跨对话续学。**0.2.0 候选版采用本机轻量课程库＋独立逐页 OCR＋Google Drive 历史归档**，无需服务器。云端只用于开发。

日常核心按需读取正文；默认 OCR 进程组预算为 **2048 MiB（2 GiB）**，不包含 Codex、浏览器或 Drive 客户端。Drive 用于节省长期磁盘空间，不代替运行内存。实际验收范围见 [0.2.0 验收记录](docs/acceptance-v0.2.md)；旧版本本机验收不能代替新包验收。

2026-11-05 本机报告将原候选包判为 **FAIL（20 通过、5 失败、12 阻塞）**。当前修订针对五个实测缺陷，尤其 Windows venv 启动时序下的 Job 限制失效；修复后的 CI 与原包/本机验收分别记录，详见 [失败与修复映射](docs/local-acceptance-2026-11-05.md)。

后续性能候选保留每次写入前的历史完整性检查，消除 JSON 回执的重复读取与哈希；新提交和幂等重试使用同一合成快照分别测量。结果与边界见 [性能修订报告](docs/performance-v0.2.md)，本机执行入口见 [定向复验任务](docs/performance-recheck-v0.2.md)。候选文件名包含提交号，产品包与完整证据分开交付，详见 [附件说明](docs/delivery-perf-candidate.md)。这些云端结果不替代修订包的用户本机验收。

## 安装与升级

基础环境支持 Python 3.10–3.12；独立 OCR 环境固定 Python 3.12。先下载并解压发布包，在项目根目录运行。以下以 PowerShell 为例：

```powershell
python -m pip install -r requirements-base.txt
$learnerLibrary = Join-Path $env:USERPROFILE "Documents\Codex\learning-library"
python scripts/install_skill.py --library "$learnerLibrary"
```

默认 Skill 安装到用户目录 `.agents/skills/codex-learner`；课程库必须位于普通本地磁盘，不能放入 Skill、虚拟环境或 Drive 同步目录。首次安装器只初始化明确指定的新库，安装不会上传、迁移或清理课程。

升级沿用现有库、Python、OCR 内存和 Drive 配置，不重新指定 `--library`：

```powershell
python scripts/install_skill.py --backup-and-replace
```

旧 Skill 会保留备份。然后用实际安装路径执行 `doctor`，核对版本、文件清单哈希、解释器、模型和配置。schema 1 课程只读，升级步骤见 [迁移与使用说明](docs/v0.2-upgrade.md)。

### 可选 OCR

模型与环境放在 Skill、课程库、Drive 之外。先预览，再显式安装和预取经过 SHA-256 校验的模型：

```powershell
python scripts/install_ocr.py
python scripts/install_ocr.py --python "C:\Path\To\Python312\python.exe" --install --prefetch
```

默认 RapidOCR＋pypdfium2，CPU 两线程、一个工作进程、每次一页。模型只在扫描页需要时加载，完成任务后进程退出。原件上限 32 MiB；默认 150 DPI、最长边 2000 像素、最多 300 万像素。读取文字层或 OCR 成功仍需按来源类型核对，表格、公式、图片和扫描结果不会自动变成可信评分依据。超预算、超时或提取超限明确停止，已完成页可从检查点继续。

Docling 为单独的可选复杂页环境，需要明确选择页面和离线模型清单；未验证其所有模型都能在 2 GiB 下运行，超限保留原件供核对。

## 学习与记录

在 Codex 中选择 **codex-learner**，说“打开学习器，用这些资料建立课程”或“继续某课程”。先讲解本章，确认末段或明确跳过后再练习。聊天中提前作答可以保存，不推进讲解阶段。

- 原答先保存，再写评价。反馈中断后恢复待评价记录。
- 同一请求重试沿用 ID；重新作答使用新 ID 并关联原答。
- 首次练习、延迟复测、新情境复测分别展示，间隔由实际记录时间计算。
- schema 2 清单保存关系和正文引用；原答、讲义、反馈按需读取，回执复用正文。
- 本机断网时，已取回课程和历史记录可读；未取回的历史归档需联网下载。

静态网页及模拟宿主测试不代表真实 Codex 客户端验收通过。

## Google Drive 归档

使用官方 Drive 桌面客户端的**流式**私有目录。活跃库与 Drive 分离，Drive 接收已闭合的版本化 ZIP。

顺序固定为：**生成一致快照 → 流式打包并校验 → 复制到 Drive → 确认客户端同步 → 从 Drive 网页另行下载 → 学习器重新校验 → 用户明确释放本地课程**。

复制成功只会显示 `sync_unconfirmed`。独立下载核验记录明确说明网络来源是用户声明，程序不会把同步目录或原包充当云端回读。释放后保留轻量索引；恢复先下载到本地、检查完整性和路径，再启用课程。同 ID 课程不会被静默覆盖。待评价、待重评或有运行任务的课程不能释放。

完整命令和失败恢复见 [归档操作说明](docs/v0.2-upgrade.md#google-drive-历史归档)。

## 开发与测试

```bash
python -m venv .venv
# 激活该环境后：
python -m pip install -r requirements-base.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m playwright install chromium
python -m unittest discover -s tests/browser -v
python scripts/benchmark_memory.py --output artifacts/memory.json
python scripts/package.py
```

安装包按清单打包，不包含课程、配置、凭据、缓存或模型。发布证据须绑定最终 ZIP 哈希；原生客户端、真实 Drive 传输和干净 Windows 安装结果分别记录。

- [0.2.0 迁移、OCR 和归档操作](docs/v0.2-upgrade.md)
- [0.2.0 验收记录](docs/acceptance-v0.2.md)
- [项目工程报告与功能状态](docs/project-engineering-report-2026-11-05.md)
- [性能修订与本机复验](docs/performance-v0.2.md)
- [完整原计划](docs/plan-v2.2.md)
- [记录接口](skills/codex-learner/references/operations.md)
- [教学 Skill](skills/codex-learner/SKILL.md)
