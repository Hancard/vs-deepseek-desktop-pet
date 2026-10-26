# Change Log

## [0.3.46] — 2026-10-27

### Fixed

- **本机回环请求被系统代理劫持 → 独立 watcher 静默失效**（`desktop_pet/watcher.py` / `desktop_pet/standalone.py`）
  `watcher.py` 的两处 `/push` 推送和 `standalone.py` 的 `_ping_ok()` 都用
  `urllib.request.urlopen`，而它默认读 `http_proxy` / `https_proxy`，**对 127.0.0.1
  并不一定绕过**：CPython 的 `proxy_bypass_registry()` 只认 `<local>`（无点主机名）
  和"前缀 / 后缀通配"，Windows 上极常见的 `ProxyOverride=...;127.*;...` 这种中间
  通配写法匹配不到。实测本机（ProxyOverride 里明明写着 `127.*;<local>`）：
  ```
  proxy_bypass('127.0.0.1')                                = False
  http_proxy 指向无人在听的端口时 urlopen('http://127.0.0.1:<port>/push')
                                                           = URLError [WinError 10061]
  build_opener(ProxyHandler({})).open(同一地址)             = 200
  ```
  于是"代理软件退出、环境变量还残留"（开发者机器上非常常见）时：`--dir` 那条 watchdog
  的**每一条诊断都投不出去**，而失败分支是 `except Exception: pass` —— 用户看到的是
  "桌宠对代码一声不吭"，日志里零行；`_ping_ok` 则恒返回 False，它在 `main()` 里用来
  区分「已有 Airi 实例」和「端口被无关程序占用」，本来是前者的情况会被记成后者。
  修：两处都改用显式的 `_LOCAL_OPENER = build_opener(ProxyHandler({}))`。
  **公网下载 Cubism Core（`live2d_assets.py`）保持默认 opener** —— 那条本来就该走代理。
- **推送失败时签名已被提交 → 那批错误再也不会播报**（`desktop_pet/watcher.py`）
  两处去重都是"先 `last_signature = signature`，再发请求"，而推送失败几乎是常态：
  用户先改代码、后开桌宠；或者桌宠刚被关掉。那一刻签名已经记成"这版状态报过了"，
  于是**同一批错误永久静默**，除非错误内容再变一次（签名变化）。文件里另外两条链路
  （`known_errors` 清空、all_clear 判定）都在认真区分"说过"和"送到了"，只有这处把
  两者混为一谈。修：签名只在**收到 2xx 之后**提交；失败时不提交，下一轮（1.5s）
  自然重试 —— 与 `push_message` 侧"宁可重试也不静默丢单"同一口径。
- **单文件模式（`--file`）的诊断永远没有语言信息**（`desktop_pet/watcher.py`）
  目录模式的 payload 带 `language`，`watch_single_file()` 那份漏了 —— 后端
  `p.get('language', 'unknown')` 兜成 `'unknown'`，而 `response_generator` 会把
  language 拼进台词（`{language}` 占位符）。同一个字段在两条路径上形状不一致，
  以后每加一个消费方都得再猜一次。修：按 `languageId` 去重后拼好再发。
- **`{"error_count": true}` 被念成"有 1 个错误"**（`standalone.py` / `python_backend/response_generator.py`）
  `_as_int()` / `_as_seq()` 是裸 `int(value)`，而 `bool` 是 `int` 的子类：
  `_as_int(True) == 1`、`_as_seq(True) == 1`。后果不只是"数错了"：一份手改坏的
  设置文件（`"openCorpusSeq": true`）会被读成**合法序号 1**，扩展看到 `seq > 0`
  就打开语料并回执，桌宠的首次点击反而撞上"已处理"再也不打开；`/push` 传
  `{"count": true}` 则会冒一张红色的 errorAlert 说"1 个错误"。
  `response_generator` 同一个位置也是 `int(context.get("error_count") or 0)` ——
  `true` 直接变成 `errorAlert`。修：三处统一在 `int()` **之前**判 `isinstance(..., bool)`，
  与 0.3.45 给 `_as_number()` / `parseDetectSec()` 立的口径一致（布尔不是数字）。
- **设置文件被写坏时，点一次「检测周期」会把用户正在编辑的内容整份覆盖**（`standalone.py`）
  `_persist_locked()` 写盘前用 `load_settings()` 合并磁盘值 —— 而它在读失败时
  **回落默认值**，调用方分不清"文件里就是 30"和"文件坏了所以给你 30"。于是：
  用户在编辑器里把 `~/.dsh/pets/airi-pet-settings.json` 改出一个少括号的版本（或者
  网盘占位 / 杀软短暂锁住），此时点一次「检测周期 → 10 秒」，`disk` 变成
  `{'detectIntervalSec': 30, 'openCorpusSeq': 0}` 再被写回磁盘 —— **用户手上那份
  内容被换成两个键**，`openCorpusSeq` 也从真实值被打回 0。`_settings_from_disk()`
  存在的全部意义就是区分这两种情况（0.3.45 刚加的），这里却没用它。
  实测：`before=b'{ "detectIntervalSec": 5, "openCorpusSeq": 17'` →
  `after=b'{ "detectIntervalSec": 10, "openCorpusSeq": 0 }'`，函数还返回 `True`。
  修：改用严格版；**文件存在但读不出来时一个字节都不写**并返回 `False`（前端会冒
  "周期没保存成功…再试一次？"，用户回去就能看见自己把文件写坏了）；文件不存在时
  仍用内存快照 + 新值正常创建。
- **POST 在途时右键，界面会被 GET 打回旧值**（`ui.html`）
  0.3.45 给 `syncDetectSec()` 加了 `if (pendingDetectSec !== null) return;`，但那
  只覆盖"点了但还没发出去"的 150ms 防抖窗口：防抖一到点就把 `pendingDetectSec`
  清空，请求还在路上 —— 这段窗口里右键，GET 读到的还是**没落盘**的旧值，界面被打回
  旧值，随后 POST 成功。结果正是 0.3.45 声称消灭的"界面说谎"：界面 30 / 磁盘 31。
  服务端是多线程的，POST 与 GET 的完成顺序本来就不保证，判据必须是"我这边还有没落地
  的写"。修：新增 `postInFlight` 计数（`postDetectSec` 里 +1、统一收尾 -1，
  用 `.then(release, release)` 而不是在每个 `return` 前各减一次 —— 那个 `.then` 里有
  三个提前 return 分支，逐分支减必然漏），`syncDetectSec` 的闸门改成
  `pendingDetectSec !== null || postInFlight > 0`。
  运行时证据（node 跑真实函数）：点击后 `detectSec=31`、`pendingCleared=true`、
  `postInflight=1`，GET 返回 30 → 界面仍是 `31`（0.3.45 上是 `30`）。
- **面板 dispose 回调不认领自己的实例**（`src/extension.ts`）
  `panel.onDidDispose()` 回调用的是**模块级** `panel`：`indexOf(panel)` 会摘错
  subscriptions 中的元素，而 `panel = undefined` 是无条件的 —— 一旦这个事件迟到到
  下一个面板创建之后，就会把**新面板**抹掉，之后 `postToWebview()` 的 `if (!panel) return`
  吞掉所有更新，面板停在一帧旧数据上却明明还开着（用户只会觉得"面板不刷新了"）。
  修：闭包里绑定 `const created`，摘和置空都认这一个实例（`if (panel === created)`）。
- **注释订正**：`readDetectIntervalMs` 的文档里 `None(true)` 应为 `Number(true)`。

## [0.3.45] — 2026-10-26

### Fixed

- **设置文件里写 `"detectIntervalSec": true` 仍是 1 秒（Python 侧没跟上）**（`standalone.py`）
  0.3.44 只在扩展侧挡住了布尔值，桌宠自己的 `clamp_detect_sec()` 还在走
  `float(value)` —— 而 `bool` 是 `int` 的子类、`float(True) == 1.0`，于是启动读成
  **1 秒**（30 倍播报），更糟的是下一次**任何**保存（比如点一次「编辑语料」）都会把
  这个 1 整份写回磁盘，扩展 watch 到就真的开始每 1 秒一拍。同一个洞还有
  `float('1_0') == 10` 与 `float('  ')`（抛异常 → 30），而 JS 的 `Number()` 对它们
  分别是 `NaN` / `0` —— 两边对同一份文件给出不同答案。修：新增 `_as_number()`，
  只认"int/float（非 bool）"与"整串匹配十进制/科学计数正则的字符串"，其余一律
  不可用；扩展侧同步新增 `parseDetectSec()`，两边同构。
- **`2.5` 在两边差一秒**（`standalone.py` / `src/extension.ts`）
  `Math.round(2.5)` 是 3，Python 的 `round(2.5)` 是 2（银行家舍入）—— 用户手改
  `2.5` 就成了"菜单显示 2 秒、实际每 3 秒一拍"。修：扩展侧新增 `roundHalfEven()`，
  与 Python 的口径对齐，并附跨实现一致性断言（20 个样例两边结果必须相等）。
- **字段非法时扩展"保持当前值"、桌宠回落 30 秒**（`src/extension.ts`）
  `readDetectIntervalMs()` 对非法字段返回 `undefined`（调用方不动周期），而桌宠
  `load_settings()` 对同一个值回落 30 —— 设置文件被写坏之后，菜单显示 30 秒、扩展
  却还按上一拍的 5 秒跑。修：字段非法 → 默认 30 秒（与 Python 同值）；只有"文件读不到 /
  JSON 半截"才继续返回 `undefined`（那可能是编辑器正在写，不该改周期）。
- **`GET /settings` 只返回启动时的内存快照**（`standalone.py`）
  `settings` 是启动 `load_settings()` 的一份快照，而这个文件明确支持用户手改
  （README 教的改周期写法就是整份覆盖）。手改之后、下一次 POST 之前，右键菜单一直
  显示旧值 —— 而前端把它当**权威**：菜单显示 30 秒，用户点「+」，前端从 30 算出 31
  发 POST，`_persist_locked` 把磁盘上手写的 5 **覆盖掉**。用户看到的就是"我改的文件
  它不认，还给我改回去了"（0.3.43 给 `openCorpusSeq` 修过同一个坑，这里是"读的人
  也没刷新"那一半）。修：`settings_payload()` 先用新加的 `_settings_from_disk()`
  刷新内存；文件坏了（半截 JSON）时**什么都不做**，不能把正在生效的值改回默认。
- **`{"type": null}` / `{"emotion": 1e999}` 让整条气泡消失**（`standalone.py`）
  `json.dumps` 默认 `allow_nan=True`，`1e999` 解析成 `float('inf')` 后帧里就写出字面量
  `Infinity` —— 那不是合法 JSON，前端 `JSON.parse` 直接抛错（被 catch 成一条
  `reportJS`），**气泡不显示**；坏帧还留在队列里，之后每次重连按 `SSE_REPLAY_COUNT`
  重播都再炸一次。`type` 是另一面：`msg.get('type', 'chatMessage')` 只在**键缺失**
  时回落，`null` / 数字 / 拼错的名字都原样进帧，前端 `if (!msg || !msg.type) return;`
  或 `switch` 落空 —— 而 HTTP 已经回了 200 `{"status":"pushed"}`，发送方以为送到了。
  修：在 `push_message()` 这个唯一出口里把三个字段全部归一化（`_as_msg_type`
  白名单、`_as_emotion`、`_as_text`），并用 `allow_nan=False` 把"帧必须能被
  `JSON.parse`"变成硬约束。
- **落盘失败后连续点「编辑语料」会算出同一个序号**（`standalone.py`）
  `_persist_locked` 失败时把内存回滚成磁盘值，而序号公式是
  `max(内存, 磁盘) + 1` —— 下一次点击算出**完全相同**的 seq，而
  `_corpus_open_wait[seq] = slot` 是无条件覆盖：前一个 worker 的等待槽被顶掉，
  扩展只回执一个 seq，前一个等满 2.5s 判成"没人接单"走 `code` 回落 → 同一个语料
  文件被两个程序各打开一次（"两边都能写，谁后保存谁覆盖"）。修：新增只增不减的
  `_corpus_seq_floor`，序号取三者的最大值 + 1。
- **0.3.44 引入的回归：字面花括号被整条丢弃**（`python_backend/corpus.py`）
  `_norm_placeholders()` 原来无条件把 `{ 单词 }` 收窄成 `{word}`，归一之后
  `_PLACEHOLDER_RE` 才第一次能匹配到它 —— 不在白名单里就**整条台词丢弃**。
  于是「备注：{ 待定 }」「这个 { 名字 } 我先记下了」「`if (a > b) { return }`」
  这些本来原样显示的句子突然全没了，用户只看到"我写的台词不生效"。
  修：只归一**已知占位符名**（`_ALL_PLACEHOLDER_NAMES`，即所有场景白名单的并集），
  其余花括号保持原样。
- **`error_count: 1e999` 让 `generate_response` 抛异常**（`python_backend/response_generator.py`）
  `int(inf)` 抛 `OverflowError`，那里只兜 `(TypeError, ValueError)` —— 异常从后端入口
  穿出，整条播报绕过正常路径（连 LLM 都不试），只靠兜底台词，且每次都留一条
  "generate failed: OverflowError"看着像后端坏了。修：连 `OverflowError` 一起兜
  （`/push` 侧的 `_as_int` 早就是这样，这是漏掉的第六处）。
- **「编辑语料」在序号被重置后可能既不打开也不回执**（`src/extension.ts`）
  0.3.44 的判据是 `seq === lastCorpusOpenSeq`：设置文件被整份覆盖（README 教的
  `Set-Content '{"detectIntervalSec":5}'`）会把 `openCorpusSeq` 一起抹掉，桌宠重启后
  从 `max(内存,磁盘)+1` 起算，算出的号可能正好是本会话已处理过的那个 —— 于是
  `return false` 直接跳过，**连回执都不发**，桌宠白等 2.5s 再回落 `code` 命令。
  修：请求身份改成 `序号@文件mtime` 签名 —— 同一次写入被 `watchFile` 触发两遍时
  两者都相同（照旧丢弃），序号被重置后重新写入时 mtime 必然是新的（正常处理）。
- **spawn 失败后仍等满 8 秒并补一条误导性诊断**（`src/extension.ts`）
  Python 路径不存在时 `spawn` 走 `'error'` 事件（ENOENT），**不抛异常**，`try/catch`
  拦不到 —— 用户先看到准确的"启动桌宠失败: spawn python ENOENT"，8 秒后又看到
  "桌宠进程已启动但服务器未响应 … 请检查是否装了 pywebview"，被引去查一个根本没
  参与的问题。修：加 `spawnFailed` 标记，进程没起来就不再等、也不再补那条警告。
- **非 Live2D 模式下 `head` / `desk` 的点击台词永远触发不到**（`ui.html`）
  `zoneAt()` 开头是 `if (!live2d.ready || !live2d.app) return 'body';` —— 而
  `LIVE2D_ENABLED=false` / 素材缺失 / 半初始化失败时角色是图片或 CSS Q 版人，
  **照样可以点**（`alphaAt()` 专门写了外接框兜底）。结果 `click.head` / `click.desk`
  里写的台词一条都不出现（日志里只有 `zone=body`），`ry` 也恒为 -1，想校准都没有
  数据。修：新增 `zoneBox()`，非 Live2D 时用角色元素的外接框，命中判定与分区共用
  同一个框。
- **Live2D 半初始化失败后角色彻底消失**（`ui.html`）
  `ready = true` 写在 `await model.motion(...)` **之前**，动作加载失败就跳到 catch：
  清场把 `live2dStage` / `live2d-active` 撤了，却没有把 1049 行撤掉的 `cssChar`
  放回来 —— 而 `showImageCharacter()` 在 `CHAR_IMG` 为空（只用 Live2D、或删掉了
  `assets/character*`）时第一行就 return。于是页面上只剩名牌和一片空白，
  `fallbackCharacterEl()` 返回 null → `alphaAt()` 恒 0 → 点哪儿都没反应、
  右键菜单也唤不出来。修：清场时先把 CSS 角色加回来，再让图片接管。
- **情绪变体图的异步回调会把 PNG 垫回模型下面**（`ui.html`）
  `onload` / `onerror` 只认领了"自己的情绪"，没认领"自己的渲染方式"：图片加载
  期间 `initLive2D` 完全可能已经接管，回调却无条件 `charImg.style.display='block'`
  —— 不透明 PNG 垫在 canvas 底下，从模型透明区透出来（"两个角色叠在一起"）。
  修：两个回调都先判 `live2d.ready`。
- **成功的 POST 被后发请求作废，回滚到磁盘上已经不存在的值**（`ui.html`）
  `diskDetectSec` 的更新写在 `if (seq !== settingsSeq) return;` **之后**：点 +
  （30→31，落盘成功）→ 立刻再点 +（31→32，失败）—— 第一个响应因序号过期被丢掉，
  基准还停在 30，第二个失败就回滚到 30，而磁盘上是 31：**界面 30 / 磁盘 31 /
  气泡说"没保存成功"**，三个值互不相同（正是 0.3.44 注释宣称已消灭的那种）。
  修：磁盘事实先于序号闸门更新（闸门只保护界面）。
- **右键打开菜单时的 GET 会把刚点的乐观值打回旧值**（`ui.html`）
  `setDetectSec()` 到真正 POST 之间隔着 150ms 防抖：点完 chip 立刻右键，GET 先回来
  就把乐观值改回旧值 —— 界面上数字跳一下再跳回去，而磁盘那时还没变。修：有待发的
  点击时（`pendingDetectSec !== null`）基准照更新、界面留给那次 POST 收尾。
