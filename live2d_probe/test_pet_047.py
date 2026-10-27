# -*- coding: utf-8 -*-
"""0.3.47 回归测试。

覆盖本轮修的 6 处：

  A 段  desktop_pet/watcher.py 的单文件模式
        A1 推送失败后**必须靠 pending 重试**（0.3.46 的"送达才提交"被外层
           `current_mtime > last_mtime` 挡死，实际上一次都不会重试）
        A2 对照：目录模式在同一时序下本来就会重推（判据不能只看单文件）
        A3 送达之后不许继续重发（pending 要清空）
  B 段  src/extension.ts 的「编辑语料」请求判据
        B1 只改 detectIntervalSec（序号不变）→ 不许打开语料文件
        B2 真的 +1 → 必须打开
        B3 同内容重写一遍（watchFile 双触发）→ 不许重复打开
        B4 序号被整份覆盖抹掉后再写回同号 → 必须打开（0.3.44 的老洞）
  C 段  desktop_pet/standalone.py 的 _persist_locked
        C1 用户自己加的键必须原样保留（只归一化认识的两个键）
        C2 已知键的坏值仍要自愈（别为了保键把归一化丢了）
        C3 文件坏了照样一个字节都不写（0.3.46 的 D1 不许回归）
  D 段  standalone._corpus_open_ack 的序号口径
        D1 布尔不是序号（0.3.46 立的"布尔不是数量"口径的第五个入口）
        D2 真序号照旧回执（别修过头）
  E 段  /event 的日志措辞
        E1 `{"type": null}` 不许被记成 "non-object body: dict"
        E2 真·非对象仍然记 non-object body: list
  F 段  desktop_pet/main.py（旧入口）与 standalone 的参数对齐
        F1 窗口高 480
        F2 load_html 必须收到 find_character_image()（立绘那条 documented 功能）

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 每段包在自己的 try 里 —— 旧版没有的符号会抛 AttributeError，把后面整段吞掉。
  · 断言源码前先剥注释：本轮注释里写着"不能是 xxx"，会误伤自己。
  · B 段必须**从当前 src 现编译**一份 extension.js 再跑，不能复用 out/ ——
     out/ 是构建产物，判断力验证时 src 被 stash 回旧版而 out/ 还是新的，
     B 段就会假通过。
"""
import ast
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='airi_test047_')
# ⚠️ 必须在 import standalone / watcher **之前**设：否则测试会往生产日志/设置里写
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

REPO = Path(r'S:\My event\projects\vscode-anime-assistent')
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'
TSC = str(REPO / 'node_modules' / 'typescript' / 'bin' / 'tsc')

ROOT = str(REPO)
sys.path.insert(0, os.path.join(ROOT, 'desktop_pet'))
sys.path.insert(0, os.path.join(ROOT, 'python_backend'))

PASS = 0
FAIL = 0
LINES = []


