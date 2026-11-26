# 自习室座位实时助手（试点版）

一间自习室、15 个座位、一台架在支架上的相机 —— 实时显示哪些位置空着，
并逐步积累历史数据来回答"我该什么时候来"。

```
┌──────────┐   RTSP/USB    ┌──────────────────────┐   只发 JSON    ┌──────────┐
│  相机    │ ────────────► │  本机处理（不出画面） │ ────────────► │  网页    │
│ 支架立起 │               │ 差分/检测 + 状态机    │   SSE 推送     │ 手机可开 │
└──────────┘               └──────────────────────┘                └──────────┘
```

**设计红线**：画面只在本机处理，不上传、不落盘、不做人脸。网页上永远不出现视频，
只有"这个座位现在算不算被占"。

---

## 快速开始

无需安装任何依赖（只要 `opencv-python`），不需要 Node、不需要打包。

```bash
cd library-seat-assistant

# 1. 先看效果（不需要相机）
python3 run.py --mock

# 2. 拿一段视频试（也不需要相机，但画面是真的）
python3 run.py --source data/demo.mp4 --fast

# 3. 接上相机跑真实数据
python3 run.py --source 0

# 4. 按"7 个隔断位 + 两张四人桌"的真实版式生成座位图
python3 run.py --reset-seatmap --layout room15
```

**最省事的试用方式**：跑第 2 条，打开
<http://127.0.0.1:8000/calibrate>，把一段自习室录像拖进去上传。
程序会自动拿第一帧当空镜、循环播放，你只要画好座位框就能看效果。

> 录制要点：**开头先留 10 秒空房间**。程序把第一帧当基准，那一帧里坐着的人
> 会被当成"背景"，永远不会被识别成有人。

**从 GitHub 克隆后第一次运行**（`data/` 不入库，所以在本地重新生成座位图）：

```bash
python3 run.py --layout room15 --room "你的房间名"
```

浏览器打开 <http://127.0.0.1:8000>，手机连同一 WiFi 打开终端里打印的局域网地址。

首次启动会生成一张占位的 15 座座位图。**真实房间一定和它不一样**，打开
<http://127.0.0.1:8000/calibrate>（仅本机可访问）按真实画面重画，并点「拍空镜」。

> macOS 第一次用摄像头会弹权限确认；如果看到 `not authorized to capture video`，
> 去「系统设置 → 隐私与安全性 → 摄像头」里给终端/编辑器打开权限。

---

## 现在能做什么

| 能力 | 状态 | 说明 |
|---|---|---|
| 座位实时地图（空/占/暂离/未知） | ✅ | SSE 推送，1–2 秒刷新 |
| 迟滞状态机 | ✅ | 人起身拿书不会立刻变空位 |
| 数据不可用时明确降级 | ✅ | 相机掉线 → 全部"未知"，绝不显示"全空/满座" |
| 历史曲线 + 30 分钟粒度落盘 | ✅ | `data/history.jsonl` |
| "什么时候来"预测 | ✅ | 同星期几中位数 + 当日偏差衰减，攒 3 天数据后启用 |
| 空位趋势图 | ✅ | 最近一小时 |
| 用户纠错按钮 | ✅ | 记录到 `app.reports`，用来调阈值 |
| 浏览器内座位标定 | ✅ | 点四点画框，实时判分辅助调阈值 |
| **上传视频分析** | ✅ | 不必等相机到货，先用录像验证 |
| 多人脸/人体识别精度 | ❌ | 见下 |

### 关于识别精度（重要）

当前默认用 **`diff` 模式**：房间空着时拍一张"空镜"作为基准，
之后逐座位比较画面变化比例。判断规则很简单：

```
座位核心区变化 > 20%  → 有人
            > 6%   → 有东西（书包/外套）→ 暂离
            其余    → 空
```

**优点**：零依赖、当天能跑、可解释、隐私最好（只比较像素，不解码语义）。
**缺点**：不认人，只认"变化"。椅子被拉出来、衣服搭在椅背上、阳光移动，
都会造成误判。它对**固定机位 + 稳定光照 + 空镜准确**非常敏感。

所以试点阶段它够用（用来验证"这件事有没有人需要"），但**它不是最终方案**。
等采集到真实数据后，切到 `--mode yolo`（需 Python 3.11/3.12 + `pip install ultralytics`，
因为 torch 目前还没有 3.14 的轮子）。切换时只需要换观测器，状态机和前端都不用动。

---

## 常用命令

