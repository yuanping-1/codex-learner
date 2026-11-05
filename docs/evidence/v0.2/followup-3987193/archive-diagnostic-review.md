# 定向部分释放诊断工具

新增 `scripts/diagnose_archive_regression.py`，只运行打包源码中两个白名单真实用例，默认一个新进程/新目录，显式 `--repeat` 上限10。`tests/support.py`增加可覆写临时目录和seed方法，默认行为保持。诊断子类把fixture持久保留；正式配置和已有库不进入运行路径。

- `--case partial`：新的真实测试创建归档，随后本地下载核验模拟、删除course.json前中断、归档保护拒绝、恢复；现有manifest顺序先删除15个正文后留下24项。实际数量以stdout证据为准。
- `--case full`：在manifest第一个成员删除前中断，留下完整归档成员。
- `--case partial --archive <原失败ZIP>`：仅接受SHA `ede14f3b361e3f148dc63111598daf79151edce4662ed926061565b645494ea4`。从受控manifest流式构建新的活跃fixture，再运行真实测试的confirm/release/guard/restore链；明确跳过新create_archive，不能与完整创建生命周期混称。

probe在seed后、测试方法前启用。现有测试额外核残留文件哈希，probe也扰动时序，因此只证明实际业务顺序与采集协议，不声称复现原故障时序。所有样本为diagnostic，first_call_acceptance_eligible=false，无自动重试。不捕获自然事件时保持NOT_CAPTURED，根因UNCONFIRMED；probe报告/采集错误令计数完整性false，无事件时UNKNOWN。setup失败不虚称已创建归档/保留fixture；实际创建attempt/completed和最终fixture存在性分别记录。

源码身份包括实际隔离产品模块、test_archives/support/create_demo/driver/probe/helper前后hash。worker先导入选定安装包，再加载本包测试。原LearnerError结构化字段、cause链和traceback保留；probe退出恢复patch后保留最终fixture快照并做完整离线读回。

Python3.10与3.12最终各10项单测全部通过，分别见archive-diagnostic-final-py*.json和stderr日志。覆盖真实部分/完整链、固定hash replay模式、原错和回滚前stage观察、patch恢复、setup/创建失败、collector报告与采集不完整UNKNOWN、输入保护及CLI新子进程身份。

最终源码另以实际原失败ZIP在Linux执行1次replay：PASS，39成员闭合读回，archive_creation_attempted=false，事件0，NOT_CAPTURED。材料位于archive-diagnostic-native-replay-final-linux及旁边命令/原始输出。此前单样本及早期6/7项开发检查保留，不替代最终检查。没有运行目标Windows或新的60次首调，不关闭自然WinError5和性能BLOCKED。
