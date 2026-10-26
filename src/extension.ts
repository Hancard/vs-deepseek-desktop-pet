/*
 * ============================================================================
 * airi-monitor (vscode-anime-assistent) — VS Code 扩展入口文件 (v0.2.0 精简版)
 * ============================================================================
 *
 * 【职责】
 * 轻量诊断桥接器：监听 VS Code 的 C/C++/Python 诊断错误，
 * 将原始诊断数据转发到 Airi 独立桌宠服务器 (standalone.py)。
 *
 * 不再内置 Python 后端、HTTP 服务器或桌面宠物进程。
 * 这些功能由 standalone.py + watcher.py 独立提供。
 *
 * 【数据流】
 *   VS Code 诊断变化 → handleDiagnosticsChanged
 *     → 仅标记 diagPushPending（Webview 面板实时更新，桌宠不实时打扰）
 *     → 桌宠响应时钟（周期可配，默认 30s，唤醒时重置起点）→ pushPendingDiagnostics
 *       → POST http://127.0.0.1:19876/push
 *         → standalone 内建 generate_response → SSE → 桌宠气泡
 *
 *   当前文件 bug 监视（file scan watchdog）→ scanActiveFile
 *     → 与诊断推送共用同一个可配周期时钟（petTick）：
 *       bug 变化推送；未变化每 60s 提醒；修到 0 说一次"文件干净"后沉默
 *     → POST /push {trigger: 'file_scan', file, error_count, sample_errors}
 *
 * 【检测周期怎么改】
 *   右键桌宠 →「检测周期」→ 1~60 秒（默认 30）。桌宠把值写进
 *   ~/.dsh/pets/airi-pet-settings.json，这里 fs.watchFile 监听它并立刻换周期。
 *   通道为什么是文件、以及为什么只有 Python 一个写入方，见 PET_SETTINGS_PATH。
 *
 * 【编辑语料怎么开】
 *   右键桌宠 →「编辑语料」→ 桌宠把 openCorpusSeq 加一写进**同一个设置文件**，
 *   这里看到序号变大就 openTextDocument 打开 ~/.dsh/pets/airi-pet-corpus.jsonc，
 *   然后 POST /event {type:'corpusOpened'} 回执。桌宠在等这个回执决定要不要
 *   走第二级回落（`code` 命令）—— 不回执会让它在 VS Code 已经打开的情况下
 *   再拉起一个窗口。见 openCorpusIfRequested()。
 */

import * as vscode from 'vscode';
import * as http from 'http';
import { spawn } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

// ============================================================================
// 模块级状态
// ============================================================================

/** Webview 面板的单例引用（备用显示） */
let panel: vscode.WebviewPanel | undefined;

/** 扩展自身路径（用于定位 desktop_pet/standalone.py） */
let extensionRoot = '';

/**
 * 支持监控的语言 ID 集合
 */
// exported：src/test/extension.test.ts 会断言这个集合（以前那条用例是在
// 测试文件里另抄一份字面量自己断言自己，恒真、零覆盖）。
export const supportedLanguageIds = new Set(['c', 'cpp', 'python']);

/**
 * 独立桌宠服务器 (standalone.py) 端口（可用环境变量 AIRI_STANDALONE_PORT 覆盖）
 *
 * ⚠️ 必须做 0~65535 的范围校验：http.get 对越界端口是**同步抛**
 * `ERR_SOCKET_BAD_PORT`（实测 node：99999 / -1 / 70000 三个值都是同步抛，
 * 不是 error 事件）。而它发生在 Promise executor 里 → Promise reject →
 * launchStandalonePet 静默失败（抛点在它唯一那个 try 之外），用户点「启动
 * 桌宠」什么都不会发生、连一条报错都没有。
 */
const STANDALONE_PORT = (() => {
	const n = Number(process.env.AIRI_STANDALONE_PORT);
	return Number.isInteger(n) && n > 0 && n < 65536 ? n : 19876;
})();

/** 桌宠服务器健康状态缓存（避免每次诊断变化都探测） */
let standaloneHealthy = false;
let lastHealthCheckAt = 0;
const HEALTH_CHECK_INTERVAL_MS = 15000;

/** 状态栏按钮单例：常驻显示桌宠状态，点击拉起 standalone.py */
let petStatusBar: vscode.StatusBarItem | undefined;

/** 状态栏轮询间隔（刷新「运行中/未运行」标签） */
const STATUS_POLL_INTERVAL_MS = 10000;

// ============================================================================
// activate()
// ============================================================================