- **「编辑语料」的守卫比后端回执窗口短**（`ui.html`）
  0.3.44 把释放窗口定成 700ms，而后端 `CORPUS_OPEN_ACK_TIMEOUT` 是 **2.5s**：扩展
  500ms 轮询没及时回执时（VS Code 正在启动/忙），用户在这个"毫无反馈"的空档里再点
  一次非常自然 —— 那是第二个请求，扩展只回执最大的 seq，先发的那个等满 2.5s 走
  `code` 回落 → 同一个语料文件被打开两次。修：窗口提到 2600ms（≥ 回执窗口）。

## [0.3.44] — 2026-10-25

### Fixed

- **`/char/` 的"只允许单层文件名"能用 `%252F` 双重编码绕过**（`common.py`）
  路由先 `unquote` 一次用于判断"有没有斜杠"，随后 `_safe_join()` 对同一个字符串
  **又解码一次**：`%252F` 第一层解码成字面量 `%2F`（不含斜杠，通过检查），第二次
  解码才变回真斜杠。实测 `GET /char/live2d%252fvendor%252fLICENSE-...txt` →
  **200 + 真实文件内容**（该路由的注释承诺"带子路径的一律 403、不把整个 assets/
  递归暴露"）。修：`_safe_join(..., unquote=False)` 让"已经解码过"的调用方不再
  被解第二次；再加一道按**结果**的判定（`target.parent` 必须是 assets 根），
  因为编码花样不止 `%2F` 一种。
- **`.jpeg` 立绘以 `application/octet-stream` 发出去**（`common.py`）
  `find_character_image()` 认 `.jpeg`（README 承诺的四种之一），`_CHAR_MIME` 也认，
  只有响应头用的 `_MIME` 没列 —— 同一个格式在两张表里两个答案。补上，并在测试里
  断言"五种立绘后缀在两张表里答案一致"。
- **`/push` 收到 `count: 1e999` 时直接断连、日志零行**（`standalone.py`）
  JSON 的 `1e999` / `Infinity` 解析成 `float('inf')`，`int(inf)` 抛 **`OverflowError`**
  —— 它**不是** TypeError/ValueError 的子类，只写那两个的 `except` 漏过去，一路穿到
  `handle_error`（只吞连接类异常）。实测客户端收到 **0 字节**、`RemoteDisconnected`，
  而 `.airi-pet.log` 里一行都没有（扩展以 `stdio:'ignore'` 起进程）。这个坑本项目
  已经踩过三次（`clamp_detect_sec` / `_as_seq` / `_corpus_open_ack`），`/push` 的
  `count`、`error_count` 与兜底里的 `ec` 是第四、五、六处。修：新增 `_as_int()`
  统一兜 `(TypeError, ValueError, OverflowError)`，三处都改走它。
- **兜底台词绕过"过期作废"闸门，桌宠会自己推翻自己**（`standalone.py`）
  只有成功路径检查 `my_seq < _gen_done_seq`，而兜底是直接 `_builtin_reply` →
  `push_message` 说话的：既不作废过期结果，也不推进 `_gen_done_seq`。后端一次超时/
  异常就够触发 —— `file_scan`（seq=1，随后失败）→ `all_clear`（seq=2，说"干净了"）
  → seq=1 的兜底迟到，播出"还有 3 个 bug"，而内部仍认为最新的是"干净了"。反向也
  成立：兜底不推进序号，本该作废的**旧成功结果**会被照常播出。修：抽出 `_emit()`，
  成功结果与兜底共用同一道闸门（`_builtin_reply` 增加 `_push` 参数）。
- **「编辑语料」的序号被写小，功能长期失效**（`standalone.py`）
  `seq = _as_seq(settings['openCorpusSeq']) + 1` 用的是**启动快照**，而 `_persist_locked`
  写盘前会与磁盘合并 —— 合并不保护"正在写的这个键"，于是磁盘上是 100、内存是 0 时
  算出 1 并写进磁盘：序号倒退，扩展那边 `seq <= last` 从此一律不接单（要一路点到
  101 才恢复），且被写小的旧值不可恢复；每次点击都要等满 2.5s 再回落 `code` 命令，
  在 VS Code 已开着时再拉一个窗口。实测响应 `{"openCorpusSeq": 1}`、磁盘 `100 → 1`。
  修：取**内存与磁盘的较大值** + 1。
- **单条消息没有统一上限**（`standalone.py`）
  0.3.43 只把"解析失败"和"非对象"两条分支统一截到 500，但 `payload.text` 分支、
  `message` 默认值分支（默认值就是整个 body）、以及 LLM 生成的结果都没截。body 上限
  1MB、队列上限 200 → 最坏 ~200MB 常驻，每个 SSE 连接每次都要写 1MB。修：截断放进
  `push_message()` 这个**唯一出口**（`MAX_PUSH_CHARS = 4000`），以后新增调用方不用
  再记得这件事。
- **手动重建的 EventSource 不带续传点，断线恢复后把最近 20 条重播一遍**（`standalone.py` + `ui.html`）
  浏览器**自动**重连会替我们带 `Last-Event-ID`，但服务端回了非 `text/event-stream`
  导致 CLOSED 时，前端会自己 `new EventSource(url)` 重建 —— 全新对象没有这个状态，
  服务端按"全新连接"处理，`replay = sse_queue[-20:]` 全弹一遍（桌宠把说过的话再念
  一次，20 条按打字机滚很久）。修：前端记住 `event.lastEventId` 并在重建时挂
  `?after_seq=N`，服务端在头之外也认这个查询参数。
- **语料读一次失败就永远不重试（改了台词不生效）**（`corpus.py`）
  0.3.38 只修了一半：读坏时保住了 last-known-good，却仍然 `_cache.update(key=key)`。
  凡是"stat 成功、open 失败"的情形（OneDrive 仅联机占位 / 杀软短暂锁文件 / 编辑器
  保存窗口），文件的 `(mtime, size)` 已经是新的、不会再变 —— 这个键被记进缓存后，
  之后每次调用都命中，**本次运行内再也不会生效**。修：失败一律不推进 key（下一拍就
  重试），日志按错误内容去重（新增 `err_logged`），不刷屏。
- **占位符写成 `{ file }`（带空格）既不校验也不替换，原样念进气泡**（`corpus.py`）
  校验用 `\{(\w+)\}`、替换用 `str.replace("{file}")`，两处都要求紧贴，于是
  `喂，{ file } 里有 3 个 bug` 会被判成"没有占位符"放行，然后原样出现在气泡里 ——
  与语料文件头"免得气泡里冒出 `{xxx}`"的承诺相反。内置台词本身就是「第 {line} 行」
  这种排版，用户照着写带空格的说法太自然了。修：读入时统一归一（`_norm_placeholders`），
  下游校验与替换就只剩一种形态。
- **`line: null` 被念成"第 None 行"**（`response_generator.py`）
  `errors[0].get("line", "?")` 的默认值只在**键不存在**时生效，JSON 里 `"line": null`
  会原样得到 `None`。同一条链路里 file_scan 分支早就改成 `_nvl(...)` 了，diagnostics
  这半边漏了（0.3.40 修过同一个坑的另一次）。
- **设置文件被整份覆盖后，「编辑语料」要连点 12 次才恢复**（`extension.ts`）
  判据是 `seq <= lastCorpusOpenSeq`。README 教的改周期写法是
  `'{"detectIntervalSec": 5}' | Set-Content ...` —— **整份覆盖**会把 `openCorpusSeq`
  字段一起抹掉，桌宠重启后从 0 起算、下次点击写 1；而扩展内存里还是旧值（比如 12），
  于是 1..12 全被判成"已处理"：既不打开、也**不回执** → 桌宠等满 2.5s 后回落 `code`，
  在已开着的 VS Code 之外再拉一个窗口。修：判据改成"和上一次**相同**才忽略" ——
  这里读的本来就是文件当前值（不是事件里夹带的旧值），序号变小只可能来自文件被重置；
  真正要防的重放是"同一次请求被 watchFile 触发两遍"，那两条的 seq 必然相等。
- **`"detectIntervalSec": true` 把周期打成 1 秒**（`extension.ts`）
  `Number(true) === 1`，而过滤器只挡 `!isFinite || <= 0` —— 合法 JSON 里写个 `true`
  就把 30 秒变成 1 秒（每拍推一次、后端每拍起一个生成线程），且违反"字段非法保持
  当前值"的契约。`Number(["45"]) === 45` 同理。修：只接受 number 与数字字符串，
  其它一律 `NaN` → 保持当前值，与 Python 侧 `clamp_detect_sec` 口径一致。
- **打字机把 emoji 切成半个代理对，每个 emoji 闪一帧「�」**（`ui.html`）
  `text.charAt(i++)` 走的是 UTF-16 **码元**：先追加高位代理、下一帧才补低位，中间
  那一帧就是孤立代理。0.3.43 只修了 `clipBubbleText`（存储侧），打字这半边没跟上。
  修：一次吞掉整个码点。
- **连点「+」时旧请求的失败会抢跑、菜单的 GET 会把乐观值打回旧值**（`ui.html`）
  序号只在 `postDetectSec` 里自增，而那里离真正发请求还隔着一个 150ms 防抖：
  ① 上一个 burst 的 POST 在这 150ms 内失败 → `detectFailed` 的 `seq !== settingsSeq`
  拦不住（序号还没变）→ 界面被回滚、还冒一条"周期没保存成功"，可新一轮其实存成功了
  （数字先跳一下再回来）；② 打开菜单时的 GET 同样在这 150ms 内返回 → 把刚点下去的
  值打回旧值（5 → 30 → 5）。修：变更发生的第一时间就自增 `settingsSeq`。
- **失败回滚落在一个"从没落过盘"的值上**（`ui.html`）
  0.3.43 用"一次连点 burst 的起点"当基准；可只要用户停 >150ms 再点，
  `pendingDetectSec` 已经清空，新 burst 记的"起点"是**乐观值**（31），而磁盘上其实
  还是 30 —— 回滚后界面 31、磁盘 30、气泡还说"没保存成功"，三个值互不相同。
  修：基准改成 `diskDetectSec`（最后一次服务端确认落盘的值），无论中间连点多少下、
  间隔多久，回滚目标永远是磁盘上真实生效的那个。
- **「编辑语料」的在途守卫只活了一个 RTT，盖不住它要防的 180ms 窗口**（`ui.html`）
  0.3.43 在 `fetch` 的 `.then/.catch` 里把标志置回 false，可那个 POST 在 Python 侧是
  "登记 slot → 落盘 → 起线程 → 立刻 200"，本机 RTT 只有 10~20ms —— 比菜单
  `transition: visibility 0.18s` 的过渡窗口还短，双击的第二下照样打进来：两个 worker、
  扩展只回执最大的 seq、先发那个 2.5s 后回落 → 同一个语料文件被两个程序各打开一次。
  修：固定按住 700ms（> Windows 双击间隔上限，< 后端 2.5s 回执超时），回调不再碰标志。
- **`Foo.CPP` 被交给 gcc、languageId 记成 c**（`watcher.py`）
  `check_c()` 取后缀没 `lower()`，Windows 上 `.CPP` / `.Cxx` 是合法且常见的写法 ——
  `is_cpp` 恒为 False，C++ 文件被 C 编译器检查（iostream / 类 / 模板全报假错误），
  而调用方是按 lower 后的扩展名分发到这里的，两边口径不一致。修：`lower()`。

## [0.3.43] — 2026-10-24

### Fixed

- **立绘永远加载不出来（`AIRI_CHARACTER_IMAGE` / `assets/character.png` 全线失效）**（`common.py`）
  `load_html()` 注入的是 `Path.as_uri()`，也就是 `file:///S:/.../character.png`；而页面本体
  是由本地静态服务器以 `http://127.0.0.1:<port>/` 提供的（v0.3 起就是同源 HTTP，文件头
  注释写得很清楚）。Chromium / WebView2 **禁止 http 文档加载 file: 子资源**，于是：
  ① 自定义立绘和 `assets/character.png` 两条对外承诺的功能 100% 失效；② 情绪立绘
  （`character_happy.png` 这类按文件名拼出来的）同样取不到；③ **全程零日志** —— 文件确实
  存在，所以 `find_character_image` 和 `load_html` 两条 WARNING 都不会打，前端只有一条
  静默 `onerror` 退回 CSS 小人。用户看到的就是"我的立绘怎么变成默认小人了"。
  修：新增 `_character_url()` 与 `/char/<name>` 路由 —— 在 `assets/` 根目录下的立绘走
  同源 URL（情绪变体因此仍然可用），其它任意路径内联成 `data:` URI。路由只放行单层
  文件名（先 unquote 再判斜杠，`%2F` 绕不过去）。
- **`AIRI_ASSET_PORT` 越界直接崩，日志里一行都没有**（`common.py`）
  `int('99999')` 成功、`if not port` 也放行，然后 `socket.bind()` 抛 **`OverflowError`**
  —— 它**不是 OSError**，`start_asset_server` 的 `except OSError` 抓不住，异常穿到顶层；
  扩展以 `stdio:'ignore'` 起进程 → traceback 被丢掉，`.airi-pet.log` 最后一行还是
  "character card square"。用户点「启动桌宠」完全没有反应。修：端口夹到 0..65535
  （越界回落 0），`except` 放宽到 `(OSError, OverflowError, ValueError)`。
- **资产服务器的"端口被占用"分支永远不可达**（`common.py`）
  `HTTPServer.allow_reuse_address` 默认 1，而 Windows 的 `SO_REUSEADDR` 允许**完整重复
  绑定**（实测两个 server 绑同一端口都成功）。于是用户设了固定端口而上一个实例没退干净
  （或端口被别的服务占着）时，我们照样报告成功、`page_url` 指向那个端口 —— 新窗口可能
  加载到**另一个进程的旧页面**（里面的端口是旧的 → 气泡/设置全废），或者 404 白窗，
  日志却是"asset server started"。修：`_AssetServer.allow_reuse_address = False`
  （资产服务器平时用 `port=0` 由内核分配，不存在"重启撞 TIME_WAIT"的副作用；
  SSE 服务器保留复用，那条路要的就是"退出后能立刻重启"）。
- **`AIRI_WIN_BG=off` 时 standalone 白等 10 秒并打假告警**（`standalone.py`）
  `fix_window_background()` 在该模式下**必然**返回 `(False, ...)`，而调用方是
  `while 到 10 秒 deadline、只认 ok` 的循环。0.3.41 只在 `main.py` 补了 off 短路，
  **主入口 standalone 漏了** —— 启动多花 10 秒（前面还有最长 20 秒的 Mica 循环），
  最后再打一条"气泡/名牌周围可能还会有一圈浅色方块"的假 WARNING，排障时把人往
  DWM/Form 方向带。修：照抄那段短路。
- **`get_position()` 用 `[0, 0]` 当错误码 → 拖拽跳到左上角**（`common.py`）
  `[0,0]` 是一个**看起来完全合法**的坐标：前端 `applyPos` 只挡 `!pos` 和"是不是数字"，
  两个条件都通过，于是 `winStartX/Y` 被写成 0/0，下一次 `pointermove` 用「鼠标位置 − 0」
  算位移，窗口直接蹦到屏幕左上角。前端早就写好了 `if (!pos) return;`，是后端的错误码
  把它堵死了 —— 0.3.38 只修了"0 被当假值丢掉"那一半。修：失败返回 `None`，
  真实的 `(0,0)` 仍原样返回。
- **`CombineRgn` 的 `NULLREGION` 被当成成功 → 空 region 装到窗口上**（`common.py`）
  `_merge` 用 `rc2 != 0` 判成功，而 `NULLREGION(1)` 表示合并结果是**空区域**。气泡贴到
  窗口底边时尾巴三角形三个点被 `_clip_y` 钳成共线，`CreatePolygonRgn` 返回一个合法但
  **空**的句柄（不是 0，`if not h_piece` 拦不住），`CombineRgn` 返回 NULLREGION，`kept`
  却 > 0 → `SetWindowRgn` 把空区域装上窗口：**窗口整个不可见、鼠标点不到（连右键菜单
  都打不开 → 退不出去，只能杀进程）**。0.3.38 只堵了 `ERROR(0)` 这半边。
  修：只认 `SIMPLEREGION(2)` / `COMPLEXREGION(3)`。
- **`exit_app()` 失败完全静默，且可能根本退不出去**（`common.py`）
  唯一的逃生口是裸 `except Exception: pass`：`destroy()` 抛异常时函数静默返回，窗口还在、
  进程还在，日志里一行都没有 —— 同文件其它五个桥方法失败都会留痕，只有这条不留。
  修：失败记一行日志，并兜底 `os._exit(0)`（用户按的是"退出"，那就必须退出）。
- **一条孤立代理字符能让桌宠永久失声**（`standalone.py`）
  Python 的 str 可以合法地含孤立代理（`'\ud800'`），只是不能编码成 utf-8。
  来源之一是**本扩展自己**：`clipMessage()` 按 UTF-16 码元 `slice`，第 59 个码元正好是
  某个 emoji 的前半时就切出孤立代理；来源之二是任何本机客户端 `POST /push`。
  `json.dumps(ensure_ascii=False)` 把它原样留在帧里，写帧时 `.encode('utf-8')` 抛
  `UnicodeEncodeError`，被 `except Exception: pass` 吞掉、连接关闭、日志零行。更糟的是
  坏消息**留在队列里且排在所有后续消息前面**：前端自动重连→带 Last-Event-ID→从这条开始
  重播→再炸，3 秒一轮无限循环。结果：桌宠再也不出任何气泡，只有重启进程才能恢复。
  修：`_sse_safe()` 入队前做可编码化（replace 只影响孤立代理，正常文本一字不动）；
  写帧失败按类型留痕（断连不吵，编码错误必记）。
