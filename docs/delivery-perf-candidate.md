# 性能修订候选：产品包与证据附件

当前软件版本字符串保持 `0.2.0`。性能修订使用文件名 `codex-learner-0.2.0-perf-<12 位提交号>.zip`，并以包内 `BUILD.json` 的完整提交号、成员清单及外部 ZIP SHA-256 共同确定身份。文件名不能替代校验，开发树未提交的文件也不能冒充该提交的发布产物；对外交付从干净提交构建。

产品附件同时提供 `*-recheck.md`：由实际 ZIP 校验后的成员、BUILD 提交和 ZIP SHA-256 生成，可直接交给本机 Codex 执行。生成入口为 `scripts/create_recheck_task.py --package <候选ZIP> --output <新任务文件>`；它不会覆盖不同内容的旧任务，也不把任务生成视为本机验收通过。

安装 ZIP 包含产品源码、安装与验收工具及说明，**排除整个 `docs/evidence/v0.2/revised-release/` 历史证据快照**。同一 CI 提供独立的 `codex-learner-0.2.0-perf-<提交号>-evidence.zip` 及其外部 SHA-256，内含完整历史快照、工程报告、评审采纳记录和明确允许的性能结果。

两份 ZIP 内均保留 `codex-learner/` 仓库相对目录。将匹配的两个附件解压至同一个新的父目录，即可按报告的相对链接读取证据；重叠文档应为相同字节。证据附件的 `EVIDENCE_MANIFEST.sha256.json` 覆盖其全部输入和 `EVIDENCE_BUILD.json`，清单自身由外部 ZIP SHA-256 绑定。它不包含安装 ZIP、模型、课程库、配置或凭据；完整软件和相关源码链接仍由产品包提供。

证据打包只收固定文件列表，不递归收集工作目录。历史快照按其原清单逐项核对，保留旧提交 `1c2565465dbdb43a10fc25a93b034011613d71bd` 和旧包 SHA-256 的结论；新附件的整理提交不把历史结果升级为新候选已通过。尚不存在的可选性能报告在 `EVIDENCE_BUILD.json` 中明确列为缺失，不能以打包成功代替实际验收。

证据附件还保留工程报告引用的 [原候选 37 项验收任务书](local-acceptance-handoff-v0.2.md)。它绑定旧包，仅供历史查阅，继续从产品 ZIP 排除，不能作为性能新候选的执行入口；新候选使用 [性能修订本机复验任务](performance-recheck-v0.2.md) 并核对本轮包身份。

仓库根目录的可复现入口：

```sh
python scripts/package.py
python scripts/package_evidence.py
```

默认输出使用当前提交号，避免写回旧 `codex-learner-0.2.0.zip`。已有冻结包、旧报告和原始失败证据应保留。两个入口均支持 `--output` 指定新的路径；不要用此参数覆盖冻结产物。Actions 附件保留期为 30 天，正式长期发布及用户本机验收仍需单独完成。