export function activate(context: vscode.ExtensionContext) {
	console.log('[airi-monitor] activate() started');
	extensionRoot = context.extensionPath;

	// --- 注册命令 ---
	const openAssistantCommand = vscode.commands.registerCommand('vscode-anime-assistent.openAssistant', () => {
		createOrShowAssistantPanel(context);
	});
	const launchPetCommand = vscode.commands.registerCommand('vscode-anime-assistent.launchPet', () => {
		void launchStandalonePet();
	});
	context.subscriptions.push(openAssistantCommand, launchPetCommand);

	// --- 诊断监听 ---
	// 当语言服务器 / linter 检测到错误时触发（始终按工作区整体统计）
	const diagnosticsDisposable = vscode.languages.onDidChangeDiagnostics(() => {
		handleDiagnosticsChanged();
	});
	context.subscriptions.push(diagnosticsDisposable);

	// --- 桌宠响应时钟（周期由桌宠侧设置文件决定，默认 30s）---
	// 诊断变化与当前文件扫描共用这一个节奏：每 N 秒最多主动说一条。
	//   * 诊断变化只标脏（diagPushPending），tick 时才真正推送
	//     —— 敲代码时每个字母触发的诊断事件不再实时打扰桌宠
	//   * scanActiveFile 在同一 tick 里跑：bug 变化推送、未变化按冷却提醒、
	//     修到 0 说一次"干净了"后沉默
	//   * 两条路径同拍都想说话时当前文件优先（诊断脏标记留到下一拍，
	//     避免一拍冒两个气泡）
	// 桌宠被唤醒（launchStandalonePet 成功）时重置计时起点。
	petTickMs = readDetectIntervalMs() ?? PET_TICK_DEFAULT_MS;
	startPetTick();
	context.subscriptions.push(new vscode.Disposable(() => {
		if (petTickTimer) { clearInterval(petTickTimer); petTickTimer = undefined; }
	}));

	// --- 监听桌宠的「检测周期」设置 ---
	// 右键桌宠 →「检测周期」改的就是它：桌宠把值写进 PET_SETTINGS_PATH，
	// 这里 watch 到变化立刻换周期（并重置计时起点），不用重启任何东西。
	// watchFile 对"文件还不存在"的情况同样有效（创建时也会触发）。
	// 「编辑语料」走的是**同一个文件**：桌宠把 openCorpusSeq 加一，
	// 这里看到序号变大就打开语料文件并回执。
	// 先把当前序号记为"已处理"：上一次会话里点过的请求不该在重启后被重放。
	// ⚠️ 必须过滤 Infinity（0.3.43）：设置文件里写 `"openCorpusSeq": 1e999`
	// 是**合法 JSON**，JSON.parse 得 Infinity，而 `Infinity || 0` 仍是
	// Infinity —— 此后 openCorpusIfRequested 的 `seq <= lastCorpusOpenSeq`
	// 恒真，整个会话里「编辑语料」**永远不会**走 VS Code 第一级（每次都掉到
	// code 命令 / 系统默认程序，而 code 在已打开时会再拉一个窗口，正是三级
	// 回落想避免的观感）。下面那条守卫用的是 Number.isFinite，两边口径必须
	// 一致；Python 侧 _as_seq 连 OverflowError 都兜了，说明这种输入形态在
	// 本项目里本来就是认作要防的。
	const initialCorpusSeq = Number(readCorpusOpenSeq());
	lastCorpusOpenSeq = Number.isFinite(initialCorpusSeq) && initialCorpusSeq > 0
		? initialCorpusSeq : 0;
	// 启动时的签名也算"已处理"：上一次会话里点过的请求不该在重启后被重放。
	lastCorpusOpenSig = corpusOpenSig(lastCorpusOpenSeq);
	const onPetSettingsChanged = (): void => {
		syncPetTickFromSettings('settings file');
		void openCorpusIfRequested();
	};
	fs.watchFile(PET_SETTINGS_PATH, { interval: PET_SETTINGS_POLL_MS }, onPetSettingsChanged);
	context.subscriptions.push(new vscode.Disposable(
		() => fs.unwatchFile(PET_SETTINGS_PATH, onPetSettingsChanged)));

	// --- 启动时扫描已有诊断（有错误才处理，干净启动不打扰） ---
	// Webview 诊断面板改为按需打开：命令 "Open Anime Assistant"
	if (collectErrors().length > 0) { handleDiagnosticsChanged(); }

	// --- 状态栏按钮：常驻显示桌宠状态，点击拉起 standalone.py ---
	petStatusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
	petStatusBar.command = 'vscode-anime-assistent.launchPet';
	petStatusBar.show();
	context.subscriptions.push(petStatusBar);
	void refreshPetStatus();
	const statusTimer = setInterval(() => { void refreshPetStatus(); }, STATUS_POLL_INTERVAL_MS);
	context.subscriptions.push(new vscode.Disposable(() => clearInterval(statusTimer)));

	vscode.window.showInformationMessage('deepseek Monitor 已就绪 — 诊断消息将转发到桌宠', '启动桌宠').then((selection) => {
		if (selection === '启动桌宠') { void vscode.commands.executeCommand('vscode-anime-assistent.launchPet'); }
	});
	console.log(`[airi-monitor] Ready. Push target: http://127.0.0.1:${STANDALONE_PORT}/push`
		+ ` , detect interval: ${petTickMs / 1000}s`);
}

// ============================================================================
// deactivate()
// ============================================================================

export function deactivate() {
	panel = undefined;
}

// ============================================================================
// handleDiagnosticsChanged()
// ============================================================================

/** 上次推送到桌宠的内容签名（相同内容不重复推送） */
let lastPushSignature = '';

/** 工作区是否出现过受支持的错误（从未出过错时不推 all_clear） */
let hadRelevantErrors = false;

interface ErrorItem {
	file: string;
	languageId: string;
	message: string;
	source: string;
	line: number;
	character: number;
}

/** 从文件路径推断语言 ID（用于未在编辑器中打开、拿不到 languageId 的文件） */
function inferLanguageFromPath(fsPath: string): string {
	const ext = path.extname(fsPath).toLowerCase();
	if (ext === '.py' || ext === '.pyi') { return 'python'; }
	if (ext === '.c') { return 'c'; }
	if (['.cpp', '.cc', '.cxx', '.c++', '.h', '.hpp', '.hh', '.hxx'].includes(ext)) { return 'cpp'; }
	return '';
}

/** 收集诊断错误。传入 uris 时只收集这些文件，否则收集整个工作区。
 * 注意：语言服务器对已关闭文件的诊断仍保留在 getDiagnostics() 里，
 * 不能因为文档未打开就跳过，否则关掉报错文件会被误判为「全部修好」。 */
function collectErrors(uris?: readonly vscode.Uri[]): ErrorItem[] {
	const targets = uris ? new Set(uris.map((u) => u.toString())) : undefined;
	const items: ErrorItem[] = [];
	for (const [uri, diagnostics] of vscode.languages.getDiagnostics()) {
		if (targets && !targets.has(uri.toString())) { continue; }
		const document = vscode.workspace.textDocuments.find((doc) => doc.uri.toString() === uri.toString());
		const languageId = document?.languageId ?? inferLanguageFromPath(uri.fsPath);
		if (!languageId || !supportedLanguageIds.has(languageId)) { continue; }
		for (const d of diagnostics) {
			if (d.severity !== vscode.DiagnosticSeverity.Error) { continue; }
			items.push({
				file: uri.fsPath,
				languageId,
				message: d.message,
				source: d.source ?? 'unknown',
				line: d.range.start.line + 1,
				character: d.range.start.character + 1,
			});
		}
	}
	return items;
}