- **`/push` 解析失败分支把最大 1MB 的原文塞进队列**（`standalone.py`）
  非对象分支早就截到 500，解析失败分支却是 `push_message('chatMessage', body)` ——
  body 上限 `MAX_BODY_BYTES=1MB`，队列 200 条 → 最坏 ~200MB 常驻，且每个 SSE 连接
  每次都要写 1MB。修：两条分支统一 500。
- **设置落盘：`openCorpusSeq` 不回滚，手改的设置被覆盖**（`standalone.py`）
  抽 `_persist_locked()` 统一处理两件事：① 落盘失败**回滚内存**（`openCorpusSeq` 以前
  没回滚：内存 N+1、磁盘 N，之后任何一次成功保存都会把 N+1 写下去，扩展下一次 watch 到
  就判定"有新请求"—— 用户没点任何菜单，VS Code 自己打开了语料文件）；② 写盘前**先读磁盘
  合并**（`settings` 是启动时的快照，用户手改设置文件后，下一次保存会把整份快照写回去，
  静默覆盖 —— 用户看到"我改的文件它不认，还给我改回去了"）。
- **`log()` 只兜 `OSError`，写日志本身会抛**（`standalone.py`）
  `UnicodeEncodeError` 是 `ValueError` 的子类、**不是** OSError。消息里混进孤立代理时
  `open(...).write()` 抛异常，而这个调用点不在任何 try 里 —— worker 线程被打死：
  "台词库给你在 VS Code 里打开了…" 那句气泡永远发不出去，文件却真的打开了。
  修：同时兜 `ValueError`，且**不能只 pass**（日志是唯一通道），退化成 replace 后的
  形式再写一次。
- **`openCorpusSeq` 读到 `Infinity` 让「编辑语料」永久失效**（`extension.ts`）
  设置文件里写 `"openCorpusSeq": 1e999` 是合法 JSON，`JSON.parse` 得 `Infinity`，
  而 `Infinity || 0` 仍是 Infinity → 此后 `seq <= lastCorpusOpenSeq` 恒真，整个会话里
  永远走不到 VS Code 第一级（每次掉到 `code` 命令 / 系统默认程序）。
  修：`Number.isFinite` + `> 0` 过滤，与 `openCorpusIfRequested` 那条守卫同口径。
- **诊断面板打开后恒显示"没有错误 — 一切正常"**（`extension.ts`）
  创建面板后不推快照，而 HTML 里 `#list` 的初始内容写死的就是那句话。工作区明明挂着
  十几个错误、只是没人在打字（`onDidChangeDiagnostics` 不再触发）时，用户打开面板看到
  的是**一条假结论**，会以为已经修好。修：HTML 末尾发 `ready` 握手，扩展收到后推一次
  `pushDiagnosticsSnapshot()`（直接 postMessage 可能早于页面加载完成而被丢弃）。
- **连续第 3 条消息会把第 2 条气泡瞬间抹掉**（`ui.html`）
  `removeBubble()` 只加 `fade-out`（只改 opacity/transform，**不脱离文档流**），450ms 内
  它仍占着高度；而 `fitBubbles()` 量的是**整个容器**的 rect，却用
  `querySelectorAll(':not([data-removing])')` 挑受害者 —— 于是"删一条"删掉的是**刚出现
  的第二条**（可能只说了半句），真正该走的还在慢慢淡出。三条两行以上的连续消息就能复现
  （3×2 行 + 2×gap ≈ 206px > 可用高度 ≈ 200px），`MAX_BUBBLES=2` 等于退化成只留一条。
  修：溢出时先把"正在淡出"的那条立刻 `remove()`，再考虑删活的。
- **右键菜单不随失焦关闭 → 切回来点角色会误触「退出」**（`ui.html`）
  窗口是 `on_top=True`，菜单一直浮在最上层，而唯一的关闭路径是 **document 上的 click**
  —— 用户切到别的程序时那个 click 落在别的进程里，菜单就一直开着。回来后习惯性地点一下
  角色（正好在刚才右键的位置，「退出 deepseek」就压在光标下方 5~33px）→ 桌宠直接退出，
  用户完全没有因果线索。修：`window blur` 里一并 `hideMenu()`。
- **检测周期保存失败后回滚到"从未落盘过的中间值"**（`ui.html`）
  `prev` 是每次点击各自捕获的，`clearTimeout` 把前几次的闭包连同 `prev` 一起丢掉，
  只有最后一个活到 POST。连点 5 下（30→35）后失败 → 回滚到 34，而磁盘上真正生效的是
  30：错误气泡说"没保存成功"，界面显示的值却既不是新值也不是旧值。
  修：只在 burst **起点**记一次（`burstPrev`）。
- **「编辑语料」没有在途守卫，180ms 内双击会发两次**（`ui.html`）
  `hideMenu()` 只移除 `.show`，而 `transition: visibility 0.18s` 期间元素按规范仍
  **保持 visible**（参与命中测试的本来也不是 opacity）—— 双击的 down-up-down-up 完全够。
  后端每次请求都新起一个 worker，扩展只回执最大的 seq，先发那个 slot 永远等不到回执
  → 2.5s 后走 `code` / `os.startfile` 回落 → 同一个语料文件被两个程序各打开一次
  （两边都能写，谁后保存谁覆盖）。修：`corpusOpenBusy` 在途标志。
- **底板采样的"重试 20 次"是死代码，第一次失败就永久锁定**（`ui.html`）
  `cardSamples++` 排在失败检查**之前**：第 6 次一旦失败，`cardSamples` 已经停在 6，
  函数开头 `cardSamples >= CARD_SAMPLES` 把后续每次调用都挡回，`cardFailures` 永远到不了
  20 → 本次会话再也不画角色底板，而 `updateCharCard` 一条日志都没有。
  修：计数只在成功时递增。
- **`clipBubbleText` 可能切出半个代理对**（`ui.html`）
  `slice` 按 UTF-16 码元走，第 200 个码元正好是某个 emoji 的高位代理时，切出来的是孤立
  代理 —— 气泡里显示成 `�`，而且这个坏字符会一路进 SSE 帧。修：切点落在高位代理时退一
  码元。

## [0.3.42] — 2026-10-23

### Fixed

- **立绘还在加载时点角色完全没反应**（`ui.html`）
  `showImageCharacter()` 在设 `src` **之前**就把 CSS 角色撤了（`remove('visible')`），
  而图片从设 src 到 decode 完成之间 `getBoundingClientRect()` 是 0×0 ——
  `alphaAt()` 的 `fr.width < 1` 于是恒成真，`handlePetClick` 里
  `alphaAt(...) < 24` 恒成立，`api.pet_click` 一次都不调。首帧几十~几百 ms；
  立绘 URL 挂起时要一直持续到 `onerror`。前端零报错、日志零痕迹。
  修：CSS 角色改到 `onload` 之后再撤（`onerror` 则重新显示），这段时间命中
  判定落到它身上。
- **情绪图加载竞态：慢的旧情绪覆盖快的新情绪**（`ui.html`）
  `setEmotion` 起一个 `Image` 去探测情绪图，在 `onload/onerror` 回调里才切
  `charImg.src`。回调触发顺序 = 网络返回顺序，不等于调用顺序：连着切两个
  情绪时，先请求的那张若后返回，会把已经切好的新情绪**盖回旧的** —— 表情
  和刚说的那句话对不上。修：回调里比对"当前想要的表情"，过期的一律丢弃。
- **Live2D 初始化失败后停在半初始化状态**（`ui.html`）
  `catch` 里只置 `live2d.failed = true` 并调 `showImageCharacter()`，但
  `live2d.ready` / `model` / `app` 保留了失败**前**已经赋过的值（初始化是
  分步的，中途抛错时可能已经赋了一部分）。此后 `alphaAt()` 走
  `if (!live2d.ready || !live2d.app)` 的**另一条**分支，读的是那个已经废掉
  的 canvas —— 命中判定落在一个不存在/没画面的 canvas 上。修：catch 里把
  `ready` / `model` / `app` 一并复位。
- **拖拽过程中异步回填的坐标会把拖拽起点冲掉**（`ui.html`）
  按下时先用本地值记 `winStartX/Y`，再异步问 `get_position()` 拿真实坐标
  回填。回填的 Promise 不保证在松手前回来：拖到一半回来就把起点改成了一个
  陈旧值，下一次 `pointermove` 用「新鼠标位置 − 旧起点」算出的位移是错的
  —— 桌宠**瞬移**。修：拖拽期间（`dragging`）不再回填。
- **内联 `animation` 打穿 `.live2d-active` 的规则**（`ui.html`）
  切到 Live2D 时先 `classList.add('live2d-active')`（CSS 里有
  `animation: none`），紧接着又写内联 `charBox.style.animation = ...`。内联
  样式优先级高于 class，且**永不恢复** —— CSS 那条规则等于失效，Live2D
  渲染时还在跑 CSS 抖动。修：加 class 后立刻 `removeProperty('animation')`。

- **`disable_window_backdrop` 在不支持该属性的系统上恒 False**（`common.py`）
  `DWMWA_SYSTEMBACKDROP_TYPE`(38) 是 Windows 11 build 22000 才有的，Win10
  /DWM 合成被禁用的会话里 Get 和 Set 都返回 `E_INVALIDARG` → `before =
  after = -1`、`ok = False`、`after == DWMSBT_NONE` 为假，三个条件一起把结
  论钉死成 False。而**两处**调用方都是 `while 到 20 秒 deadline` 只认 `ok`
  的循环：① 平白空转 20 秒；② 打出一条"没能关掉 Mica backdrop"的 WARNING
  —— 这台机器上根本没有 Mica 可关，这条会把排障彻底带偏；③ 更要紧的是
  底色修复 `fix_window_background()` **串行排在这个循环之后**，那 20 秒里一
  次都不会被调，用户看到的就是"气泡/名牌周围那圈浅色方块要过 20 秒才消
  失"。修：读不到这个属性（`< 0`）直接判定"无需关闭"并返回 True。
- **`move_window` 的钳制一抛异常，窗口就完全不动**（`common.py`）
  注释写的是"钳制失败就按原坐标移 —— 移动永远不能挂"，但整个钳制和
  `self._window.move(...)` 在**同一个** `try` 里：钳制里任何一步抛异常
  （`get_virtual_screen` 返回 None 以外的异常、窗口尺寸属性访问失败、
  `int()` 抛 ValueError）都会把 `move()` 一起跳过，表现是**拖不动**，和注
  释承诺的正好相反。修：钳制单独 `try`，失败只用原坐标继续 `move()`。

- **检测周期读到 `null` 被打成 1 秒**（`extension.ts`）
  `readDetectIntervalMs` 用 `Number(data?.detectIntervalSec)` 之后判
  `Number.isFinite` —— 但 `Number(null)` 是 **0**（不是 NaN），能通过
  `isFinite`，再被下游夹到最小值：设置里存的是"未配置/关掉"，读出来变成
  每秒扫一次。修：显式排除 `null` / `''` / `undefined` 这类 falsy 值。
- **推送给 webview 的诊断条目没截断，与注释承诺不符**（`extension.ts`）
  注释写"最多 20 条"，实际 `items` 直接把整个数组塞进 postMessage：工作
  区错误多的时候一条几 KB 的 message 全量过桥，webview 侧渲染也跟着卡。
  修：按注释截断到 20 条。
- **推送签名不含 count，第 21 条之后的修复被吞**（`extension.ts`）
  `signature` 只拼了前 20 条的 `file|line|message`：错误从 30 条修到 5 条
  时，前 20 条之外的变化**不影响签名**，而总数变了却没进签名 —— 于是
  "从 30 修到 5"和"从 25 修到 5"这两种情况的签名可能相同，桌宠该报喜的
  时候不报。修：签名里加上 `count`。

- **pywebview 缺失时进程永久存活，扩展谎报"已启动"**（`standalone.py`）
  `except ImportError` 分支里是 `while True: time.sleep(1)`：HTTP 服务还在，
  所以扩展的存活探测**一直成功**，状态栏显示已启动，但窗口永远出不来 ——
  而且 Ctrl-C 之外没有任何办法让它退出（僵尸进程）。修：记日志后直接
  `sys.exit(1)`，让扩展的存活探测如实失败。
- **`/push` 的 else 分支没有归一化文本**（`standalone.py`）
  只改了 `payload` 分支：走 else 的 plain 消息里 `message` 若是数字/对象
  （外部 POST 的内容不可信），前端 `typeText` 的 `charAt` 直接抛
  `TypeError` —— 气泡空白 + jsError 黑匣子报警。修：抽 `_as_text()`，两个
  分支都走。
- **`/settings` 落盘失败时不回滚内存**（`standalone.py`）
  先改内存 `settings['detectIntervalSec']` 再 `save_settings`；写盘失败
  （只读/磁盘满/权限）时内存已经改了，界面显示新值、重启后又变回旧值，
  而且 `saved:false` 只对这一次请求可见。修：失败时把内存回滚成旧值。
- **SSE 线程在客户端断连后滞留**（`standalone.py`）
  `while True: time.sleep(0.3)` 只在**写失败**时才 return：客户端早就断了
  但写缓冲还没满（内核 socket 缓冲 + 无心跳），线程一直活着，每 0.3 秒扫
  一次队列，直到下次推送失败才退出。同时挂着多个断连客户端时，这些线程
  就是纯空转。修：每轮写一个心跳注释行（写失败即退出）。

## [0.3.41] — 2026-10-22

### Fixed

- **文件监听整份静默失效**（`watcher.py`）
  过滤条件作用在整个**绝对路径**的 `f.parts` 上，于是监听根**之上**的祖先
  目录也被一起判了：只要监听根位于任何以 `.` 开头的祖先目录下
  （`.workbuddy` / `.cache` / `.cursor` / `.vscode`…）或祖先里有 `build` /
  `dist` / `venv` / `node_modules`，**每一个**文件都会被跳过。rglob 照跑、
  启动信息照常打印，然后 `changed_files` 永远是空的 —— 桌宠从此一声不响，
  用户既看不到错误也看不到任何提示，完全无从判断是"没监听"还是"代码没
  bug"。实测（修复前）：监听根下 3 个 `.py` 扫到 **0** 个；真实目录
  `~/.workbuddy` 下 4468 个 `.py` 存活 **0** 个。
  修：过滤只作用在 `relative_to(watch_path)` 的那几段上，抽成 `should_skip()`。
- **C/C++ 检查串行阻塞最长 200 秒**（`watcher.py`）
  每轮最多检查 20 个文件，而 C/C++ 检查是起 gcc/g++ 子进程（各 10s 超时）
  且**串行**执行：切分支/`git checkout` 时最坏 20×10 = 200 秒。这三分多钟里
  轮询间隔和整棵树扫描一行都不会执行 —— 期间保存的 `.py` 也不播报，
  `all_clear` 也要等拖完才发。修：有界线程池（4 个）并发，最坏降到 ~50s。
- **对已删除文件产出伪诊断；子进程输出未指定编码**（`watcher.py`）
  ① 文件被删掉/移走时仍会起 gcc，它输出 `cc1.exe: fatal error: xxx.c: No
  such file or directory`，里面带 `error:` 于是被当成**真错误**计入总数并
  长期留驻（实测 `check_c(不存在的文件)` 返回 1 条）。② `subprocess.run`
  只写了 `text=True`：此时用**系统 ANSI 代码页**解码（ACP=936 → cp936），
  而 gcc 输出是 UTF-8，解码失败抛 `UnicodeDecodeError` —— 它**不是**
  `OSError`，会穿过 `except FileNotFoundError` 落到裸 `except Exception`，
  变成一条 `"'utf-8' codec can't decode"` 的伪诊断推给用户。
  ⚠️ ② 在本机未能实测复现（本机 `locale.getpreferredencoding()` 是 utf-8，
  开了 UTF-8 mode 恰好躲过；但 ACP 仍是 936，未开 UTF-8 mode 的中文 Windows
  上必然触发）。
- **SSE 进入 CLOSED 后永久断流**（`ui.html`）
  EventSource 只对"网络断了"自动重连；一旦服务端回了**不是
  text/event-stream** 的响应（端口被别的进程占住时最常见：那个端口上的服务
  对 `/events` 回 404 或一段 HTML），浏览器会置成 CLOSED 并**彻底停止重
  试** —— 之后一条消息都收不到，状态点恒为 offline，扩展推过来的话也进不
  了气泡，而且没有任何提示。用户只能整个重启桌宠。
  修：`onerror` 判 `readyState === CLOSED`，带退避（1s 起、封顶 15s）重建；
  `CONNECTING` 不插手（那是浏览器自己在重试）。
- **检测周期：保存失败界面说谎 / 响应乱序回弹**（`ui.html`）
  ① POST 的 `.catch` 是空实现，失败后界面一直停在假值上，直到下次右键被
  `syncDetectSec` 弹回去 —— 表现为"设置莫名其妙自己跳回去"。② 200 不等于
  落盘成功：`save_settings` 写文件失败时返回 `saved:false`，以前不看这个字
  段。③ 服务端是**多线程**服务器，响应不保证按发出顺序回来：右键开菜单
  （发 GET）后立刻点 chip（发 POST），先处理完的 POST 会被随后那个读到旧值
  的 GET 覆盖。④ 连点「+」10 下 = 10 个并发写。
  修：POST/GET 共用自增 `settingsSeq`，回调只接受序号最大的那个；失败与
  `saved:false` 都回滚 + 气泡提示；POST 合并 150ms 只提交最后一次的值。
- **底板取样失败计数在 5↔6 之间无限振荡**（`ui.html`）
  两条失败分支写的是 `cardSamples = CARD_SAMPLES - 1`，下一轮"已锁定"判定
  又是 false —— **永远锁不定**：视口尺寸恒取不到时每 500ms 重跑一次完整
  `characterRects()`（两次全画布 `readPixels` + `drawImage`），永久 CPU 空
  转。修：失败次数封顶（20 次）后强制锁定。
