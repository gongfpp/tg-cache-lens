# 同类项目与推广观察

调研日期：2026-11-04。以下按公开页面描述比较，没有对其他项目进行独立功能测试；不能据此判断实际成功率或性能。

## 有一定规模的相关项目

以下 Star / Fork 数于 2026-11-04 从 GitHub API 查询，会随时间变化。它们以在线下载为主，适合参考产品和传播方式，不应被当成与本项目完全相同的本地缓存恢复工具。

| 项目 | Star / Fork | 公开定位及值得参考之处 |
|---|---:|---|
| [tdl](https://github.com/iyear/tdl) | 8,240 / 833 | Telegram 命令行工具箱，提供下载、上传、转发和导出；首页有中英文入口、功能列表、预览和独立文档。 |
| [Telegram-Media-Downloader](https://github.com/Neet-Nestor/Telegram-Media-Downloader) | 6,391 / 636 | Telegram Web 的用户脚本；通过 GitHub 和 Greasy Fork 提供安装入口。 |
| [tangyoha/telegram_media_downloader](https://github.com/tangyoha/telegram_media_downloader) | 5,643 / 657 | 下载 Telegram 会话中的媒体，提供 Web 状态界面和 Bot 工作流；首页有中英文文档、反馈入口和 Telegram 社区链接。 |
| [Dineshkarthik/telegram_media_downloader](https://github.com/Dineshkarthik/telegram_media_downloader) | 2,759 / 402 | 基于 Telegram 会话下载媒体，提供 Web UI；使用上次读取记录避免重复下载。 |
| [telegram-desktop-decrypt](https://github.com/atilaromero/telegram-desktop-decrypt) | 220 / 66 | 直接解密 Desktop `tdata`，可输出包括视频在内的缓存文件；仓库已归档，最后推送为 2021 年，不能假定兼容当前 Desktop。 |

GitHub 展示的是项目本身的包装和入口，并不能证明各渠道分别带来多少用户。tgplay 在本次查询时为 0 Star / 0 Fork，只适合说明其公开的 Reddit 发帖方式，不适合用作热门项目代表。

| 项目 | 定位和公开展示 | 与 Cache Lens 的关系 |
|---|---|---|
| [Telegram Media Recovery](https://github.com/Alirezatz/TelegramMediaRecovery) | 最直接的同类：Windows WPF 界面和 CLI，从 Desktop `tdata` 恢复多类媒体；README 突出本机只读、媒体类型、覆盖率筛选、HTML 清单和处理技术。 | 它侧重多格式批量恢复和清单；Cache Lens 更聚焦视频列表、内嵌预览和选择片段合并。前者对部分流式文件描述为填零输出，不能据此断言两者的可播放率高低。 |
| [telegram-desktop-decrypt](https://github.com/atilaromero/telegram-desktop-decrypt) | 面向技术用户的 Desktop 数据解密命令行工具，README 以构建命令和子命令展示用法。 | 相邻的底层工具；Cache Lens 提供图形化的视频浏览与导出。 |
| [tgplay](https://github.com/vishnudas-bluefox/tgplay) | macOS 命令行工具，围绕下载未结束时播放缓存视频；突出本机、免登录和支持的外部播放器。 | 面向边下载边播放；Cache Lens 面向查找和导出已有缓存。作者曾在 [r/Telegram 用问题、使用场景和项目链接介绍它](https://www.reddit.com/r/Telegram/comments/1vnzubi/is_there_a_way_to_watch_telegram_videos_before/)，这是可见的宣传样本，不能据此推断其全部推广渠道。 |
| [tdl](https://github.com/iyear/tdl) | Telegram 命令行工具箱，围绕账号登录、下载和上传等工作流；项目首页突出下载能力、文档和安装入口。 | 属于通过 Telegram 服务访问内容的相邻选择，不是同一种本地缓存恢复流程。 |
| [Telegram Media Downloader](https://github.com/Neet-Nestor/Telegram-Media-Downloader) | 浏览器脚本，页面突出 Telegram Web 内媒体下载、安装步骤和功能演示。 | 面向网页端操作，与本机 Desktop 缓存读取不同。 |

官方 [Takeout API](https://core.telegram.org/api/takeout) 也是用户整理数据时可考虑的路径，侧重账户数据导出，与读取已存在的本地缓存不同。

## 可借鉴的宣传方式

从上述公开页面能直接观察到：同类项目普遍先说清一个具体场景，再展示安装或操作步骤；tgplay 的 Reddit 帖子则以问题开头，说明工具如何工作，最后邀请反馈。仅凭这些页面无法判断各渠道带来的用户量。

Cache Lens 的表达可以围绕“把本机 Telegram 视频缓存整理成可预览、可选择导出的列表”，配一张合成数据的真实界面截图和一个 30–60 秒演示。描述时区分完整视频与可恢复片段，不宣称恢复已清理的数据，不暗示能恢复所有缓存。

## 建议的下一步

1. 发布可下载的 Windows 包，确认干净环境能找到 FFmpeg，并在 README 首屏放清楚下载入口和运行步骤。
2. 用合成素材录制简短演示：选择目录、筛选、预览、分析片段、合并导出；不展示私人缓存或真实账号路径。
3. 先发中文开发者社区、Telegram 相关社区或适合的开源展示板块，检查各社区的自荐规则；发帖重点说清场景、限制和希望获得的反馈。
4. 将 [宣传文章](launch-zh.md) 改成适合平台的长短版本，附截图、仓库地址和明确的问题；不要向不相关帖子批量发广告。
5. 收集系统版本、Desktop 版本、错误描述和可复现的合成样本；不要让用户公开上传真实 `tdata`、视频、聊天记录或密码。
6. 后续根据反馈优先评估：跳过已导出的批量操作、片段分析速度、更多 Desktop 缓存格式、macOS 新版安装包及英文说明。