// ============================================================================
// scanActiveFile() — 当前文件 bug 监视（file scan watchdog）
// ============================================================================

/** 桌宠主动响应周期默认值：30 秒一拍（用户指定）。
 *  ⚠️ 现在这只是**默认值** —— 实际周期由桌宠侧的设置文件决定
 *  （右键菜单「检测周期」可改，范围 1~60 秒），见 PET_SETTINGS_PATH。
 * 诊断变化只标脏，真正的推送在这一拍里做——敲代码时的每次诊断事件
 * 都不再实时打扰；"以桌宠被唤醒开始计时"由 launchStandalonePet 成功后
 * 调 startPetTick() 重置起点实现。 */
const PET_TICK_DEFAULT_MS = 30000;

/** 「检测周期」的合法范围（秒），与 standalone.py 的 DETECT_MIN/MAX_SEC 对齐。
 *  两边都夹一次是故意的：设置文件是可以被手改的，扩展不能假设它一定合法。 */
const DETECT_MIN_SEC = 1;
const DETECT_MAX_SEC = 60;

/** 字段存在但**不是数字**时的取值（0.3.45），与 standalone.py 的
 *  DETECT_DEFAULT_SEC 同值。为什么不是"保持当前值"：桌宠那侧
 *  `load_settings()` 对同一个坏值是回落 30 的，而 `GET /settings` 现在会先把
 *  磁盘读进内存（见 settings_payload）—— 菜单会显示 30，扩展却按上一拍的
 *  5 秒跑。两边给出同一个数比"谁更保守"重要。 */
const DETECT_DEFAULT_SEC = 30;

/** 桌宠的设置文件（**Python 侧是唯一写入方**，扩展只读）。
 *
 *  为什么走文件而不是让扩展轮询桌宠的 HTTP：
 *  菜单在桌宠页面里，跑的却是扩展的时钟 —— 两边之间原本没有任何
 *  「桌宠 → 扩展」的反向通道（扩展只在 /push 上一路往桌宠送）。用
 *  「Python 落盘 + 这里 fs.watchFile」当通道：单写入方不会互相覆盖、
 *  天然持久化、不产生每秒一次的轮询请求。
 *  路径必须与 standalone.py 的 SETTINGS_PATH 一致（~/.dsh/pets/，与
 *  Live2D core 缓存同目录）。 */
const PET_SETTINGS_PATH = path.join(os.homedir(), '.dsh', 'pets',
	'airi-pet-settings.json');

/** 设置文件轮询间隔：菜单点一下要"立刻"生效，默认的 5s 粒度太钝。 */
const PET_SETTINGS_POLL_MS = 500;

/** 桌宠的台词库（语料）文件 —— 右键菜单「编辑语料」打开的就是它。
 *  与设置文件同目录（~/.dsh/pets/），理由见 PET_SETTINGS_PATH：
 *  桌宠跑的是 VSIX 安装副本，语料若放在安装目录里，下次升级就被覆盖。
 *  扩展名用 .jsonc：VS Code 原生按"带注释的 JSON"处理它（语料文件里
 *  有中文注释和尾逗号，纯 .json 会被标一屏红色波浪线）。 */
const PET_CORPUS_PATH = path.join(os.homedir(), '.dsh', 'pets',
	'airi-pet-corpus.jsonc');

/** 已经处理过的「编辑语料」请求的签名（`序号@mtimeMs`）。
 *
 *  ⚠️ 为什么是签名而不是只比序号（0.3.45）：序号**不是**请求的唯一身份。
 *  设置文件支持用户整份覆盖（README 教的 `Set-Content '{"detectIntervalSec":5}'`
 *  会把 openCorpusSeq 一起抹掉），桌宠重启后又从 max(内存,磁盘)+1 起算，
 *  于是可能算出一个**本会话已经用过**的号 —— 只比序号的话这种请求既不打开
 *  也不回执，桌宠白等 2.5s 再回落 code 命令（在 VS Code 已经开着时再拉一个
 *  窗口，正是三级回落要避免的观感）。反过来，"同一次写入被 watchFile 触发
 *  两遍"的两条事件序号**和** mtime 都相同 —— 用签名就同时挡住重复、放过
 *  重置。启动时用文件当前签名初始化，理由见下。 */
let lastCorpusOpenSeq = 0;
let lastCorpusOpenSig = '';

/** 设置文件的「请求签名」：序号 + mtime（毫秒）。
 *
 *  mtime 用 stat 取，取不到（文件刚被删）就当 0：那一次事件本来就是"文件
 *  没了"，序号也读不出来，顶层 `seq <= 0` 会挡掉。 */
function corpusOpenSig(seq: number): string {
	let mtime = 0;
	try { mtime = fs.statSync(PET_SETTINGS_PATH).mtimeMs; } catch { mtime = 0; }
	return `${seq}@${mtime}`;
}

/** 读设置文件里的「编辑语料」请求序号；文件缺失/损坏返回 undefined。 */
function readCorpusOpenSeq(): unknown {
	try {
		const data = JSON.parse(fs.readFileSync(PET_SETTINGS_PATH, 'utf8')) as
			{ openCorpusSeq?: unknown };
		return data?.openCorpusSeq;
	} catch {
		return undefined;
	}
}

/** 设置文件变了 → 看看是不是桌宠点了「编辑语料」。
 *
 *  打开方式是三级回落的第一级：能在这里打开就不用去拉 `code` 命令
 *  （那会在 VS Code 已经开着的时候再起一个窗口/抢焦点）。
 *  打开之后必须**回执**给桌宠 —— 它正等这个信号来决定要不要往下回落。
 */