- **启动桌宠在 8 秒等待窗口内会 spawn 第二个进程**（`extension.ts`）
  "等服务器起来"最多跑 8 秒，期间存活探测一直为 false；用户最常见的操作
  "点了没反应再点一下"会**完整地再走一遍 spawn** —— 同时两个 python（第二
  个 bind 失败，要等它自己的 5 秒守卫才退出），且用户收到两条一模一样的
  "桌宠已启动"通知。修：用 Promise 把整段包住，重复调用返回同一个。
- **迟到的回滚会抹掉更新的状态**（`extension.ts`）
  `scanActiveFile` 的投递是异步的（500ms 超时），而检测周期最短可设到 1 秒。
  主线程繁忙时超时回调可能迟到到下一拍之后，无条件 `fileScanState = prev`
  会把**更新的那份直接抹掉** —— 桌宠恢复后把已判过的内容再播一遍。
  修：改成引用比较（只在状态仍停在自己写的那份时才回滚）。
- **diagnostics items 的 message 未截断**（`extension.ts`）
  `file_scan` 那条链路早就 `clipMessage(60)`，这条却原样投喂；C++ 模板错误、
  clang-tidy 说明、Python traceback 随便几 KB，20 条叠加后 body 能到几百
  KB —— 既容易撞 500ms 超时，也让每次签名比较都在序列化大字符串。
- **面板端口写死 19876 / 面板 dispose 不摘除**（`extension.ts`）
  用 `AIRI_STANDALONE_PORT` 换过端口时这块面板会指错方向；面板每次新建都
  往 `subscriptions` 追加、dispose 时不摘，反复"开→关"会一直堆积。
- **AIRI_WIN_BG=off 白等 10 秒并打印误导 WARNING**（`main.py`）
  按配置"别动底色"时 `fix_window_background()` 必然返回 False，而重试循环
  只认 `ok`，于是把 10 秒跑满再打一条并不是故障的 WARNING。
- **其他**：`reportSil` 是唯一没有 `.catch` 的 fetch（异步拒绝逃得过外层
  try/catch，会被全局 unhandledrejection 反过来上报一条 `jsError: Failed to
  fetch` 淹掉真正原因）；「编辑语料」请求没到达时菜单收起后一片死寂、前后
  端日志都零痕迹；SSE 消息缺 payload 时被空 catch 静默吞掉。

### Tests

- `live2d_probe/test_pet_041.py`（61 条）。**判断力已验证**：对 0.3.40 跑是
  **29 条 FAIL**，修完 61 条全绿。
- `src/test/extension.test.ts` 的"支持语言集合"用例原本是在测试文件里另抄
  一份字面量自己断言自己（改成 `['rust','go','ts']` 依旧全绿，零覆盖）。
  改为从 `extension.ts` 导入真实的 `supportedLanguageIds`。

## [0.3.40] — 2026-10-21

### Fixed

- **后台生成失败时静默丢单**（`standalone.py`）
  0.3.39 把生成挪出请求线程后，`/push` 从此**无条件回 200** —— 而扩展拿到
  200 就提交 `fileScanState` / `lastPushSignature`，再也不会补发。于是"生成
  没说出话"在这条链路上等于"这件事从没发生过"。三种情形实测（修复前）
  `sse_queue` 增量均为 0：生成抛异常 / 返回 `None` / 返回空文本。
  修：失败一律回落到 `_builtin_reply()`，保证"收了 200 就一定说得出话"。
  ⚠️ 兜底必须跳过 `heartbeat` —— `response_generator` 对它**故意返回 None**
  表示"这次不说话"，把"决定不说"改成"说一句内置台词"会让桌宠每次心跳
  都自言自语。
- **同一件事被说两遍 / 过期结果推翻刚说过的话**（`standalone.py`）
  后台生成既没有在途去重、也没有顺序保证。实测（修复前）：
  ① 0.2 秒内连发两次同内容 `/push` → `sse_queue` 增量 = 2（两个 VS Code
  窗口操作同一仓库时就是这个形状）；② 先 `file_scan`「还有 3 个 bug」、
  用户改好后 `all_clear`「干净了」，慢的那条后到 → 播报顺序
  `['干净了', '还有3个bug']`，桌宠自己推翻自己。
  修：用 `(trigger, file, error_count, reason, sample_errors)` 做在途签名；
  每次生成发单调序号，序号落后于已完成的最大号就直接丢弃。
- **body 读预算只按单次 recv 计时，慢速滴字节仍能占死 handler 线程**
  （`standalone.py`）0.3.39 加的 5 秒读超时是**单次 recv 的预算**：
  `self.rfile` 是 `makefile('rb', -1)` 造的 `BufferedReader`，`read(n)` 内部
  反复 recv 直到凑够 n 或 EOF，每次 recv 各自重新计超时。对端每 4 秒滴一个
  字节就永远不触发。实测（修复前）跨源 POST 声明 64 字节：0.5s/字节 →
  403 晚 **12.5s**；2s/字节 → 晚 **11.0s**（预算本该 5s）。
  修：新增 `_read_bounded()`，用 `read1()`（只对底层流发一次读请求）把
  "还要不要继续等"拿回自己手里，按**整体截止时间**逐轮驱动；两处读 body
  的地方统一走它。修后实测两种滴速都在 **5.0s** 被释放。
- **`resolve_port()` 在端口守卫之前写日志**（`standalone.py`）
  `__main__` 里 `resolve_port()` 跑在 `_ping_ok()` 之前，而它自己会 `log()`。
  实测（修复前）：实例已在运行、`AIRI_STANDALONE_PORT=0` 再启动 → 进程秒退，
  但日志被追加 98 字节的 `out of range`，且没有本次的 `--- boot ---`，
  看不出是哪个进程写的 —— 正好绕过了 0.3.39 刚立下的"第二个进程不写日志"。
  修：只攒进 `_port_warning`，由 `main()` 在守卫放行后 `flush_port_warning()`。
- **`sample_errors` 传标量让整条播报变成空气泡**（`response_generator.py`）
  实测 `sample_errors=5` → `TypeError: 'int' object is not iterable`，异常冒出
  `generate_response`，用户一句话都收不到，下次提醒要等满 60s 冷却。
- **`sample_errors` 字段为 null 时念出 "第 None 行：None"**
  （`response_generator.py`）`dict.get(k, default)` 只在**键不存在**时给
  默认值，键存在但值是 JSON `null` 时返回 `None`。实测气泡原文：
  `a.py 里还有 2 个 bug… 第None行：None`。
  ⚠️ 修的时候不能用 `or` 兜底：行号**真可以是 0**，`or` 会把合法值吃掉。
- **`error_count` 归一化后没回写 context**（`response_generator.py`）
  `generate_response` 把 `int(error_count)` 只放进局部变量就交给
  `try_deepseek_api`，后者读的是 context 里的原始值 —— `error_count="3"`
  时 `str > int` 抛 TypeError，被裸 `except Exception: pass` 吞掉，表现为
  "配了 API key 却永远只说内置台词"且零提示。
- **DeepSeek 客户端没有超时**（`response_generator.py`）不传 `timeout` →
  httpx 默认 600s + `max_retries=2`，网络不通时一次生成最坏挂约 30 分钟。
  0.3.39 之后它跑在后台线程，不拖请求但线程一直堆着。修：`timeout=8.0`、
  `max_retries=1`。
- **周期提醒会逐字重复上一句台词**（`response_generator.py`）bug 内容不变
  的周期提醒（`reason='remind'`，每 60s 一次）里台词池和变量完全一样，
  `random.choice` 没有记忆。实测 7 条的池子连抽 40 次，出现 3~5 次与上一句
  逐字相同。`_pick_click_line`（点击路径）早就有这个去重，场景台词没覆盖。

### Tests

- `live2d_probe/test_pet_040.py`（32 条）：A 段后台生成三条不变量（含
  heartbeat 静默这条反向断言）、B 段读预算整体截止时间（含 200KB 合法包
  不被误伤的反向断言）、C 段告警落盘时机、D 段后端输入加固与去重。
  **判断力已验证**：对 0.3.39 跑是 **18 条 FAIL**，修完 32 条全绿。
- `test_pet_039.py` 的 A7（穷举"不该有漏网的裸 `rfile.read`"）跟着改：
  入口从两处合并成 `_read_bounded` 一处，断言要反映新架构。

## [0.3.39] — 2026-10-20

### Fixed

- **二次启动会把正在运行的那个实例的日志清空**（`standalone.py`）
  `_init_log()` 以 `'w'` 打开 `.airi-pet.log`，而"已有实例在跑"要等 `main()`
  里绑定失败才看得出来 —— 于是第二个进程先把**正在跑的那个实例的全部排障
  记录**清空，再写一行 `already running` 退出(0)。用户报障时打开日志只剩
  一句"已退出"，真实现场已被抹掉；而且那个实例此后继续 append 到被截断的
  文件里，两个进程的记录混在一起。点两次状态栏按钮 / 扩展重复拉起 / 双击
  launch.bat 都能触发。
  修：端口守卫提到 `_init_log()` **之前**，命中已有实例时既不截断也不写。
  ⚠️ 排障日志是本项目唯一的可见通道 —— 扩展以 `stdio:'ignore'` 起进程，
  print / stderr 全被丢掉，日志被清 = 出问题后完全无从查起。
- **`AIRI_STANDALONE_PORT=0` 起出一个连不上自己的桌宠**（`standalone.py`）
  端口只有 `int()`、没有范围校验。而 **0 是合法整数**：绑 0 = 内核随机分配
  端口，绑定**成功** → 启动守卫不触发 → 窗口照常弹出。但 `load_html` 注入
  的是 `http://127.0.0.1:0`，页面里 SSE / fetch 全部打到不存在的端口，扩展
  却仍往 19876 推 —— 桌宠站得住、不说话、菜单读不到设置，日志零异常。
  实测：绑 0 拿到 2106。扩展侧 0.3.37 已夹 `[1,65535]`，这侧一直没夹。
- **跨源请求「声明多发少」能永久占死 handler 线程**（`standalone.py`）
  `_cors_guard` 排空 body 的手写 `while remaining > 0` 既无上限也无读超时。
  0.3.37 在 `_read_body()` 的注释里只论证了"负数进不去循环"，漏了"声明
  999999 字节却一字节不发"这第二种挂死 —— 那个 read 在无超时 socket 上安静
  等到 EOF，线程被占住、403 永远写不出去。
  实测（修复前）：连发 3 条并保持连接 → 线程数 2→5，客户端 3 秒读超时全部
  TimeoutError（收到的根本不是 403）。修：抽出 `_drain_body()`，同一套护栏
  （夹上限 + 读超时 + 读完恢复阻塞），`do_OPTIONS` 的 403 也补上排空。
- **`/push` 同步等 LLM → 扩展 500ms 超时 → 桌宠每拍复读同一条**
  （`standalone.py`）`generate_response()` 在请求线程里同步跑，配了
  `DEEPSEEK_API_KEY` 就是一次真实网络往返（OpenAI 客户端没传 timeout，默认
  600s + 2 次重试）。而扩展侧超时是 **500ms**：超时后 `fileScanState` 回滚、
  `lastPushAt` **倒退**（60s 提醒冷却被绕过）、`lastPushSignature` 不提交 →
  下一拍重新判定又是同一批错误。最坏的是 `push_message` 在回响应**之前**
  执行，所以"说了 + 没记账"，正是复读机的成因。
  实测（修复前）：注入 sleep(3) 的生成函数，`/push` 耗时 3.00s。
  修：生成搬到后台线程，`/push` 立刻回 200。
- **非 Live2D 时点角色毫无反应**（`ui.html`）`alphaAt` 在
  `!live2d.ready` 时 `return 0`，而 `handlePetClick` 的下一句是
  `if (alphaAt(...) < 24) return;` —— 立绘 / CSS 角色下点击整条链路在第一道
  判断就断了，连 `zoneAt()` 那条 `return 'body'` 的兜底都到不了。前端零报错
  （是 return 不是异常），日志一行都没有。
  实测（修复前）：非 Live2D 下点角色正中，`pet_click` 调用 **0 次**，而同一
  时刻 `zoneAt()` 返回的是 `'body'`。修：改用角色元素的外接框判定。
- **错误气泡的表情恒为 idle**（`ui.html`）
  `var emotion = (...) || 'idle'` 已把它兜成字符串，于是下面那句
  `setEmotion(emotion || 'angry')` 的 `|| 'angry'` **永远取不到** —— 红色错误
  气泡在抖，角色表情纹丝不动。修：兜底值改成 `null`，各分支自己决定。
- **气泡一出现就让 Live2D 双重晃动**（`ui.html`）`showBubble` 每次直接写内联
  `animation`，优先级高于样式表，把 `.live2d-active{animation:none}` 打穿且
  **永不恢复**（CSS 注释里写明：模型自带呼吸摆动，叠 idleSway = 双重晃动）。
  而且 idleSway 带 rotate —— `.character-box` 的 rect 从此随动画漂移，角色卡
  外接框与 region 采样每帧都在变。修：只在没有 `live2d-active` 时才重设。
- **alpha 判为不可信后，命中判定却走那条不可信的路径**（`ui.html`）
  `silAlphaMode === 'off'`（gl 与 2d 都不可信）时，三元式偏偏选 `'2d'` ——
  正是刚被判定"整张画布都是角色"的那条。于是 canvas 内任何坐标 alpha 都
  ≈255，点哪都说话；而同一时刻 `pushSilhouette` 因 `source='unreliable'`
  拒绝设 region，窗口还是整块矩形 —— 恰是那条"两者必须一致"注释要避免的
  错配。修：`'off'` 时按 canvas 矩形命中（与肉眼所见一致）。
- **Cubism core 的缓存目录只被读、从没被写**（`live2d_assets.py`）
  `ensure_core()` 会去 `~/.dsh/pets/.runtime/` 找现成的 core，但代码里从来
  没有任何地方往那儿写 —— 死路径。core 是专有许可、不能随 VSIX 分发，于是
  每次**重装/升级扩展**都要重新下载一次，离线环境下直接降级成立绘（"升级完
  角色变回图片了"）。修：拿到可用 core 后顺手镜像一份到那个目录。
- **core 校验只看体积不看内容**（`live2d_assets.py`）下载那份会校验
  `b'Live2DCubismCore' in blob`，本地已有的这份只查 `st_size >= 50KB` —— 一个
  "体积够大但不是 core"的文件（错误页、别处的 js）会被判成可用，Live2D 初始
  化按成功走、实际渲染失败，而降级原因在日志里看不出来。修：统一标准。

### Changed

- **`start.bat` 去掉硬编码的用户名路径**：原来写死了
  `C:\Users\<用户名>\AppData\Local\Programs\Python\Python312\python.exe`，而
  它随 VSIX 一起分发 —— 既泄露开发者本机目录，又在任何别的机器上静默回落到
  `python`。改成 `AIRI_PYTHON_PATH → python → python3 → py` 并用 `where`
  逐级校验，全都没找到时明确报错。`launch.bat` / `launch_full.bat` 同样补上
  `where` 校验（三个 bat 仍然全部是纯 ASCII —— cmd.exe 用 OEM 码页解码批处理，
  UTF-8 中文会把整个脚本的解析搞崩）。
- **`.vscodeignore` 收口**（vsce 只认它，不看 `.gitignore`）：补上
  `live2dcubismcore.min.js`（Live2D 专有运行时，**不可再分发**；0.3.38 的包里
  实测有 207,155 字节），以及 `out/test/**`、`方案B_*.md`、`.env.example`、
  `python_backend/main.py`。注意 `python_backend/corpus.py` 与
  `response_generator.py` **是运行时必需的**，不能排除。

### Tests

- 新增 `live2d_probe/test_pet_039.py`（45 条）：排空 body 的两道护栏（静态
  契约 + **实测**线程数与真实 403）、端口取值范围 9 组、`/push` 耗时与
  "生成真的发生了"、以及**用 node 跑 ui.html 真实函数源码**验证点击兜底 /
  表情兜底 / 动画包裹 / `'off'` 命中四条。
  判断力已验证：对着 0.3.38 跑是 **24 条 FAIL**（A/B/C/D/F 五段全红）。

## [0.3.38] — 2026-10-19

### Fixed

- **语料加了注释就整份失效**（`python_backend/corpus.py`）
  `strip_jsonc` 处理尾随逗号时，向前只跳空白、**不跳注释**。而语料文件头
  `_FILE_HEADER` 是这么写的：「本文件允许注释和多余的尾逗号」—— 用户照着做
  （加一条台词 + 在行尾写 `// 新加的`）就正好踩中：注释被剥掉后留下非法的
  尾逗号，`json.loads` 直接报错，**12 个场景 + 3 个点击分区全部回落内置**，
  用户改的台词一条不生效。
  实测（修复前）：加台词 + 行尾注释 → `scenes` 从 12 变 0。修复后 12、新台词生效。
- **语料存成非 UTF-8 时桌宠起不来**（`corpus.py` + `standalone.py`）
  `open(..., encoding='utf-8-sig')` 抛的 `UnicodeDecodeError` 是 `ValueError`
  子类，**不是 `OSError`**，所以从 `except OSError` 旁边穿了出去。取词路径有
  `except Exception` 兜着（看起来只是"回落内置"），但 `corpus_status()` 没有，
  而它在启动段是**顶层裸调用**（在 `main()` 之前）→ 进程直接没了。
  用记事本把语料另存为 ANSI(GBK) / Unicode(UTF-16LE) 即可触发。
  修：捕获 `UnicodeDecodeError`；`corpus_status()` 自带兜底（排障函数自己不能崩）。
