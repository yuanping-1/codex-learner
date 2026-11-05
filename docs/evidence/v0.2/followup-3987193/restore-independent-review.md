# 3987193 恢复证据独立审阅

本报告只读检查已验证封包、固定版本源码和官方 CPython 3.12.10 实现快照；没有执行上传的辅助脚本，没有重新运行 restore 或 60 次规模基准，没有修改产品及原始证据。

## 可以确认的新通过范围

独立脚本 `byte-audit.py` 重新核验 121 个成功样本：旧 S05 精确故障链 1 个、无探针首调 60 个、独立探针诊断 60 个。5,259 个清单文件、合计 5,361,024 字节的长度/SHA 均与各自 manifest 和原始 S05/F06 ZIP 一致。当前/上一快照及 JSON 回执的全部传递引用得到检查，且覆盖清单成员。完整结果为 `byte-audit.json`，不是对用户汇总数字的简单转抄。

无探针首调每个样本仅一个 primary-first，准备恢复数为 0，无自动重试；两组各 30 PASS。旧 S05 链中索引注入命中一次、实际 phase=write_index、预期原错与 completed 回滚相符，之后同 PID、同库、同 ZIP 正常恢复；索引在注入前后的实际字节相同。该链可以将 **3987193 本轮** 对应项标 PASS，不能改写 02c922d 的历史未完成记录。

60 个探针诊断恢复全部成功，60 份探针报告的自然事件与观察器错误均为 0。因此可以确认诊断协议执行，但没有捕获自然失败现场，不能据此排除外部占用或权限等原因。

## 自然失败的确切序列与一项重要纠正

原记录位于 `regressions/test_archives.json`，用例名称为 `test_release_failure_before_first_unlink_can_restore_matching_residue`。固定版本 `tests/test_archives.py:360–379` 的序列是：生成归档、复制下载并核验；将 Path.unlink 临时包装为**仅在删除 course.json 时抛错**；调用 release；检查 course.json 仍存在且索引阻止普通 load；解除注入后调用 restore。该 restore 在 `archives.py:676` 的 stage_course.rename(destination) 出现自然 WinError 5。错误为 publish_course、rollback completed、restore_residue completed，返回后 stage_exists=false，staging_preserved=false。

**用例名称不等于真实状态。此次不是“任何文件删除前”。** 固定格式的 MANIFEST.files 使用排序键，course.json 前有 15 个 bodies/* 成员。实际失败夹具恰好缺少这 15 个正文文件，其他 24 个清单成员的长度/哈希全部一致；缺失列表精确等于 manifest 中 course.json 之前的前缀。这与 release 循环按 manifest 顺序先删除正文、随后在 course.json 注入停止完全对应，不是从证据缺失猜测执行过程。

保留夹具相对位置为：

`regression-failed-fixtures/test_archives/test_archives.ArchiveTests.test_release_failure_before_first_unlink_can_restore_matching_residue/0/`

其中原 archive 和 web-download 的 ZIP 字节相同，SHA-256 均为 `ede14f3b361e3f148dc63111598daf79151edce4662ed926061565b645494ea4`。清单共有 39 个成员，course.json 在零基索引 15；归档索引保持 archived、修订号 7 和同一归档 SHA。该夹具共 31 个文件，未保留 stage 目录。

所以本次异常必须描述为：**删除 15 个正文后、删除课程清单前的部分释放残留，在随后恢复发布目录时失败**。它仍是实际产品恢复失败，不能被改称测试 mock 问题，也不能被后来 60 个无探针成功抵消。

## 资源生命周期检查

未从固定版本中发现可确认的产品自有资源泄漏：

- 解压的 ZIP、输入成员、输出流、文件哈希流均有上下文关闭；stage 校验的清单读取也在返回前关闭。
- release 的 library/course 两个 file_lock 在 finally 解锁，外层 with 关闭文件流，即使 Path.unlink 的注入抛错也走退出路径。restore 发布期间持有的 library/.library.lock 位于被移动课程子树之外。
- release 中目录 rglob 的集合构建和 restore 中残留校验 rglob 都在 publish 前耗尽；[CPython v3.12.10 pathlib](https://github.com/python/cpython/blob/v3.12.10/Lib/pathlib.py) 的 wildcard selector 会先 list(scandir) 并退出 with，walk 也在 yield 前关闭 scandir。保留 DirEntry/生成器/Python 异常对象不等于保留一个仍打开的目录句柄。
- Windows resolve 最终路径查询的 [CPython v3.12.10 posixmodule](https://github.com/python/cpython/blob/v3.12.10/Modules/posixmodule.c) 在 `_getfinalpathname_impl` 的 cleanup 中 CloseHandle。源码不能证明每个现场原生调用都成功关闭，当前也没有该类漏句柄证据。

这只排除了“源码中已经可见一个确定没有关闭的普通流”的简单结论。没有失败时 OS 句柄/共享模式或有效权限记录，不能排除 C 扩展/原生资源、外部进程、过滤驱动或瞬时状态；也不能凭 WinError 5 直接归因其中任何一项。

本轮失败与 02c922d 的五个发布失败落在同一个调用点，但准备状态/归档/调用上下文不同，不足以证明同一根因或计算有意义的跨轮失败率。硬链接建立失败发生在另一用例的 os.link 准备阶段，与这里分列。

## 下一项有区分力的诊断

当前 60 个独立诊断仅覆盖 F06 空库及 S05 正常 release 后的残留；正常 release 主要留下锁文件。它们没有覆盖本次 **正文已删、course.json 尚在的部分释放残留**。继续重复同两组直到全成功不会补上这项缺口。

建议另建固定数量、明确标记为诊断的新夹具，优先使用本次失败归档的确切 SHA，重现该实际中断点：记录每次 unlink 的目标/是否完成，断言 course.json 注入确实命中、前缀 15 个正文已删除、其余 24 个成员仍与归档一致、索引仍保护课程；随后同进程同库解除 release 注入，在 restore 调用期间启用既有失败观察器。观察器只在自然 rename 抛错后、原异常传播前采集，不重试、不延迟进入 rename、不拦截回滚，不把诊断加入无探针首调统计。若固定样本中仍没有自然事件，结果保持“未捕获”，不是根因解决。

同时补一个真正“第一个 manifest 成员 unlink 前”中断的独立回归，显式断言未删除任何文件；旧实际部分残留回归应保留并重命名。两种状态分开，不能仅修名字后丢掉真实发生过失败的状态。

已通过的 60 个首调与 S05 链可以引用现有结果；总体恢复仍 FAIL，自然现场仍未取得，正式性能按既定前置条件保持 BLOCKED。无需为了这份只读审阅重跑已通过的大组测量。
