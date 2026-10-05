# Change Log

## [0.3.17] — 2026-10-05

> 新功能：桌宠主动监视当前打开文件的 bug。此前桌宠只在**诊断发生变化的那一
> 刻**说话（签名去重），文件里的存量 bug 一直没人提。

### Added
- **当前文件 bug 监视（file scan watchdog）**：每 10 秒检查一次当前激活
  编辑器的文件（C/C++/Python），并按下面规则说话：
  * bug 内容/数量**变化** → 立刻播报，并点名前 3 条的行号与内容
  * 数量**未变但仍有 bug** → 每 60 秒提醒一次（换一套提醒台词，防刷屏）
  * 修到 **0** → 说一次"文件干净了"，之后保持沉默
  * 切文件自动跟随；干净文件不打扰
- 新增配置项：`airiMonitor.fileScan.enabled`（默认 true）、
  `airiMonitor.fileScan.repeatIntervalSec`（默认 60，最小 10）
- 语料新增 file_scan / file_remind / file_clear 三个场景；
  DeepSeek 路径同步支持 file_scan 提示词

### Fixed
- `classify_error_category()` 直接对 `message` 调 `.lower()`：外部 /push 传
  数字（`sample_errors: [{message: 123}]`）会 AttributeError 崩断连接。
  这是 v0.3.14 加固的更深一层——元素本身是 dict，但**字段值**类型不可信
- `sample_errors` 传字符串时被按字符拆成多条（"oops" → o/o/p/s），
  气泡里出现"第?行：o"这类垃圾，现在整体当一条
- `categorize_errors()` 跳过非 dict 元素（公共 API 不只走 generate_response）
- file_scan 在无后端（response_generator 导入失败）时也有内置兜底台词

## [0.3.16] — 2026-10-05

> VS Code 本体重装后可用版本为 1.83.1（update CDN 的 latest 只提供 1.83.1，
> 本机暂存的 1.140.0 缺 exe 无法拼装完成），故把扩展引擎声明降级以适配。

### Changed
- `engines.vscode`: `^1.110.0` → `^1.83.0`。扩展 API 面很窄（诊断集合、
  命令注册、child_process、output channel，均为早期版本 API），1.83 完全
  覆盖；`^` 向上兼容，将来 VS Code 升级不受影响。

## [0.3.15] — 2026-10-05

> 用户报：桌宠能启动但看不到前端页面，双屏可见、单屏不可见。
> 根因是 DPI 双重缩放，与屏幕数量只是间接相关。

### Fixed
- **DPI 双重缩放把初始位置放出屏幕外**（desktop_pet/standalone.py、main.py、
  common.py）：pywebview 的 `create_window(x=,y=)`/`move()` 按逻辑像素消费
  （winforms.py 内部 `Location = Point(x * GetDpiForWindow/96)`），而
  `get_screen_size()` 在 DPI-aware 进程里返回**物理像素**。150% 缩放 +
  2560 主屏时 x=2220 被放到物理 3330 —— 窗口整个在屏幕右侧之外。
  双屏拼接（虚拟桌面 4267 宽）时落进第二块屏所以一直"看起来正常"，
  单屏立刻暴露。
  - `common.get_dpi_scale(hwnd=None)`：GetDpiForWindow -> GetDpiForSystem
    -> GetScaleFactorForDevice 三级兜底；自带 SetProcessDpiAwareness，
    与调用顺序无关
  - 初始位置改为按物理尺寸（win_w*scale）右下对齐后除回 scale 转逻辑
  - `move_window` 拖拽钳制同步修坐标系：物理虚拟桌面边界先除 scale 转
    逻辑再钳 —— 修复前 150% 缩放下右/下边界钳制失效，单屏也能把桌宠
    拖出屏幕找不回来

### Tests
- 新增 `live2d_probe/test_window_dpi.py`：真 pywebview 窗口 + GetWindowRect
  量物理矩形，断言完整落在主屏物理范围内、物理尺寸=逻辑尺寸×scale、
  逻辑位置回读一致。修复前旧公式在本机（150%）实测越界 FAIL，修复后
  ALL PASS。需系统 Python 3.12（pywebview）。

## [0.3.14] — 2026-10-05

> 例行审计轮。桌宠已重启（VSIX 0.3.12 运行日志健康，v0.3.8–12 运行时修复
> 全部生效）。本轮补审最后一块未覆盖区域：python_backend/ 全部四个文件。

### Fixed
- **response_generator.py 外部输入加固**（python_backend/）：`generate_response`
  是后端 stdio 路径与 standalone /push 路径共用的大脑，但对外部输入不设防——
  * `sample_errors` 元素非 dict（外部 /push 可传 `items: ["字符串"]`）时
    `categorize_errors` 的 `err.get()` 直接 AttributeError
  * `error_count` 传字符串（`{"trigger": "diagnostics", "error_count": "3"}`
    走 standalone 的 trigger 直通路径）时 `error_count >= 5` 抛 TypeError
  修法：入口处统一归一化——`error_count` 强转 int（失败归 0），
  `sample_errors` 非 dict 元素包成 `{"message": str(e)}`，非法类型丢弃；
  归一化后的列表写回 context，`try_deepseek_api` 同样受益。

### Tests
- 新增 `live2d_probe/test_response_generator_hardening.py`：单元层 7 用例
  （字符串错误数组、混合类型、count 字符串/None、归一化分类正确性）+
  真服务器 HTTP 端到端 3 用例 + 队列内容合法性检查，13 项全 PASS。

## [0.3.13] — 2026-10-04

> 用户请求：继续检查 bug 并修复 push（例行审计轮）。审计覆盖此前未读过的
> watcher.py / main.py / 全部启动批处理 / requirements.txt / .gitignore /
> package.json，以及 ui.html 的首次**全量**通读（此前几轮只覆盖被改动区域）。

### Fixed
- **启动批处理纯 ASCII 化**（desktop_pet/launch_full.bat、start.bat）：两个
  文件的 echo 里有 UTF-8 中文（"完整启动"/"Airi 桌宠 - 一键启动"），而 cmd
  用 OEM 码页（简体中文 Windows = GBK）解码批处理——UTF-8 中文会被拆开并
  吃掉后续换行，cmd 把中文碎片当命令执行，**整个脚本解析崩掉、后面一行
  都不跑**。文件开头 `chcp 65001` 救不了（2026-09-22 已在本机实测实锤）。
  字节级复查：两文件原各含 12/18 个高位字节、且不是合法 GBK；改英文后
  三个 bat 全部 0 高位字节。
