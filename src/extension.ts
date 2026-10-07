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
 *     → 桌宠响应时钟（30s 周期，唤醒时重置起点）→ pushPendingDiagnostics
 *       → POST http://127.0.0.1:19876/push
 *         → standalone 内建 generate_response → SSE → 桌宠气泡
 *
 *   当前文件 bug 监视（file scan watchdog）→ scanActiveFile
 *     → 与诊断推送共用同一个 30s 周期时钟（petTick）：
 *       bug 变化推送；未变化每 60s 提醒；修到 0 说一次"文件干净"后沉默
 *     → POST /push {trigger: 'file_scan', file, error_count, sample_errors}
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
const supportedLanguageIds = new Set(['c', 'cpp', 'python']);

/** 独立桌宠服务器 (standalone.py) 端口（可用环境变量 AIRI_STANDALONE_PORT 覆盖） */
const STANDALONE_PORT = Number(process.env.AIRI_STANDALONE_PORT) || 19876;

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

	// --- 桌宠响应时钟（30s 周期）---
	// 诊断变化与当前文件扫描共用这一个节奏：每 30 秒最多主动说一条。
	//   * 诊断变化只标脏（diagPushPending），tick 时才真正推送
	//     —— 敲代码时每个字母触发的诊断事件不再实时打扰桌宠
	//   * scanActiveFile 在同一 tick 里跑：bug 变化推送、未变化按冷却提醒、
	//     修到 0 说一次"干净了"后沉默
	//   * 两条路径同拍都想说话时当前文件优先（诊断脏标记留到下一拍，
	//     避免一拍冒两个气泡）
	// 桌宠被唤醒（launchStandalonePet 成功）时重置计时起点。
	startPetTick();
	context.subscriptions.push(new vscode.Disposable(() => {
		if (petTickTimer) { clearInterval(petTickTimer); petTickTimer = undefined; }
	}));

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

	vscode.window.showInformationMessage('Airi Monitor 已就绪 — 诊断消息将转发到桌宠', '启动桌宠').then((selection) => {
		if (selection === '启动桌宠') { void vscode.commands.executeCommand('vscode-anime-assistent.launchPet'); }
	});
	console.log(`[airi-monitor] Ready. Push target: http://127.0.0.1:${STANDALONE_PORT}/push`);
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

/** 桌宠主动响应周期：30 秒一拍（用户指定）。
 * 诊断变化只标脏，真正的推送在这一拍里做——敲代码时的每次诊断事件
 * 都不再实时打扰；"以桌宠被唤醒开始计时"由 launchStandalonePet 成功后
 * 调 startPetTick() 重置起点实现。 */
const PET_TICK_MS = 30000;

/** 响应时钟句柄（startPetTick 重置起点用） */
let petTickTimer: NodeJS.Timeout | undefined;

/** 诊断是否有未推送到桌宠的变化（petTick 消费后清零） */
let diagPushPending = false;

/** 启动/重置桌宠响应时钟（30s 从现在起算） */
function startPetTick(): void {
	if (petTickTimer) { clearInterval(petTickTimer); }
	petTickTimer = setInterval(petTick, PET_TICK_MS);
}

/** 桌宠响应时钟的一拍：**一拍只说一条**。
 *  优先当前文件（更具体、带行号），它没话说时才轮到工作区诊断。
 *  两条同拍都推 = 气泡瞬间叠两个（前端 MAX_BUBBLES=2），
 *  违背"每 30 秒主动响应一次"的约定。 */
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
 *  从实时路径移入 30s 周期时钟）
 *  @returns 本批是否已消费（false = 桌宠不可达，调用方需保留脏标记） */
async function pushPendingDiagnostics(): Promise<boolean> {
	if (!(await checkStandaloneAlive())) { return false; }
	// 从未出现过受支持的错误时不推 all_clear（例如无关语言的诊断事件）
	const workspaceErrors = collectErrors();
	if (workspaceErrors.length === 0 && !hadRelevantErrors) { return true; }

	const message = workspaceErrors.length > 0
		? {
			type: 'diagnostics' as const,
			payload: {
				count: workspaceErrors.length,
				items: workspaceErrors.slice(0, 20),
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
	const signature = JSON.stringify([
		(message as { type?: string }).type ?? (message as { trigger?: string }).trigger,
		workspaceErrors.map((e) => `${e.file}|${e.line}|${e.message}`),
	]);
	if (signature === lastPushSignature) { return true; }
	lastPushSignature = signature;

	pushToStandalone(message);
	return true;
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
	fileScanState = { uri: uri.toString(), signature, count, lastPushAt: now };

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
		if (await checkStandaloneAlive()) { pushToStandalone(message); }
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
	// 面板是用户主动打开的，保持实时；桌宠气泡走 30s 响应时钟
	postToWebview({
		type: 'diagnostics',
		payload: { count: workspaceErrors.length, items: workspaceErrors.slice(0, 20), timestamp: new Date().toISOString() }
	});

	// 标脏即可：真正的推送在下一个 30s 响应 tick（petTick → pushPendingDiagnostics）。
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
		const req = http.get(
			{ hostname: '127.0.0.1', port: STANDALONE_PORT, path: '/ping', timeout: 600 },
			(res) => { res.resume(); resolve(res.statusCode === 200); }
		);
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
		? '$(heart) Airi 桌宠: 运行中'
		: '$(rocket) Airi 桌宠: 未运行';
	petStatusBar.tooltip = alive
		? `Airi 桌宠服务器运行中 (port ${STANDALONE_PORT})，点击可查看启动状态`
		: `点击启动 Airi 桌宠 (desktop_pet/standalone.py, port ${STANDALONE_PORT})`;
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

async function launchStandalonePet(): Promise<void> {
	if (await checkStandaloneAlive()) {
		vscode.window.showInformationMessage('Airi 桌宠已经在运行了');
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
			vscode.window.showInformationMessage('Airi 桌宠已启动，开始监视你的代码 (￣▽￣)');
			void refreshPetStatus();
			// 以桌宠被唤醒为起点重新计 30s：唤醒后先安静一个周期，
			// 之后每 30s 才主动响应一次
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

function pushToStandalone(message: unknown): void {
	try {
		const body = JSON.stringify(message);
		const req = http.request({
			hostname: '127.0.0.1', port: STANDALONE_PORT, path: '/push',
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'Content-Length': Buffer.byteLength(body),
			},
			timeout: 500,
		}, () => { /* 忽略响应 */ });
		req.on('error', () => { /* 服务器未运行，静默跳过 */ });
		req.on('timeout', () => { req.destroy(); });
		req.write(body);
		req.end();
	} catch { /* 静默失败 */ }
}

// ============================================================================
// Webview 备用面板
// ============================================================================

function createOrShowAssistantPanel(context: vscode.ExtensionContext): void {
	if (panel) { panel.reveal(vscode.ViewColumn.Beside); return; }

	panel = vscode.window.createWebviewPanel(
		'airiAssistant', 'Airi Assistant', vscode.ViewColumn.Beside,
		{ enableScripts: true, retainContextWhenHidden: true }
	);
	panel.webview.html = getWebviewHtml();
	panel.onDidDispose(() => { panel = undefined; });
	context.subscriptions.push(panel);
}

function postToWebview(message: unknown): void {
	if (!panel) { return; }
	void panel.webview.postMessage(message);
}

function getWebviewHtml(): string {
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
<h2>Airi Monitor</h2>
<p class="status">诊断消息自动转发至桌宠 (port 19876)</p>
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
</script></body></html>`;
}