- **热重载读到坏文件会把上一版完好语料清空**（`corpus.py`）
  `_read_corpus_file` 出错时无条件 `_cache.update(scenes={})`。编辑器保存语料
  是"截断 → 原地写"，30 秒的检测周期完全可能落在中间；或语料在网盘/OneDrive
  下处于仅联机占位状态。原本完好的语料被抹空，且 key 被设成这次失败的
  `(mtime, size)` —— 只要它不再变就**永远不重试**。
  修：保留 last-known-good，只更新 key 与 error（留痕照旧，功能不退）。
- **相对路径立绘让桌宠零提示起不来**（`desktop_pet/common.py`）
  `find_character_image()` 接受相对路径（README 就是这么教用户写的），而
  `load_html()` 里 `Path(相对).as_uri()` 抛 `ValueError`；两个调用点都是裸调用，
  进程一崩，traceback 走 stderr 被扩展以 `stdio:'ignore'` 丢掉 —— 用户点了
  「启动桌宠」什么都没有，日志文件里一行线索都不留。
  修：`find_character_image` 返回绝对路径；`load_html` 对坏立绘只降级（退回
  CSS 角色），读不到 ui.html 时返回 `None`，两个调用点显式判空并落日志。
  顺带：`resolve()` 对不存在的路径也是成功的，会产出指向空处的 `file://`
  （页面显示裂图，比降级更难看）→ 补存在性判断。
- **region 合并丢弃 `CombineRgn` 返回值**（`common.py`）
  `_merge()` 算出了返回码却被调用点丢掉，而 `kept += 1` 是无条件的。
  `CombineRgn` 一旦失败（返回 `ERROR=0`，此时 `h_total` 一点没变，仍是空区域），
  `kept` 却已经 > 0，`if not kept` 这道"不许把空 region 设上去"的防线被绕过 →
  `SetWindowRgn` 成功装上一个**空区域**：窗口不可见、鼠标完全点不到（连右键
  菜单都打不开 → 退不出去，只能杀进程）。而且返回 `ok=True`，前端 `silFails`
  不递增、`silLastKey` 不清，形状又没变 → 错误状态会一直驻留。
- **`rad` 未钳制 + 异常白名单不全**（`common.py`）
  圆角矩形的 `ew/eh` 是整段里唯一没钳制的量：`rad=inf` → `int(round(...))` 抛
  `OverflowError`（不是 `ValueError`）；大到超 `c_long` → ctypes 抛
  `ArgumentError`。两者都不在白名单里，会逃出去把**已经并好的几十块 region
  全部作废**，整拍形状白干。修：钳到不超过矩形自身 + 白名单补齐。
- **`get_position` 是四个桥方法里唯一没兜底的，且 `x or 0` 把坐标 0 当假值**
  （`common.py` + `ui.html`）窗口已 destroy 后补发一次 pointerdown 就会穿过桥
  （JS 侧的 rejection handler 是空函数，表现为"这次拖拽跳一下"，日志毫无痕迹）；
  而 `x or 0` 是假值兜底 —— 窗口真的停在 `x==0`（拖拽钳制允许贴屏幕左/上边缘）
  时返回 0，JS 侧 `(pos && pos[0]) || winStartX` 把 0 当"没取到"丢掉。
  修：Python 侧加 try + 用 `None` 判断；JS 侧改 `typeof/isFinite` 判断。
- **立绘不支持 `.jpg`**（`common.py`）`assets/README.md` 写着支持 PNG/GIF/
  **JPG**/WebP，代码里候选名只有 png/gif/webp —— 用户把 `character.jpg` 放进
  `assets/` 会静默退回 CSS 角色，一句警告都没有。

### Tests

- 新增 `test_corpus_jsonc.py`（21 条）：尾逗号+注释的四种写法、非 UTF-8 两种
  编码（含冷启动与热重载）、坏文件保留 last-known-good 与"修好后能恢复"。
  判断力已验证：对 0.3.37 是 **11 条 FAIL**。
- 新增 `test_pet_startup_guard.py`（26 条）：立绘绝对路径/格式、`load_html`
  降级与判空、`get_position` 的 0 坐标与异常兜底、region 合并契约。
  判断力已验证：对 0.3.37 是 **19 条 FAIL**。
- ⚠️ 教训：B 段第一版在异常路径下引用了没被赋值的 `r`，抛 `NameError` 把
  **后面几条用例整段吞掉** —— 看起来像"GBK 没事"，其实压根没跑。
  测试里每条用例都必须能独立跑完。
- 全量回归 **28/28**。

### 顺带确认没问题的（别重复查）

- `common.py` 的 GDI 句柄配对（创建/删除逐条核对，无泄漏）、结构体对齐、
  `byref` 用法、`SetWindowRgn` 返回值判断 —— 都对
- 资产服务器路径穿越：17 个载荷（`../`、`..\`、`%2e%2e`、`C:\`、`//?/`、
  内嵌 NUL）**全部正确拒绝**
- `get_lines` / `get_click_lines` 的取词路径不会崩（有 `except Exception`）
- 点击轮换真身 `standalone._pick_click_line`：空池/1 条/2 条都不会死循环或越界

## [0.3.37] — 2026-10-18

### Fixed

- **畸形 `Content-Length` 会把服务端线程永久挂死（可静默累积的泄漏）。**
  `self.rfile.read(int(Content-Length))` 只兜了 `TypeError/ValueError`：
  - 负数 → `read(-1)` 是 **readall** 语义，一直读到 EOF；而客户端发完请求头
    是在**等响应**、不会关连接 ⇒ 线程永久阻塞；
  - 虚大（声明 1000 只发 20）→ `read(1000)` 阻塞等收满 ⇒ 同样挂死。

  两者**都不抛异常**（socket 没设读超时），`handle_error` 不会被调用、日志一行
  都没有。实测（修复前）：连发 5 次 `Content-Length: -1` 且保持连接 → 进程线程数
  从 2 涨到 7，之后再也没退过；每个泄漏都带走一个 socket 句柄。
  修：统一走 `AiriHandler._read_body()` —— 先夹 `0..1MB`（越界 400），再加 5 秒
  读超时挡"声明合法但少发"，读完把超时恢复成阻塞（SSE 不能带着超时跑）。
  ⚠️ 四处 `Content-Length` 里第 4 处（`_cors_guard` 那个 `while remaining > 0`
  循环）负数天然进不去，**本来就是安全的**，已单独写进回归防止被误删。

- **超大 `Last-Event-ID` 让 SSE 连接永久饿死。** `after_seq` 只做 `int()`，
  不钳上界。`_seq` 是进程级单调计数且**重启归零**，客户端带一个比当前 `_seq`
  更大的 id 时 replay 必然为空，而 `last_seq` 就停在那个巨大值上 ⇒ 此后 `_seq`
  得涨到 `after_seq+1` 才可能有消息被选出，其间一条都收不到；循环外层是
  `except Exception: pass`，日志零痕迹。
  实测（修复前）：同样推 3 条消息，全新连接收到 **14 条**，带
  `Last-Event-ID: 99999999` 的那条收到 **0 条**。
  修：`after_seq` 钳到 `max(0, min(after_seq, _seq))`（在 `queue_lock` 内读）。

- **拖过一次之后桌宠会一直粘着鼠标。** `dragging` 只有 `document.mouseup`
  一条复位路径，而 `move_window` 是跨桥异步的（JS → pywebview → Python →
  `ctypes.MoveWindow`），窗口跟不上光标 —— 快速甩动、拖到屏幕边缘（Python 侧
  `clamp_to_virtual_screen` 把窗口卡住而光标继续走）、拖拽中 Alt+Tab —— 松手
  那一刻光标常常已在窗口之外，`mouseup` 被桌面/别的窗口吃掉，`dragging` 就
  **永远是 true**。之后不按任何鼠标键，只要光标经过桌宠，`mousemove` 照常
  `move_window`。
  修：改用 pointer events，按下时 `setPointerCapture` 把后续指针事件锁到
  `petArea`（光标甩出窗口也收得到 `pointerup`）；再叠三道兜底 ——
  `pointermove` 里 `buttons === 0` 复位、`pointercancel` 复位、`window blur` 复位。

- **扩展侧非法端口让「启动桌宠」静默失效。**
  `Number(process.env.AIRI_STANDALONE_PORT) || 19876` 只兜了 NaN/0，没有
  `0~65535` 范围校验。而 `http.get` 对越界端口是**同步抛** `ERR_SOCKET_BAD_PORT`
  （实测 node：99999 / -1 / 70000 三个值都是同步抛，不是 error 事件），发生在
  `checkStandaloneAlive()` 那个**没有 try/catch** 的 Promise executor 里 →
  Promise reject → `launchStandalonePet` 在它唯一那个 try **之外**抛出：桌宠
  永远拉不起来、连一条报错都没有。`AIRI_STANDALONE_PORT` 是 `.env.example`
  明示支持的官方配置项。
  修：端口夹紧到 `[1, 65535]`；`checkStandaloneAlive` 的 `http.get` 包 try/catch
  （`postJsonToPet` 早就有这层，这是漏掉的另一半）。

### Tests

- `live2d_probe/test_pet_body_guard.py`（22 条）—— A 段静态契约（含"调用方没有
  漏网裸读"的穷举）、B 段行为（真的收到 400、线程数不涨、读超时真生效）、
  C 段留痕、D 段 SSE 续传语义。判断力已验证：对 0.3.36 是 **16 条 FAIL**。
- `live2d_probe/test_drag_state.py`（19 条）—— A 段把状态机抽出来配最小 DOM stub
  在 node 里真跑，用 `move_window` 调用次数这个**副作用**判定有没有收手；
  B 段钉死三道兜底。对 0.3.36 是 **21 条 FAIL**（整段抽不出来）。
- `live2d_probe/test_ext_port_guard.py`（14 条）—— 13 个人工构造的端口值逐个人喂；
  带**反证**（旧写法确实放行 `-1 / 65536 / 99999 / 70000` 四个越界值，证明这条
  校验不是多余的）；B 段坐实 `http.get` 同步抛；C 段查编译产物同步。
  对 0.3.36 是 **10 条 FAIL**。
- 全量回归 26/26 通过。

### 顺带确认没问题的（免得重复查）

- `python_backend/response_generator.py`：外部输入的类型加固（`error_count`
  的 `int()`、`sample_errors` 的字符串/标量归一化、占位符默认值）都在。
- `desktop_pet/live2d_assets.py`：`check_assets()` 的"永不抛异常"契约、
  `ensure_core` 的"可用 = 落点上有合法文件"（复制而非就地使用）、CDN 下载的
  内容校验（长度 + `Live2DCubismCore` 符号）—— 都对。
- `desktop_pet/main.py`（旧入口）：`_after_window_ready` 的重试循环有 deadline，
  调的两个函数内部各自兜住异常只返回 `(ok, detail)`。
- SSE 消息队列：`MAX_QUEUE_SIZE=200` 的裁剪在 `queue_lock` 内、`append` 之后，
  两处遍历也都在锁内且生成新列表 —— 没有"边遍历边改"。

## [0.3.36] — 2026-10-17

> 用户反馈：「desk 区域点了等同于 body，调整一下各自的触发区」。
> 这次没有猜 —— 本地渲染不了 Live2D（无头浏览器不可用、模型没声明 HitAreas、
> 预览图是不透明海报），最后是**读运行中桌宠的窗口 region** 量出来的：
> `live2d_probe/measure_region.py`（GetWindowRgn + GetRegionData）。

### Fixed

- **desk 分区一条台词都触发不了 —— 旧边界把 desk 带画在了可点击区域之外。**
  实测（运行中的桌宠，窗口 286x443）：
  - Live2D canvas 的 CSS 盒是 `161x211 @ (62,179)`（`[airi-sil]` 上报的 cssBox）；
  - 窗口 region 只有两大块：角色卡 `y 202~353`（card=square，按角色实测外接框
    贴的正方形卡）和名牌 `y 391~425`；
  - 换算成 canvas 纵向占比 ry：**角色（含桌子）画在 ry 0.109~0.825**，
    而 zoneAt 的 desk 带是 `ry>=0.85` —— 整条带落在角色卡下缘之外的缝隙里，
    缝隙不在窗口 region 内，点击直接穿透到下层窗口。
  - 所以点桌子永远得到 body 的台词（用户日志 `pet_click #1 zone=body` 实锤）。
  修法：desk 边界 0.85 → **0.62**（head 0.45 不变 —— Q 版大头，没人抱怨过）。
  另：`pet_click` 现在带 **ry** 落日志 —— 以后要再校准边界，点一下桌宠、
  看日志里"那一下点在 canvas 百分之几"就行，不用再起探针。

### Tests

- 新增 `live2d_probe/test_click_zones.py`（12 条）：A 段把 `zoneFromRatio`
  抽出来在 node 里真跑（12 个边界值）；B 段用**实测数字**钉死
  "desk 带起点必须在角色卡内（<0.825）"，防止以后又把边界挪出角色外。
  判断力已验证：对 HEAD(0.3.35) 是 **12 条 FAIL**。
- 新增 `live2d_probe/measure_region.py`：从运行中的桌宠窗口读 region、
  反推轮廓行分布（这次的取证工具，留下备用）。
  ⚠️ 踩坑记录：GDI 的区域类型常量是 `ERROR=0 / NULLREGION=1 / SIMPLEREGION=2 /
  COMPLEXREGION=3` —— 第一版把 3 当成"错误"，实际 3 = **读到了**复杂区域。
- 修 `test_pet_corpus.py` H 段的过严断言：**"真实设置文件里不许有
  openCorpusSeq"改成"测试没让它新出现"**。它是「编辑语料」功能的正常产物，
  用户用过一次就写在里面（本轮实测：跑测试时用户文件里就有
  `"openCorpusSeq": 1`，把测试打红了——不是产品坏了，是断言写错了）。
- 全量回归 **23/23** 通过。

## [0.3.35] — 2026-10-16

> 例行审计。这轮出动的是「本地服务到底对谁开放」—— 桌宠的 HTTP 服务监听
> 127.0.0.1，但 CORS 头写的是 `*`，等于**对任意网页开放**。两条都是实测取证
> （起真服务器打真请求），不是静态推断。

### Fixed

- **任意网页都能让桌宠说话、还能改它的设置（CORS 头写死 `*`）。**
  主服务在 `127.0.0.1:19876`，而 ui.html 是从资产服务器（`127.0.0.1:<动态端口>`）
  加载的 —— 端口不同即跨源，响应必须带 CORS 头，否则前端一个请求都发不出去
  （0.3.25 踩过）。但写成 `*` 之后实测（`Origin: https://evil.example.com`）：

  ```
  POST /push     -> 200，桌宠照着念任意文案（application/json 与 text/plain 都行）
  GET  /settings -> 200，设置被读走
  OPTIONS 预检    -> 204 一路放行
  ```

  也就是说用户随手打开的一个网页就能 ① 冒充桌宠发言（"构建失败，点这里修复"
  这类钓鱼文案）② 开 `/events` 偷听 —— SSE 流里有 file_scan 的文件名、代码行
  和诊断内容。
  修法：只有 Origin 是**回环地址**（或根本没有 Origin —— 扩展 / curl 这类非
  浏览器客户端）才放行，其余一律 403 且**不执行**任何动作；CORS 头改成回显
  请求里的 Origin。
  ⚠️ `Origin: null`（`<iframe sandbox>` / file://）不放行 —— 桌宠页面用不到它，
  而沙箱 iframe 是攻击者随手就能造出来的。
  ⚠️ 拒绝前先把请求体读完：HTTP/1.0 下服务端随即关连接，接收缓冲区里剩未读
  字节时 Windows 会发 RST，把刚写出去的 403 一起冲掉。

- **`handle_error` 挂在了错误的类上 —— 「关窗不打 traceback」这条保护从来没生效。**
  socketserver 的 `handle_error` 是 **BaseServer**（服务器）的方法，而代码把它
  写在了 **AiriHandler**（请求处理器）里，`BaseRequestHandler` 上根本没有这个
  名字。实测：连开三次 SSE 再断开，处理器上的计数器 **0 次被调用**，stderr 上
  照样是一整段 `ConnectionResetError` traceback。
  同样的拷贝在 common.py 的资产服务器上也有一份（**同类问题要穷举** —— 0.3.20
  就写过这条教训），两边一起挪到各自的服务器类：`ThreadingHTTPServer` 与新增
  的 `_AssetServer`。
  ⚠️ 这是「看起来有防护其实没有」的典型：静态读代码看不出来，得数调用次数。

### Tests

- 新增 `live2d_probe/test_pet_origin.py`（22 条）：起真服务器打真 HTTP，既验
  「恶意来源被挡」也验「合法来源照旧」（护栏最容易写歪成连自己人也挡了）。
  判据是**副作用**而不是状态码 —— 队列有没有增长、设置文件有没有被改写：
  实测修复前恶意来源真把 `detectIntervalSec` 从 15 改成了 7。
  判断力已验证：对 HEAD(0.3.34) 是 **21 条 FAIL**。
- 新增 `live2d_probe/test_server_noise.py`（11 条）：直接喂异常给 `handle_error`，
  验证断连类异常静默、**意外异常照旧报错**（不能一起吞）。
  ⚠️ 端到端那条改成 SKIP 而非硬断言：走真实 socket 制造断连是**竞态**的
  （服务端可能抢在客户端 close 之前把 `:ok` 写完），同一份代码两次跑结果不同，
  当断言会变成一条时红时绿的测试。
  判断力已验证：对 HEAD 是 **4 条 FAIL**。
- 全量回归 **22/22** 通过。

## [0.3.34] — 2026-10-16

> 例行审计。这次是**气泡文案没有长度上限**：看不见的字照样要一字一字打完，
> 而这条路径用户自己就能踩到（右键「编辑语料」写一条长台词）。

### Fixed