async function openCorpusIfRequested(): Promise<boolean> {
	const seq = Number(readCorpusOpenSeq());
	if (!Number.isFinite(seq) || seq <= 0) { return false; }
	// ⚠️ 判据从"序号是否变大"改成"**签名**是否变过"（0.3.45）。
	//
	// 0.3.44 用的是 `seq === lastCorpusOpenSeq`（因为 `<=` 会在文件被整份覆盖、
	// 序号被重置后连点 12 次才恢复）。但那只是把"永远不接单"换成了"同号不接单"：
	// 桌宠重启后从 max(内存,磁盘)+1 起算，算出的号可能正好是本会话已处理过的
	// 那个 —— 于是既不打开、也**不回执**，桌宠白等 2.5s 再回落 code 命令。
	//
	// 签名 = 序号 + 文件 mtime，正好把两种情况分开：
	//   · 同一次写入被 watchFile 触发两遍 → 序号与 mtime 都相同 → 忽略；
	//   · 序号被重置后重新写入 → mtime 必然是新的 → 正常处理。
	// 序号本身只增不减，所以"变小"只可能来自文件被重置/替换。
	const sig = corpusOpenSig(seq);
	if (sig === lastCorpusOpenSig) { return false; }
	lastCorpusOpenSig = sig;
	lastCorpusOpenSeq = seq;

	let ok = false;
	try {
		const doc = await vscode.workspace.openTextDocument(
			vscode.Uri.file(PET_CORPUS_PATH));
		void vscode.window.showTextDocument(doc, { preview: false });
		ok = true;
	} catch (err) {
		console.log(`[airi-monitor] open corpus failed: ${String(err)}`);
	}
	console.log(`[airi-monitor] corpus open request seq=${seq} ok=${ok}`);
	// 回执是"通知"，不是"事务"：桌宠收不到会走自己的超时回落，这里不 await
	void postJsonToPet('/event',
		{ type: 'corpusOpened', payload: { ok, method: 'vscode', seq } });
	return ok;
}

/** 当前生效的响应周期（毫秒）。由设置文件驱动，不再写死。 */
let petTickMs = PET_TICK_DEFAULT_MS;

/** 响应时钟句柄（startPetTick 重置起点用） */
let petTickTimer: NodeJS.Timeout | undefined;

/** 诊断是否有未推送到桌宠的变化（petTick 消费后清零） */
let diagPushPending = false;

/** 与 Python 的 `_NUM_RE` 同形：十进制小数 / 科学计数，整串必须匹配。 */
const NUM_LITERAL_RE = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;

/** 银行家舍入（四舍六入五取偶），与 Python 内置 `round()` 一致。
 *
 *  ⚠️ `Math.round(2.5)` 是 **3**，而 `round(2.5)` 是 **2** —— 用户手改
 *  `detectIntervalSec: 2.5` 时，扩展按 3 秒跑、桌宠按 2 秒显示，正是
 *  0.3.42 那条注释在骂的"界面和实际不一致"。 */
function roundHalfEven(n: number): number {
	const floor = Math.floor(n);
	const diff = n - floor;
	if (diff > 0.5) { return floor + 1; }
	if (diff < 0.5) { return floor; }
	return floor % 2 === 0 ? floor : floor + 1;   // 正好 .5 → 取偶
}

/** 把设置文件里的值收敛成秒；**不是数字形态就返回 undefined**（0.3.45）。
 *
 *  ⚠️ 判据必须是"形态像数字"，不能是 `Number(raw)`。`Number()` 认一堆
 *  桌宠侧 `float()` 不认（或反之）的写法，两边于是对同一个文件给出不同答案：
 *    · `Number('0x10')` = 16，`float('0x10')` 抛异常 → 一边 16 秒一边 30 秒；
 *    · `Number('  ')` = **0**，`float('  ')` 抛异常；
 *    · `None(true)` = **1**（0.3.44 挡掉的那半）；
 *    · `Number(['45'])` = 45（数组带单个数字字符串）。
 *  这里改成"先按正则判形态、再转换"，与 Python 的 `_as_number` 同构。
 */
function parseDetectSec(raw: unknown): number | undefined {
	let n: number;
	if (typeof raw === 'number') {
		n = raw;
	} else if (typeof raw === 'string' && NUM_LITERAL_RE.test(raw.trim())) {
		n = Number(raw.trim());
	} else {
		return undefined;          // 布尔 / 数组 / 对象 / null / 非数字串
	}
	if (!Number.isFinite(n)) { return undefined; }   // NaN / ±Infinity 字面量
	return n;
}

/** 读桌宠设置里的检测周期（毫秒）。
 *
 *  @returns
 *   · **undefined** —— 文件读不到 / JSON 坏了（可能是编辑器正在写）。
 *     调用方保持当前值：半截文件不代表用户改了什么。
 *   · **number** —— 字段的合法值，越界按 [1,60] 夹取（与 Python 一致）。
 *     字段**存在但不是数字形态**时返回默认值 30 秒（0.3.45）：桌宠那次
 *     `load_settings()` 对同一个值也是回落 30，两边必须给出同一个数，
 *     否则"菜单显示 30 秒、实际 5 秒"这种不一致就永远存在。
 */
function readDetectIntervalMs(): number | undefined {
	try {
		const data = JSON.parse(fs.readFileSync(PET_SETTINGS_PATH, 'utf8')) as
			{ detectIntervalSec?: unknown };
		const raw = (data as { detectIntervalSec?: unknown })?.detectIntervalSec;
		const sec = parseDetectSec(raw);
		if (sec === undefined) { return DETECT_DEFAULT_SEC * 1000; }
		const clamped = Math.max(DETECT_MIN_SEC,
			Math.min(DETECT_MAX_SEC, roundHalfEven(sec)));
		return clamped * 1000;
	} catch {
		return undefined;      // 文件不见了 / JSON 半截：保持当前周期
	}
}

/** 把设置文件里的周期同步到时钟上（变了才重启时钟）。
 *  @returns 是否发生了变化 */
function syncPetTickFromSettings(source: string): boolean {
	const ms = readDetectIntervalMs();
	if (ms === undefined || ms === petTickMs) { return false; }
	const prev = petTickMs;
	petTickMs = ms;
	startPetTick();   // 重启时钟：新周期从现在起算（与"唤醒时重置起点"一致）
	console.log(`[airi-monitor] detect interval ${prev / 1000}s -> ${ms / 1000}s (${source})`);
	return true;
}

/** 启动/重置桌宠响应时钟（从当前周期从现在起算） */
function startPetTick(): void {
	if (petTickTimer) { clearInterval(petTickTimer); }
	petTickTimer = setInterval(petTick, petTickMs);
}

