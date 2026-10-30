# TG Cache Lens

macOS 原生 Telegram 的本地视频缓存 CLI 与独立 Mac GUI。扫描必须只读；导出到独立目录。

- 核心体验与验证边界见 `docs/verification.md`，安装与用户操作见 `README.md`。
- 完整长度确认和成功解码是不同证据；不得把未知长度、解码通过或文件大小当成完整性证明。
- 完整性未确认的文件不导出；导出前重新读取索引和内容。输出不得覆盖源文件或已存在文件。
- `uv run --extra gui pytest -q` 验证真实媒体、CLI 和 GUI。`scripts/capture_demo.py` 生成测试素材；公开 README 使用真实窗口和终端截图。
- 私人缓存、扫描报告、导出视频与本机路径不得进入提交或发布包。公开前参考 `docs/public-audit.md`。