- **补提交 .vscodeignore**：`live2d_probe/**` 排除出 VSIX 打包——探针/校验
  工具是本机排查用的，不该跟着扩展发布（此改动躺在工作区漏了提交）。

### Audited（无问题）
- watcher.py：限流（每轮最多 20 个检查）、签名去重（含错误总数）、
  删除文件缓存清理、all_clear 触发条件均正确。
- main.py：旧入口保持最小改动，region/底色处理与 standalone 一致。
- ui.html 全量：历史修复（region 动画守卫、打字机锁高、SSE 续传、
  拖拽/点击判别、右键菜单收敛+确认推送）全部在位；剩余仅为无害死代码
  （backendExit 无发送方、errorAlert 的 `|| 'angry'` 恒不触发）。
- .gitignore 覆盖 .airi-pet.log 与 Cubism Core；requirements.txt 正常。

## [0.3.12] — 2026-10-03

> 用户请求：继续检查 bug 并修复 push（例行审计轮）。审计覆盖 ui.html 全部
> 前端逻辑、standalone.py /push 分发链、common.py 窗口/region 层、
> live2d_assets.py 素材链与 extension.ts 诊断转发。

### Fixed
- **右键菜单弹出瞬间不再被 region 短暂裁边**（ui.html）：菜单有 180ms 的
  scale(0.92)+translateY(-4px) 过渡动画，此前的立即推送量到的是动画中途的
  小一圈盒子，菜单边缘会被 region 裁掉几百毫秒才被周期轮询纠正——与气泡
  popUp 是同一类问题，气泡那边有 540ms 确认推送，菜单这边漏了。现在弹出
  230ms 后再确认推一次。
- **/push 对畸形 payload 不再炸链路**（standalone.py）：
  * `items: null` → 此前 `None[:5]` 抛 TypeError → 500 断连；
  * `count`/`error_count` 传字符串/null → 此前 `_builtin_reply` 的 `> 0`
    比较抛 TypeError（all_clear 与 diagnostics 两分支都修）；
  * `text` 传数字/对象 → 此前原样进队列，前端 `typeText` 的 `charAt` 直接
    崩（jsError 黑匣子报警、气泡空白）。非字符串一律安全序列化。
- **package.json repository.url 指向改名后仓库**（Hancard/vs-deepseek-desktop-pet），
  此前还写着旧用户名旧仓库名，全靠 GitHub 重定向兜底。

### Verified
- 新增 `live2d_probe/test_push_hardening.py`：真 HTTP 端到端，9 个畸形用例
  （items-null / count-str / count-null / text-number / text-dict / text-null /
  all_clear×2 / items-str）全部 200 且队列内容合法，正常 diagnostics 与
  chatMessage 路径回归通过。
- ui.html 三个 script 块 node --check 全过；standalone.py py_compile 通过。

## [0.3.11] — 2026-10-02

> 用户请求：继续检查 bug 并修复 push（例行审计轮）。审计覆盖 SSE 事件流、
> 打字机并发、拖拽/点击判别与桥 API 面完整性。

### Fixed
- **SSE 断线重连不再整批重播旧消息**（standalone.py）：此前每条事件不带
  `id:` 字段，EventSource 一次自动重连就会把最近 20 条（SSE_REPLAY_COUNT）
  旧消息当新气泡全弹一遍。现在每条事件带 `id: <seq>`，重连时按
  `Last-Event-ID` 头只补发缺口；全新连接维持原重播行为。续传基线从
  after_seq 起步——replay 为空时若停在 0，轮询循环会把整个队列再发一遍
  （真 socket 测试实测抓到）。
- **空文案不再创建空气泡**（ui.html）：后端返回空 text 时，`showBubble`
  此前仍会建一个不可见的空气泡，留一块空 region 挂 8 秒。

### Verified
- 新增真 socket 端到端测试 `live2d_probe/test_sse_resume.py`：全新连接重播、
  从中间续传、从最新续传（零补发）、畸形 Last-Event-ID 安全降级，四场景全过。
- 归档 e2e 推送测试（test_push_e2e.py）复跑 PASS；ui.html 三个 script 块
  node --check 全过。

## [0.3.10] — 2026-09-26

> 用户请求：继续检查 bug 并修复 push（例行审计轮）。桌宠自 9-23 起未重启、
> 无新运行时证据，审计深入到此前未覆盖的进程生命周期与 /push 分发链。

### Audited（无问题）
- 进程生命周期：当前无残留桌宠进程；双击状态栏的启动竞态由
  「绑定失败 → ping 探测 → 已有实例 exit(0)」兜住（standalone.py main）。
- extension.ts 诊断转发：15s 健康缓存、签名去重、all_clear 触发条件均正确。

### Fixed
- **all_clear 在无后端时被静默吞掉**（standalone.py /push）：`trigger`
  形状的消息走 `generate_response` 分支，后端不可用时什么都不做——
  diagnostics 分支有内置兜底台词，这条分支漏了。抽出
  `_builtin_reply(error_count)` 供两分支共用，all_clear 现在也有
  「哼，全部修好了…算你厉害。」的回应。
- 注：你机器上 response_generator 可导入，实际走的是后端路径；
  此修复保障后端缺失（换机/缺依赖）时的行为一致性。

### Verified
- `live2d_probe/test_push_e2e.py`：真 socket 起 HTTP 服务器线程，
  真 POST 到 /push —— 场景 A（强制无后端）兜底台词精确匹配；
  场景 B（真实后端）产出非空个性化回复。四项全过。

## [0.3.9] — 2026-09-25

> 用户请求：继续检查 bug 并修复（例行审计轮，无新用户报障；
> 昨晚 50 分钟会话日志无 JS 报错、无心跳滞后 —— v0.3.8 冻结治理实测有效）。