/** 桌宠响应时钟的一拍：**一拍只说一条**。
 *  优先当前文件（更具体、带行号），它没话说时才轮到工作区诊断。
 *  两条同拍都推 = 气泡瞬间叠两个（前端 MAX_BUBBLES=2），
 *  违背"每一拍最多主动响应一次"的约定（周期见 petTickMs）。 */
function petTick(): void {
	if (scanActiveFile()) { return; }
	if (!diagPushPending) { return; }
	diagPushPending = false;
	void pushPendingDiagnostics().then((consumed) => {
		// 桌宠没起来 / 探测失败：把脏标记放回去，下一拍再试。
		// 旧写法无条件清标记 —— 桌宠未运行时这批诊断变化被静默吞掉，
		// 之后即使桌宠起来了也再不会补报（用户看不到"有 N 个错误"）。
		if (!consumed) { diagPushPending = true; }
	});
}

/** 把积压的诊断状态推送到桌宠（原 handleDiagnosticsChanged 的推送逻辑，
 *  从实时路径移入周期时钟 petTick）
 *  @returns 本批是否已消费（false = 桌宠不可达，调用方需保留脏标记） */
async function pushPendingDiagnostics(): Promise<boolean> {
	// ⚠️ 这里以前先做一次 `checkStandaloneAlive()` 再推。那个探测结果**带 15 秒
	// 缓存**（HEALTH_CHECK_INTERVAL_MS），桌宠刚死的窗口里它是陈旧 true：
	// 消息投不出去，却因为"探测说活着"而被当成已消费 —— 脏标记被清掉，
	// 这批诊断变化就此消失（除非用户又改出新的错误）。
	// 现在不看缓存、直接投，只认"有没有收到 2xx"。
	const workspaceErrors = collectErrors();
	// 从未出现过受支持的错误时不推 all_clear（例如无关语言的诊断事件）
	if (workspaceErrors.length === 0 && !hadRelevantErrors) { return true; }

	// ⚠️ items 里的 message 必须与 file_scan 那条链路一样先截断（0.3.41）。
	// 诊断消息可以非常长：C++ 模板实例化错误、clang-tidy 说明、Python 完整
	// traceback 随便就是几 KB。file_scan 侧早就 clipMessage(60) 了，这条路径
	// 却原样投喂 —— 20 条叠加后 POST body 能到几百 KB，既拖慢投递（更容易
	// 撞 postJsonToPet 的 500ms 超时），也让每次签名比较都在序列化大字符串。
	// 面板显示同样会糊成一片，所以这里连同 webview 一起用裁剪后的文本。
	const items = workspaceErrors.slice(0, 20).map((e) => ({
		...e, message: clipMessage(e.message, 200),
	}));

	const message = workspaceErrors.length > 0
		? {
			type: 'diagnostics' as const,
			payload: {
				count: workspaceErrors.length,
				items,
				timestamp: new Date().toISOString(),
				language: [...new Set(workspaceErrors.map((e) => e.languageId))].join(', '),
			},
		}
		: {
			trigger: 'all_clear' as const,
			error_count: 0,
			language: 'unknown',
			files: [],
			sample_errors: [],
		};

	// 相同错误状态不重复推送（避免每次诊断事件都让 Airi 重复吐槽）
	// 签名用**裁剪后**的文本：和真正发出去的 items 保持一致，否则同一批
	// 错误的签名会因为 message 里那些超长尾巴而无谓地变长
	// ⚠️ 签名必须带上总数（0.3.42）。items 只取前 20 条，count 却是全量：
	// 工作区 25 个错误时推一次，用户接着把**第 21~25 个**修掉（前 20 条一条
	// 没动）—— items 还和上次一模一样，签名不变 → return true → petTick 里
	// 的 diagPushPending 被当成"已消费"清掉。这次修复**永远不会播报**：
	// count 从 25 变成 20 但签名没变，之后只有前 20 条再动才会重新开口。
	// 用户修了 5 个 bug，桌宠一声不吭 —— 与"脏标记不可吞"同源，只是丢在
	// 签名这一层（count 本来就在 payload 里，只是没进签名）。
	const signature = JSON.stringify([
		(message as { type?: string }).type ?? (message as { trigger?: string }).trigger,
		workspaceErrors.length,
		items.map((e) => `${e.file}|${e.line}|${e.message}`),
	]);
	if (signature === lastPushSignature) { return true; }

	// ⚠️ 签名要在**确认送达之后**才提交：先记再投的话，投失败时下一拍会被
	// "和上次一样"挡掉 —— 这条播报就再也不会补发（与"脏标记不可吞"同源）。
	const delivered = await pushToStandalone(message);
	if (delivered) { lastPushSignature = signature; }
	return delivered;
}

/** 每个文件的监视状态（只保留最近一个文件） */
interface FileScanState {
	uri: string;
	/** bug 内容签名（行号+消息，排序后拼接），变化即推送 */
	signature: string;
	count: number;
	lastPushAt: number;
}
let fileScanState: FileScanState | undefined;

function fileScanConfig(): { enabled: boolean; repeatSec: number } {
	const cfg = vscode.workspace.getConfiguration('airiMonitor');
	const repeat = cfg.get<number>('fileScan.repeatIntervalSec', 60);
	return {
		enabled: cfg.get<boolean>('fileScan.enabled', true),
		repeatSec: Number.isFinite(repeat) ? Math.max(10, repeat) : 60,
	};
}

/** 错误消息截断：只取首行、最多 60 字符，避免气泡塞进整段 traceback */
function clipMessage(message: string, maxLen = 60): string {
	const oneLine = message.split('\n')[0] ?? '';
	return oneLine.length > maxLen ? oneLine.slice(0, maxLen - 1) + '…' : oneLine;
}