- **`typeText` 按字定速（最少 16ms/字），而气泡显示能力只有 5~6 行 ——
  一条长文案能把气泡占住几分钟。**
  气泡 CSS 是 `max-height:132px + overflow:hidden`，13px/1.7 的行高只放得下
  5~6 行；多出来的字用户一行都看不到，但打字机照样按 16ms/字把整条念完。
  实测：5000 字 → 80 秒，50000 字 → **13.3 分钟**，这段时间气泡被它占着
  （`MAX_BUBBLES=2`），后面的话排不上号。
  ⚠️ 这不是"外部 POST 才会踩"的理论风险：右键「编辑语料」给用户自己用，
  在 `~/.dsh/pets/airi-pet-corpus.jsonc` 里写一条 5000 字台词保存，桌宠就
  会照着念 80 秒（已实测）。
  修法：`clipBubbleText()` 把文案裁到 `MAX_BUBBLE_CHARS`(200) 并附
  "…（已省略 N 字）"，超限的那部分本来也看不见；裁完打字时长从 80 秒/13.3
  分钟降到 **3.4 秒**。
- **气泡被移除后，打字机仍在往脱离文档的节点里打字。**
  `typeText` 是 `setTimeout` 自递归链，没有"节点还在不在"的判断：气泡按
  8 秒 TTL 消失或被新气泡挤掉之后，这条链还在跑 —— 长文案被打断时要空转
  好几分钟。修法：每拍先查 `el.isConnected`，不在文档里就停。

### 顺带确认没问题的（这次查过、没动）

- `watcher.py`：限流（每轮最多 20 个检查）、mtime 只在检查后提交、删除文件
  后清理缓存、签名含错误总数 —— 都对。
- `corpus.py`：**坏语料不崩**。实测 11 种坏法（语法坏 / 根是数组 / 根是字符串 /
  空文件 / scenes 是字符串 / 场景是对象 / 台词混非字符串 / BOM / 超长 /
  占位符越界）全部静默回落到内置 7 条，没有一次抛异常。

### Tests

- 新增 `live2d_probe/test_bubble_text.py`：A 段是**行为级** —— 把
  `clipBubbleText` 抽出来在 node 里真跑（与 `test_alpha_orientation.py`
  抽 `rowsBottomUp` 同一套路），断言短文案原样、超限被裁且带提示、
  5000/50000 字的打字时长从 80/800 秒降到 3.4 秒、数字/null 不炸；
  B 段是静态契约（`typeText` 的 `isConnected` 守卫、`showBubble` 的调用点）。
  判断力已验证：同一组断言对 HEAD(0.3.33) 全部 MISS。
- 全量回归 20 项全绿。

## [0.3.33] — 2026-10-15

> 例行审计，这次抓到的是扩展（TS）侧一处**共用判断依据**的缺陷：把
> `checkStandaloneAlive()` 的**缓存**当成"桌宠还活着"的证据。缓存最长 15 秒，
> 于是桌宠刚死的那段时间里，一系列"不可吞"的保护全都在错误的地基上工作。

### Fixed

- **推送没送到，却被当成送到了 —— "脏标记不可吞"事实上失效。**
  `scanActiveFile()` 与 `pushPendingDiagnostics()` 都先用
  `checkStandaloneAlive()` 判断桌宠在不在，而那个结果带 15 秒缓存；真正的
  推送又是 fire-and-forget（忽略响应）。桌宠刚死的窗口里缓存仍是 true：
  消息投进黑洞，状态却照常提交 —— 0.3.19 定下的"桌宠不可达必须回滚"
  在这段时间里根本不会触发。用户侧表现：桌宠重启后第一条 bug 播报消失，
  file_scan 那条要等满一个 `repeatSec`（60s）才会再提醒一次。
  修法：判据换成唯一的事实 —— `postJsonToPet` 现在返回
  `Promise<boolean>`（收到 2xx 才算送达），两条路径按**投递结果**决定回滚；
  失败时顺带作废健康缓存。附带修掉一个同源隐患：去重的
  `lastPushSignature` 以前在投递**之前**就提交，投失败后下一拍会被
  "和上次一样"挡掉 —— 那条播报再也不会补发，现在改为送达后才提交。
- **点状态栏启动桌宠，只得到"已经在运行了"，然后什么都没发生。**
  `launchStandalonePet()` 同样用了带缓存的探测结果：桌宠刚死时缓存还是
  true，函数直接返回；用户要等下一次 10 秒的状态栏轮询把缓存刷新过来才
  点得动。启动是用户显式动作，判断必须基于当下的事实 —— 现在启动前强制
  重新探测（`lastHealthCheckAt = 0`，与 `refreshPetStatus()` 一致）。

### Tests

- 新增 `live2d_probe/test_extension_push.py`：A 段是 `src/extension.ts`
  的静态契约（扩展的 TS 集成测试要真 VS Code 宿主，无头环境跑不了），
  B 段守住"编译产物同步"（VSIX 里带的是 `out/`，改了 `src` 忘了 `tsc`
  就是修了却没生效），C 段是行为级 —— 起真服务器验证 TS 赖以判断
  "送达与否"的 HTTP 契约（活端口 2xx / 死端口拿不到 2xx / 畸形 body
  仍算送达）。
  判断力已验证：同一组断言对 HEAD(0.3.32) 全部 MISS，且三条旧病灶都还在。
- 全量回归 19 项全绿。

## [0.3.32] — 2026-10-14

> 用户截图反馈「对话框有点问题」：旧气泡完整，**最新气泡被裁成只剩中间两个
> 字**，且两条气泡在最后一次点击后 23 秒仍未按 8 秒 TTL 消失。顺着这条线
> 查下去是**两个独立缺陷叠在一起**——一个制造了错误的窗口形状，另一个让
> 修正推送没能发生。

### Fixed

- **气泡只锁了高度、没锁宽度 → 打字前清空文本把气泡盒塌缩成 ~32px 窄条，
  窄条被写进窗口 region（本次裁字的直接原因）。**
  `showBubble` 为了打字机效果会先 `el.textContent = ''` 再逐字填充；上一轮
  只锁了**高度**（`el.style.height = el.offsetHeight`），宽度是 shrink-to-fit
  的自然宽度，清空后就是 padding 那点宽。紧接着的
  `setTimeout(pushSilhouette(true), 0)` 量到的正是这条窄条 —— 它一进
  `SetWindowRgn`，条外像素全部不显示。设计上 540ms 后还有一次修正推送能
  救回来，**但那要求页面在这 540ms 里还活着**。修法：宽度与高度成对落下
  （`box-sizing: border-box`，`offsetWidth` 即整盒宽），气泡从诞生第一帧起
  就是最终盒子，任何时刻被推上 region 都是对的。
- **页面在那 540ms 里被冻结，而事后整条链路是失明的。**
  两条 8 秒 TTL 的气泡 23 秒后仍在 → 定时器停摆；而 `push_message` 每次推送
  都会查 JS 心跳（滞后 >3s 必留痕），日志里最后一次点击时心跳还是新的 →
  冻结发生在点击后约 0.4 秒。两处跟进：
  - `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 以前是 `os.environ.setdefault`
    —— 扩展拉起桌宠时子进程继承 VS Code 宿主的完整环境，**宿主链上任何一环
    设过这个变量，整组禁节流开关就被静默放弃**（不报错、日志无痕）。改成
    合并：宿主开关全保留、我们的补在后面；`--disable-features` 取并集合成
    一个 flag（它的值是逗号表，重复出现会后写覆盖先写）。
    同时补上 `CalculateNativeWinOcclusion` —— 桌宠是分层（region 裁剪）+
    置顶窗口，正是 Windows 原生遮挡计算最容易误判成"被完全盖住"的形状。
  - `pet_heartbeat()` 现在会在**恢复的第一拍**补记停摆时长。冻结期间什么都
    执行不了（包括日志本身），所以只能在恢复时留痕 —— 以前这条"为什么气泡
    被裁了一截却永远不恢复"在日志里连一条痕迹都没有。

### Tests

- 新增 `live2d_probe/test_bubble_region.py`：A 段是 ui.html 的静态契约
  （无头环境跑不了浏览器布局，shrink-to-fit 的塌缩无法单元复现），
  B / C 段是行为级断言（开关合并、心跳停摆留痕）。
  判断力已验证：同一组断言对 HEAD(0.3.31) 是 `A=FAIL B=FAIL C=FAIL`，
  对工作区是 `A=pass B=pass C=pass`。

## [0.3.31] — 2026-10-14

> 例行审计。这轮逮到 **5 个缺陷**，全都具有同一个形状：**"宣告成功"与
> "东西真的到位"分家**。每一个都拿到了"修复前失败 / 修复后通过"的实测对照，
> 并各自补齐了会红的回归。

### Fixed

- **`AIRI_CUBISM_CORE` 谎报成功 → 桌宠在开窗之前整个进程崩掉（用户侧零痕迹）。**
  `ensure_core()` 的 env 分支以前直接 `return True` —— 文件还躺在用户自己
  指定的路径上，而调用方 `check_assets()` 紧接着 `CORE_PATH.stat()` 落点 →
  `FileNotFoundError` 一路逃出 `standalone.main()`。表现是**点了启动，什么都
  没发生**：没有窗口、没有报错、`.airi-pet.log` 里连一行都没有（扩展用
  `stdio:'ignore'` 起进程，`print` 全被丢掉）。
  而且这个 env 变量**本来也不可能生效** —— 页面只会从 `/vendor/` 取 core
  （见 `common._AssetHandler`），所以"跳过下载"必须等于"把文件搬到位"。
  修法：抽出 `_cache_core_from()` 作为"搬到落点"的单一实现，env 分支改成
  `if _core_ok(path) or _cache_core_from(src, path): return True`。
  ⚠️ **全新克隆必现**：`live2dcubismcore.min.js` 在 `.gitignore` 里。
- **`check_assets()` 的隐性契约收口为"永不抛异常"。** 它只是一次"能不能上
  Live2D"的自检，失败的正确后果是退回图片 / CSS 角色 —— 而不是让桌宠起不来。
  两个入口（`standalone.main` / `main.main`）都是**裸调用**，所以"不抛"
  只能由函数自己保证。现在它是"永不抛"的包装 + `_check_assets()` 实现体，
  收尾那次 `CORE_PATH.stat()` 也进了 `try`（宁可降级，不要抛）。
- **`/event` 的入参没做类型守卫，把良性畸形输入伪装成"解析错误"。**
  分支里写的是 `p = msg.get('payload') or {}` —— 只兜 falsy。`payload=5`
  这种**真值非 dict** 会在 `p.get` 上抛 `AttributeError`，再被外层 `except`
  记成 `[airi-standalone] /event parse failed`。`/push` 侧早就有 `isinstance`
  守卫（0.3.25），这是同一个坑的第二个入口。实测 15 条畸形输入里 **10 条**
  被记成 parse failed —— 在排障日志里制造假线索（0.3.26 就是被这类假线索
  带偏的）。修法：`_as_dict()` 收敛 payload、顶层非对象改为留痕
  `ignored non-object body: <类型>`、`_corpus_open_ack` 的 `int()` 补兜
  `OverflowError`（Python 的 `json` **默认接受**字面量 `Infinity`）。
- **资产服务器：`HEAD` 绕过整个白名单。** 处理器只覆写了 `do_GET`，于是
  HEAD 落到 `SimpleHTTPRequestHandler.do_HEAD → send_head() → translate_path()`，
  而它依赖的 `self.directory` **从来没被指定过** = 进程 CWD；扩展拉起桌宠时
  cwd 恰好是 `desktop_pet/`。实测：
  `HEAD /common.py → 200 Content-Length=48284`、`HEAD /standalone.py → 200
  Content-Length=48069`（对照 `GET /common.py → 404`）。页面**从来不发 HEAD**
  （页面本体与模型都用 GET），这条路纯属攻击面。修法：`do_GET` / `do_HEAD`
  共用同一份 `_dispatch()` 路由（不是"再补一份 HEAD 白名单"）。
- **资产服务器：畸形路径把请求打崩。** `_safe_join()` 里 `Path.resolve()`
  遇到内嵌 NUL 抛的是 `ValueError`（不是越界那种 `ValueError(relative_to)`），
  而它不在任何 `try` 里 → 一个 `GET /vendor/%00` 就让请求**直接断连**
  （客户端 `RemoteDisconnected`），宿主控制台吐一整段
  `ValueError: stat: embedded null character in path`。修法：把 `ValueError`/
  `OSError` 一起兜住，走与越界同一条路（403）。
- **异形窗口：早退路径泄漏 GDI 区域句柄。** `_apply_window_region()` 里
  `if not kept:`（所有矩形都被裁没）这条路上，`h_total` 既没交给系统
  （`SetWindowRgn` 没被调到）也没自己 `DeleteObject` —— **每次调用泄漏
  1 个 GDI 句柄**。region 是页面 **500ms 一拍**推上来的，也就是每秒 2 个；
  进程 GDI 句柄默认上限 10000，到顶后 `CreateRectRgn` 开始失败，
  "异形窗口"这个功能就**永久坏掉**（不是变慢，是彻底不工作）。
  这条早退路径还特别容易进：最小化、DPR 变化的一瞬间、拖出屏幕把矩形
  全裁掉，都会得到 `kept == 0`。修法：早退前 `gdi32.DeleteObject(h_total)`
  （另两条退出路径一直是删了的，只有这条漏了）。

### Tests

- **`test_live2d_assets_core.py`（新增）** —— Bug "env 谎报成功"的回归。
  含一条**旧 bug 形状的单元复刻**：把 `ensure_core` 换成 `lambda: True`
  谎报成功 → `check_assets()` 必须返回 `(False, [...])` 而**不是抛**。
  隔离做全：core 落点整体搬进 `%TEMP%`（同时改 `CORE_PATH` 与
  `ensure_core.__defaults__`）、`Path.home()` 指向假 HOME（否则"复用
  `~/.dsh/pets` 副本"那条路会真的读用户机器，让"断网应失败"的用例假通过）、
  `CORE_URL` 指向死地址。
- **`test_asset_server.py`（新增）** —— HEAD 白名单 + 畸形路径的回归。
  ⚠️ 它**必须把 cwd 切到 `desktop_pet/`**（复现扩展拉起时的条件），否则
  缺陷在修复前也"看起来是好的"。断言含 5 个白名单外源码文件必须 404、
  白名单内三条路的 `Content-Length` 等于磁盘实际字节数、`favicon` 204 无 body、
  5 条内嵌 NUL 与 3 条 `..` 越界必须 403 且**一条都不断连**。
- **`test_region_handle_leak.py`（新增）** —— GDI 句柄泄漏的回归。
  判据是**增量随 N 的线性度**而不是单次数值：N=200 / N=2000 两阶梯。
  ⚠️ 两个测量陷阱都写进了断言：`GetGuiResources(GetCurrentProcess(), ...)`
  **恒返回 0**（伪句柄不被接受，必须 `OpenProcess(0x0400)`）；
  泄漏会把句柄顶到上限让后续测量全部失真。安全性：只走 `kept == 0` 的
  早退分支（`SetWindowRgn` 根本不会调到），并在末尾复核**桌面窗口的 region
  签名（类型 + 外接框）前后一致**。
- **`test_push_hardening.py` 补 2.7 段（13 → 28 条用例）** —— `/event` 侧的
  离线版断言：15 条**合法 JSON** 的畸形输入一个都不该产生
  `parse failed`（断言日志行数增量 == 0），且顶层非对象要留痕、坏 `seq`
  要走"迟到回执"分支而非崩。
- **三个新回归都实测过"有判断力"**（同一批输入跑 HEAD `324ee85` vs 工作区）：

  | 回归 | 修复前 | 修复后 |
  |---|---|---|
  | `/event` 畸形输入（15 条） | `parse failed` = **10**，无忽略留痕 | **0**，有忽略留痕 |
  | `HEAD /common.py` | **200** `CL=48284` | **404** |
  | `GET /vendor/%00` | **`RemoteDisconnected`**（+ traceback） | **403** |
  | region 早退 N=200 / N=2000 | 增量 **200 / 2000**（1.00000/次） | **5 / 0** |

- 全量离线回归 **17 项全 rc=0**（原 14 项 + 新增 3 项；`test_push_hardening`
  是扩充而非新增）。`py_compile` 三个改动文件通过。
- **生产侧零污染复核**：`desktop_pet/.airi-pet.log` 仍是 2944 字节 /
  Oct 12 09:18（未被测试写入），`~/.dsh/pets/` 快照未变。

### Notes

- 用户侧待验证（本轮只有代码级证据，未在真机窗口上跑）：
  重启 VS Code + 重开桌宠后，`.airi-pet.log` 里应能看到
  `cubism core ... bytes` 而不是崩在开窗前；「编辑语料」的三级回落在
  扩展未运行时走 `code` 命令那级。

## [0.3.30] — 2026-10-13

> 审计「编辑语料」（0.3.29）时逮到两个问题，都在那条**三级回落**的通路上。
> 两个都只在"扩展没在跑"这一侧才露头，所以 0.3.29 的测试全绿也没拦住。

### Fixed

- **`code` 命令回落拼串漏引号 → 静默失败 + 谎报成功 + 第三级成死代码。**
  `_launch_code_cli()` 把命令行拼成 `'%s "%s"' % (exe, path)`：只给参数加了
  引号，**exe 裸拼**。而本机 `shutil.which('code')` 返回的是
  `S:\Microsoft VS Code\bin\code.CMD` —— **含空格**。交给 `cmd.exe` 后它拿
  `S:\Microsoft` 当命令名 → 命令根本找不到；`Popen` 自己不抛错
  （stderr 早已丢进 `DEVNULL`），于是连锁两件事：
  1. 日志写 `opened via code CLI`、气泡说「台词库打开了」—— **在说谎**；
  2. 因为返回了非 `None`，第三级 `os.startfile` **永远轮不到** ——
     三级回落静默退化成两级。
  修法：改用 `subprocess.list2cmdline([exe, path])`（它会连参数内部的引号
  一起处理）。实测含空格路径下替身**真的被执行**；旧写法则不会。
- **等待槽"先发布、后登记"，窗口期内回执会被丢掉 → 同一个文件被打开两次。**
  `/settings {openCorpus}` 旧写法是**先落盘**（= 把请求发布给扩展）**再**由
  后台线程登记等待槽。扩展的 `fs.watchFile` 是 500ms 轮询，可能下一拍就回执；
  回执先到 → `_corpus_open_ack` 找不到槽 → 被当成「迟到回执」丢掉 →
  worker 白等 2.5s 后走 `code` 回落 → VS Code 里**同一个文件开两个标签页**。
  修法：等待槽在落盘**之前**登记（登记必须早于发布）；新增 `_corpus_release()`
  只摘"还是我这一个"的槽；worker 的提前返回路径（语料文件没生成出来）也
  顺手摘槽，否则它会一直挂在 `_corpus_open_wait` 里。

### Tests

- `test_pet_corpus.py` 补 **13 条断言**（92 → 105）：
  - 拼串形状：exe 与参数各自带引号、仍是 `shell=True`；
  - **行为级**：含空格目录下的 `.cmd` 替身真的被执行（旧写法在这里静默失败）
    —— 已单独验证这条断言**有判断力**（旧失败 / 新成功）；
  - `code` 找不到时落到第 3 级 `os.startfile`，且拿到的是语料路径；
  - `/settings` 一返回，等待槽就已登记（把超时放大到 5s 让窗口可观测）；
  - 三条静态不变量把「登记早于落盘」「用 `list2cmdline`」「提前返回也摘槽」
    钉在源码上，改回去就红。
- 全量回归 14 项 rc=0；`tsc --noEmit` / `eslint` / `compileall` 全绿；
  跑完真实日志与 `~/.dsh/pets/` 零改动。

## [0.3.29] — 2026-10-12

> 新功能：**右键菜单可手动编辑桌宠的语料（台词库）**。默认走内置台词，
> 想改哪句改哪句 —— 改完保存即生效，写坏了自动回落内置。

### Added

- **右键桌宠 →「编辑语料（台词库）」**：打开 `~/.dsh/pets/airi-pet-corpus.jsonc`，
  里面是**全部内置台词**（12 个场景 × 92 条诊断台词 + 3 个点击分区 × 21 条
  点击台词），带中文说明与每个场景的可用占位符清单。
- **JSONC 格式**（VS Code 原生支持注释 + 尾随逗号）：语料是给人手改的，
  注释能显著降低改坏的概率。Python 侧自带一个状态机剥注释
  （`corpus.strip_jsonc`），**不用正则** —— 台词里出现 `//`（比如
  `"// TODO 还没写"`）时正则会把字符串内部当注释吃掉。
