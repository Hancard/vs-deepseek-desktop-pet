# DS_Monitor     vs-deepseek-desktop-pet

> deepseek鲸鱼娘在其它项目的支持下将作为一个傲娇的二次元桌宠，陪伴你在 VS Code 中写代码。
> 她会监控你的 C/C++/Python 编译错误，用毒舌又暖心的方式吐槽你的 bug。

当前版本：**v0.3.7**

> ⚠️ **素材版权**：本项目内置的 Live2D 模型 **不是本项目原创**，来自
> [A8Chann/dsh-pet-live2d](https://github.com/A8Chann/dsh-pet-live2d)，许可为
> **CC BY-NC-SA 4.0（署名 · 非商业性使用 · 相同方式共享）**。
> 详见 [素材来源与许可](#素材来源与许可)。**任何权利人如认为使用超出授权，联系后立即删除。**

---

## 截图（预期效果）

```
┌──────────────────────┐
│  "笨蛋！括号都配不对  │  ← 聊天气泡（打字机效果，8 秒自动消失）
│   的吗？好好数数！"   │
└──────┬───────────────┘
       │
    ┌──┴──┐
    │ (•̀ᴗ•́)│  ← Live2D 角色 / 立绘（可拖拽移动窗口，点一下会说话）
    └─────┘
    ● online           ← 在线状态指示
```

窗口无边框、置顶显示、可在屏幕任意位置拖动。
**角色之外是真透明**（关掉 DWM 的 Mica 背景材质 + WebView2 逐像素 alpha），
能看到后面的桌面/编辑器，不会有一块灰白或纯黑色方块托底。
角色带情绪表情系统：`idle` / `greeting` / `angry` / `happy` / `surprised`，
**点角色的头/身体/桌面会用不同台词回应你**（拖拽不会误触发）。

---

## 架构

```
┌────────────────────────────────────────────┐
│  VS Code Extension (TypeScript) — 轻量桥接 │
│  src/extension.ts                          │
│                                            │
│  • 监听 C/C++/Python 诊断错误              │
│  • POST /push 转发到桌宠服务器 (19876)     │
│  • checkStandaloneAlive() 健康探测(15s缓存)│
│  • Webview Panel (VS Code 内备用显示)      │
│  • Launch deepseek Desktop Pet 一键启动命令│
└──────────────┬─────────────┬───────────────┘
               │             ▲ POST /push
               ▼             │ (watcher.py 独立监听文件保存)
┌────────────────────────────────────────────┐
│  standalone.py — 独立桌宠服务器             │
│  ThreadingHTTPServer + SSE + pywebview     │
│                                            │
│  • /push 接收诊断 → generate_response      │
│  • SSE 广播 → ui.html 气泡 + 情绪切换      │
│  • assets/character.png 自定义立绘         │
└──────────────┬─────────────────────────────┘
               ▼
┌────────────────────────────────────────────┐
│  python_backend/ — 回复生成（傲娇大脑）     │
│  corpus.py 语料库 / response_generator.py  │
│  本地语料库 或 DeepSeek API 双通道          │
└────────────────────────────────────────────┘
```

- **VS Code 扩展**：只做一件事——把诊断错误转发到桌宠服务器。不启动任何 Python 进程，桌宠不在线时静默跳过。
- **standalone.py**：桌宠本体 + 服务器 + 大脑，完全独立于 VS Code 运行。
- **watcher.py**：可选的独立文件监听器，不用 VS Code 也能用（保存 .py/.c/.cpp 时自动跑语法检查并推送）。

---

## 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| VS Code | ≥ 1.110.0 | 扩展运行环境 |
| Node.js | ≥ 18 | TypeScript 编译（开发扩展时需要） |
| Python | ≥ 3.9 | watcher 与后端均兼容 3.9+（3.12 实测） |
| pywebview | ≥ 4.0 | 桌面宠物窗口 |
| Windows | 10+ | WebView2 运行时（Win11 已内置） |

### 安装步骤

```powershell
# 1. 进入项目目录
cd "S:\My event\projects\vscode-anime-assistent"

# 2. 安装 Node.js 依赖（仅开发/调试扩展时需要）
npm install

# 3. 安装 Python 依赖
pip install -r desktop_pet/requirements.txt
# (可选，启用 DeepSeek AI 回复时)
pip install -r python_backend/requirements.txt

# 4. 编译 TypeScript（仅开发扩展时需要）
npm run compile

# 5. (可选) 启用 DeepSeek AI 回复：设置系统环境变量
# setx DEEPSEEK_API_KEY "sk-xxxxx"
# 可配置项见 .env.example（注意：需设为系统环境变量，代码不自动加载 .env 文件）
```

### 运行（三选一）

```powershell
# 方式一：状态栏按钮（推荐）
# 打开项目 → 按 F5 起扩展开发宿主 → 右下角状态栏点「deepseek」按钮
# （等价于命令面板 Ctrl+Shift+P → "Launch deepseek Desktop Pet"）
# 扩展会自动查找 Python 并启动桌宠，8 秒内确认启动成功

# 方式二：一键启动脚本（桌宠 + 文件监听器）
start.bat

# 方式三：手动启动（能看到完整控制台输出）
python desktop_pet/standalone.py
```

> 启动诊断都会写进 `desktop_pet/.airi-pet.log`。方式一/二看不到控制台输出，
> **出问题先看这个日志**（原因见下文「桌宠显示的是立绘而不是 Live2D」）。
>
> 调试扩展本身：在 VS Code 中打开项目 → 按 F5 → 新窗口自动加载扩展。

---

## 项目结构

```
vscode-anime-assistent/
├── src/
│   └── extension.ts              # 扩展入口（轻量诊断桥接器）
│       ├── activate()            # 注册命令 + 诊断监听 + Webview 面板
│       ├── handleDiagnosticsChanged()  # 收集 C/C++/Python 错误
│       ├── checkStandaloneAlive()      # 探测桌宠服务器（15s 缓存）
│       ├── launchStandalonePet()       # 一键启动桌宠命令
│       ├── pushToStandalone()          # POST http://127.0.0.1:19876/push
│       └── getWebviewHtml()            # VS Code 内备用 Webview UI
│
├── desktop_pet/                  # ★ 桌面宠物（独立运行，不依赖 VS Code）
│   ├── standalone.py             # 主入口：HTTP 服务器 + SSE + pywebview 窗口 + 点击台词池
│   ├── watcher.py                # 独立文件监听器（保存即检查）
│   ├── common.py                 # 共用工具（屏幕尺寸/窗口API/立绘查找/HTML注入/素材服务器/关 DWM Mica/窗口形状裁剪）
│   ├── live2d_assets.py          # Live2D 素材定位、Core 自动抓取与校验、闭包预检
│   ├── main.py                   # [旧版入口] 需外部 HTTP 服务器，当前链路未使用
│   ├── ui.html                   # 桌宠 UI（SSE、拖拽、气泡、情绪表情、Live2D、轮廓采集与点击命中）
│   ├── assets/
│   │   ├── character.png         # 自定义立绘（可选情绪变体，见下文）
│   │   └── live2d/               # Live2D 素材（模型 + 渲染引擎，Core 不入库）
│   │       ├── model/ds-whale-girl/   # CC BY-NC-SA 4.0，见「素材来源与许可」
│   │       └── vendor/                # pixi.js + Live2D 引擎（MIT）；Core 首次启动下载
│   ├── launch.bat                # 仅启动桌宠
│   ├── launch_full.bat           # 启动桌宠 + 文件监听器
│   ├── .airi-pet.log             # 启动诊断日志（每次启动重写，不入库）
│   └── requirements.txt          # pywebview>=4.0
│
├── live2d_probe/                 # 开发期验证沙盒（不影响桌宠运行，可整目录删除）
│   ├── README.md                 # ★ 踩坑记录与验证结论 —— 改这里的前先读它
│   ├── verify_pet_ui.py          # 桌宠 ui.html 端到端渲染验证（真 Chrome + 真 WebGL）
│   ├── verify_click_region.py    # 窗口形状 + 点击说话（含 GetWindowRgn before/after 读回）
│   ├── verify_silhouette_js.py   # 前端轮廓/点击/拖拽（真 Chrome 合成鼠标事件）
│   ├── smoke_pet_server.py       # 生产静态服务器冒烟（含 8 条路径越界攻击）
│   ├── probe_vscode_launch.py    # 复刻扩展启动条件跑真 standalone.py
│   ├── probe_transparency.py     # 透明方案对照探针
│   ├── probe_live_window.py      # 【活窗口】枚举窗口/逐层抓图，找"谁在铺底"
│   ├── probe_live_alpha.py       # 【活窗口】隐藏/显示差分 —— 唯一可信的透明判据
│   ├── probe_live_fix3.py        # 【活窗口】判别"窗口改动到底生效没有"
│   ├── probe_live_fix4.py        # 【活窗口】决定性实验：关 Mica 前后对比
│   ├── probe_verify_fix.py       # 【活窗口】验证修复（读回 backdrop + 差分）
│   └── find_webview.py           # 定位各解释器的 pywebview 路径/版本，列透明相关源码行
│
├── python_backend/               # 回复生成（傲娇大脑）
│   ├── character.py              # deepseek 角色人格 + System Prompt
│   ├── corpus.py                 # 场景语料库 + 情绪映射 (EMOTION_MAP)
│   ├── response_generator.py     # 错误分类 + 语料库/DeepSeek API 双通道
│   ├── main.py                   # [旧版] stdin/stdout 后端，当前链路未使用
│   └── requirements.txt          # openai>=1.0.0 (可选，仅 DeepSeek 需要)
│
├── start.bat                     # 一键启动：桌宠 + 文件监听器
├── .env.example                  # 环境变量模板
├── package.json                  # VS Code 扩展清单
└── out/                          # 编译产物 (extension.js)
```

---

## 通信协议

### VS Code 扩展 → 桌宠服务器 (HTTP POST /push)

发现错误时：

```json
{
  "type": "diagnostics",
  "payload": {
    "count": 2,
    "items": [
      {"file": "main.c", "languageId": "c", "message": "expected ';'", "source": "gcc", "line": 12, "character": 5}
    ],
    "timestamp": "2026-09-13T22:00:00.000Z",
    "language": "c"
  }
}
```

错误清零时（仅在服务器在线时发送）：

```json
{"trigger": "all_clear", "error_count": 0, "language": "unknown", "files": [], "sample_errors": []}
```

### 桌宠服务器 → ui.html (SSE /events)

```
data: {"type":"chatMessage","payload":{"text":"哼，终于修好了。","emotion":"happy"}}

data: {"type":"errorAlert","payload":{"text":"笨蛋！括号都配不对！","emotion":"angry"}}
```

### HTTP 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/push` | POST | 接收诊断 / all_clear 事件（扩展和 watcher.py 都推这里） |
| `/events` | GET | SSE 事件流（桌宠窗口连接） |
| `/ping` | GET | 健康检查 → `{"status":"ok","server":"airi-standalone"}` |
| `/event` | POST | 接收桌宠窗口消息（如 `desktopReady`） |

---

## 功能特性

### 桌面宠物窗口
- 🖱️ **拖拽移动**：按住角色拖到屏幕任意位置
- 📌 **始终置顶**：无边框，悬浮在所有窗口之上
- 🪟 **真透明（不是画个桌面色）**：关掉 DWM 的 Mica 背景材质之后，
  WebView2 的逐像素 alpha 直接透出后面的桌面 —— 角色周围没有方块托底
- 👻 **点穿**：窗口形状由页面按角色的 alpha 轮廓算出（`SetWindowRgn`），
  形状之外的鼠标点击会落到下层窗口，不会挡住 VS Code
- 🫧 **聊天气泡**：打字机效果 + 错误红色边框 + 8 秒自动消失（最多 3 条）
- 👆 **点击互动**：点角色的**头 / 身体 / 桌面**分别给不同台词 + 情绪。
  命中判定走渲染结果的 alpha 通道（点到空白处不说话），
  与拖拽共用同一个 `mousedown`，**移动超过 5 px 就只算拖拽、不算点击**
- 😤 **情绪表情系统**：每条回复带情绪，角色表情自动切换，8 秒后回落 idle
- 🐋 **Live2D 角色**：默认用 DS鲸鱼娘 Live2D 模型渲染（可呼吸、会眨眼），失败自动退回立绘
- 🖱️ **右键菜单**：退出 deepseek（销毁窗口并结束进程，重开用启动命令/脚本）
- 🔄 **自动重连**：SSE 断线后自动恢复连接

### Live2D 角色（默认渲染方式）

角色位默认由 Live2D 模型占据 —— 会呼吸、会随机眨眼，情绪会切成对应表情。
素材在 `desktop_pet/assets/live2d/`，由 `live2d_assets.py` 在启动时自检。

**三级降级，外观绝不会让桌宠起不来**：

| 情况 | 表现 |
|---|---|
| 一切正常 | Live2D 模型 |
| 素材缺失 / WebGL 不可用 / 任一步失败 | 自动退回 `character.png` 立绘（静态图片） |
| 立绘也没有 | 退回内置 CSS Q 版角色 |

控制开关（环境变量）：

| 变量 | 作用 |
|---|---|
| `AIRI_LIVE2D=0` | 强制关闭 Live2D，回到原来的图片/CSS 角色 |
| `AIRI_SILHOUETTE=0` | 关闭窗口形状裁剪，即**放弃鼠标点穿**（整块矩形的点击都归桌宠）。**不影响画面透明** |
| `AIRI_CARD=<模式>` | 角色底板（水族箱）：`off` / `tight` / `square`（默认）/ `frame` / `all`，见下文「块状白」 |
| `AIRI_WIN_BG=<模式>` | 宿主 Form 底色的处理方式：`dark`（默认，只压黑、不碰分层，输入路径与 v0.3.2 一致）/ `key`（opt-in 实验：颜色键挖洞 —— 本机实测会整窗鼠标穿透，勿用）/ `off`（不动，做 A/B 用）。见下文「块状白」 |
| `AIRI_CUBISM_CORE=<路径>` | 指定 Cubism Core 的 js 文件位置 |
| `AIRI_ASSET_PORT=<端口>` | 固定素材服务器端口（默认自动选空闲端口） |

> `AIRI_SILHOUETTE` 与 `AIRI_LIVE2D` **互相独立** —— 形状是按"当前渲染出来的东西"
> 算轮廓的，所以立绘、CSS 角色也一样吃这个开关。

**关于 Cubism Core**：`live2dcubismcore.min.js` 是 Live2D 的**专有运行时，不可再分发**，
所以它**不在仓库里**（见 `.gitignore`）。首次启动时会自动从 Live2D 官方 CDN 抓取并缓存到
`desktop_pet/assets/live2d/vendor/`，**所以首次运行需要联网**。若你的环境无法联网，
手动把该文件放到那个目录即可。

**情绪映射**（现有情绪 → 模型表情）：

| 情绪 | 表情 |
|---|---|
| `idle` | 无（回待机） |
| `greeting` | 开心兴奋 |
| `angry` | 生气 |
| `happy` | 星星眼 |
| `surprised` | 感叹号 |

> 模型自带 44 个表情 + 8 个动作，目前只接了 idle 动作与上表 5 个情绪。
> 其余动作（挥锤子、自拍、喷水…）尚未接线。

### 窗口透明与点击互动

这两件事**是分开的两条路**，别混在一起看：

| 需求 | 机制 | 说明 |
|---|---|---|
| **画面透出桌面** | `common.disable_window_backdrop()` | 关掉 DWM 的 Mica 背景材质。**这才是"窗口不是透明的"的真正根因** |
| **鼠标点穿** | `SetWindowRgn`（页面算轮廓 → Python 裁形状） | 形状之外的点击落到下层窗口（比如 VS Code）。**它不改变画面** |

**画面透明**：pywebview 在**系统深色模式**下会执行
`DwmSetWindowAttribute(hwnd, 38, 2)`（`DWMWA_SYSTEMBACKDROP_TYPE = Mica`），
浅色模式下则设 `1`（`NONE`）。Mica 由 DWM 绘制、**不在窗口的 GDI 重定向表面里**，
所以 `SetWindowRgn` 裁不到它、`LWA_COLORKEY` 挖不掉它，只有整窗 `LWA_ALPHA` 动得了。
这解释了两张截图的差异：浅色模式 `#F0F0F0`、深色模式 `#202020`
（`SystemColors.Control` 恰好也是这两个值，所以很容易误判成"Form 底色"）。

修复就是把它设回 `NONE`，并把 `form.update_title_bar_theme` 包一层 ——
否则切系统主题时 Mica 会被重新装回去。调用点在 `webview.start(_after_window_ready)`。

```
修复前  窗口区域与桌面一致的像素   0.00%      ← 整块不透明
关掉 Mica 后                    90.49%      ← 角色周围真透出桌面
改回 Mica                       0.00%      ← 反向复现
```
（`live2d_probe/live_fix4.txt`，在**用户正在跑的窗口**上用屏幕 BitBlt 实测。）

**为什么不用 `TransparencyKey` / `LWA_COLORKEY`**：实测对这块 Mica 完全无效
（返回成功、屏幕不动）。而它真正能挖的只有窗口自己的 GDI 表面，
按颜色键控还会把角色抗锯齿边缘抠出毛边。

**点穿**：页面读自己渲染结果的 alpha 通道算出轮廓，上报给 Python 侧
`ExtCreateRegion` + `SetWindowRgn`：

```
ui.html                                     common.py
  characterRects()                            set_window_region(payload)
    gl.readPixels(0,0,cw,ch)  ← 显式绑默认 FBO     ExtCreateRegion(RGNDATA)
    逐行扫描线 → 合并成矩形                         SetWindowRgn(hwnd, hrgn, True)
  collectUiRects()   气泡 / 名字 / 状态条
  pushSilhouette()   每 500 ms 推一次   ───────▶  _apply_window_region()
  alphaAt(x,y)       点击命中判定
  zoneAt(clientY)    head(<0.45) / body(<0.85) / desk
```

实测点穿确实生效（`WindowFromPoint`）：客户区 (20,20)（形状之外）命中的是**别的窗口**，
(140,220)（形状之内）命中的是桌宠自己。

两条安全阀：轮廓矩形为空、或所有矩形都被裁光时，`_apply_window_region()` **直接拒绝**——
宁可放弃点穿，也不能让窗口变成零面积（那就彻底看不见、也点不到了）。

**点击判定**：模型没有声明 Cubism HitAreas，引擎的 `hitTest()` 用不了，
所以只能自己读 alpha。`alphaAt() < 24` 视为空白（点到空白不吭声）；
拖拽超过 5 px 就不再算点击；防抖 600 ms。
台词池在 `standalone.py:CLICK_LINES`，只用前端认识的四个情绪词
（写个前端没有的情绪，表情会静默不变）。

### 自定义立绘

不想用 Live2D 时，把图片放到 `desktop_pet/assets/character.png` 即可替换内置的 CSS Q 版角色
（或设 `AIRI_LIVE2D=0` 强制走这条路）。
支持情绪变体：在同目录放置 `character_angry.png`、`character_happy.png`、
`character_surprised.png`、`character_greeting.png`，缺失的情绪会自动回落到默认图。

### VS Code 集成
- 诊断错误实时转发（C/C++/Python）
- Webview 面板作为备用显示（不依赖 Python 环境）
- 健康探测：桌宠不在线时不发送任何请求

### 回复策略
1. **DeepSeek API**（设置 `DEEPSEEK_API_KEY` 环境变量）→ AI 生成个性化傲娇回复
2. **本地语料库**（默认）→ 按错误类型（语法/类型/导入/未定义名等）从对应场景随机抽取台词

---

## 命令

在 VS Code 中按 `Ctrl+Shift+P`：

| 命令 | 说明 |
|------|------|
| `Launch deepseek Desktop Pet` | 一键启动桌宠（自动查找 Python，启动后确认服务器在线） |
| `Open Anime Assistant` | 打开 VS Code 内置助手面板（备用显示，按需打开） |

---

## 常见问题

### Q: 桌面宠物没有出现？

1. 确认已安装 pywebview：`pip show pywebview`（注意用你启动时的那个 Python）
2. 确认 Python ≥ 3.9：`python --version`
3. 用命令面板 `Launch deepseek Desktop Pet` 启动，失败会有具体错误提示
4. 手动运行 `python desktop_pet/standalone.py` 看控制台报错

### Q: 右键退出了 deepseek，怎么再打开？

重新执行 `Launch deepseek Desktop Pet` 命令，或运行 `start.bat` /
`python desktop_pet/standalone.py`。（v0.2.4 起右键菜单是「退出」而非「隐藏」，
旧版本隐藏后无法找回，只能结束 python 进程。）

### Q: 拖拽时窗口跳到屏幕左上角？

已通过 Python API `get_position()` 修复。如仍出现，确认 pywebview ≥ 4.0。

### Q: 窗口不是透明的 / 角色坐在一块灰白或纯黑色方块上？

**v0.3.1 起这条已经修掉了，根因是 DWM 的 Mica 背景材质**（不是 Form 底色，也不是 WebView2 的问题）。
pywebview 在**系统深色模式**下会给窗口装 Mica，浅色模式下 `#F0F0F0`、深色模式下 `#202020`，
看着就像一块不透明的方块。

如果还是方块，按顺序查：

1. **看日志** `desktop_pet/.airi-pet.log` 里有没有这行（正常应该长这样）：
   ```
   [airi-dwm] Mica backdrop disabled (hwnd=0x341194 backdrop=2->1 set=True guarded=True)
   ```
   - 没有这行 → `webview.start(_after_window_ready)` 没接上，或者 pywebview 版本变了
     （`_get_form()` 靠 `BrowserView.instances` 找窗体，pywebview 改内部结构就会失效）；
   - `backdrop=1->1` 说明装的时候就已经是 NONE，那我们看到的方块另有来源；
2. **确认是"深色模式下特有的"** —— 切到浅色模式（或改
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize\AppsUseLightTheme = 1`）
   方块没了，就基本锁定 Mica；
3. **看 `[airi-live2d] silhouette ON/OFF`** —— 这行只管**点穿**，跟透明无关。
   设过 `AIRI_SILHOUETTE=0` 只会让点击不再穿透，画面不会变。

> ✅ **这一条这次是在用户真实窗口上以屏幕像素验证的**（不是推理）：
> 关 Mica 前"与桌面一致 0.00%"，关掉后 90.49%，改回去又 0.00%。
> 取证脚本 `live2d_probe/probe_live_fix4.py`，数据在 `live2d_probe/live_fix4.txt`。

### Q: 角色周围有一块块状白色？

**根因在窗口，不在 CSS —— 第五轮已定位并修掉，并且有真窗口 A/B 数据。**

`ui.html collectUiRects()` 上报给 `SetWindowRgn` 的每个矩形都带 pad（气泡、名牌各一个）。
region 之外会被整块剪掉（漏出桌面），但 **pad 环在 region 之内、页面又什么都没画** ——
于是露出来的是宿主 Form 的底色。而 pywebview 的 `winforms.py:286-292` 在
`transparent=True` 分支里**忘了给 `Form.BackColor` 赋值**（那句赋值在 `else` 分支），
Form 就保持 WinForms 默认的 `SystemColors.Control`，浅色主题下 = **`#F0F0F0`**。

"白块 = DOM 盒 + pad" 这件事尺寸能逐个对上，所以不是猜：

| 位置 | DOM 盒 | + pad | 算出来 | 用户截图实测 |
|---|---|---|---|---|
| 气泡 | 212×36.6 | 5 | 222×46.6 | **222×46** ✓ |
| `Airi` | 33×15.6 | 3 | 39×21.6 | **40×20** ✓ |
| `online` | 37×14 | 3 | 43×20 | **44×20** ✓ |

修法（v0.3.5 修订，黑边彻底消灭）：颜色键路线**已弃用** —— 真窗实测发现颜色键会让
WebView2 内容**整窗鼠标穿透**（"看得见但点不着"，见 `live2d_probe/live_fix6.txt`：
8/8 个采样点全部命中桌面），且 `.NET TransparencyKey` 属性会把窗口打成
`alpha=0`（不可见 + 穿透，`winbg_fix6.txt`）。现在默认 **`dark`**：
`fix_window_background()` 只把 Form 底色压黑（.NET 属性访问全部
`BeginInvoke` 编组到 GUI 线程）。v0.3.4 曾用"pad 收窄到 1px"过渡，但方形
region 撞上圆角元素、pad 环、半透明像素都会跟黑底混出**黑边**。v0.3.5
双管齐下：`_apply_window_region` 升级为**同形状裁剪**（圆角矩形 +
三角形尾巴，按 `border-radius` 逐像素对齐），同时 `ui.html` 的气泡/
尾巴/缸壁/名牌板**全部改不透明绘制**，名牌由 `.pet-plate` 自己画板 ——
region 里不再有"页面没画的像素"，黑边无从产生。
`AIRI_WIN_BG=dark`（默认）/ `key`（opt-in 实验，本机会整窗穿透，勿用）/ `off`。

真窗口 A/B（`live2d_probe/probe_live_fix5.py`，数据见 `live2d_probe/live_fix5.txt`）：

| 指标 | `off` | `key`（默认） |
|---|---|---|
| 名牌区域里 `#F0F0F0` 像素 | 1475 / 7000（**21.07%**） | **0（0.00%）** |
| 气泡区域里 `#F0F0F0` 像素 | 133（0.42%） | **0（0.00%）** |
| 整个窗口里浅灰像素 | 4894（3.91%） | 2081（1.66%） |
| 与桌面像素一致 | 52.41% | **79.28%** |

（`key` 那次剩下的 1.66% 浅色是**模型自己的白美术** —— 蕾丝头饰 + 白书桌板，
正是底板要框住的东西本身，不是 bug。）

模型那一半**不可能靠改代码消掉**（它就在贴图里）。第五轮按需求把底板做成了
**蓝色水族箱**：缸壁 + 水面高光 + 斜射光柱 + 缸底沙地 + 上浮气泡，全部纯 CSS 渐变
（不用 `filter` / `backdrop-filter` —— 透明窗口上这两样都会出事）。

| `AIRI_CARD=` | 效果 |
|---|---|
| `off` | 不画（回归裸角色，白美术散在桌面上） |
| `tight` | 水族箱紧贴角色实测外接框 |
| **`square`（默认）** | 同上但取正方形 |
| `frame` | 只描一圈玻璃边，缸内照旧透出桌面 |
| `all` | 连名牌一起圈进缸里 |

卡的大小不是写死的：跟着**实测的角色外接框**走（`137×124`，而不是 `.character-box`
那个 160×210），并且前 6 次采样取并集后锁定 —— 不锁的话模型一呼吸卡就抖。
预览图：`live2d_probe/ui_preview.png`（三种模式纵向拼接）。

其余环境相关：

- Windows 10：需安装 Edge WebView2 运行时
- Windows 11：已内置，无需额外安装
- 非 Windows：窗口透明支持取决于系统 WebView

### Q: 点击角色没反应？

1. 点在**角色的实际像素**上才有用（点到透明区域不吭声，这是故意的）——
   试试点头部或身体中间。
2. **拖动不会触发点击**（移动超过 5 px 就只算拖拽）。轻轻点一下、别拖。
3. 防抖 600 ms，连点期间第二次会被忽略。
4. 台词池在 `desktop_pet/standalone.py` 的 `CLICK_LINES`，想加词改那里。

### Q: 气泡不显示？

桌宠窗口会自动重连 SSE。确认 19876 端口没被占用：`netstat -ano | findstr 19876`。

### Q: 如何换掉默认角色？

默认角色是 Live2D 模型（见上文「Live2D 角色」）。想换成立绘：

1. 设为静态图片 → 把你的图片放到 `desktop_pet/assets/character.png`，
   并设环境变量 `AIRI_LIVE2D=0` 关掉 Live2D（支持可选的情绪变体，见上文）。
2. 换成别的 Live2D 模型 → 把模型目录放进 `desktop_pet/assets/live2d/model/`，
   再改 `live2d_assets.py` 里的 `MODEL_ENTRY` 指向新的 `.model3.json`。
   注意：**本项目的集成是按这一个模型调的**（眼睛参数索引、眨眼时序），
   换模型可能要重新校准 —— 而且别的模型里 `ParamEyeLOpen` 未必存在。

### Q: 桌宠显示的是立绘而不是 Live2D？

**先看日志** —— `desktop_pet/.airi-pet.log`（每次启动重写，只留本次）。
里面有这次启动用的 Python、工作目录，以及 Live2D 自检的逐项结果：

```
[airi-live2d] model Version=3 closure=57 files, 57 present, 0 missing
[airi-live2d] cubism core 207155 bytes
[airi-live2d] live2d ENABLED          <-- 这行说明走的是 Live2D
[airi-live2d] silhouette ON           <-- 窗口形状裁剪（点穿）开着
[airi-dwm] Mica backdrop disabled (...)  <-- 这行说明 DWM 背景已关掉 = 画面真透明
```

如果没有这个文件，说明桌宠根本没启动起来（去 VS Code 的输出/通知里找错误）。

> **为什么必须靠日志**：VS Code 扩展是用 `stdio: 'ignore'` 起桌宠的
> （`src/extension.ts:launchStandalonePet`），**进程的所有输出都被丢弃**。
> 所以这个日志文件是唯一能看到「为什么降级」的地方。

日志里是 `live2d disabled (image fallback)` 时，按顺序排查：

1. 素材是否齐全：`python desktop_pet/live2d_assets.py` 会打印闭包自检结果
2. Cubism Core 是否下载成功（**首次运行需联网**，见上文「Live2D 角色」）
3. 显卡/驱动是否提供 WebGL（虚拟机、远程桌面下常常没有）
4. 是不是自己设过 `AIRI_LIVE2D=0`

日志里是 `ENABLED` 但屏幕上仍是静态立绘，说明是**页面内**失败（WebGL 上下文创建不出来）。
这种情况按 `F12` 打不开控制台，需要临时改一下 `ui.html` 里的 `initLive2D()`
把 catch 的内容画到页面上，或者把 `LIVE2D_ENABLED` 分支的降级去掉让它直接报错。

想让角色一定用 Live2D（宁可报错也不降级）时，去 `ui.html` 里把 `initLive2D()` 的
失败分支改成不调 `showImageCharacter()`。

---

## 素材来源与许可

Live2D 角色素材 **不是**本项目原创，按原许可使用：

| 内容 | 来源 | 许可 |
|---|---|---|
| 模型 `assets/live2d/model/ds-whale-girl/` | [A8Chann/dsh-pet-live2d](https://github.com/A8Chann/dsh-pet-live2d) | **CC BY-NC-SA 4.0** |
| `assets/live2d/vendor/live2d-vendor.js` | 同上（pixi.js + untitled-pixi-live2d-engine） | MIT |
| `assets/live2d/vendor/live2dcubismcore.min.js` | Live2D 官方 CDN | Live2D 专有，**不入库** |

模型版权链：**上善无形**（角色原作 OC「溟月」）→ **ZipZipPipe**（DeepSeek 女仆二创）
→ **氵六青**（本模型 Live2D 制作）。

CC BY-NC-SA 4.0 意味着：**可以自由使用和再分发，但必须署名、不得商用、
衍生作品需沿用同一许可**。素材目录下已随附原始 `LICENSE` 文件。
个人自用没问题；**商用需分别取得各权利人授权**。

### 侵权即删（Takedown notice）

**本项目无意侵犯任何人的权利。** 上述素材仅用于**个人桌面宠物**这一非商业用途，
按原协议**完整保留署名**，且**未作任何修改** —— 65 个文件与上游逐个 MD5 比对**完全一致**
（目录里的 ASCII 文件名是上游 `tools/build-pet.mjs` 构建时就有的，不是我们改的），
原 `LICENSE` 随目录一起保留。

如果**任何权利人**认为本仓库对素材的使用超出了授权范围 —— 无论是角色形象、模型绑定、
贴图、动作还是表情 —— 请通过下面任一方式联系：

- 在本仓库开 [Issue](https://github.com/Hancard/vscode-anime-assistent/issues)
- 或通过 GitHub 主页邮箱 / B 站私信

**我会立即删除相关内容，无需说明理由、不做争辩。** 请不必先走 DMCA 流程。

> **Takedown request**: The Live2D assets bundled here are **not** original to this project.
> They come from [A8Chann/dsh-pet-live2d](https://github.com/A8Chann/dsh-pet-live2d) under
> **CC BY-NC-SA 4.0** (Attribution–NonCommercial–ShareAlike), are used for a **personal,
> non-commercial desktop pet only**, are **unmodified**, and keep their original `LICENSE`
> file. If you are a rights holder and believe this repository exceeds your grant, please
> open an [issue](https://github.com/Hancard/vscode-anime-assistent/issues) —
> **the material will be removed immediately, no questions asked.**

---

## 开发

```powershell
# 编译
npm run compile

# 监听模式
npm run watch

# 代码检查
npm run lint

# 运行测试
npm test

# 打包 .vsix
npm install -g @vscode/vsce
vsce package
```

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 0.1.0 | 2026-06-23 | 傲娇人格、聊天气泡 UI、Python 后端、DeepSeek API 预留 |
| 0.2.0 | 2026-07-19 | **独立桌宠架构**：standalone.py 内置服务器 + SSE + pywebview，watcher.py 独立文件监听，情绪立绘系统 |
| 0.2.1 | 2026-09-13 | 重建丢失的 common.py（修复启动崩溃）、`Launch Airi Desktop Pet` 一键启动命令、健康探测、Webview 转义修复 |
| 0.2.2 | 2026-09-14 | all_clear 误报修复（双侧）、watcher 错误解析/语言ID修复、SSE 序号防错位、推送去重、补 publisher 字段 |
| 0.2.3 | 2026-09-16 | 错误分类误判修复（NameError 被错判为类型错误）、单文件 watcher 去重、情绪回落立绘复位 |
| 0.2.4 | 2026-09-19 | 诊断统计覆盖未打开文档、端口冲突防僵尸窗口、右键「隐藏」改「退出」、watcher 不写 __pycache__/只推 error/变更限流、Py3.9 兼容、环境变量补实现、移除 C_Cpp_Runner 配置与 helloWorld |
| 0.3.0（未发布） | 2026-09-22 | **Live2D 角色**：DS鲸鱼娘模型替代静态立绘（会呼吸/眨眼/按情绪切表情）；废掉临时文件改本地静态服务器（`file://` 下 WebView2 会 CORS 拦掉模型闭包）；Cubism Core 运行时自动抓取+校验；素材缺失或任何一步失败自动退回立绘/CSS 角色。窗口形状裁剪（页面从渲染结果提 alpha 轮廓 → `SetWindowRgn`，用于**鼠标点穿**，不改画面）；**点击角色说话**（alpha 命中 + 头/身体/桌面分区 + 台词池，拖拽不误触发）。⚠️ 本版声称"用 region 解决了透明"，**已被 0.3.1 推翻** |
| 0.3.1（未发布） | 2026-09-22 | **真透明（真正根因）**：pywebview 在系统**深色模式**下会给窗口装 DWM 的 **Mica 背景材质**（`DWMWA_SYSTEMBACKDROP_TYPE=DWMSBT_MAINWINDOW`），它由 DWM 绘制、不在窗口的 GDI 表面里 —— 所以 `SetWindowRgn` 裁不到、`LWA_COLORKEY` 挖不掉，只有整窗 `LWA_ALPHA` 有效。改为 `disable_window_backdrop()` 关掉它并防止切主题时被装回来。**用户真实窗口屏幕实测：0.00% → 90.49% 透出桌面**。同时修正：region 只负责点穿；气泡出现时强制刷新窗口形状（`setTimeout(pushSilhouette, 0)` 其实不传参） |
| 0.3.2（未发布） | 2026-09-23 | **修透明窗口的"块状白"**：`backdrop-filter`（透明窗口上没有"背后"可模糊，退化成糊一块不透明的矩形底，从圆角气泡四角露出来）/ `filter`（提升成独立合成层，与 `background-clip:text` 同用时最典型）/ 次像素抗锯齿（要求不透明的底）三条成因逐个拆掉。**新增角色底板 `AIRI_CARD`**：`off`/`tight`/`square`（默认）/`frame`/`all`，卡跟着 canvas alpha 的**实测外接框**走（137×124，而非写死的 160×210），采样 6 次取并集后锁定以免跟着 idle 动作抖，并进窗口形状。⚠️ 白块**在 headless Chrome 里复现不出**，三条修复是按机制推断、需目视确认 |

详见 [CHANGELOG.md](CHANGELOG.md)。