### Fixed
- **拖拽屏幕边缘钳制**（common.py）：`move_window` 之前把窗口左上角钳进
  虚拟桌面（多显示器拼接矩形，`GetSystemMetrics(76..79)`），保证至少 60px
  留在屏内 —— 之前可以把桌宠一路拖出屏幕找不回来（只能重启）。
  钳制失败时按原坐标移，移动永远不挂。纯函数 `clamp_to_virtual_screen`
  单独可测，边界用例见 `live2d_probe/test_window_clamp.py`。
- **右键菜单边缘收敛**（ui.html）：弹出前按菜单实测宽高 + 4px 呼吸收敛进
  窗口 —— 之前贴边右键时菜单溢出窗口被 OS 裁掉一截（region 只含窗口内
  部分，溢出块永远显示不出来）。

### Verified
- `clamp_to_virtual_screen` 9 组边界用例（四边出界 / 角落极限 / 双屏负坐标）
  全过；`get_virtual_screen()` 真机返回双屏拼接矩形。
- ui.html 主 script 块 node --check 通过；verify_pet_ui / verify_silhouette_js
  （26/26）/ verify_click_region（系统 Python 3.12 + pywebview）全过。

## [0.3.8] — 2026-09-23

> 用户报："效果没改好，重改"（附截图：最新气泡的顶边被水平切掉一截，
> 且两条气泡一分钟后仍未按 8 秒规则自动消失）。

### 诊断（.airi-pet.log + 无头浏览器复现）
- 气泡 8 秒自动消失靠 2s `setInterval`，截图时两条气泡已存在 ~40s 仍未消失
  → **页面 JS 定时器停摆/被冻结**（全透明异形窗口易被 Chromium 可见性启发式
  误判为后台页而节流）。
- 定时器一死，500ms 的 region 轮询也死 → 窗口形状冻结在**最后一次推送**。
  若最后一推落在气泡 popUp 动画进行中，`getBoundingClientRect` 量到的是
  "位移+缩放"后的盒子（偏低十几像素）→ region 把最新气泡顶边永久裁掉一截。
- Python 侧 region 日志只在状态变化时打印，"只有 region #1"属正常，
  不能当"region 没跑"的依据。

### Fixed
- **region 不再吸收动画中途的过期矩形**：气泡增删时打点
  `lastBubbleChangeAt`，周期轮询在 520ms 动画窗口内跳过推送；
  `showBubble` 在动画第一帧之前（最终位置）推一次，动画结束 540ms 再确认一次。
  就算之后页面被冻结，窗口形状也停在**正确**形状上。
- **fitBubbles 单气泡兜底**：只剩一条仍溢出时，把它的 max-height 压到
  可用高度，在气泡圆角内截断，绝不让窗口上沿切字（9 轮旧版此路直接 return）。
- **气泡排序纠正**：`.bubble-area` column-reverse → column。appendChild 的
  最新气泡现在落在最下、贴着角色，尾巴（朝下）指向角色而不是指着旧气泡；
  溢出时被裁的是最旧的（在最上方）。

### Changed
- **禁用 WebView2 后台节流**：`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`
  加 `--disable-background-timer-throttling --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows --disable-features=IntensiveWakeUpThrottling`，
  治定时器停摆的根（透明窗口被误判后台页）。
- **JS 心跳诊断链**：ui.html 每 2s 经桥调 `WindowAPI.pet_heartbeat()`；
  Python 记录时间戳，region 调用与消息推送时发现心跳滞后 >3s 就写日志 ——
  这条链路以前是完全静默的。
- 新增探针 `live2d_probe/gen_ui_probe.py` / `check_ui_js.py`：无头 Edge
  渲染 ui.html 验证气泡布局（自报矩形到页面内，截图即诊断）。

## [0.3.7] — 2026-09-23

> 用户报："互动触发回复的时候字都揉在一起了"（附截图：上方一条碎气泡
> 被窗口上沿横向切开，文字半截半截叠着）。

### Fixed
- **气泡被窗口上沿切开**：`MAX_BUBBLES` 3→2 —— 窗口 443px 高、气泡区只有
  ~150px，三条气泡必然把最旧的顶出窗口外被切成两截（截图里的碎气泡）。
  另加 `fitBubbles()`：气泡区顶边越过视口顶就**立刻**从最旧开始移除，
  绝不让窗口边缘切字。
- **打字机过程跳动**：先放整段文字量出最终高度并锁死盒子，再清空逐字打 ——
  打字过程中盒子不再逐行长高、把旧气泡一路往上顶。
- **flex 压扁**：`.bubble` / `.bubble-area` / `.character-wrapper` 补
  `flex-shrink: 0`，空间不足时宁可溢出（由 fitBubbles 裁），不许被压扁。

### Changed
- 气泡排版：12px/1.55 → **13px/1.7 + 0.5px 字距**，padding 9×15；
  `max-height: 132px` 兜底（超长回复最多约 5 行，在气泡自己的圆角内截断）。

## [0.3.6] — 2026-09-23

> 用户报："报错还在，看不到前端界面"。

### Fixed
- **region 全军覆没的致命笔误**（v0.3.5 引入）：`CreateRectRgn` /
  `CreateRoundRectRgn` / `CreatePolygonRgn` 是 **gdi32.dll** 的导出，
  v0.3.5 错写成 `user32.CreateRectRgn` —— `py_compile` 查不出，
  运行时每次形状上报都抛 `AttributeError`，窗口 region 一次都没应用过
  （表现：窗口黑块 / region 不生效）。`probe_region8.py` 直调验证 PASS。
- **preLaunchTask** 从常驻 `npm: watch` 换成一次性 `npm: compile`：
  `tsc` CLI 实测零错误，"任务检测到错误"弹窗与 16 条 IDE 报错都是
  BOM 时代的缓存残留；一次性编译让 F5 变成确定性流程。
- region 异常日志现在带完整 traceback（`region #N EXCEPTION` 多行）。

### 注
- 截至本轮，`live2d_probe` 探针环境里 pywebview 透明模式的 JS 桥仍无法
  注入（与产品代码无关），桥上链路的最终确认依赖用户真机。

## [0.3.5] — 2026-09-23

> 用户报："黑边又回来了"。第六轮的"最小 pad + 细黑边"是过渡方案，
> 本轮把黑边**彻底**消灭：region 与页面绘制**同形状**，且页面全部不透明。

