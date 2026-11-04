# Telegram 相关项目源码对照

调研日期：2026-11-04。本次仅阅读公开源码与本项目实现，没有使用真实缓存，也没有测量竞品性能。GitHub 链接固定到调研时的提交，便于复查。Star 数量不代表功能相同或代码可以直接复用。

## 建议优先处理

### 1. 普通缓存扫描避免重复读取和解密

本项目 `desktop_scan.scan` 为辨认格式调用 `read_value`，然后 `materialize_plain` 经 `read_plain` 再调用一次 `read_value`；每次都要读取、解密和校验相同的条目。位置：[desktop_scan.py](../src/tg_cache_lens/desktop_scan.py#L69)、[desktop_store.py](../src/tg_cache_lens/desktop_store.py#L90)、[desktop_materialize.py](../src/tg_cache_lens/desktop_materialize.py#L95)。

[Telegram Media Recovery 的普通缓存处理](https://github.com/Alirezatz/TelegramMediaRecovery/blob/b4452434857fb1378be09b0c998739823f1afa1a/src/TelegramMediaRecovery.Core/Extraction/DatabaseExtractor.cs#L58)先读取条目，再把读取结果传给写入流程。可让我们的检查和快照创建共用同一次已验证结果，保留读取前后的索引一致性检查及导出前复验。

**预期：**普通 `cache` 类型少一次条目读取和解密；`media_cache` 和整个扫描会快多少需要合成基准验证。

### 2. 扫描结果按完成顺序返回，并限制待处理量

当前扫描采用四线程，但 [desktop_scan.py](../src/tg_cache_lens/desktop_scan.py#L108) 使用 `pool.map` 按输入顺序取结果；前面一个大视频慢时，即使后续条目已处理完成，进度也可能暂时不前进。与之相对，[tdl 的任务执行器](https://github.com/iyear/tdl/blob/70c561c0a6d44d8d7fffdea14988fc4104bc56d2/core/downloader/downloader.go#L34)限制并发，每个任务完成时单独报告结果。

建议：扫描采用有界任务队列和按完成顺序处理；界面分批插入已完成条目，而最终排序仍遵守用户选择。当前扫描会在 `scanned` 回调收到完整结果后填充列表，需连同 GUI 一起修改，单改 `as_completed` 只能改善进度反馈。缓存切换、停止和重新扫描要忽略旧任务的迟到结果。

**预期：**更早显示小视频和进度；不承诺总扫描时间一定减少。

### 3. 大视频分片按范围处理，减少峰值内存和临时写入

当前 [read_document](../src/tg_cache_lens/desktop_store.py#L117) 收集已解密片段、核对重叠，再由 [write_snapshot](../src/tg_cache_lens/desktop_materialize.py#L55) 写出临时快照；多个大视频并行时，需要关注缓存片段、快照和 FFmpeg 任务同时占用的内存及 I/O。

[Telegram Media Recovery 的流式写入器](https://github.com/Alirezatz/TelegramMediaRecovery/blob/b4452434857fb1378be09b0c998739823f1afa1a/src/TelegramMediaRecovery.Core/Media/StreamedFileWriter.cs#L19)根据分片计划依次写入并在写入时计算摘要。可借鉴“计划与数据写入分离”的结构：先确定来源和范围，之后按有限窗口解密、校验、写入稀疏快照；在能保证已有冲突检测和源复验语义时再释放片段内存。

**注意：**该项目也有分片计划，并不意味着它所有处理都是真正的常量内存；本建议需要结合我方格式验证。先用合成的大文件和多分片样本测量峰值内存、磁盘写入量和首条结果时间，不推测具体提速倍数。

### 4. 让停止操作传递到 FFmpeg 子进程

现有 [stop_analysis](../src/tg_cache_lens/gui.py#L937) 请求线程中断，但 [core.run](../src/tg_cache_lens/core.py#L130) 用阻塞的 `subprocess.run`；当 FFmpeg 或 FFprobe 正在运行时，线程通常要等调用结束或超时才有机会停止。[Telegram Web 下载脚本](https://github.com/Neet-Nestor/Telegram-Media-Downloader/blob/f5d1cf41cd67f3af1ac5b1161e32b7852f76d762/src/tel_download.js#L240)把取消信号传给正在执行的 I/O。两者机制不同，可借鉴取消贯穿任务的思路。

建议：让分析任务持有其子进程句柄，停止时终止该任务创建的子进程，等待退出并清理未完成的临时文件；已展示的片段继续保留。只影响用户主动停止的任务，不终止其他软件或用户进程。

## 体验与后续功能

### 跳过已导出、失败项重试

[tdl 的 `--skip-same`](https://github.com/iyear/tdl/blob/70c561c0a6d44d8d7fffdea14988fc4104bc56d2/app/dl/iter.go#L244)按名称和大小跳过，[tangyoha 的下载器](https://github.com/tangyoha/telegram_media_downloader/blob/fe7ccdc2cc5fce5b04a464bc66cfb1266bf7ef45/media_downloader.py#L558)维护待重试消息。当前本项目只以文件名识别“已导出”，批量导出仍会重复执行。可以增加可选的“跳过已导出”和“重试失败项”，但不能把不同的视频片段当成整片，也不能仅根据同名或大小断言内容相同。

### 输出任务进度

[Telegram Web 下载脚本](https://github.com/Neet-Nestor/Telegram-Media-Downloader/blob/f5d1cf41cd67f3af1ac5b1161e32b7852f76d762/src/tel_download.js#L420)维护排队、运行、失败、取消、重试等任务状态，并将高频进度刷新节流到约 100ms；[Dineshkarthik 的 Web UI](https://github.com/Dineshkarthik/telegram_media_downloader/blob/859272a8111b6a9ac3b32376d68f34f6a9775fd6/webui/execution_tab.py#L94)为活动下载显示单项进度。可以给我们的批量导出提供当前项、总体数量和失败重试入口；先用现有底部区域，避免引入多余任务面板。

### 索引不再引用的缓存

[Telegram Media Recovery](https://github.com/Alirezatz/TelegramMediaRecovery/blob/b4452434857fb1378be09b0c998739823f1afa1a/src/TelegramMediaRecovery.Core/Extraction/DatabaseExtractor.cs#L146)还扫描索引未引用的文件和旧版本目录。本项目主要读取当前索引；可作为未来单独的恢复模式评估，但旧分片可能缺少原片关联和顺序证据，不应直接当成当前视频或默认混入常规结果。

## 不应照搬的实现

- Telegram Media Recovery 的 [ComputeStreamDigest](https://github.com/Alirezatz/TelegramMediaRecovery/blob/b4452434857fb1378be09b0c998739823f1afa1a/src/TelegramMediaRecovery.Core/Extraction/DatabaseExtractor.cs#L544)把文件大小和每个片段的偏移、长度拼起来作为去重依据。不同内容可以拥有完全相同的分片布局，因此这并不能证明内容相同，不能用于跳过我们的不同视频。它的流式写入和摘要计算可以借鉴，但布局去重不能照搬。
- [tangyoha 的判断](https://github.com/tangyoha/telegram_media_downloader/blob/fe7ccdc2cc5fce5b04a464bc66cfb1266bf7ef45/media_downloader.py#L416)对任意非空文件都会进入跳过分支；不适合作为本项目的导出成功或完整性证据。
- [telegram-desktop-decrypt](https://github.com/atilaromero/telegram-desktop-decrypt/blob/a2c3042b3bfa7b8d1edcd93d3664e56f51c6f20e/cmd/telegram-desktop-decrypt/main.go#L305)主要处理旧式 map / tdata 解密，仓库已归档；可作历史格式参考，不能直接替代当前 Desktop `media_cache` 的处理。

## 建议实施顺序

1. 去掉普通缓存重复读取，做合成数据前后基准和来源变更测试。
2. 分批显示扫描结果和真实停止子进程，覆盖切换目录、停止及重扫。
3. 用多分片大文件衡量内存和磁盘瓶颈，再改造分片快照写入。
4. 增加跳过已导出和失败项重试，明确整片与片段的关系。

上述优先项已按本项目代码独立实现：普通缓存复用读取结果，Desktop 扫描按完成顺序逐步显示，分析取消会终止当前任务的媒体进程，大分片按需使用临时文件降低内存占用，批量导出提供跳过已导出和失败项重试。旧索引和孤立缓存扫描仍是独立的后续研究方向。

合成的 6 个 8 MiB 分片测试中，内存模式的 Python 堆峰值为 64 MiB、文件缓冲模式为 32 MiB；对应耗时约 0.35 秒和 0.46 秒。这只是一次本机微基准，不代表完整扫描速度或所有设备上的表现。

tdl 使用 AGPL-3.0，Telegram Web 脚本使用 GPL-3.0；以上基于公开行为与算法思路独立实现，未复制第三方代码。实际引用或分发第三方代码前需要分别核对许可证及署名要求。
