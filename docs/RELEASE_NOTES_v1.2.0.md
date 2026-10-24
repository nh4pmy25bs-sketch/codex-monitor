# Codex 监控台 v1.2.0

在 Windows 本机查看服务器 Codex 的输入输出、每日 token 用量和文件修改，并在每轮完成时收到桌面通知。

## 功能

- 普通 SSH 密码 / 私钥与 Tailscale SSH 连接。
- Tailscale 直接网页登录与按需服务器验证，密码可查看。
- 会话搜索、命令与补丁查看、每日 token 和 CSV 导出。
- 增量同步、历史后台补齐、VS Code 终端 Codex 完成提醒。
- 数据保存在本机，服务器上无需安装常驻服务。

## 下载

Windows 10 / 11 x64 下载 `CodexMonitor-v1.2.0-windows-x64.zip`，完整解压后运行 `CodexMonitor.exe`。保留 `_internal` 文件夹；无需安装 Python。源码用户下载 `CodexMonitor-v1.2.0-source.zip`。

服务器需要 Python 3；Git 差异需要 Git；Tailscale 连接需本机系统 OpenSSH 和已可用的 Tailscale 网络。登录网页不会自动配置本机网络。

统计以保存的日志为准，不代表官方账号剩余额度或账单。通知需保持后台服务运行。升级前备份 `data/`，请勿上传自己的记录和凭据。程序未附代码签名。原创代码采用 MIT，第三方许可与 Paramiko 对应源码随包提供。
