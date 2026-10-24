# 维护者发布指南

## 上传仓库

建议仓库名 `codex-monitor`。将源码目录中的内容作为仓库根目录提交，包含 `.github`、`.gitignore` 等隐藏文件。不要把便携运行目录、`data/` 或整个交付外层目录上传到源码仓库。

可使用 GitHub Desktop：添加源码目录为本地仓库，检查文件列表，提交后选择 Publish repository。尚未有账号时先在 GitHub 注册并登录。发布公开或私有仓库由维护者选择。

## 直接发布已有安装包

在 GitHub 的 Releases 新建版本，标签填写 `v1.2.0`，标题填写 `Codex 监控台 v1.2.0`。复制 `docs/RELEASE_NOTES_v1.2.0.md` 内容作为说明，上传交付包 `releases/` 中的 Windows ZIP、源码 ZIP 和 `SHA256SUMS.txt`，检查后发布。

下载者需要 Windows ZIP。GitHub 自动生成的 Source code 包只含源码，不能直接作为 Windows 程序运行。

## 自行构建

按 README 安装构建依赖，再运行 `build.ps1`。脚本只打包白名单文件，不读取日常运行的 `data/`。`release/` 中生成两个压缩包及校验文件。Windows 包包含 `_internal`、许可文件和源码 ZIP；不要只上传 exe。

PowerShell 校验下载文件：

```powershell
Get-FileHash .\CodexMonitor-v1.2.0-windows-x64.zip -Algorithm SHA256
```

将输出与同一版本的 `SHA256SUMS.txt` 对照。

## GitHub Actions

- `CI`：每次 push / pull request 在 Windows Python 3.12 下安装锁定依赖并运行测试。
- `Windows release`：可在 Actions 中手动运行以构建并下载附件；推送 `v*` 标签时，额外创建含附件的 **Draft Release**，由维护者检查后发布。
- 标签必须与 `core.py` 的 `VERSION` 一致，例如 `v1.2.0`。自动发布任务仅为写入 Release 请求 `contents: write`；测试不接触真实服务器。
- 仓库默认 Actions 权限、组织策略或网络限制可能阻止流程运行，需要维护者在仓库设置中处理。本地构建不依赖 GitHub Actions。

更新版本时修改 `core.py` 的 `VERSION`、界面中的版本文案、README、CHANGELOG 与对应的版本说明文件，再测试、构建并发布。二进制未附代码签名证书。

参考：[创建 Release](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)、[GitHub CLI release create](https://cli.github.com/manual/gh_release_create)。