/** 扫当前文件；返回本拍是否决定说话（供 petTick 保证一拍只说一条） */
function scanActiveFile(): boolean {
	const { enabled, repeatSec } = fileScanConfig();
	if (!enabled) { return false; }

	const editor = vscode.window.activeTextEditor;
	// 只盯真实磁盘文件（untitled/output/webview 面板跳过）
	if (!editor || editor.document.uri.scheme !== 'file') { return false; }
	const languageId = editor.document.languageId;
	if (!supportedLanguageIds.has(languageId)) { return false; }

	const uri = editor.document.uri;
	const errors = collectErrors([uri]);
	const signature = errors.map((e) => `${e.line}|${e.message}`).sort().join(';');
	const count = errors.length;
	const now = Date.now();
	const prev = fileScanState && fileScanState.uri === uri.toString() ? fileScanState : undefined;

	const changed = !prev || prev.signature !== signature;
	let shouldPush: boolean;
	if (changed) {
		// 0 → 0 的"变化"（例如切到一个本来就干净的文件）不打扰
		shouldPush = !(count === 0 && (!prev || prev.count === 0));
	} else {
		// 数量没变但仍有 bug：超过冷却期就再提醒一次
		shouldPush = count > 0 && now - prev!.lastPushAt >= repeatSec * 1000;
	}

	if (!shouldPush) {
		if (changed) {
			fileScanState = { uri: uri.toString(), signature, count, lastPushAt: prev?.lastPushAt ?? now };
		}
		return false;
	}
	// ⚠️ fileScanState 先记下**本次写的那个对象**，回滚时做引用比较（0.3.41）。
	// 直接 `fileScanState = prev` 是有条件的：这个投递是**异步**的
	// （postJsonToPet 有 500ms 超时），而 petTick 最短可以设到 1 秒一次。
	// 主线程繁忙时超时回调可能迟到到下一拍之后：那时 fileScanState 早就被
	// 下一拍换成了更新的状态，无条件回滚会把**更新的那份直接抹掉** ——
	// 桌宠恢复后会把已经判过的内容再播一遍。只有"状态还停在我写的这份"时，
	// 回滚才是我该做的事。
	const mine: FileScanState = { uri: uri.toString(), signature, count, lastPushAt: now };
	fileScanState = mine;

	const message = {
		trigger: 'file_scan' as const,
		// reason: changed = bug 内容有变化（新增/修复/清零）；remind = 超时重复提醒
		reason: changed ? 'changed' : 'remind',
		file: path.basename(uri.fsPath),
		error_count: count,
		prev_count: prev ? prev.count : count,
		language: languageId,
		sample_errors: errors.slice(0, 3).map((e) => ({
			line: e.line,
			message: clipMessage(e.message),
		})),
		timestamp: new Date().toISOString(),
	};

	void (async () => {
		// 以**真实投递结果**为准，不再拿带 15s 缓存的健康探测做前置判断
		// （见 postJsonToPet 的注释）：桌宠刚死时缓存还是 true，旧写法会
		// 把这一次"说话"当成已生效，bug 首播就此消失。
		const delivered = await pushToStandalone(message);
		if (delivered) { return; }
		// 没送出去：这一条没送到，把**整条决策状态回滚**（含 signature），
		// 让下一拍从头再判一次。只把 lastPushAt 提前到 now 的旧写法会让状态
		// 显示"已经报过"——bug 首播被静默吞掉，用户要么等满一个 repeatSec
		// 冷却、要么永远收不到"干净了"（count=0 走不到 remind 分支）。
		// 与 pushPendingDiagnostics 的"脏标记不可吞"同源。
		// prev 是本次改动前的状态对象（新文件时为 undefined）——回滚它即可。
		if (fileScanState === mine) { fileScanState = prev; }
	})();
	return true;
}

// ============================================================================
// handleDiagnosticsChanged()
// ============================================================================

function handleDiagnosticsChanged(): void {
	// all_clear 必须按工作区整体判断：
	// 本次变化的文件没错误 ≠ 全部修好，其他文件（包括已关闭但语言服务器
	// 仍在跟踪的文件）可能还有错误
	const workspaceErrors = collectErrors();
	if (workspaceErrors.length > 0) { hadRelevantErrors = true; }

	// 发送到 Webview 备用面板（同样使用工作区整体口径，避免误导）
	// 面板是用户主动打开的，保持实时；桌宠气泡走响应时钟（petTick）
	// ⚠️ 这条也得裁（0.3.42）：pushPendingDiagnostics 上面那句注释写的是
	// "连同 webview 一起用裁剪后的文本"，但这里以前是 slice(0, 20) 原样投喂
	// —— 注释是空的。面板 HTML 直接 esc(e.message) 渲染完整文本，C++ 模板
	// 实例化错误那种几 KB 的消息会把列表项撑爆。而且这条路是**每次诊断变化
	// 都触发**（下面那句注释特意说"面板保持实时"），频次比走 30 秒一拍的
	// 桌宠推送高得多，更需要裁。
	postToWebview(buildDiagnosticsMessage(workspaceErrors));

	// 标脏即可：真正的推送在下一个响应 tick（petTick → pushPendingDiagnostics）。
	// 此前这里是实时推送——每敲一个字母触发诊断变化，桌宠就收到并报一次。
	diagPushPending = true;
}

// ============================================================================
// checkStandaloneAlive() — 探测桌宠服务器是否在运行（带缓存）
// ============================================================================

async function checkStandaloneAlive(): Promise<boolean> {
	const now = Date.now();
	if (now - lastHealthCheckAt < HEALTH_CHECK_INTERVAL_MS) { return standaloneHealthy; }
	lastHealthCheckAt = now;
	standaloneHealthy = await new Promise<boolean>((resolve) => {
		// ⚠️ 必须包 try/catch：http.get 对非法参数是**同步抛**（不是 error 事件），
		// 在 Promise executor 里抛出会让整个 await 炸出去。STANDALONE_PORT 已经
		// 做过范围校验，但 hostname / path 这类参数将来若变成可配置，同样会踩。
		// 对照：postJsonToPet 早就有这层保护，这里是同一类问题的另一半。
		let req: http.ClientRequest;
		try {
			req = http.get(
				{ hostname: '127.0.0.1', port: STANDALONE_PORT, path: '/ping', timeout: 600 },
				(res) => { res.resume(); resolve(res.statusCode === 200); }
			);
		} catch {
			resolve(false);
			return;
		}
		req.on('error', () => resolve(false));
		req.on('timeout', () => { req.destroy(); resolve(false); });
	});
	return standaloneHealthy;
}

