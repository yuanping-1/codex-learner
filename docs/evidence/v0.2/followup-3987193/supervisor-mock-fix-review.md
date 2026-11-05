# 3987193 调度测试 mock 范围问题修订

## 证据与定位

回传 `supervisor-mock-call-diagnostic.json` 记录五次 mock 调用：一次 Windows `ver` 平台查询，随后四个计划中的 restore worker。首个查询通过 `platform.system → uname → win32_ver → _syscmd_ver → subprocess.check_output → subprocess.run` 到达同一个被替换的共享模块函数，先被加入 worker 调用列表，再因缺 `--output-dir` 抛出 ValueError。其副作用使预设失败/超时序号前移。该证据解释本次单独执行的测试失败，不能解释自然发布 WinError 5，也不能据此补造 CI 缓存状态。

## 最小修改

仅修改 `tests/test_benchmark_restore.py`：测试替换 `benchmark_restore` 模块自身的 `subprocess` 依赖引用，提供模拟 `run` 与真实 `TimeoutExpired`。不修改共享标准库模块中的 `run`，因而平台查询保留自己的实际调用路径。不改 `scripts/benchmark_restore.py` 或产品代码，不添加元数据预热，也不更改首调计数、FAIL/timeout序号和原始输出断言。

新增回归显式清空 `platform._uname_cache`；元数据查询通过真实 Python 辅助进程执行 `check_output`，确认其不消耗四个模拟 worker 的序号。随后仍调用实际 `platform.system`，在 Windows 可执行真实冷缓存查询。Linux 辅助进程只验证共享 subprocess 边界，不声称执行了 Windows `ver`。补充断言确认测试结束后依赖和标准库函数均已恢复。

## 验证

Python 3.12 与 3.10 的 `test_benchmark_restore.py` 各10项全部通过，日志及UTC/命令位于 `supervisor-mock-fix-py312.*`、`supervisor-mock-fix-py310.*`。没有运行60次首调或正式性能比较。

另做一份预期失败对照：只在内存中把新回归恢复成旧的共享模块mock，辅助查询再次被当作worker并因缺 `--output-dir` 产生ValueError；原始测试源码未改写。该对照保存于 `supervisor-prior-scope-control.json/.log`，属于明确标注的Linux回归有效性检查，不是原本机失败的重新采样或替代材料，也不复现完整Windows错误吞吐路径。

用户本机原套件FAIL保持；新测试仍需目标Windows运行。此测试修订不改变已通过60次首调/旧S05链的产品字节，不关闭自然恢复失败或正式性能BLOCKED。
