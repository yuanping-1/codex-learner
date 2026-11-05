# 3987193 跟进的云端原始检查

首轮完整Linux回归244项：236 PASS / 8 SKIP。实际命令、UTC、退出码和完整stdout/stderr一并保存；跳过项未计为通过。两版Python的定向测试和独立review另保存在云端完整证据附件中。

`validation.json` 绑定未改动的Skill、安装器、性能/恢复基准及原探针文件哈希。当前仅新增测试/诊断与文档；包外最终身份验证须核对最终候选。实际失败ZIP的Linux单次replay通过，但无自然失败事件，根因UNCONFIRMED、性能BLOCKED保持。

`input-verification.json` 是本次上传封包的独立核验；`byte-audit.json` 是121成功样本/5259文件及传递引用的重读核验。保护审计只比较用户提供的前后快照，不声称云端访问了正式本机文件。