// ============================================================================
// refreshPetStatus() — 刷新状态栏按钮文本（强制重新探测，绕过缓存）
// ============================================================================

async function refreshPetStatus(): Promise<void> {
	if (!petStatusBar) { return; }
	lastHealthCheckAt = 0; // 强制重新探测，否则轮询永远命中 15s 缓存
	const alive = await checkStandaloneAlive();
	petStatusBar.text = alive
		? '$(heart) deepseek 桌宠: 运行中'
		: '$(rocket) deepseek 桌宠: 未运行';
	petStatusBar.tooltip = alive
		? `deepseek 桌宠服务器运行中 (port ${STANDALONE_PORT})，点击可查看启动状态`
		: `点击启动 deepseek 桌宠 (desktop_pet/standalone.py, port ${STANDALONE_PORT})`;
}

// ============================================================================
// launchStandalonePet() — 从 VS Code 内一键启动桌宠 (standalone.py)
// ============================================================================

function findPythonPath(): string {
	const candidates = [
		process.env.AIRI_PYTHON_PATH,
		path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'),
		'python',
	];
	for (const candidate of candidates) {
		if (!candidate) { continue; }
		if (candidate === 'python' || fs.existsSync(candidate)) { return candidate; }
	}
	return 'python';
}

/** 启动中的 Promise（并发守卫，见 launchStandalonePet 注释） */
let launchingPet: Promise<void> | undefined;

/** 启动独立桌宠。重复调用返回同一个 Promise，不会 spawn 第二个进程。 */
function launchStandalonePet(): Promise<void> {
	// ⚠️ 0.3.41 —— 下面那段"等服务器起来"最多要跑 8 秒，期间
	// checkStandaloneAlive() 一直返回 false（端口还没 bind）。用户最常见的
	// 操作恰恰是"点了没反应，再点一下"：第二次调用会**完整地再走一遍
	// spawn**，于是同时有两个 python.exe（第二个 bind 失败，要等它自己的
	// 5 秒守卫 + _ping_ok 才退出），并且用户会收到两条一模一样的
	// "桌宠已启动"通知。用 Promise 把整段包住即可。
	if (launchingPet) { return launchingPet; }
	launchingPet = doLaunchStandalonePet();
	void launchingPet.finally(() => { launchingPet = undefined; });
	return launchingPet;
}

async function doLaunchStandalonePet(): Promise<void> {
	// ⚠️ 必须先**强制重新探测**（v0.3.33）。checkStandaloneAlive() 用的是最长
	// 15 秒的缓存，桌宠刚死的那段时间里它还是 true —— 用户点状态栏只会看到
	// "已经在运行了"，然后什么都没发生（要等下一次 10s 的状态栏轮询把缓存
	// 刷新过来才点得动）。"启动"是用户显式动作，判断必须基于当下的事实。
	lastHealthCheckAt = 0;
	if (await checkStandaloneAlive()) {
		vscode.window.showInformationMessage('deepseek 桌宠已经在运行了');
		return;
	}

	const petDir = path.join(extensionRoot, 'desktop_pet');
	const script = path.join(petDir, 'standalone.py');
	if (!fs.existsSync(script)) {
		vscode.window.showErrorMessage(`找不到桌宠脚本: ${script}`);
		return;
	}

	const python = findPythonPath();
	try {
		const child = spawn(python, [script], {
			cwd: petDir,
			detached: true,
			stdio: 'ignore',
			windowsHide: true,
		});
		child.on('error', (err) => {
			vscode.window.showErrorMessage(`启动桌宠失败: ${err.message}`);
		});
		child.unref();
	} catch (err) {
		vscode.window.showErrorMessage(`启动桌宠失败: ${err instanceof Error ? err.message : String(err)}`);
		return;
	}

	// 等服务器起来后确认
	const deadline = Date.now() + 8000;
	while (Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, 500));
		lastHealthCheckAt = 0; // 强制重新探测
		if (await checkStandaloneAlive()) {
			lastHealthCheckAt = Date.now();
			vscode.window.showInformationMessage('deepseek 桌宠已启动，开始监视你的代码 (￣▽￣)');
			void refreshPetStatus();
			// 以桌宠被唤醒为起点重新计时（周期 = 当前检测周期，默认 30s）：
			// 唤醒后先安静一个周期，之后每一拍才主动响应一次
			startPetTick();
			return;
		}
	}
	vscode.window.showWarningMessage(
		`桌宠进程已启动但服务器未响应 (port ${STANDALONE_PORT})。` +
		'请检查 Python 环境是否安装了 pywebview (pip install pywebview)。'
	);
	void refreshPetStatus();
}

// ============================================================================
// pushToStandalone() — HTTP POST 到独立桌宠服务器
// ============================================================================

/** 向桌宠服务器 POST 一段 JSON。
 *  @param path '/push'（推消息给桌宠）或 '/event'（上报事件）
 *  @returns **是否真的送到了**（收到 2xx）。桌宠没起来时返回 false，不抛。
 *
 *  ⚠️ 为什么必须回传投递结果（v0.3.33）：旧写法忽略响应、直接返回 void，
 *     于是调用方只能拿 `checkStandaloneAlive()` 的**缓存**（最长 15s）当
 *     "送没送到"的依据 —— 桌宠刚死的那一小段窗口里缓存仍然是 true：
 *     状态照常提交、消息投进黑洞，而 petTick 里"桌宠不可达就回滚"的那套
 *     不变量（0.3.19 定的：脏标记绝不能被吞）**事实上失效**。用户侧的表现
 *     是：桌宠重启后第一条 bug 播报不见了，file_scan 那条要等满一个
 *     repeatSec（60s）才会再提醒一次。
 *     现在的判据是"HTTP 有没有真的收到 2xx" —— 这是唯一的事实。
 *     顺带在失败时立刻作废健康缓存，免得后续决策继续用陈旧结论。 */