### Fixed
- **黑边根因**：dark 模式下 region 里凡页面没画的像素都露黑底 Form。
  来源有三 —— ① 方形 region 撞上圆角元素，四角露出弧形黑缺角；
  ② pad 环整圈露黑；③ 半透明像素（气泡 0.92、尾巴 0.45、缸壁 0.85）
  跟黑底混出发暗。三个一起修：
  - **region 同形状化**（`common._apply_window_region` 重写）：
    支持圆角矩形（`CreateRoundRectRgn`）与多边形（`CreatePolygonRgn`），
    按元素的 `border-radius` 逐像素对齐裁剪；纯矩形协议保持兼容。
  - **页面绘制全部不透明化**（`ui.html`）：气泡底 `--bubble-bg` 改实色、
    气泡渐变边框换算成不透明等效色、尾巴与气泡同色实色、缸壁 `#e4f6ff`
    实色、box-shadow 改 inset（原外圈本来就被 region 裁掉）。
  - **名牌改成真的板**：`.pet-plate` 包住名字/状态，不透明深蓝底 +
    圆角 + 粉描边 —— 原来"黑方块"是 Form 底透出来的，现在由页面自己画。
- **右键菜单被 region 裁成不可见**（顺带修掉的存量 bug）：菜单弹出/收起
  现在都会重推窗口形状，`collectUiRects()` 含菜单矩形（rad=10）。
- **package.json 的 UTF-8 BOM**（上一轮引入）：PS `Set-Content -Encoding
  UTF8` 自带 BOM，扩展扫描器解析失败 → 调试宿主里整个扩展不加载，
  状态栏按钮/命令全消失（commit `5b59dcb`）。`npm run compile` 实测
  exit=0，IDE 里 16 个 TS 报错是 BOM 时代 tsserver 的缓存残留。

### Verified
- `verify_pet_ui` / `verify_silhouette_js` / `verify_click_region` /
  py_compile 全部通过；UI 预览四变体重新生成。

## [0.3.4] — 2026-09-23

> 用户报回归："点击没反应、也不能拖动"。本轮把输入链路测成了数，
> 找到两个**真窗口级别的硬伤**，并给前端装上了"黑匣子"。

### Fixed
- **`.NET TransparencyKey` 会把窗口打成"不可见 + 鼠标穿透"**（第六轮根因一）
  - 隔离实验（`live2d_probe/winbg_fix6.txt`）：给干净 Form 赋
    `TransparencyKey`，`GetLayeredWindowAttributes` 读回
    `flags=LWA_ALPHA alpha=0` —— **不是** `LWA_COLORKEY`。
    alpha=0 的分层窗口 = 看不见 + 对鼠标完全穿透。
  - 修法：不再碰 .NET 属性，改为 ctypes 直接 `SetLayeredWindowAttributes`，
    并**打完读回验证**（返回成功≠状态正确）。
- **颜色键（LWA_COLORKEY）整窗鼠标穿透，且键色在本机不可控**（第六轮根因二）
  - 真窗实测（`live2d_probe/live_fix6.txt`）：颜色键状态下
    `WindowFromPoint` 对窗口内 8/8 个采样点全部命中**桌面**
    （"Program Manager"）—— WebView2 的 DirectComposition 内容不在
    颜色键的鼠标判定面（GDI 表面）里，键控后整窗穿透，
    表现正是"**看得见但点不着**"。
  - 手动 `SetLayeredWindowAttributes(LWA_COLORKEY, #010203)` 五种写法
    （windll/WinDLL/显式 DWORD/Show 后再打/再打一次）键色读回**全部是
    `#000000`**（`colorkey_test2.txt`）—— 挖洞行为不可控。
  - **结论：颜色键路线整体弃用。** `AIRI_WIN_BG` 默认从 `key` 改为
    **`dark`**（只把 Form 底色压黑，完全不碰分层 —— 输入路径与 v0.3.2
    完全一致）。`key` 保留为 opt-in 实验，`off` 不变。
- **"块状白"改用"最小 pad + 细黑边"方案**
  - Form 底色压黑后，pad 环露出的就是细黑边（视觉上是一条投影线）。
  - `collectUiRects()` pad 从"气泡 8 / 名牌 4"收窄为 **1px**；
    气泡尾巴（盒外 14×7 三角形）改为**单独成矩形**并进 region，
    不再靠整条 8px 的 pad 带。
- **跨线程 WinForms 属性访问全部 BeginInvoke 编组到 GUI 线程**
  - `_after_window_ready` 跑在 pywebview 的后台线程；v0.3.3 在那里直接
    赋 `form.BackColor`/`TransparencyKey`（后者内部 `RecreateHandle()`）。
    WinForms 控件非线程安全，跨线程属性访问有把 GUI 线程搞挂的风险
    —— 现在一律 `form.BeginInvoke(MethodInvoker(...))`。
- **前端 JS 黑匣子**：`window.onerror` / `unhandledrejection` → HTTP 上报
  宿主日志（`[airi-js] ERROR ...`）；新增 `WindowAPI.ping()` 桥自检，
  前端启动时立即 + `pywebviewready` 各打一次（`[airi-js] bridge ping ...`）。
  再出"点击没反应"时，日志能直接分辨：页面崩了 / 桥没注入 / 前端没命中。

### Verified
- 真窗实测：`dark` 模式窗口**非分层**（无 alpha、无颜色键），
  输入路径与 v0.3.2 一致；`key` 模式整窗穿透（即本轮修掉的回归机制）。
- `verify_pet_ui` / `verify_silhouette_js` / py_compile 全部通过。

## [0.3.3] — 2026-09-23（未发布）

> 这一版**推翻了 0.3.2 对"块状白"的判断**，并且这次是在**真窗口**上做了 A/B，
> 不是"按机制推断"。0.3.2 的那三条 CSS 修复方向不对，留着只是因为它们本来是
> 透明窗口上的坏习惯；真正的原因在窗口层面，见下。

