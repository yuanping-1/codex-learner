# 0.2.0 修订候选：既有证据快照

**整理日期：2026-11-05（北京时间）。** 为避免包外材料仅保留在被忽略的 `artifacts/` 目录中，本目录保存 11 份既有材料的原字节副本，合计 393,385 字节。整理未重新执行测试，也未改变原结论。

| 身份 | 值 |
| --- | --- |
| 产品源提交 | `1c2565465dbdb43a10fc25a93b034011613d71bd` |
| ZIP SHA-256 | `1eb92400cc2858e86c529813c4eedf50099c17ab6083ca2fed79e06161de0df2` |
| 原 CI | [41954569713](https://github.com/yuanping-1/codex-learner/actions/runs/41954569713) |
| 修订用户本机验收 | PENDING |
| 完整交付 | PARTIAL |

## 内容

| 材料 | 用途 |
| --- | --- |
| [release-verification.json](release-verification.json) | 同包代码回归、CI、包身份与剩余项汇总 |
| [windows-job-validation.json](windows-job-validation.json) | 27 轮 Windows Job 时序、分配与清理结果，含日志哈希 |
| [package-verification.json](package-verification.json) | Linux 隔离安装及包校验；明确没有执行真实 Codex 聊天 |
| [recheck-task.md](recheck-task.md) | 绑定该修订包的既有本机复验任务书 |
| [revised-final-core.log](revised-final-core.log)、[revised-browser.log](revised-browser.log)、[revised-ocr.log](revised-ocr.log) | 原本地核心、浏览器模拟、OCR 协议/基准日志 |
| [windows310-core-final.log](windows310-core-final.log)、[windows312-core-final.log](windows312-core-final.log)、[windows-ocr-final.log](windows-ocr-final.log) | Windows 三组运行的原 CI 日志 |
| [revised-ocr-linux-validation.json](revised-ocr-linux-validation.json) | 修订代码真实 Linux OCR 的六次结果，复制自上一级既有记录 |

[evidence-manifest.json](evidence-manifest.json) 记录每份材料的原仓库相对位置、新位置、字节数和 SHA-256。清单覆盖上述 11 份输入，不包含清单自身和本说明。已核对 release 汇总引用的 7 份材料及 Windows 三组日志哈希。

## 阅读与交接边界

- 文件原字节保持，未重写 JSON 中的历史路径。按清单的 `source_path → snapshot_path` 映射查找；`/workspace` 和 `/tmp` 路径是当时合成环境的运行记录，不是用户安装位置。
- 原日志中的日期、命令和测试结果属于原运行，不属于本次整理。工程报告本身也未包含在已冻结 ZIP 中。
- 本目录没有原用户本机的完整验收 ZIP，也没有安装 ZIP 本体；不能用本快照声称独立复核了用户全部本机证据。
- SHA-256 用于绑定文件字节和检查意外变化，不是第三方签名或新增独立验收。
- 本目录已准备在仓库工作区；提交至版本控制和长期发布应在后续交付中完成。Actions 附件有保留期限，快照不延长安装包下载期限。

交接时连同本目录及 [工程报告](../../../project-engineering-report-2026-11-05.md) 保存。不要只复制报告中的相对链接。