- **热重载**：读取时按 `(mtime_ns, size)` 做缓存键，用户改完保存立刻重读，
  最多一个响应周期后生效；不用重启桌宠，更不用重装扩展。
- **首次启动自动生成**语料文件（存在则不动）。用户把文件删了想"恢复出厂"，
  下次启动自动长回来。
- **`POST /settings {openCorpus: true}`** + **`/event {type:'corpusOpened'}` 回执**：
  「编辑语料」的三级回落打开通道（见下）。
- **语料来源写进启动日志**：`[airi-corpus] file=... exists=... scenes(user)=N
  clicks(user)=N dropped=N error=...` —— "我改了怎么没生效"就看这一行。

### 设计要点

- **单一真值**：`standalone.py` 里原来的 `CLICK_LINES` 字典（21 条点击台词）
  整块搬进了 `corpus.py`。点击台词和诊断台词现在**只有一份数据**，都归
  右键菜单能改的那个文件管。`standalone.CLICK_LINES` 保留成指向
  `corpus.CLICK_LINES` 的只读别名，纯粹是为了让探针还能做结构检查。
- **打开方式三级回落**：① VS Code 扩展（复用 0.3.28 建好的"设置文件 + `fs.watchFile`"
  通道：桌宠把 `openCorpusSeq` 加一写进去，扩展看到序号变大就 `openTextDocument`
  打开 `.jsonc`）→ ② `code` 命令 → ③ `os.startfile` 系统默认程序。
  第 ① 级成功后扩展会 **POST `/event` 回执**，桌宠收到就不再往下回落 ——
  不回执的话，VS Code 已经打开了，`code` 命令还会再拉起一个窗口。
  等待回执有 2.5 秒超时，超时才落到第 ② 级。
- **回执只"唤醒"、不摘槽**：桌宠的 `_corpus_open_worker` 被唤醒后还要读
  `ok`/`method` 才能决定要不要回落，所以摘槽必须由它自己在 `finally` 里做。
- **扩展只在"序号严格变大"时才打开**，且启动时把文件里的当前值记为"已处理"：
  否则"上次点的时候扩展没开、由回落打开过一次"的请求，会在重启 VS Code 后
  被当成新请求再打开一遍。
- **前台不冒泡、后台冒一条**：页面点了菜单**不自己说话**（那是第二条气泡，
  超过 `MAX_BUBBLES=2` 的舒适区），真正打开成功/失败由 Python 推**唯一一条**
  结果气泡 —— 它才知道三级回落最后落在哪一级。

### 安全回落（本功能最重要的部分）

语料从"内置常量"变成"用户随时可能写坏的文件"，所以校验放在入口，
每一条都能单独降级、互不牵连：

| 情况 | 行为 |
|---|---|
| 文件不存在 / 不是 UTF-8 / JSON 语法错 | 全部用内置（且不算错误，不刷日志） |
| 某场景不是数组 / 是空数组 | **只有那个场景**回落内置，其它场景照用用户的 |
| 元素不是字符串 / 全是空白 | 丢弃该元素 |
| 台词里有该场景**用不到**的占位符 | 丢弃那一条（免得 `{line}` 原样进气泡） |
| 点击台词情绪词不在 `CLICK_EMOTIONS` 里 | 丢弃那一条 |
| 未知场景名 / 未知分区名 | 忽略（不改用户文件、也不报错） |
| 整个文件删掉 | 回落内置；下次点菜单重新生成 |

**占位符白名单取"所有调用路径的交集"而不是并集**：同一个场景可能被多条
路径调用、各自传入的变量键不一样（例如 `many_errors` 在 diagnostics 路径传
`{count, language}`，在 file_scan 路径传 `{file, count, language, line, lines}`）。
若用户写了含 `{file}` 的 `many_errors` 台词，走到 diagnostics 路径时
`{file}` 无处可换 → 原样进气泡。所以白名单只收**每条路径都满足的键**，
宁可少给。（这个坑在 0.3.18 / 0.3.20 以内置语料的形式出现过两次。）

### Changed

- `standalone.py`：`_pick_click_line()` 改走 `corpus.get_click_lines(zone)`
  （用户语料优先、回落内置）；`corpus` 模块单独 try-import 并有 `_CLICK_FALLBACK`
  兜底 —— 不能因为 `response_generator` 那条链出问题，连"点一下有反应"都没了。
- `response_generator.pick_corpus()` 不再直接引用模块级列表常量
  （`from corpus import FILE_SCAN` 绑的是**当初那个 list 对象**，热重载后
  用户改完这边还指着旧的，表现成"我改了怎么没生效"）→ 每次调 `corpus.get_lines()`。

### Tests

- 新增 `live2d_probe/test_pet_corpus.py`：**92 条断言全绿**，覆盖
  JSONC 状态机（含字符串里的 `//`、尾随逗号、BOM、转义引号）、导出结构与幂等、
  上表每一行回落、热重载、两处接线、`/settings` 与 `/event` 端到端回执、
  **三级回落（回落函数换成桩，绝不真去拉起 VS Code）**、
  **等待槽不泄漏**、内置语料自洽（占位符 ⊆ 白名单 / 情绪词前端认识）、
  三端接线静态断言。
- 测试把 `AIRI_PET_CORPUS` / `AIRI_PET_SETTINGS` / `AIRI_PET_LOG` 全隔离到
  `%TEMP%`，并断言用户真实文件**一个字节都没动**（含"真实设置文件里没有多出
  `openCorpusSeq`"）。跑完整轮回归后真实日志仍是 2944 字节。

## [0.3.28] — 2026-10-12

> 新功能：**右键菜单可手动设置桌宠的检测周期（1~60 秒，默认 30）**。
> 这是本项目的第一个"用户设置项" —— 之前所有开关都是环境变量，改完要重启桌宠。

### Added

- **右键桌宠 →「检测周期 ▸」**：二级面板里给预设（1 / 2 / 5 / 10 / 15 / 30 / 60 秒）
  和 `−`/`+` 逐秒微调（1~60 全区间可达，不只是预设值），改完立即生效并持久化。
  周期越短越跟手、越长越安静；1 秒 ≈ 边写边说。
- **`GET/POST /settings`**（`standalone.py`）：读/写桌宠设置。POST 只认
  `detectIntervalSec` 一个键，值一律夹到 `[1, 60]`，`saved` 字段回报是否成功落盘。

### 设计要点（这个功能真正难的地方）

- **旋钮在扩展、菜单在桌宠，中间原本没有反向通道。** 要被改的是扩展侧的
  桌宠响应时钟（`petTick`，原本写死 `PET_TICK_MS = 30000`，`scanActiveFile` 与
  诊断推送共用它）；而右键菜单在桌宠页面里 —— 现状只有扩展往桌宠 POST
  `/push` 这一条单向路。
- **通道选了"文件"，不是"扩展轮询 HTTP"**：桌宠把值写进
  `~/.dsh/pets/airi-pet-settings.json`，扩展 `fs.watchFile(interval: 500)` 监听。
  理由：① 只有 Python 一个写入方 → 不存在两边互相覆盖；② 天然持久化
  （重启 VS Code / 重开桌宠都在）；③ 不用为了"改完立刻生效"去每秒轮询一次
  本机 HTTP。落盘用**临时文件 + `os.replace` 原子替换** —— 直写会留给
  扩展一个读到半截 JSON 的窗口。目录沿用 `~/.dsh/pets/`（Live2D core 缓存
  也在这儿），不是为这个功能新发明的路径。
- **二级菜单必须复用同一个 `#contextMenu` 面板**（`hidden` 切视图），不能另起
  一个元素：`collectUiRects()` 只按这个 id 把菜单并进窗口 region，新面板不在
  region 里 → 会被裁成"看不见的菜单"（与 0.3.25 那条"相对路径打到资产服务器
  上 404"同属"看不见的故障"）。测试里有一条断言把这个不变量钉住了。
- **`SETTINGS_URL` 必须是绝对地址**（主服务器端口）。页面是从资产服务器的
  动态端口加载的，相对路径 `/settings` 会打到资产服务器 → 404。就是 0.3.25
  那条 `[airi-sil]` 全库 0 条的同一个坑，所以直接照 `EVENT_URL` 的写法来。
- **点击事件要掐断冒泡**：`document` 上的 click 监听是"点哪都收菜单"，
  步进/预设/切换视图必须 `stopPropagation()`，否则连点 `±` 会变成"点一下关一次"。
- **两端都夹值**：设置文件是能被手改的，扩展不能假设它合法；范围常量
  （1 / 60）和文件名在 Python / TS / README 三处必须一致，有断言看着。

### Fixed

- **`clamp_detect_sec` 与前端取整不一致**：实现里用的是 `int(float(x))`
  （**截断**），而前端 `Math.round` 是四舍五入 —— 同一个 3.6 秒会被两端算成 3 和 4，
  正是"界面显示 4 秒、实际跑 3 秒"的种子。改成 `int(round(float(x)))` 对齐。
  （这个是本轮新写的代码，测试第一遍就把它抓出来了。）
- **`round(float('inf'))` 抛的是 `OverflowError`**：Python 的 `json` **默认接受**
  字面量 `Infinity`/`NaN`，所以 `{"detectIntervalSec": Infinity}` 是真能到达
  `/settings` 的。只兜 `(TypeError, ValueError)` 会让它 500 断连 ——
  与 0.3.17 / 0.3.25 那几轮"畸形 body 炸穿链路"同类。现在 ±Infinity / NaN /
  字符串垃圾一律算"不可用"→ 回落默认值 30。

### Tests

- 新增 `live2d_probe/test_pet_settings.py`（**67 条断言，全绿**）：
  - 静态不变量：模板标记全文件只出现一次、`load_html` 之后**无残留 `{{ }}` 标记**
    （多打一个错别字标记就永远替换不掉）、`SETTINGS_URL` 是绝对地址、
    **全文件只有一个 `class="context-menu"`**（region 不变量）、
    步进/预设/切视图都 `stopPropagation`、两端范围常量与文件名一致
  - `clamp_detect_sec` 17 组边界（含 `3.6`、`True`、`''`、`[]`、`{}`、`±inf`、`nan`）
  - `/settings` 真 socket：14 组 POST（越界夹值 / 缺键 400 / 数组 / 标量 /
    非 JSON / 空 body / **非 UTF-8 二进制** / `Infinity`）+ GET 形状 + 落盘核验
    + 不留 `.tmp`
  - 设置文件语义 11 组（子进程各自 import 一次）：缺失 / 损坏 / 数组 / 字符串 /
    越界 / 坏值 / 空对象 → 一律回落默认值，**坏文件不许让桌宠起不来**
  - **用户真实设置文件零改动**：测试在 `import standalone` **之前**把
    `AIRI_PET_SETTINGS` 指向 `%TEMP%`（和 `AIRI_PET_LOG` 同一个套路 ——
    这条路径比日志更要命，改掉的是用户自己设的值）
- 全量回归 13 项 rc=0（新增这项之外，`check_ui_js.py` 也确认新菜单 JS 能过 node 语法检查）。

## [0.3.27] — 2026-10-12

> 例行审计。本轮的核心收获**不是一个新 bug，而是把上一轮那条"根因未定"的悬案
> 结掉了 —— 它根本不是产品 bug，是我自己的测试打进来的噪声**，而上一轮的
> CHANGELOG 把它当成产品问题记了账。顺带修了一个测试产物写进产品目录的小毛病。

### Fixed

- **测试污染生产排障日志**（`desktop_pet/standalone.py` + 5 个测试）：
  `test_push_hardening.py` / `test_file_scan.py` / `test_push_e2e.py` /
  `test_sse_resume.py` / `test_response_generator_hardening.py` 都是
  `import standalone` 之后直接起**进程内**生产服务器的，于是**测试自己的请求流
  也会走生产代码的 `log()`**，落点就是开发者那份 `desktop_pet/.airi-pet.log`。
  其中 `test_push_hardening.py` 有一条**故意投毒**的非 UTF-8 `/event` 用例
  （`b'\xff\xfe\xfd not utf8 \xc3\x28'`）→ `json.loads` 抛 JSONDecodeError →
  `log('[airi-standalone] /event parse failed: ...')`。
  定量证据：
  - 跑前 `parse failed` 3 行 / 2720 字节 → 跑后 4 行 / 2832 字节，**+112 = 恰好一行**；
  - 逐个测量其余四个测试：全部 +0 字节 → 唯一污染源就是它；
  - 新增的 `test_pet_log_isolation.py` 把这条链路做成了可复现的断言（第 3 条的
    替身日志 `tmp_size=112` 也正好复现这个字节数）。
  危害不只是"日志脏"：`/event` 的 `except` 分支**不打印 body**（见 0.3.25 的
  加固），所以日志里无法分辨是谁发的 —— 一个假线索配上一条不能自证的日志，
  直接让上一轮把它当成"产品里来路不明的 parse 失败"记账。
  修法：`LOG_PATH = os.environ.get('AIRI_PET_LOG') or <默认路径>`，
  5 个测试在 `import standalone` **之前**把 `AIRI_PET_LOG` 指向 `%TEMP%`。
  **必须在 import 之前**：`LOG_PATH` 是模块加载时算出来的，而
  `test_sse_resume.py` 的路子（自建 `ThreadingHTTPServer(standalone.AiriHandler)`
  后立刻 `serve_forever()`）没有 ready 等待，import 之后再改会有竞态。
- **`test_push_e2e.py` 把结果写进产品目录**：文件头 docstring 说结果写
  `live2d_probe/_e2e_test.txt`，代码却写的是 `os.path.join(ROOT, '_e2e_test.txt')`
  而 `ROOT` = `desktop_pet/` → 实际落在 `desktop_pet/_e2e_test.txt`。
  两处后果：产品目录里多出一个测试产物；该路径既不在 `.gitignore`
  也不在 `.vscodeignore` 里（`live2d_probe/.gitignore` 的 `_*` 规则罩不到它），
  会挂进 `git status`、还可能被打进 VSIX。**是代码错、docstring 对**，
  改代码写进 `live2d_probe/`；顺带删掉已生成的 `desktop_pet/_e2e_test.txt`。

### Changed

- 5 个测试的 `sys.path.insert(r'S:\My event\...')` 硬编码路径改为按
  `os.path.dirname(os.path.abspath(__file__))` 推算 —— 仓库换目录/别人克隆后
  不再指着一个不存在的路径；输出文件名同步走 `_HERE`。
- `.vscodeignore` 补 `desktop_pet/_probe_*.html`：上一轮只补了 `_clamp_test.txt`，
  而 `_probe_*.html` 是 `gen_ui_probe.py` 给用户用的诊断产物，同样不该进 VSIX
  （vsce 只认 `.vscodeignore`，不看 `.gitignore`，这是 0.3.26 记下过的坑）。
- `desktop_pet/assets/README.md` 标题 `# Airi 角色立绘目录` → `deepseek`
  （0.3.23/0.3.24 改名审计的又一处用户可见面残留）。同文件的
  `AIRI_CHARACTER_IMAGE` 是环境变量名，属有意保留的内部标识符，不动。