```bash
python3 run.py --mock                 # 演示数据，无相机
python3 run.py --source 0             # 默认摄像头
python3 run.py --source 1             # 第二个摄像头
python3 run.py --source rtsp://user:pass@192.168.1.50:554/stream1   # IP 相机
python3 run.py --source none          # 不接相机，只用上传的视频
python3 run.py --source data/demo.mp4 # 本地视频（循环播放）
python3 run.py --source data/demo.mp4 --fast   # 视频演示用短时序，看得到变化
python3 run.py --port 8080            # 换端口
python3 run.py --room "三楼自习室"     # 房间名
python3 run.py --reset-seatmap        # 丢弃座位图，重新生成 15 座
python3 run.py --reset-seatmap --layout room15   # 7 个隔断位 + A/B 两张四人桌
python3 run.py --admin-token abc123   # 允许从手机访问标定页/画面
python3 tools/test_detector.py        # 无相机自测（17 项）
python3 tools/make_demo_video.py      # 重新生成 data/demo.mp4
```

`--fast` 只是把状态机时序缩到 4s/15s，方便看视频演示。
**接真相机时不要加**，15s/180s 才是对的。

视频分析的历史会写到 `data/history_demo.jsonl`，不会污染真实房间的预测基线。

环境变量可调参数：`SEAT_ON_SEC`（判定占用的秒数）、`SEAT_OFF_SEC`（判定空位的秒数）、
`SEAT_PERSON_RATIO`、`SEAT_BELONGING_RATIO`、`SEAT_DIFF_THRESHOLD`。
现场调阈值主要靠标定页的「实时判分」面板，别拍脑袋改代码。

---

## 目录结构

```
library-seat-assistant/
├── run.py                  启动入口
├── app/
│   ├── config.py           所有可调参数
│   ├── seatmap.py          座位多边形 + 默认布局 + 几何
│   ├── detectors.py        观测器：diff / yolo / mock（可替换）
│   ├── state_machine.py    迟滞、暂离、不可用降级
│   ├── history.py          落盘、聚合、预测
│   ├── pipeline.py         取流线程 + 主循环 + App
│   └── server.py           HTTP + SSE + API
├── web/
│   ├── index.html          座位地图
│   ├── app.js              SSE、趋势图、纠错
│   ├── calibrate.html      标定页
│   └── calibrate.js
├── tools/test_detector.py  合成画面自测
├── docs/
│   ├── 00-方案总览.md      完整方案（功能、架构、选型、风险）
│   └── 01-试点执行手册.md  15 座试点怎么落地
└── data/                   座位图、历史、空镜（不入库）
```

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 当前全量状态 |
| GET | `/events` | SSE 实时流 |
| GET | `/api/seatmap` | 座位图 |
| POST | `/api/seatmap` | 保存座位图（管理员） |
| GET | `/api/history?minutes=60` | 历史记录 |
| GET | `/api/forecast?hours=3` | 预测 |
| GET | `/api/advice` | 一句话建议 |
| POST | `/api/report` | 用户纠错 `{seat_id, says}` |
| POST | `/api/upload?name=x.mp4` | 上传视频并切换分析源（管理员） |
| POST | `/api/reference` | 拍空镜（管理员） |
| GET | `/api/camera.jpg` | 当前画面（管理员，本地） |
| GET | `/api/debug/scores` | 各座位判分（管理员） |

管理员 = 本机访问，或带 `?token=<--admin-token>`。
画面接口故意只对管理员开放 —— 公开页面上不应该出现任何视频。

---

## 常见问题

**`segmentation fault python3 run.py`**
已知的两个原因都已在代码里处理：

1. macOS 的摄像头授权必须由主线程发起 —— 现在启动时先主线程试开一次。
2. 切换数据源（比如上传视频）时，旧采集线程可能正卡在 `cap.read()` 里，
   从别的线程 `release()` 会 use-after-free 直接段错误 —— 现在改为
   **先 join 线程退出、由线程自己释放**。

如果仍然崩溃，先隔离问题：

```bash
python3 run.py --source none     # 完全不碰摄像头，只用上传的视频
python3 run.py --source 1        # 换一个设备编号（内置摄像头通常是 0）
```

再去「系统设置 → 隐私与安全性 → 摄像头」确认终端/编辑器已获授权。
同一时间只能有一个程序占用摄像头，先关掉 Zoom/腾讯会议、视频号直播等。

崩溃日志在 `~/Library/Logs/DiagnosticReports/Python-*.ips`，
看 `faultingThread` 的名字和栈顶就能定位是哪个线程出的问题。

**上传时提示"仅本机可上传视频"**
用终端打印的 `127.0.0.1` 或本机局域网地址都能上传；
从别的设备（手机）打开则只能看，不能上传——这是有意的。

**页面一直显示"未知"**
按顺序检查：相机是否在线（看终端）→ 有没有拍空镜 → 座位框是否画在正确位置。

---

## 下一步

1. 按 `docs/01-试点执行手册.md` 布点、标定、开始记录。
2. 跑满两周，用每天的精度核对表评估是否达到 90%。
3. 数据够了以后：换 `--mode yolo` 提精度，或者先做"考试周模式"。
4. 有了准确率和留存数据，再去找图书馆谈第二个房间。