### Fixed
- **"块状白"的真正根因：窗口 region 的 pad 环里露出宿主 Form 的底色**（第五轮）
  - pywebview `winforms.py:286-292` 在 `transparent=True` 时只做了
    `SetStyle(SupportsTransparentBackColor)` + `browser.DefaultBackgroundColor = Transparent`，
    **没有给 `Form.BackColor` 赋值** —— 走透明分支时那句赋值在 `else` 里。
    于是 Form 保持 WinForms 默认底色 `SystemColors.Control`，浅色主题下 = **`#F0F0F0`**。
  - 为什么平时看不出来：窗口被 `SetWindowRgn` 裁成了 DOM 元素的形状，region 之外
    整个窗口被剪掉、显出来的是桌面。而 `ui.html collectUiRects()` 给每个矩形都加了
    pad（气泡 5、名牌 3），**那一圈 pad 环里页面什么都没画** ⇒ 露出 `#F0F0F0`。
  - **尺寸逐个对得上**（这就是"证据"，不是猜）：
    | 位置 | DOM 盒 | + pad | 算出来 | 用户截图实测 |
    |---|---|---|---|---|
    | 气泡 | 212×36.6 | 5 | 222×46.6 | **222×46** ✓ |
    | `Airi` | 33×15.6 | 3 | 39×21.6 | **40×20** ✓ |
    | `online` | 37×14 | 3 | 43×20 | **44×20** ✓ |
  - **修法**：`common.fix_window_background()` 把 Form 底色设成 `#010203`（画面上不可能的
    颜色），同时拿它当 `TransparencyKey` ⇒ 页面没画的像素被**真挖成洞**（桌面透出来，
    而且自动点穿）。`AIRI_WIN_BG=key`（默认）/ `dark`（只压暗、不打洞，退路）/ `off`。
  - ⚠️ 第三轮试过 `LWA_COLORKEY` 说"挖不掉底色"，原因是**键色猜错了**：拿 `#202020`
    去挖 `#F0F0F0` 的底，自然一像素都不动。本轮又顺手复现了一次同类错误 —— 探针
    把键色取成画面里最亮的 `#FFFFFF`，结果"与桌面一致"反而 52.41% → 35.58%，
    还把模型自己的白美术挖出洞。**键色必须等于宿主底色，不能凭"看起来像"。**
- **真窗口 A/B 实测**（`live2d_probe/probe_live_fix5.py`，同一台机器、同一位置各起一次）：
  | 指标 | `AIRI_WIN_BG=off` | `AIRI_WIN_BG=key` |
  |---|---|---|
  | 名牌区域里 `#F0F0F0` 像素 | **1475 / 7000（21.07%）** | **0（0.00%）** |
  | 气泡区域里 `#F0F0F0` 像素 | 133（0.42%） | **0（0.00%）** |
  | 整个窗口里浅灰像素 | 4894（3.91%） | 2081（1.66%） |
  | 画面上出现最多的色 | `#FFFFFF` / **`#F0F0F0` 0.95%** | `#FFFFFF`（`#F0F0F0` 已消失） |
  | 与桌面像素一致 | 52.41% | **79.28%** |
  （"key"的那次剩下的 1.66% 浅色是**模型自己的白美术**：蕾丝头饰 + 白书桌板，
  也就是底板要框住的东西本身，不是 bug。）
- **region 的 pad 放宽**：气泡 5→8、名牌 3→4。以前 pad 环会露底色所以越小越好，
  现在环是真透明的，pad 大一点反而对点击手感好。**气泡的 pad 必须 ≥ 7** ——
  `.bubble::after` 那个尾巴是往下伸 7px 的，pad 小于它尾巴会被 region 裁掉。

### Changed
- **角色底板从白色改成蓝色水族箱**（用户要求："把角色周围的白块改成蓝色水族箱的环境"）：
  缸壁（2px 玻璃边）+ 水面高光 + 斜射光柱 + 缸底沙地 + 4 颗上浮气泡，全部纯 CSS 渐变，
  **不用 `filter` / `backdrop-filter` / `mix-blend-mode`**（透明窗口上这三样都会出事）。
  位置大小仍然跟着 canvas alpha 的实测外接框走（`AIRI_CARD` 五种模式不变，
  `frame` 模式会隐藏缸内装饰只留边框）。

### Note
- 0.3.2 里"headless Chrome 复现不出、修复是按机制推断"那段**仍然成立**，
  但它推的那三条不是根因。本轮改用**真 WebView2 窗口 + 屏幕像素**做判据，
  才把这件事真正钉死。教训：**复现不出来的时候不要退回"按机制推断"，
  要换一个能把现象量出来的判据**（这里是"屏幕上有多少像素等于 #F0F0F0"）。

## [0.3.2] — 2026-09-23（未发布）

> `package.json` 同时从 `0.2.4` 补到 `0.3.2` —— 之前它落后 CHANGELOG 两个版本
> （0.3.0 / 0.3.1 都已写好但没同步），在 VS Code 里看版本号会以为是旧版。

### Fixed
- **透明窗口上"块状白"的三个成因，逐个拆掉**（用户第三次反馈，这次从像素上量清楚了）：
  1. **`.bubble` / `.context-menu` 的 `backdrop-filter` 删除** —— 透明窗口上没有"背后"
     可模糊，Chromium 会退化成给这一层糊一块不透明的底。气泡本身是圆角的，
     而这块底是矩形，于是**从圆角四角露出来**，正是用户说的"角色上方的对话框也有这情况"。
     气泡本来就有 92% 不透明的底色，这层模糊既看不出效果、又只制造问题。
  2. **`.pet-name` 的 `filter: drop-shadow()` 删除** —— 这个名字用的是
     `background-clip: text` + `color: transparent` 做渐变字，"文字裁剪层"再叠一个
     `filter` 会多出一层独立合成层，透明底上这层的底可能没被清成透明。
  3. **全局 `-webkit-font-smoothing: antialiased` / `-moz-osx-font-smoothing: grayscale`** ——
     次像素（ClearType）抗锯齿要求底不透明；页面底透明时，文字的栅格化缓冲区会被
     按不透明处理，出来就是"与文字同宽的一个方块"。
     另外删掉了 `.bubble.old` 的 `filter: saturate(0.6)`（同类风险，且被 `opacity:0.45` 盖过）。
- **取证方式**：`_analyze_blocks.py` 对用户截图做连通域分析，把"白"分成两类 ——
  DOM 画的规则矩形（矩形度 0.798、`rgb(241,228,233)`、正好盖住 `.pet-name`+`.pet-status`）、
  和模型自带美术（矩形度 0.4~0.6，如白书桌板 0.573）。**只有前者是 bug**。