def check(name, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        LINES.append('PASS  %s' % name)
    else:
        FAIL += 1
        LINES.append('FAIL  %s   %s' % (name, detail))


def section(t):
    LINES.append('')
    LINES.append('--- %s ---' % t)


def skip(name, why=''):
    LINES.append('SKIP  %s   %s' % (name, why))


def strip_comments(src):
    """剥掉 # 注释和字符串里的干扰？只剥行注释够了（本轮注释里有反例写法）。"""
    out = []
    for line in src.splitlines():
        q = None
        cut = len(line)
        for i, ch in enumerate(line):
            if q:
                if ch == q:
                    q = None
            elif ch in '"\'':
                q = ch
            elif ch == '#':
                cut = i
                break
        out.append(line[:cut])
    return '\n'.join(out)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(pred, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


# ===========================================================================
# A 段：watcher 单文件模式要能重试
# ===========================================================================
section('A watcher 单文件模式的"送达才提交"真的能重试吗')

GOT = []
GLOCK = threading.Lock()


def _make_sink(port):
    import http.server

    class _Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n) if n else b''
            with GLOCK:
                GOT.append(raw)
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _broken_py(root, name='broken.py'):
    p = os.path.join(root, name)
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write('def f(:\n    pass\n')
    return p


try:
    import watcher

    # --- A1 单文件 ---
    d1 = tempfile.mkdtemp(prefix='t047a1_', dir=TMP)
    bad = _broken_py(d1)
    p1 = free_port()
    with GLOCK:
        GOT.clear()
    threading.Thread(target=watcher.watch_single_file, args=(bad, p1),
                     daemon=True).start()
    time.sleep(2.5)                      # 先经历一次"端口没人听"的失败
    with GLOCK:
        n_fail_phase = len(GOT)
    srv1 = _make_sink(p1)
    try:
        got1 = wait_for(lambda: len(GOT) > 0, 6.0)
        with GLOCK:
            n1 = len(GOT)
    finally:
        srv1.shutdown()
    check('A1-1 失败阶段确实没送出去', n_fail_phase == 0, 'got=%d' % n_fail_phase)
    check('A1-2 接收端起来后单文件模式会重推', got1 and n1 >= 1, 'got=%d' % n1)

    # --- A3 送达之后不许继续重发 ---
    time.sleep(3.0)
    with GLOCK:
        n1b = len(GOT)
    check('A3 送达后不再重复刷同一条', n1b <= n1 + 1, 'before=%d after=%d' % (n1, n1b))

    # --- A2 目录模式（对照）---
    d2 = tempfile.mkdtemp(prefix='t047a2_', dir=TMP)
    proj = os.path.join(d2, 'proj')
    os.makedirs(proj)
    _broken_py(proj)
    p2 = free_port()
    with GLOCK:
        GOT.clear()
    threading.Thread(target=watcher.watch_directory, args=(proj, p2),
                     daemon=True).start()
    time.sleep(2.5)
    srv2 = _make_sink(p2)
    try:
        got2 = wait_for(lambda: len(GOT) > 0, 6.0)
        with GLOCK:
            n2 = len(GOT)
    finally:
        srv2.shutdown()
    check('A2 目录模式在同一时序下本来就会重推', got2 and n2 >= 1, 'got=%d' % n2)
except Exception as exc:
    check('A 段执行', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
# B 段：扩展的「编辑语料」判据（从当前 src 现编译再真跑）
# ===========================================================================
section('B 只改检测周期不许打开语料文件')

VSCODE_STUB = r"""
const CALLS = [];
class Disposable {
  constructor(fn) { this._fn = fn; }
  dispose() { if (this._fn) { this._fn(); } }
}
const st = { text: '', tooltip: '', command: '', show() {}, dispose() {} };
module.exports = {
  __CALLS: CALLS,
  Disposable,
  StatusBarAlignment: { Left: 1, Right: 2 },
  ViewColumn: { Beside: -2 },
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  Uri: { file(p) { return { scheme: 'file', fsPath: p, toString() { return 'file:///' + String(p).replace(/\\/g, '/'); } }; } },
  commands: { registerCommand() { return new Disposable(); }, executeCommand() { return Promise.resolve(); } },
  languages: { onDidChangeDiagnostics() { return new Disposable(); }, getDiagnostics() { return []; } },
  window: {
    activeTextEditor: undefined,
    createStatusBarItem() { return st; },
    showInformationMessage() { return Promise.resolve(undefined); },
    showWarningMessage() { return Promise.resolve(undefined); },
    showErrorMessage(m) { CALLS.push(['errorMessage', String(m)]); return Promise.resolve(undefined); },
    showTextDocument(doc) { CALLS.push(['showTextDocument', doc && doc.__path]); return Promise.resolve({}); },
  },
  workspace: {
    textDocuments: [],
    getConfiguration() { return { get(_k, d) { return d; } }; },
    onDidChangeConfiguration() { return new Disposable(); },
    openTextDocument(u) {
      const p = (u && u.fsPath) || String(u);
      CALLS.push(['openTextDocument', p]);
      return Promise.resolve({ __path: p, uri: u });
    },
  },
};
"""

PROBE_JS = r"""
const fs = require('fs');
const path = require('path');
const HOME = process.env.PROBE_HOME;
process.env.USERPROFILE = HOME;
process.env.HOME = HOME;
const SETTINGS = path.join(HOME, '.dsh', 'pets', 'airi-pet-settings.json');
const CORPUS = path.join(HOME, '.dsh', 'pets', 'airi-pet-corpus.jsonc');
const vscode = require('vscode');
const ext = require(process.env.PROBE_EXT);

function writeAtomic(obj) {
  const tmp = SETTINGS + '.probe';
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2), 'utf8');
  fs.renameSync(tmp, SETTINGS);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function opens() { return vscode.__CALLS.filter((c) => c[0] === 'openTextDocument').length; }

(async () => {
  fs.mkdirSync(path.dirname(SETTINGS), { recursive: true });
  fs.writeFileSync(CORPUS, '// probe corpus\n', 'utf8');
  writeAtomic({ detectIntervalSec: 30, openCorpusSeq: 3 });
  ext.activate({ extensionPath: process.env.PROBE_ROOT, subscriptions: [] });
  await sleep(1400);

  const a0 = opens();
  await sleep(60);
  writeAtomic({ detectIntervalSec: 10, openCorpusSeq: 3 });      // 只改周期
  await sleep(2200);
  console.log('P1=' + (opens() - a0));

  const b0 = opens();
  await sleep(60);
  writeAtomic({ detectIntervalSec: 10, openCorpusSeq: 4 });      // 真请求
  await sleep(2200);
  console.log('P2=' + (opens() - b0));

  const c0 = opens();
  await sleep(60);
  writeAtomic({ detectIntervalSec: 10, openCorpusSeq: 4 });      // 同内容重写
  await sleep(2200);
  console.log('P2B=' + (opens() - c0));

  const d0 = opens();
  await sleep(60);
  writeAtomic({ detectIntervalSec: 10 });                        // 序号被抹掉
  await sleep(1200);
  writeAtomic({ detectIntervalSec: 10, openCorpusSeq: 4 });      // 又从 4 写回
  await sleep(2200);
  console.log('P3=' + (opens() - d0));

  console.log('CORPUS=' + CORPUS);
  process.exit(0);
})().catch((e) => { console.error('PROBE_FAIL', e && e.stack || e); process.exit(1); });
"""


def run_node_probe():
    """@returns dict 或 None（编译/运行失败时 None）"""
    work = tempfile.mkdtemp(prefix='t047b_', dir=TMP)
    outdir = os.path.join(work, 'out')
    os.makedirs(os.path.join(work, 'node_modules', 'vscode'), exist_ok=True)
    with io.open(os.path.join(work, 'node_modules', 'vscode', 'index.js'),
                 'w', encoding='utf-8') as fh:
        fh.write(VSCODE_STUB)
    # ⚠️ 必须现编译：out/ 是构建产物，判断力验证时 src 被回滚而 out/ 不会
    ts_out = subprocess.run(
        [NODE, TSC, str(REPO / 'src' / 'extension.ts'), '--module', 'commonjs',
         '--target', 'es2020', '--skipLibCheck', '--outDir', outdir],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    emitted = os.path.join(outdir, 'extension.js')
    if not os.path.isfile(emitted):
        return {'error': 'tsc emit failed: %s' % (ts_out.stdout or ts_out.stderr)[:300]}
    with io.open(os.path.join(work, 'probe.js'), 'w', encoding='utf-8') as fh:
        fh.write(PROBE_JS)
    home = os.path.join(work, 'home')
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env['NODE_PATH'] = os.path.join(work, 'node_modules')
    env['PROBE_HOME'] = home
    env['PROBE_EXT'] = emitted
    env['PROBE_ROOT'] = str(REPO)
    r = subprocess.run([NODE, os.path.join(work, 'probe.js')], capture_output=True,
                       text=True, timeout=180, env=env, cwd=work)
    res = {'raw': (r.stdout or '') + (r.stderr or '')}
    for line in (r.stdout or '').splitlines():
        if '=' in line and line.split('=')[0] in ('P1', 'P2', 'P2B', 'P3', 'CORPUS'):
            k, v = line.split('=', 1)
            res[k] = v
    return res


try:
    esrc = io.open(str(REPO / 'src' / 'extension.ts'), encoding='utf-8').read().lstrip('\ufeff')
    esrc_bare = strip_comments(esrc)
    # B0：0.3.45 的 mtime 签名必须彻底退场（留着就是"改周期开语料"的根源）
    check('B0-1 不再有 corpusOpenSig', 'corpusOpenSig' not in esrc_bare,
          '仍有 corpusOpenSig')
    check('B0-2 不再有 lastCorpusOpenSig', 'lastCorpusOpenSig' not in esrc_bare,
          '仍有 lastCorpusOpenSig')

    res = run_node_probe()
    if res is None or 'error' in res:
        skip('B1 只改周期不许打开语料', (res or {}).get('error', 'probe failed'))
        skip('B2 真请求必须打开', 'probe failed')
        skip('B3 同内容重写不重复打开', 'probe failed')
        skip('B4 序号被抹掉后写回仍要打开', 'probe failed')
    else:
        check('B1 只改周期不许打开语料', res.get('P1') == '0',
              'P1=%s  (期望 0)' % res.get('P1'))
        check('B2 真请求必须打开', res.get('P2') == '1',
              'P2=%s  (期望 1)' % res.get('P2'))
        check('B3 同内容重写不重复打开', res.get('P2B') == '0',
              'P2B=%s (期望 0)' % res.get('P2B'))
        check('B4 序号被抹掉后写回仍要打开', res.get('P3') == '1',
              'P3=%s  (期望 1)' % res.get('P3'))
except Exception as exc:
    check('B 段执行', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
# C 段：_persist_locked 不许吞掉未知键
# ===========================================================================
section('C 设置文件里用户自己加的键不许被抹掉')

import standalone  # noqa: E402

SPATH = standalone.SETTINGS_PATH


def write_text(path, text):
    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


def read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def read_json(path):
    with io.open(path, encoding='utf-8') as fh:
        return json.load(fh)


try:
    # --- C1 未知键保留 ---
    write_text(SPATH, json.dumps(
        {'detectIntervalSec': 30, 'openCorpusSeq': 5, 'note': 'hi',
         'theme': {'accent': '#ff6b9d'}}, ensure_ascii=False))
    standalone.settings.clear()
    standalone.settings.update({'detectIntervalSec': 30, 'openCorpusSeq': 5})
    with standalone.settings_lock:
        ok1 = standalone._persist_locked('detectIntervalSec', 10)
    d1 = read_json(SPATH) if os.path.exists(SPATH) else {}
    check('C1-1 保存成功', ok1 is True, 'ok=%r' % (ok1,))
    check('C1-2 已知键生效', d1.get('detectIntervalSec') == 10,
          'detect=%r' % (d1.get('detectIntervalSec'),))
    check('C1-3 未知键 note 保留', d1.get('note') == 'hi', 'note=%r' % (d1.get('note'),))
    check('C1-4 未知键 theme 保留（对象原样）',
          isinstance(d1.get('theme'), dict) and d1['theme'].get('accent') == '#ff6b9d',
          'theme=%r' % (d1.get('theme'),))
    check('C1-5 openCorpusSeq 不动', d1.get('openCorpusSeq') == 5,
          'seq=%r' % (d1.get('openCorpusSeq'),))

    # --- C2 已知键的坏值仍要自愈 ---
    write_text(SPATH, json.dumps({'detectIntervalSec': 'abc', 'note': 'keep'}))
    within = None
    with standalone.settings_lock:
        within = standalone._persist_locked('openCorpusSeq', 7)
    d2 = read_json(SPATH)
    check('C2-1 坏值被归一化成合法整数',
          d2.get('detectIntervalSec') == standalone.DETECT_DEFAULT_SEC,
          'detect=%r' % (d2.get('detectIntervalSec'),))
    check('C2-2 未知键在自愈路径上也保留', d2.get('note') == 'keep',
          'note=%r' % (d2.get('note'),))
    check('C2-3 新写的键生效', d2.get('openCorpusSeq') == 7,
          'seq=%r' % (d2.get('openCorpusSeq'),))

    # --- C3 文件坏了：一个字节都不写（0.3.46 的 D1 不许回归）---
    broken = '{"detectIntervalSec": 30, "openCorpusSeq": '
    write_text(SPATH, broken)
    before = read_bytes(SPATH)
    with standalone.settings_lock:
        ok3 = standalone._persist_locked('detectIntervalSec', 5)
    after = read_bytes(SPATH)
    check('C3-1 读坏的文件拒绝覆盖', ok3 is False, 'ok=%r' % (ok3,))
    check('C3-2 文件字节完全没变', before == after,
          'before=%r after=%r' % (before[:40], after[:40]))
except Exception as exc:
    check('C 段执行', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
# D 段：_corpus_open_ack 的序号口径
# ===========================================================================
section('D 回执里的布尔不是序号')

try:
    slot = {'event': threading.Event(), 'ok': False, 'method': None}
    with standalone._corpus_open_lock:
        standalone._corpus_open_wait[1] = slot
    try:
        handled = standalone._corpus_open_ack(True, True, 'bogus')
        check('D1-1 布尔不被当成序号 1', handled is False, 'handled=%r' % (handled,))
        check('D1-2 别人的槽没被唤醒', not slot['event'].is_set(),
              '事件被 set 了')
        check('D1-3 槽里的 ok 没被改成 True', slot['ok'] is False,
              'ok=%r' % (slot['ok'],))
        handled2 = standalone._corpus_open_ack(1, True, 'ext')
        check('D2-1 真序号照旧回执', handled2 is True, 'handled=%r' % (handled2,))
        check('D2-2 槽被唤醒且 ok=True',
              slot['event'].is_set() and slot['ok'] is True,
              'set=%r ok=%r' % (slot['event'].is_set(), slot['ok']))
        # 负数/Infinity 也不该命中任何槽
        check('D2-3 负数不命中', standalone._corpus_open_ack(-1, True, 'x') is False)
        check('D2-4 inf 不命中', standalone._corpus_open_ack(1e999, True, 'x') is False)
    finally:
        with standalone._corpus_open_lock:
            standalone._corpus_open_wait.pop(1, None)
except Exception as exc:
    check('D 段执行', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
# E 段：/event 的日志措辞
# ===========================================================================
section('E /event 的日志不许把"对象缺 type"说成非对象')

try:
    port = free_port()
    ready = threading.Event()
    threading.Thread(target=standalone.start_server, args=(port, ready),
                     daemon=True).start()
    ready.wait(5)
    OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'http://127.0.0.1:%d' % port

    def post(path, obj):
        data = json.dumps(obj).encode()
        req = urllib.request.Request(
            base + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with OPENER.open(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception as exc:
            return '%s: %s' % (type(exc).__name__, exc)

    st1 = post('/event', {'type': None, 'payload': {'a': 1}})
    st2 = post('/event', [1, 2])
    time.sleep(0.3)
    logtxt = ''
    if os.path.exists(os.environ['AIRI_PET_LOG']):
        logtxt = io.open(os.environ['AIRI_PET_LOG'], encoding='utf-8',
                         errors='replace').read()
    obj_lines = [ln for ln in logtxt.splitlines() if 'object without' in ln]
    bad_lines = [ln for ln in logtxt.splitlines()
                 if 'non-object body: dict' in ln]
    nonobj_lines = [ln for ln in logtxt.splitlines()
                    if 'non-object body: list' in ln]
    check('E1-1 两条请求都 200', st1 == 200 and st2 == 200, 'st1=%r st2=%r' % (st1, st2))
    check('E1-2 对象缺 type 有专门一行（含 type 值）',
          bool(obj_lines) and 'None' in obj_lines[0], obj_lines[:1])
    check('E1-3 不再被记成 non-object body: dict', not bad_lines, bad_lines[:1])
    check('E2 真·非对象仍然记 non-object body: list', bool(nonobj_lines),
          logtxt[-300:])
except Exception as exc:
    check('E 段执行', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
# F 段：旧入口 main.py 的参数对齐
# ===========================================================================
section('F desktop_pet/main.py 与 standalone 的参数一致')

try:
    msrc = io.open(str(REPO / 'desktop_pet' / 'main.py'), encoding='utf-8').read()
    ssrc = io.open(str(REPO / 'desktop_pet' / 'standalone.py'), encoding='utf-8').read()
    mtree = ast.parse(msrc)

    dims = {}
    load_call = None
    for node in ast.walk(mtree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple):
            names = [t.id for t in node.targets[0].elts if isinstance(t, ast.Name)]
            if names == ['win_w', 'win_h'] and isinstance(node.value, ast.Tuple):
                vals = [v.value for v in node.value.elts
                        if isinstance(v, ast.Constant)]
                if len(vals) == 2:
                    dims['main'] = tuple(vals)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'load_html'):
            load_call = node
    stree = ast.parse(ssrc)
    for node in ast.walk(stree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple):
            names = [t.id for t in node.targets[0].elts if isinstance(t, ast.Name)]
            if names == ['win_w', 'win_h'] and isinstance(node.value, ast.Tuple):
                vals = [v.value for v in node.value.elts
                        if isinstance(v, ast.Constant)]
                if len(vals) == 2:
                    dims['standalone'] = tuple(vals)

    check('F1-1 main.py 窗口高 480', dims.get('main') == (300, 480),
          'main=%r' % (dims.get('main'),))
    check('F1-2 standalone.py 也是 300x480', dims.get('standalone') == (300, 480),
          'standalone=%r' % (dims.get('standalone'),))

    second = None
    if load_call is not None and len(load_call.args) >= 2:
        second = load_call.args[1]
    ok_second = (isinstance(second, ast.Call)
                 and isinstance(second.func, ast.Name)
                 and second.func.id == 'find_character_image')
    check('F2 main.py 把立绘传给 load_html', ok_second,
          'load_html 第 2 参数=%s' % (ast.dump(second)[:60] if second else 'None'))
    check('F2-b main.py 有导入 find_character_image',
          'find_character_image' in strip_comments(msrc), '缺导入')
except Exception as exc:
    check('F 段执行', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '_t047.txt'), 'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
