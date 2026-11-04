# Codex 对话学习器

在 Codex 聊天中围绕资料整章学习、练习、反馈和续学。学习记录保存在你本机的固定课程库。

**当前为 0.1.0 验收候选：Windows 43 项核心测试、E01 原生特殊文本保存、E03 新对话恢复均通过；2026-11-04 已完成正确修订的本机安装和原生 UI 复验。** 删除排队消息可取消，保留原稿和提交 ID 重试；缩短或清空草稿后缓存警告消失。最新为 4 份作答、4 份评价、修订号 15。真实发送失败恢复与综合教学验收尚未完成，整套仍为验收候选。开发环境是 Linux 云环境，目标运行环境是你的本机 Codex。详情见 [最新本机验收证据](docs/local-ui-acceptance-2026-11-04.md)。

此前实际调用旧脚本的问题已解决：修订包内脚本、已安装脚本及新面板内嵌脚本哈希一致，本机复验通过，原配置及隔离库全部 10 个文件保持不变。已有核心测试和 E01 未重跑。[本机版本核对与修订复验](docs/local-ui-recheck.md)保留为已执行流程，无需重复。

本版包括来源归档、分段讲解检查点、原答先保存、反馈与帮助记录、逐项证据汇总、版本隔离、重复提交去重、备份恢复及交互面板。没有常驻 Web 后台、独立模型服务或云同步。

## 在 Windows 本机安装并验收

需要 Python 3.10+，以及支持对话内可视化的 Codex 客户端。Codex CLI/IDE 不提供所需面板渲染。解压安装包，在项目根目录打开 PowerShell。

先使用独立样例库，避免验收答案进入个人学习记录：

~~~powershell
python -m pip install -r requirements-pdf.txt
python -m unittest discover -s tests -v
$learnerSample = Join-Path $env:USERPROFILE "Documents\Codex\codex-learner-acceptance\sample-library"
python scripts/create_demo.py --library "$learnerSample"
python scripts/install_skill.py --library "$learnerSample"
~~~

安装器默认将 Skill 放入用户目录下的 .agents/skills/codex-learner，保存课程库绝对路径和 Python 路径，不修改全局 Codex 配置。相同内容重复安装不会改写文件；已有不同 Skill 会停止并保留原件。确需替换时使用 --backup-and-replace，旧版备份在 .agents/skill-backups，课程库不会被删除。

随后在本机 Codex 显式选择 **codex-learner**，发送：

> 使用配置中的隔离样例库，打开“比例与单位 · 验收样例”。先确认当前客户端可渲染交互面板，显示第一题面板供我提交答案。

提交包含中文、换行和引号的一份答案，核对回执与原答。然后由你在另一个工作目录新开一条 Codex 对话，选择同一 Skill，说：

> 继续“比例与单位 · 验收样例”，核对上一份完整原答、当前章节和待处理反馈。

如果原生渲染或回传不可用，按 [验收记录](docs/acceptance.md) 记为未通过；不要把静态 HTML 或复制请求当作原生验收通过。

T0 通过后，可切换到原计划中的正式课程库：

~~~powershell
$learnerLibrary = Join-Path $env:USERPROFILE "Documents\Codex\2026-10-26\codex\outputs\learning-library"
python scripts/install_skill.py --library "$learnerLibrary" --backup-and-replace
~~~

正式使用时说“打开学习器，用这些资料建立课程”或“继续某课程”，并在聊天中提供资料。Skill 先归档来源并建立目录，完整讲完一章后再练习。普通“继续”会恢复未确认片段；明确“本段已读”才推进检查点。评价区分本次练习达标、在帮助下完成、待补强和未评估。

## Linux/macOS 与开发验证

安装器支持其他本地系统；需要指定绝对或可解析的本地目录。例如：

~~~bash
python3 scripts/install_skill.py --library "$HOME/Documents/codex-learning-library"
~~~

本机文件锁不承诺多设备或网络盘协同。开发测试：

~~~bash
python3 -m unittest discover -s tests -v
python3 -m pip install -r requirements-dev.txt
python3 -m playwright install chromium
python3 -m unittest discover -s tests/browser -v
~~~

核心使用 Python 标准库。pypdf 用于 PDF 读取；有 pdftotext 时也可读取 PDF。Playwright 只用于浏览器测试。扫描页、图表和公式需要实际核对，提取失败会明确记录。DOCX 保留段落/表格位置；网页须先归档实际读取的 HTML 和 URL，不把链接当作已保存全文。

生成可分发安装包：

~~~bash
python3 scripts/package.py
~~~

安装包包含源码、Skill、安装脚本、样例和测试，不包含本机配置、课程记录或缓存。

## 资料与接口

- [完整原计划 v2.2](docs/plan-v2.2.md)
- [实现结构及环境调整](docs/implementation.md)
- [E01–E25 验收记录](docs/acceptance.md)
- [记录接口与 JSON 请求](skills/codex-learner/references/operations.md)
- [教学 Skill](skills/codex-learner/SKILL.md)

所有正式写入以脚本回读为准。答案保存和反馈保存分两步；旧面板、重试及更正不会静默覆盖原答。状态损坏时先检查备份，只有用户选择后恢复，并保留原文件。