- ⚠️ **诚实边界**：`probe_ui_blocks.py` 用真 Chrome 逐条 CSS 做 A/B（去 `filter` / 去
  `backdrop-filter` / 不用 `background-clip:text` / 两两组合），**五个变体都不画那块底** ——
  即 headless Chrome 复现不出这个现象，**上述三条修复是按机制推断的，不是在 Chrome 里看着它消失的**。
  需要重启桌宠后目视确认（旧进程 pid 44164 跑的还是老代码）。

### Added
- **角色底板 `AIRI_CARD`**（把散开的碎白拢成一张干净的卡）：
  `off` / `tight` / `square`（默认）/ `frame` / `all`，详解与五联对比图见
  `live2d_probe/README.md` 第四轮 + `live2d_probe/ui_cards.png`
  - 位置来自 canvas alpha 的**实测外接框**（137×124 @ `[85,209,222,333]`），
    不是写死的 `.character-box` 160×210 —— 那比角色大一圈，"最小"就落在这里
  - **采样 6 次取并集后锁定**：模型一直 idle（呼吸/微摆），外接框每次都差几像素，
    卡跟着变就是每 500ms 抖一下，比白边难看得多
  - 底板必须**并进窗口形状**（`collectUiRects()` 里 `pairs.push([charCard, 0])`），
    否则实心卡会被 region 裁掉，表现成"卡只显示一部分"
  - `z-index: 0` 落在 canvas（`z-index: 1`）之下、`pointer-events: none` 不吃拖拽，
    并且**只在 Live2D 就绪后**显示（图片兜底时 z-index:0 会盖住 `#charImg`）
  - `all` 是**兜住不确定性**用的：名牌那块白复现不出、也就无法保证修掉，
    并进白卡之后即使还在也看不见
- **`pushSilhouette()` 结构拆分**：原来是 `if (!SILHOUETTE || !live2d.ready) return;`。
  底板要的是"角色在哪儿"，跟"要不要异形窗口"是两件事，拆成先判 `live2d.ready`、
  再更新底板、最后才判 `SILHOUETTE` —— 这样关掉点穿也能有底板。

### Changed
- `verify_click_region.py` 断言 31 → 39
- **断言改为先剥 CSS 注释再查**：本轮"删代码 + 写注释说明为什么删"的写法会让
  `'backdrop-filter:' not in UI` 把注释判成违规（注释里就写着这行曾经存在），
  于是**修得越认真越 FAIL**。断言要查的是生效的 CSS，不是散文。
- `verify_silhouette_js.py` 首次读 alpha 加了**重试**（20×150ms）：`live2d.ready` 一翻真
  就 `readPixels`，那一帧可能还没画进 framebuffer，读回全 0 → 轮廓空 → 26 条断言里 6 条 FAIL。
  三个脚本连着跑时必现、单独重跑立刻 26/26 全过，**这 6 条 FAIL 全是假的**。
  结果文件新增 `轮廓读重试 : N 次`，用来区分"真坏"和"这一眼没看准"。

## [0.3.1] — 2026-09-22（未发布）

### Fixed
- **窗口不是透明的（真正根因：DWM 的 Mica 背景材质）**：
  pywebview 的 `BrowserForm.update_title_bar_theme()` 在**系统深色模式**下会执行
  `DwmSetWindowAttribute(hwnd, 38, 2)`（`DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_MAINWINDOW`），
  给窗口装上一层 Mica；浅色模式下它设的是 `1`（`DWMSBT_NONE`），所以**不装**。
  Mica 由 DWM 绘制、**不在窗口的 GDI 重定向表面里**，于是：
  `SetWindowRgn` 返回成功但屏幕一像素不裁、`LWA_COLORKEY` 返回成功但挖不掉，
  只有整窗 `LWA_ALPHA` 动得了它。
  这也解释了两张截图的颜色差异 —— 浅色 `#F0F0F0` / 深色 `#202020`
  （`SystemColors.Control` 恰好也是这两个值，所以上一轮误判成"宿主 Form 底色"）。
  修复：`common.disable_window_backdrop()` 把 backdrop 设回 `NONE`，
  并把 `form.update_title_bar_theme` 包一层，防止**切系统主题时 Mica 被重新装回去**；
  调用点 `webview.start(_after_window_ready)`（Form 是 start 内部才创建的，所以轮询等它出现）。
  **实测（用户真实窗口 + 屏幕 BitBlt）**：关 Mica 前窗口与桌面一致 `0.00%`，
  关掉后 `90.49%`，改回又 `0.00%`。取证见 `live2d_probe/live_fix4.txt`、`probe_live_fix4.py`
- **上一轮"改 use `SetWindowRgn` 实现透明"的结论是错的**：region 只影响**鼠标命中**，
  不改变画面（WebView2 走 DirectComposition 合成，父窗口 region 裁不到它）。
  上一轮判据用的是 `GetWindowRgn` 返回值 —— **API 读回成功 ≠ 屏幕上生效**。
  已把 region 的定位改成"点穿"，画面透明交给 Mica 修复
- **气泡出现时没有强制刷新窗口形状**：`setTimeout(pushSilhouette, 0)` 按规范是
  **不带参数**调用的，所以 `force` 一直是 `undefined`。改成
  `setTimeout(function() { pushSilhouette(true); }, 0)`，与"气泡消失"那条路径一致

### Changed
- 日志新增 `[airi-dwm] Mica backdrop disabled (hwnd=... backdrop=2->1 set=True guarded=True)`，
  一行就能看出当时读到的 backdrop、改成什么、防重复包装是否装上
- `live2d_probe/` 新增一整类**活窗口取证**脚本（不重启桌宠、直接对用户正在跑的窗口做实验）：
  `probe_live_window.py` / `probe_live_alpha.py` / `probe_live_fix2.py` / `probe_live_fix3.py` /
  `probe_live_fix4.py` / `probe_verify_fix.py` / `probe_bridge.py` / `find_webview.py`
- `verify_click_region.py` 断言 29 → 31（把两条查"已删代码"的陈旧断言换成新契约），
  并修掉 `GetPixel` 未声明 `argtypes` 导致的 64 位句柄 `OverflowError`

