# Malody V 回放轨道播放器

本地网页工具，用 `.mcz/.osz` 谱面包和对应的 Malody V `.mrv` 回放还原 Key 模式的音符、输入与判定，并可在后台导出带音频的 MP4。支持原生 Malody `.mc`、原生 osu!mania `.osu`、Malody V 再导出的 osu!mania 谱包，以及 Const/Flip Mod。

原作者：Winter_Melon。项目代码采用 [MIT 许可证](LICENSE)。

## 启动

首次从源码运行前安装项目及固定依赖：

```powershell
python -m pip install -e .
```

之后可在本目录启动：

```powershell
python main.py
```

也可以指定端口和不自动打开浏览器：

```powershell
python main.py --port 8766 --no-browser
```

程序仅监听 `127.0.0.1`。在页面中选择谱面包和回放并载入，即可播放回放、显示打击位置、拖动时间轴，以及调整轨道尺寸、下落速度和音频延迟，轨道右侧会按判定实际发生时间显示实时 Accuracy 与四类判定累计值。修改 Python 代码后需重启服务。

## MP4 导出

载入回放后点击“导出 MP4”。默认画面仅包含轨道；可按需在左侧加入谱面信息，或在右侧加入实时 Accuracy 与 Perfect、Cool、Good、Miss。视频固定为 60 FPS，沿用页面当前的轨道尺寸、下落速度和音频延迟，覆盖播放器完整时间轴。导出在 Python 后台运行，不影响播放器进度；浏览器可以最小化，但服务窗口必须保持运行。

同一时间只能导出一个视频。运行期间按钮用于取消任务，也不能更换输入文件或导出参数；完成后浏览器会自动下载。谱包没有音频时会生成静音 MP4 并提示。

## 显示约定

| 颜色 | 含义 |
| --- | --- |
| 白色 | Perfect |
| 黄色 | Cool |
| 绿色 | Good |
| 红色 | Miss、额外输入或长条重新接上的按下 |

Note 使用空心细框，Input 使用较短的粗线，两者距离表示击打偏差。长条头横线保留头判颜色并加粗；侧边严格按按住状态着色，断开后变红、重新接上后变白；尾部横线继承最终侧边颜色。白色虚线表示实际按住区间，可显示多次断开与重接。

曲目信息会显示 `No Mod`、`CONST`、`FLIP` 或 `CONST + FLIP`。Flip 会同时镜像谱面显示与配对轨道，Input 保持回放记录的游玩轨道。

统计区显示谱面总键数、Accuracy、各类判定、额外输入和长条重接次数；每项下方按实际渲染的从左到右轨道列出分项数量，Flip 回放显示镜像后的轨道顺序。Accuracy 按 Perfect/Cool/Good/Miss 的 `100/75/40/0` 权重计算。

轨道下方的密度图以当前采样时刻前后各 0.5 秒为窗口，每 100 ms 统计一次 Note 数；长条只计头部。曲线使用 5 点居中移动平均平滑显示，纵轴、当前/最大密度和悬停提示仍使用原始的“1 秒内 Note 数”。白色竖线表示播放器当前位置，可在图中点击或横向拖动来调整播放进度。密度图只在网页中显示，不会加入导出的 MP4。

音频时间按 `谱面时间 - 谱面音频 offset - 手动延迟` 计算。手动延迟范围为 −1000 至 +1000 ms，默认 −300 ms，同时用于当前播放和视频导出。

## 当前限制

- 只接受包含 `.mc/.osu` 的 `.mcz/.osz` 和 Malody V `.mrv`，不支持裸谱面或 osu! `.osr`；
- 谱包最大 256 MiB，回放最大 128 MiB；文件会完整保存在本机进程内存中；
- 仅支持 No Mod、Const 和 Flip；其他 Mod 或无法按已验证规则配对的数据会停止分析；
- 长条只有头部参与 Accuracy，尾部、断开与重接仅用于恢复按住状态；
- 支持 Malody 多 BPM，但无论是否开启 Const，SV 和谱面 effect 暂时都不改变显示速度；
- MP4 固定使用 H.264/yuv420p 与 AAC 192 kbps；不渲染 storyboard、背景、hitsound 或皮肤。

## 隐私

上传内容只在本机处理且不会发送到外部服务。谱面、回放和音频保存在进程内存；视频导出会写入系统临时目录，下一次导出或服务退出时清理。

## 代码结构

| 路径 | 内容 |
| --- | --- |
| `main.py` | 本地播放器入口。 |
| `src/replay_renderer/server.py` | 仅监听本机的 HTTP 服务、内存会话和音频分段传输。 |
| `src/replay_renderer/chart.py` | `.mcz/.osz` 内的 `.mc/.osu` 匹配、谱面时间轴和音频资源解析。 |
| `src/replay_renderer/protobuf.py` | 无 `.proto` 依赖的最小 protobuf wire reader。 |
| `src/replay_renderer/replay.py` | `.mrv` 帧、输入、结算与判定恢复。 |
| `src/replay_renderer/analysis.py` | 按轨道关联谱面音符、结算事件和按下输入，生成前端时间轴。 |
| `src/replay_renderer/export.py` | 使用 Pillow 逐帧绘制，并调用随 imageio-ffmpeg 提供的 FFmpeg 编码 MP4。 |
| `src/replay_renderer/static/` | HTML、CSS 和 Canvas 播放逻辑。 |
| `tests/` | 使用运行时合成数据的 `unittest` 测试。 |

## 测试

```powershell
python -m unittest discover -s tests -v
```