function postJsonToPet(path: string, message: unknown, timeoutMs = 500): Promise<boolean> {
	return new Promise<boolean>((resolve) => {
		try {
			const body = JSON.stringify(message);
			const req = http.request({
				hostname: '127.0.0.1', port: STANDALONE_PORT, path,
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					'Content-Length': Buffer.byteLength(body),
				},
				timeout: timeoutMs,
			}, (res) => {
				res.resume();
				const code = res.statusCode ?? 0;
				resolve(code >= 200 && code < 300);
			});
			req.on('error', () => {
				// 服务器未运行 / 连接被拒：静默，但让下一次探测重新来过
				standaloneHealthy = false;
				lastHealthCheckAt = 0;
				resolve(false);
			});
			req.on('timeout', () => {
				req.destroy();
				standaloneHealthy = false;
				lastHealthCheckAt = 0;
				resolve(false);
			});
			req.write(body);
			req.end();
		} catch {
			resolve(false);
		}
	});
}

/** 推消息给桌宠。 @returns 是否送达（未送达的调用方必须回滚自己的状态） */
function pushToStandalone(message: unknown): Promise<boolean> {
	return postJsonToPet('/push', message);
}

// ============================================================================
// Webview 备用面板
// ============================================================================

function createOrShowAssistantPanel(context: vscode.ExtensionContext): void {
	if (panel) { panel.reveal(vscode.ViewColumn.Beside); return; }

	panel = vscode.window.createWebviewPanel(
		'airiAssistant', 'deepseek Assistant', vscode.ViewColumn.Beside,
		{ enableScripts: true, retainContextWhenHidden: true }
	);
	panel.webview.html = getWebviewHtml(STANDALONE_PORT);
	// ⚠️ 面板打开时必须推一次快照（0.3.43）。
	// 以前创建完就返回，而 HTML 里 #list 的初始内容写死的是
	// "没有错误 — 一切正常"：工作区明明挂着十几个错误、只是没人在打字
	// （onDidChangeDiagnostics 不再触发），用户打开面板看到的是**一条假的
	// 结论**，会以为已经修好了。这不是"没刷新"，是主动显示错误的安慰。
	// 用 ready 握手而不是直接 postMessage：webview 页面还没加载完时发出去
	// 的消息会被丢掉，只有页面自己说"我准备好了"才可靠。
	panel.webview.onDidReceiveMessage((msg: unknown) => {
		if ((msg as { type?: string } | undefined)?.type === 'ready') {
			pushDiagnosticsSnapshot();
		}
	});
	panel.onDidDispose(() => {
		// 顺手把自己从 subscriptions 里摘掉：不摘的话每"开→关"一轮就多留
		// 一个已 dispose 的 WebviewPanel（连带它的 webview 资源句柄），
		// 直到扩展停用都不会释放。
		const i = context.subscriptions.indexOf(panel as unknown as vscode.Disposable);
		if (i >= 0) { context.subscriptions.splice(i, 1); }
		panel = undefined;
	});
	context.subscriptions.push(panel);
}

function postToWebview(message: unknown): void {
	if (!panel) { return; }
	void panel.webview.postMessage(message);
}

/** 诊断面板要的那条消息（count 用工作区整体口径，items 裁到 20 条） */
function buildDiagnosticsMessage(errors: ErrorItem[]): unknown {
	return {
		type: 'diagnostics',
		payload: {
			count: errors.length,
			items: errors.slice(0, 20).map((e) => ({
				...e, message: clipMessage(e.message, 200),
			})),
			timestamp: new Date().toISOString(),
		},
	};
}

/** 面板首帧快照（0.3.43）：开关面板时不能让它停在写死的初始文案上 */
function pushDiagnosticsSnapshot(): void {
	postToWebview(buildDiagnosticsMessage(collectErrors()));
}

function getWebviewHtml(port: number): string {
	// 端口必须插值：用户可以用 AIRI_STANDALONE_PORT 换端口，写死 19876 会让
	// 这块面板**在换过端口的环境里指错方向**，用户照着它排查就走岔了。
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"/><style>
:root{color-scheme:dark;--bg:#1a1a2e;--text:#e8e8e8;--muted:#8888aa;--accent:#ff6b9d;--error-bg:rgba(255,71,87,0.12);--error-border:#ff4757}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);height:100vh;padding:12px;overflow-y:auto}
h2{color:var(--accent);margin-bottom:8px;font-size:14px}
.status{font-size:12px;color:var(--muted);margin-bottom:12px}
.error-item{background:var(--error-bg);border-left:3px solid var(--error-border);padding:6px 10px;margin:4px 0;border-radius:4px;font-size:12px}
.error-item .msg{color:var(--text)}
.error-item .meta{color:var(--muted);font-size:10px}
.empty{color:var(--muted);font-size:12px;text-align:center;padding:20px}
</style></head><body>
<h2>deepseek Monitor</h2>
<p class="status">诊断消息自动转发至桌宠 (port ${port})</p>
<div id="list"><p class="empty">没有错误 — 一切正常</p></div>
<script>
const vscode=acquireVsCodeApi(),list=document.getElementById('list');
function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML}
window.addEventListener('message',function(e){
  var m=e.data;if(m.type!=='diagnostics')return;
  var items=m.payload.items||[],count=m.payload.count||0;
  if(count===0){list.innerHTML='<p class="empty">没有错误 — 一切正常</p>';return}
  var html='';items.slice(0,20).forEach(function(e){
    var fname=e.file.split(/[\\\\/]/).pop();
    html+='<div class="error-item"><div class="msg">'+esc(e.message)+'</div><div class="meta">'+esc(e.languageId)+' | '+esc(fname)+':'+esc(e.line)+'</div></div>';
  });list.innerHTML=html;
});
// 告诉扩展"页面已就绪，可以推首帧了"（0.3.43）。不握手的话扩展那边
// postMessage 可能早于页面加载完成，消息会被 webview 直接丢弃，
// 面板就停在 HTML 里写死的"没有错误 — 一切正常"上。
vscode.postMessage({type:'ready'});
</script></body></html>`;
}