## [0.3.0] — 2026-09-22（未发布）

### Added
- **Live2D 角色**：DS鲸鱼娘模型替代静态立绘，会呼吸、随机眨眼、按情绪切表情。
  素材在 `desktop_pet/assets/live2d/`，启动时由 `live2d_assets.py` 自检
- **窗口形状裁剪（点穿）**：页面从渲染结果的 alpha 通道提轮廓（角色 + 气泡 + 名字 + 状态条），
  每 500 ms 上报给 Python，由 `SetWindowRgn` 裁出窗口形状 —— 形状之外的鼠标点击落到下层窗口。
  ⚠️ **画面透明不靠它**，见 0.3.1
- **点击角色说话**：按 alpha 命中判定 + 头/身体/桌面三区分配，各给不同台词与情绪；
  与拖拽共用 `mousedown`，移动超过 5 px 只算拖拽；防抖 600 ms
- 环境变量 `AIRI_SILHOUETTE=0` 关闭形状裁剪（与 `AIRI_LIVE2D` 相互独立，
  立绘/CSS 角色同样适用）
- 环境变量 `AIRI_CUBISM_CORE` / `AIRI_ASSET_PORT` 见 README
- `live2d_probe/` 验证沙盒：端到端渲染、形状裁剪/点击、静态服务器、扩展启动条件复刻

### Changed
- **废掉 `create_temp_html()`，改本地静态服务器**：窗口指向 `http://127.0.0.1:<port>/`。
  `file://` 下 WebView2 会对 fetch/XHR 一律 CORS 拦截，`model3.json` 的闭包（57 文件）
  一个都取不到；同源 HTTP 一次解决，顺带绕开 WebView2 对本地磁盘脚本的路径白名单
- **Cubism Core 运行时自动抓取 + 校验**：`live2dcubismcore.min.js` 是专有运行时、
  不入库，首次启动从官方 CDN 抓取并缓存；带浏览器 UA（裸请求返回 403）+ 长度与符号校验
- **`handle_error()` 覆盖**：静态服务器与 standalone 服务器的 `ConnectionResetError` /
  `BrokenPipeError` 不再打成一整段 traceback（客户端关窗/刷新是正常收尾路径）
- `standalone.py` 启动日志新增 `live2d ...` / `silhouette ON|OFF` 行，
  用于排查"为什么降级/为什么没裁剪形状"

### Fixed
- 轮廓为空或全部被裁光时**拒绝应用** region，避免窗口退化成零面积
- ⚠️ 本版本原本声称"用 `SetWindowRgn` 解决了透明"，**该结论已被 0.3.1 推翻并修正**：
  region 不改变画面。0.3.0 未发布，此处保留记录以免以后重蹈覆辙

## [0.2.4] — 2026-09-19

### Fixed
- **关掉报错文件后误报 all_clear**：`vscode.languages.getDiagnostics()` 会保留已关闭文档的诊断，
  而旧逻辑只统计打开的文档，关掉报错文件就触发"全部清零"。现在按工作区整体诊断统计；
  已关闭文档的语言按扩展名推断（.py/.c/.cpp/.h 等）
- **端口冲突防僵尸窗口**：`AIRI_STANDALONE_PORT` 被占用时启动 standalone.py，
  现在通过 `_ping_ok()` 区分"已有 Airi 实例"（静默退出）与"无关进程占用端口"（报错退出），
  不再弹出第二个连不上服务器的桌宠窗口
- **右键「隐藏 Airi」改为「退出 Airi」**：隐藏后窗口无恢复入口且进程仍占端口，属 UX 陷阱；
  现在 `exit_app()` 销毁窗口并结束进程，重开用启动命令或脚本
- **watcher 不再写 `__pycache__`**：Python 检查从 `py_compile` 子进程改为进程内 `compile()`
  （`tokenize.open` 处理编码声明），检查目录不再生成缓存文件
- **watcher 只对 gcc error 响应**：原来 warning 也计入错误数，编译警告会打断"全部清零"的判断。
  现在只收集 `error:` 行
- **watcher 变更限流**：单轮扫描最多检查 20 个变更文件（MAX_CHECKS_PER_SCAN），
  git checkout 等大批量变更不再卡住轮询；未检查的文件顺延到下一轮重查（mtime 延迟提交）
- **watcher 签名含错误数**：内容签名加入 error_count，错误数量变化时能正确触发推送
- **ui.html 同情绪消息不再吞掉回落计时**：相同情绪连续推送时计时器现在会重置，
  气泡/表情的 8 秒回落与新消息对齐；无边框窗口上屏蔽 Chromium 默认右键菜单
- **standalone.py 恶意/异常 Content-Length 防护**：/push 与 /event 的请求头解析包 try/except，
  非数字不再抛异常中断服务器线程
- **python_backend Py3.9 兼容**：`str | None` 注解改为 `Optional[str]`，3.9 导入不再崩溃
- **launch_full.bat 编码修复**：GBK+LF 转为 UTF-8+CRLF，与文件头 `chcp 65001` 匹配

### Changed
- **环境变量补实现**：`.env.example` 声明的 `AIRI_STANDALONE_PORT`（扩展 + standalone.py 双侧）
  与 `AIRI_CHARACTER_IMAGE`（立绘路径覆盖）现在真正被代码读取
- **Webview 面板改为按需打开**：启动时不再自动弹出，用命令 `Open Anime Assistant` 打开
- **移除 C_Cpp_Runner 本机配置**：`.vscode/settings.json` 与 `launch.json` 中的
  C_Cpp_Runner 条目（含机器特定编译器路径）从仓库删除，恢复项目可移植性
- **移除 helloWorld 命令**：扩展命令与对应测试一并删除
- **`.vscodeignore` 排除 `__pycache__`/`*.pyc`**：vsce 打包不再混入 Python 缓存文件
- README 对齐实际行为（Python ≥ 3.9、退出菜单、协议示例 language 字段、命令表、FAQ）

## [0.2.3] — 2026-09-16

### Fixed
- **错误类别误判**：type 关键词 `"is not"` 排在 name 之前，Python 的
  `name 'x' is not defined`（NameError）会被误判为 type_error，永远选不到
  "未定义变量"场景的台词。现在按"具体类别优先"重排（syntax → name → import → type），
  并补充 `convert` 关键词。12 条真实错误消息分类测试全部通过
