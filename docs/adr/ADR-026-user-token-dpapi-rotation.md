# ADR-026：用户令牌 Windows DPAPI 加密保存 + `ame token rotate`

- 状态：已采纳（2026-11-03，Phase 3a）。**Windows 部分未在实机验证。**
- 关联：ADR-017、SECURITY Q15

## 决定
1. Linux/macOS：保持 `<data_dir>/api_token`，权限 0600（不变）。
2. Windows：令牌用 DPAPI（CurrentUser，ctypes 调 CryptProtectData，无新依赖）加密，存 `api_token.dpapi`；
   旧的明文 `api_token` 在首次读取时转换，密文解密校验成功后才删除明文。
3. `ame token rotate`：生成新令牌原子替换（临时文件 + os.replace），不在屏幕打印；`ame token show` 打印当前令牌。
   运行中的服务继续用旧令牌，重启后生效。
4. 测试用假的加密器（tests/phase3a），真实 DPAPI 只能在 Windows 实机验证（验收 W22）。