### Tests

- 新增 `live2d_probe/test_pet_log_isolation.py`（4 条断言，全绿）：
  ① 不设 `AIRI_PET_LOG` 时 `LOG_PATH` 仍指向 `desktop_pet/.airi-pet.log`（默认行为不变）；
  ② 设了之后 `LOG_PATH` 跟着走；③ 隔离下投毒 `/event`：替身日志**收到**
  `parse failed` 行（证明"没写进真实日志"不是因为压根没触发），真实日志纹丝不动；
  ④ 真跑一遍 `test_push_hardening.py`：真实日志字节数与 `parse failed` 计数都不变
  —— 这条是真正的回归闸门，谁把测试里那行 `AIRI_PET_LOG` 删了它就红。
- 旧文件 `desktop_pet/.airi-pet.log` 里遗留的 5 条假 `parse failed` **未手工清理**：
  该文件每次启动由 `_init_log()` 截断重写，且已 gitignore、不进 VSIX，
  留着它正好是这条证据链的现场（下一次手动启动桌宠即归零）。

### 本轮排除（避免误诊，都查过且判为不改）

- `watcher.py` 的 `watch_single_file` payload 缺 `'language'` 键：走
  `standalone` 的 `trigger`/`error_count` 直通分支 → `generate_response` 里
  `context.get('language', 'python')` 有默认值 → `{language}` 语料不会残留占位符，
  **无用户可见后果**（`directory` 分支的写法不统一，属洁癖，非 bug）。
- `python_backend/main.py` 的 `item.get("languageId")` 在 item 非 dict 时会
  AttributeError：被 `except Exception: pass` 吞掉，且该模块**是死代码**
  （`src/extension.ts` 只 spawn `standalone.py`）。
- `common.get_dpi_scale()` 把 scale < 1.0 抬到 1.0：有意为之（低于 100% 不缩放）。
- `package.json` 没写 `extensionKind`：无法证实有实际影响，不改（不认没有证据的猜测）。
- `README.md` 那行 `| \`Airi\` | 33×15.6 | 3 | 39×21.6 | **40×20** ✓ |`：这是当年
  用户截图里**名牌文字**的实测对账行。名牌文案 0.3.22 已改成 `deepseek`
  （比 `Airi` 长），但重测需要真开 GUI —— 本轮**不编数字**，留作待办。

## [0.3.26] — 2026-10-11

> 例行审计：一个真 bug（`'2d'` 回退路径的轮廓/点击上下镜像）+ 一处改名漏网
> + 两个诊断工具自己坏掉。**alpha 行序这条只有代码级证据、没能实机复现**，
> 原因见下（不是没查，是这台机器的运行路径根本走不到）。

### Fixed

- **`'2d'` alpha 回退路径整体上下镜像**（`desktop_pet/ui.html` `readAlphaGrid`）：
  `getImageData` 的行 0 是图片**最上边**，`readPixels` 的行 0 是画布**最下边**；
  下游 `scanAlpha()` / `alphaAt()` 只认一种约定（行 0 = 底边 —— `alphaAt` 自己的
  注释就写着"WebGL 的 y 轴朝上，DOM 的朝下，这里要翻"），可这两条读取路径被
  当成行序相同。后果（只在 `'2d'` 回退路径上，即 gl 读数不可信时）：
  `characterRects().rects` 整体镜像 → `pushSilhouette` 把倒过来的轮廓交给
  `SetWindowRgn`，窗口形状上下颠倒；`alphaAt()` 的 `gy` 同样镜像 → 点头顶命中
  桌面、点空白处反而命中角色。
  新增 `rowsBottomUp(data, w, h)` 把行序归一，`'2d'` 分支统一走它；`'gl'` 分支
  显式注明**不需要**翻。
  **为什么一直没暴露**：这台机器走旧绘图路径，`character.png` 是不透明 PNG
  → coverage≈1.0 → `silAlphaMode='off'` → `pushSilhouette` 直接 return，
  镜像不产生可见症状；而无头浏览器在本工具沙箱里起不来（chrome/msedge 的
  `--headless=new`/`=old`/`Start-Process` 四种全 0 字节），所以**这条只有代码级
  证据链，没有实机复现**。回退路径存在的意义就是 gl 读数不可信的那一天，
  那时它是唯一的轮廓/命中的来源。
- **扩展显示名仍是 `airi-monitor`**（`package.json`）：0.3.23 的改名清单改了
  `contributes.configuration.title`、命令标题、`description`，漏了
  `displayName` —— 而它就是 VS Code 扩展列表里显示的那个名字。
  内部标识符（`airiMonitor.*`、viewId `airiAssistant`、`[airi-*]` 日志前缀）
  仍有意保留，改 ID 会破坏已存设置与键绑定。
- **`live2d_probe/jscheck.py` 的 node 路径写死**：硬编码
  `...\node\versions\22.22.2-3\node.exe`，托管 node 换代到 `22.22.2-6` 之后
  该文件不存在 → `subprocess` `FileNotFoundError` → rc=1，而且是**静默**
  失效。而这个工具正是"页面什么都不显示"时的第一诊断手段 —— 它坏了，
  排查入口就焊死了。抽出 `live2d_probe/node_finder.py`（扫 `versions/*`
  目录倒序 + 系统 node + `shutil.which('node')`），两个工具共用。
- **`live2d_probe/check_ui_js.py` 探针缺失即崩**：上来就
  `io.open(desktop_pet/_probe_a.html)`，而该文件是 `gen_ui_probe.py` 的产物、
  被 gitignore —— 新克隆的仓库里必然不存在，于是这个工具对任何人都是
  rc=1（`FileNotFoundError`）。改为缺失时自动调 `gen_ui_probe.py` 生成，
  仍失败给出明确指引；顺带补占位符残留检查（那种情况下 `--check` 的报错
  是假阳性，不是真语法错）。
- `test_window_clamp.py` 的 docstring 说结果写 `live2d_probe/`，实际 `ROOT`
  指向 `desktop_pet/` —— 改成真实路径。

### Tests

- 新增 `test_alpha_orientation.py`：从**真实 ui.html** 正则抽出
  `rowsBottomUp()` 源码交给 node 跑行为断言（行序是否真翻、长度是否守恒），
  另加 4 条静态断言（`'2d'` 分支确实调了、两个消费方仍是"行 0 = 底边"、
  `'gl'` 分支没被误改）。全绿，以后改这段逻辑会自动兜住方向性回归。
- `live2d_probe/.gitignore` 的 `_*.txt` 放宽到 `_*`：`_ui_js.js`、
  `_jscheck_out.txt`、临时验证脚本一直挂在 `git status` 里，早晚有一次被
  顺手提交；根 `.gitignore` 补 `desktop_pet/_probe_*.html`、
  `desktop_pet/_clamp_test.txt`。
- `jscheck.py` rc=0；`check_ui_js.py` rc=0（3 段 script 全 OK，且走的是
  "探针不存在 → 自动生成"那条新路径）。

### 待验证（诚实记账）

- **`[airi-sil]` 在日志里仍是 0 条** —— 0.3.25 修的 `reportSil` 相对路径
  （改 `EVENT_URL`）装进 VSIX 后只跑过一次，还没在那个场景下重启验证过，
  所以**不算修好，只算改完**。重启 VS Code 后应能在
  `desktop_pet/.airi-pet.log` 里首次看到 `[airi-sil]`。
- `.airi-pet.log` 里两条 `[airi-standalone] /event parse failed:
  JSONDecodeError('Expecting value: line 1 column 1 (char 0)')`（10-10、10-11
  各一条）**根因未定**：扩展侧发的是合法 JSON，`print()` 又不含 body，
  无法从日志分辨是谁发的。本轮不改，先记账。
  → **0.3.27 更正：这条是假线索，把它当"产品的悬案"记账本身就是被它带偏了。**
  它不是产品发出来的，是 `live2d_probe/test_push_hardening.py` 跑测试时打进去的
  —— 该测试有一条**故意投毒**的非 UTF-8 `/event` 用例，而测试是 `import standalone`
  后直接起**进程内**服务器，`log()` 的落点就是这份真实日志。见 0.3.27。
  教训：这条日志头部写着"每次启动重写"，我据此假设"里面只可能是产品自己写的"，
  但测试进程从来不调 `_init_log()`，所以它只追加、不重写 —— 假设错了。

## [0.3.25] — 2026-10-10

> 例行审计：三个可复现的断连/失明 bug + 一处状态吞标记。

### Fixed

- **轮廓诊断通道整体失明**（`desktop_pet/ui.html` `reportSil`）：
  上报用的是相对路径 `fetch('/event')`，而页面是从**资产服务器**（动态端口）
  加载的 —— 相对路径打到资产服务器上，它只认 `/`、`/vendor/`、`/pet-assets/`，
  其余一律 404（`common.py` 的 `_AssetHandler.do_GET`）。
  后果：日志里 `[airi-sil]` **一条都没有**（实测 sil=0 / js=2），而且
  `silDiagSent` 的 12 条配额照常被烧掉 —— 后续真故障也不再上报。
  同文件的 `reportJS` 用的是绝对地址 `EVENT_URL`，所以它一直好使。
  改用 `EVENT_URL`。
- **`/push` 的 payload 不是 dict 时断连**（`desktop_pet/standalone.py`）：
  `'text' in msg.get('payload', {})` 在 payload 为 `null` / 数字 / 布尔时抛
  TypeError（不可迭代）；payload 为 `["text"]` 时**返回 True**，紧接着
  `msg['payload']['text']` 又抛 TypeError。两条路都是未捕获异常 → 500 断连。
  0.3.19 只加固了 body 层（body 为数组/标量/null），payload 层漏了。
  改为先 `isinstance(p, dict)` 判定。
- **`/event` 的 body 非 UTF-8 时断连**（`desktop_pet/standalone.py`）：
  `decode('utf-8')` 没带 `errors=`，且位置在 `try` 之外 —— 非 UTF-8 body
  抛 UnicodeDecodeError，连 `[airi-standalone] /event parse failed` 都写不进
  日志，请求直接断掉。与 `/push` 用同一条防线：`errors='replace'`。
- **当前文件扫描在桌宠不可达时吞掉播报**（`src/extension.ts` `scanActiveFile`）：
  推送是异步的（先 `checkStandaloneAlive()` 再 POST），但 `fileScanState`
  在推之前就已经推进到新状态 —— 桌宠没起来时这条播报被静默丢弃，之后
  要么等满一个 `repeatIntervalSec` 冷却、要么永远收不到（`count=0` 的
  "干净了"走不到 remind 分支）。与 0.3.20 修的"诊断脏标记不可吞"同源：
  检测到没送出去时**回滚整条决策状态**（含 signature），下一拍重判。

### Tests

- `test_push_hardening.py` 16 → 24 用例：新增 payload 级畸形 6 条
  （null / 数字 / 布尔 / `["text"]` / diagnostics 的 null 与数字）+ 
  `/event` 非 UTF-8 body 1 条。修复前这 7 条全部断连（已复现）。

## [0.3.24] — 2026-10-10

> 审计修正：0.3.23 改名波及面的两处漏网（用户可见 "Airi" 残留）。

### Fixed

- **右键菜单与离线气泡**（`desktop_pet/ui.html`）：「退出 Airi」→
  「退出 deepseek」；后端退出时的气泡「Airi 已离线…」→「deepseek 已离线…」。
  这两处是用户在桌宠上直接看得到的文案，上一轮改名时漏掉了。
- **根目录 `start.bat`**：echo 文案 ×3 与控制台窗口名 `start "Airi Pet" …`
  → deepseek。0.3.23 只改了 `desktop_pet/launch*.bat`，根目录一键启动脚本
  被遗漏。改后过 `check_launchers.py` 纯 ASCII 校验。
- **`--help` 描述**：`desktop_pet/main.py`（'deepseek Desktop Pet'）与
  `desktop_pet/watcher.py`（'deepseek file watcher'）的 argparse description。

### Notes

- 其余 "Airi" 命中均为代码注释 / 文档 / 版本历史 / 内部标识符
  （`airiMonitor.*`、`AiriHandler`、`AiriAsset/1.0`、`AIRI_PYTHON_PATH` 等），
  按既有约定有意保留——改内部 ID 会破坏已存设置与键绑定。

## [0.3.23] — 2026-10-09

> 品牌改名收尾：人设与全部用户可见文案 Airi → deepseek。

### Changed
- **人设（character.py）**：`CHARACTER_NAME` → "deepseek"，SYSTEM_PROMPT
  里 AI 自称改为 deepseek（走 DeepSeek API 生成台词时不再自称 Airi）；
  启动 statusChange 随之变为 "deepseek 已就绪"
- **扩展 UI（extension.ts / package.json）**：状态栏 "Airi 桌宠: 运行中/
  未运行"、就绪通知、Webview 面板标题 "Airi Assistant / Airi Monitor"、
  命令面板 "Launch deepseek Desktop Pet"、设置节标题 "deepseek Monitor"、
  扩展描述——全部改为 deepseek
- **桌宠本体（standalone.py / main.py / launch*.bat）**：窗口标题
  `title='Airi'` → `'deepseek'`、启动日志头、bat 的 echo/控制台窗口名
- **README**：面向用户的操作说明同步（命令名、状态栏按钮、右键退出、
  目录注释）；版本历史与旧测量数据保持原样

### 不改的部分（有意保留）
- 内部标识符：命令/配置 ID `airiMonitor.*`、viewId `airiAssistant`、
  日志前缀 `[airi-*]`、`AiriHandler`、`AiriAsset/1.0`、`AIRI_PYTHON_PATH`
  —— 改 ID 会使已保存的设置与键绑定失效，前缀只出现在日志里

## [0.3.22] — 2026-10-08

### Changed
- 问候语料里的"Airi 已上线"改为"**deepseek 已上线**"，与 UI 名牌的
  deepseek 品牌命名保持一致（语料中这是唯一一处出现 Airi 的台词）

## [0.3.21] — 2026-10-08

> 语料扩容轮：12 个场景 + 3 个点击分区全部加量，并为语料库上一道
> 占位符回归保险。

### Added
- **场景语料 +35 条**：syntax/type/import/name/many_errors/all_clear/
  greeting/idle/encourage/file_scan/file_remind/file_clear 共 12 个场景
  各加 3 条（多数场景），总数 57 → 92。新句全部只用该场景实际会传入的
  占位符键（`{file}/{count}/{line}/{language}`），避免缺键残留
- **点击台词 +9 条**：head/body/desk 三分区各 4 → 7 条，情绪词仍限定在
  EMOTION_EXPRESSION 认识的五个（idle/greeting/angry/happy/surprised）
- **占位符全量回归**（test_response_generator_hardening.py）：
  * 静态扫描：12 个场景 92 条语料的占位符必须落在该场景的合法键集合内
  * 动态抽样：13 种实际 (场景, 变量) 组合各抽 120 次确认无 `{` 残留
  * 点击台词情绪词合法性校验
  以后往语料里加新句子，这三道会自动兜住

## [0.3.20] — 2026-10-07

> 审计轮：修 30 秒时钟的两处逻辑漏洞 + 名牌被角色卡遮挡。

### Fixed
- **一拍只说一条**：诊断推送与当前文件扫描同拍都想说话时，会连推两条
  消息（前端 MAX_BUBBLES=2 → 两个气泡同时冒出来），违背"每 30 秒主动
  响应一次"的约定。现在当前文件优先，诊断脏标记留到下一拍
- **诊断变化不再被静默吞掉**：桌宠未运行时脏标记也会被无条件清空，
  之后桌宠起来了也再不会补报。改成推送未消费（桌宠不可达）时保留标记
- **名牌被角色卡遮挡**：`.pet-plate` 是静态元素，会被 `position:fixed;
  z-index:0` 的不透明角色卡盖住；上一轮的负 margin 把名牌压进了卡的
  范围，上沿会被吃掉。补 `position:relative; z-index:1`
- **diagnostics 分支 `{line}` 占位符残留**：`variables` 漏了 `line`
  默认值，`error_count>0` 且 `sample_errors=[]` 时（categorize_errors
  恒返回 syntax_error）会把 `{line}` 原样说进气泡。与 0.3.18 修的
  file_scan 分支同源，当时只补了那半

## [0.3.19] — 2026-10-06

> 桌宠主动响应改为 30 秒一拍。此前诊断是实时的——每敲一个字母触发的
> 诊断变化都会立刻推给桌宠报错。

### Changed
- **30 秒响应时钟（petTick）**：诊断推送与当前文件扫描合并到同一个
  30s 周期时钟，桌宠每 30 秒最多主动响应一批：
  * 诊断变化只标脏（diagPushPending），tick 时才真正推送
  * file_scan 检测从 10s tick 并入同一时钟（变化播报最多延迟 30s）
  * 桌宠被唤醒（launchPet 成功）时重置计时起点
  * Webview 备用面板保持实时更新（那是用户主动打开的，不打扰）

## [0.3.18] — 2026-10-06

> 名牌改名 deepseek + 布局紧凑化；/push 加固第二层。

### Changed
- 名牌文字 `Airi` → `deepseek`（角色 alt 文本同步）
- 名牌上移约 12px 更贴角色：wrapper gap 归零、地面阴影上提、
  名牌自身负 margin（region 与角色卡均按 getBoundingClientRect 实测，
  上移自动跟随，无需改裁切逻辑）

### Fixed
- /push body 为合法 JSON 但非对象（数组/标量/null）时不再断连
- /push body 非 UTF-8 时不再断连（decode errors='replace'）
- file_scan 空 sample_errors 时 `{line}`/`{lines}` 占位符不再原样残留
- 无后端兜底台词区分 remind/changed，周期提醒不再复读首播

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

## [0.2.0] — 2026-07-19

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