- **watcher.py 单文件模式去重**：原来只比较错误数量，数量相同但错误内容变化时
  不再推送；改为与目录模式一致的内容签名去重（并保留启动时干净文件不打扰的行为）
- **ui.html 情绪回落不完整**：情绪 8 秒超时后 CSS 表情恢复 idle，但自定义立绘
  仍停留在情绪变体图片（如一直显示 character_angry.png），现在同时切回默认立绘
- **standalone.py 启动问候兜底**：Python 后端不可用时的问候气泡从 `...`
  换成有内容的台词（上一版只修了 /push 路径，漏了启动路径）

## [0.2.2] — 2026-09-14

### Fixed
- **all_clear 误报**（扩展 + watcher 双侧）：只要本次变化的文件没有错误就发"全部清零"，
  哪怕其他文件还有错误。现在扩展按工作区整体诊断判断，watcher 按文件缓存错误后汇总判断
- **watcher.py Python 错误解析**：单个语法错误被 stderr 逐行拆成 2~3 条"错误"导致计数虚高，
  现在只取异常摘要行（`SyntaxError: ...`），最多返回一条
- **watcher.py 语言 ID**：`.cpp/.cxx/.cc` 文件报错时 languageId 错标为 `c`，现在正确标为 `cpp`
- **standalone.py SSE 队列错位**：队列被裁剪后已连接客户端的索引失效，会丢消息或重发消息。
  改为每条消息带递增序号，客户端按序号取增量；新连接只重播最近 20 条（原来重播整个队列）
- **重复推送**：相同错误状态不再重复推送到桌宠（扩展侧和 watcher 侧都加了内容签名去重），
  Airi 不会就同一批错误反复吐槽；watcher 启动时项目本身干净也不再打断问候语
- **standalone.py**：`language` 字段现在透传给回复生成器（原来硬编码 'unknown'）；
  Python 后端不可用时的兜底气泡从 `...` 换成有意义的台词
- **package.json**：补上缺失的 `publisher` 字段（原来 `vsce package` 会直接失败）和 `repository`
- **extension.test.ts**：扩展 ID 写错（`Mr.hancard.*`），按 publisher 修正；
  新增 `launchPet` 命令注册测试

## [0.2.1] — 2026-09-13

### Added
- `Launch Airi Desktop Pet` VS Code 命令：在扩展内一键启动桌宠
  （自动查找 Python → 分离进程运行 `desktop_pet/standalone.py` → 探测 19876 端口确认启动成功）
- `checkStandaloneAlive()` 健康探测（15s 缓存）：桌宠服务器未运行时不再发送无意义请求
- 重建 `desktop_pet/common.py`（此前被误删且从未提交，导致 standalone.py / main.py
  启动即崩溃 `ModuleNotFoundError: No module named 'common'`）：
  - `get_screen_size()` — ctypes 物理像素（DPI aware），非 Windows 回退 tkinter
  - `WindowAPI` — pywebview js_api（拖拽移动 / 读取位置 / 隐藏窗口）
  - `find_character_image()` — 查找 assets/ 下的自定义立绘
  - `load_html()` — 注入 ui.html 的 `{{PORT}}` / `{{CHARACTER_IMAGE}}` 占位符
  - `create_temp_html()` — 写临时 HTML 并返回 file:// URL

### Fixed
- Webview 面板：错误信息先 HTML 转义再拼接，避免编译器错误中的 `<` `>` 等字符破坏显示
- 诊断清零时的 `all_clear` 消息只在桌宠服务器在线时发送
- `start.bat` / `launch_full.bat` 中文乱码（文件头加 `chcp 65001`）
- 删除误生成的垃圾文件 `desktop_pet/$null`

## [0.2.0] — 2026-06-24

### Added
- **桌面宠物窗口**（方案 B 混合架构）：`desktop_pet/` 目录
  - Python + pywebview 透明无边框置顶窗口
  - 可拖拽移动（按住头像拖动到屏幕任意位置）
  - 聊天气泡弹入动画 + 8 秒自动消失
  - 右键菜单（隐藏 Airi）
- HTTP SSE 服务器（Node.js 内置 `http` 模块，零额外依赖）
  - 自动分配空闲端口（`listen(0)`）
  - SSE keepalive 心跳（每 20 秒）
  - 健康检查端点 `/ping`
- SSE 客户端广播机制：Python 回复同时推送到 Webview 和桌面宠物
- `Toggle Desktop Pet` VS Code 命令
- 优雅降级：Python/pywebview 不可用时仅使用 VS Code Webview
- 内联 Fallback HTML（`main.py` 内），ui.html 缺失时不崩溃
- 窗口位置通过 Python API `get_position()` 获取，避免 `window.screenX` 不准确

### Changed
- `extension.ts`：新增 `startDesktopServer()`, `broadcastToDesktop()`, `spawnDesktopPet()`, `killDesktopPet()`
- 进程生命周期：扩展停用时自动终止桌面宠物 + 关闭 HTTP 服务器
- `package.json`：新增 `toggleDesktopPet` 命令

### Fixed
- 拖拽起始点使用 Python API 而非 `window.screenX/Y`
- tkinter 屏幕尺寸获取用 `try/finally` 保证 `root.destroy()`
- Windows 屏幕尺寸优先用 `ctypes` 避免 tkinter 窗口闪烁
- 桌面宠物初始状态显示 "connecting..." 而非 "online"

## [0.1.0] — 2026-06-23

### Added
- 聊天式 Webview UI（对话气泡 + 傲娇粉色主题）
- Airi 角色人格：傲娇（ツンデレ），名字「愛莉」
- Python 后端：本地语料库随机抽取回复
- DeepSeek API 接口预留（设置 `DEEPSEEK_API_KEY` 启用）
- 错误清零检测（all_clear 事件）
- 启动问候 + 打字指示器动画

### Changed
- Webview 从诊断卡片列表改为聊天对话界面
- 诊断事件统一由 Python 后端处理后下发 Webview
- 消息协议新增 `chatMessage`, `errorAlert`, `statusChange` 类型

## [0.0.1] — Initial scaffold
- VS Code 扩展模板
- 基础诊断监听 + Python stub
