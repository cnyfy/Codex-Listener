# Codex 自动恢复 TUI V1

这是 Codex 渠道状态监测和任务自动恢复工具的 V1 分享包。

本包不包含 Python。首次启动时会自动检查电脑上的 Python 3.10 或更高版本：

- 找到可用 Python：直接启动 TUI。
- 找不到 Python 或版本过低：打开 Python 官方下载页并显示安装提示。

使用方法：

1. 解压整个文件夹。
2. 双击 `start_codex_auto_recovery_tui.cmd`。
3. 在界面按 `S`，填写中转站地址和 API Key。
4. 默认是 dry-run；确认无误后再启用 Live。

快捷键：`S` 设置，`R` 刷新，`P` 暂停/继续，`D` dry-run，`A` 全选，`N` 清空选择，`Q` 退出。

分享包需要朋友自己的 Codex 环境和自己的中转站 API Key。API Key 会保存到 Windows 凭据管理器，不会写入设置 JSON。
