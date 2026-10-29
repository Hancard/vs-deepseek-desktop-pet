# -*- coding: utf-8 -*-
"""0.3.49 回归测试。

本轮修的是一件事的两个面：扩展侧读「编辑语料」序号（`src/extension.ts` 的
`readCorpusOpenSeq` / `coerceCorpusSeq` / `openCorpusIfRequested`）。

  A 段  判据的静态性质
        A1 序号归一化存在，且**拒布尔**（`Number(true)` 是 1 —— 而 1 正是
           首装后第一次请求的序号）
        A2 "读不出来"返回的是**哨兵**，不是 undefined（两者必须分开）
        A3 哨兵早退必须发生在"清空观察值"**之前**（顺序反了就白改）

  B 段  真跑（从 src 现编译 + vscode 桩 + 真 activate），只认一个副作用：
        `workspace.openTextDocument` 被调了几次
        B1 `"openCorpusSeq": true` → 桌宠下一次请求（序号 1）必须打开
        B2 `[1]`            同上
        B3 `["1"]`          同上
        B4 `1e999`（Infinity）同上（0.3.43 的判据不许回归）
        B5 同内容重写不算新请求；真新序号要打开
        B6 **半截 JSON 之后原样存回** —— 读不出来 ≠ 序号被抹掉，不许打开
        B7 **序号被整份覆盖抹掉**后写回 —— 0.3.47 的判据不许回归，必须打开

  C 段  同一轮的第二件事：旧入口 `desktop_pet/main.py` 没把设置里的检测周期
        传给 `load_html`，首帧右键菜单恒为默认 30（standalone 那条路一直传）
        C0/C1 结构：调用点必须带上 detect_sec（关键字或 kwargs 皆可）
        C2     行为：那个值真的写进了渲染出的菜单（5 vs 不传时的 30）

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 必须从 `src/` **现编译**，不能复用 `out/extension.js`：判断力验证时
    `git stash` 回滚的是 src，out/ 还是新的，node 探针会假通过。
  · 一个场景一个 node 进程、一个独立 HOME：`activate()` 在启动时就把文件里
    的当前序号记成"已观察"，复用进程等于带上上一个场景的状态。
  · 断言只认**副作用**（openTextDocument 的次数），不认日志、不认内部变量。
  · `fs.watchFile` 是 500ms 轮询，写完至少要等 ≥1.5s 才看结果。
  · 每段包在 try 里：缺符号会抛 AttributeError，把后面整段吞掉。
"""
import io
import os
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='airi_test049_')
REPO = r'S:\My event\projects\vscode-anime-assistent'
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'
TSC = os.path.join(REPO, 'node_modules', 'typescript', 'bin', 'tsc')
EXT = os.path.join(REPO, 'src', 'extension.ts')

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


ESRC = io.open(EXT, encoding='utf-8').read().lstrip('\ufeff')


def grab(src, start, end_marker='\n}\n'):
    """抠出一段源码（从 start 到 end_marker）。找不到返回 ''。"""
    i = src.find(start)
    if i < 0:
        return ''
    j = src.find(end_marker, i + len(start))
    return src[i:j + len(end_marker)] if j > i else ''


# ===========================================================================
section('A 判据的静态性质')
# ===========================================================================
try:
    coerce = grab(ESRC, 'function coerceCorpusSeq(')
    check('A0 抠到 coerceCorpusSeq', bool(coerce), 'len=%d' % len(coerce))
    check('A1 序号归一化拒布尔（`Number(true)` 是 1，而 1 是首装后的第一个序号）',
          'boolean' in coerce and 'Number.isInteger' in coerce,
          'body=%r' % coerce[:200])

    read = grab(ESRC, 'function readCorpusOpenSeq(')
    check('A0-2 抠到 readCorpusOpenSeq', bool(read), 'len=%d' % len(read))
    # catch 分支必须返回哨兵 —— 返回 undefined 就是把"读不出来"和"键被抹掉"混成一件事
    _catch = read[read.find('catch'):] if 'catch' in read else ''
    check('A2 读不出来返回哨兵（不是 undefined）',
          'CORPUS_SEQ_UNREADABLE' in _catch and 'return undefined' not in _catch,
          'catch=%r' % _catch[:160])

    body = grab(ESRC, 'async function openCorpusIfRequested(')
    check('A0-3 抠到 openCorpusIfRequested', bool(body), 'len=%d' % len(body))
    _i_sentinel = body.find('CORPUS_SEQ_UNREADABLE')
    _i_clear = body.find('lastSeenCorpusSeq = undefined')
    check('A3 哨兵早退在"清空观察值"之前（顺序反了等于白改）',
          0 <= _i_sentinel < _i_clear,
          'sentinel=%d clear=%d' % (_i_sentinel, _i_clear))
    check('A4 序号不裸用 Number() 强转',
          'Number(readCorpusOpenSeq())' not in ESRC
          and 'coerceCorpusSeq(' in body)
except Exception as exc:
    check('A 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
section('B 真跑扩展：只有 openTextDocument 的次数算数')
# ===========================================================================
VSCODE_STUB = r"""
const CALLS = [];
class Disposable { constructor(fn) { this._fn = fn; } dispose() { if (this._fn) { this._fn(); } } }
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
    showErrorMessage() { return Promise.resolve(undefined); },
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
const SPEC = JSON.parse(process.env.PROBE_SPEC);

function put(text) {
  const tmp = SETTINGS + '.probe';
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, SETTINGS);          // 桌宠自己也是 os.replace 原子写
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function opens() { return vscode.__CALLS.filter((c) => c[0] === 'openTextDocument').length; }

(async () => {
  fs.mkdirSync(path.dirname(SETTINGS), { recursive: true });
  fs.writeFileSync(CORPUS, '// probe corpus\n', 'utf8');
  put(SPEC.init);
  ext.activate({ extensionPath: process.env.PROBE_ROOT, subscriptions: [] });
  await sleep(1400);                     // 让 watchFile 记下基线
  let prev = opens();
  const deltas = [];
  for (const step of SPEC.steps) {
    put(step);
    await sleep(1800);
    const now = opens();
    deltas.push(now - prev);
    prev = now;
  }
  console.log('DELTAS=' + JSON.stringify(deltas));
  process.exit(0);
})().catch((e) => { console.error('PROBE_FAIL', (e && e.stack) || e); process.exit(1); });
"""

#: (名字, 初始文件内容, [每步写入的内容], [每步期望的新增打开次数])
SCENARIOS = [
    ('B1 "openCorpusSeq": true —— 桌宠下一次请求（序号 1）必须打开',
     '{"detectIntervalSec": 30, "openCorpusSeq": true}',
     ['{"detectIntervalSec": 30, "openCorpusSeq": 1}'],
     [1]),
    ('B2 "openCorpusSeq": [1] —— 数组不是序号',
     '{"detectIntervalSec": 30, "openCorpusSeq": [1]}',
     ['{"detectIntervalSec": 30, "openCorpusSeq": 1}'],
     [1]),
    ('B3 "openCorpusSeq": ["1"] —— 同上',
     '{"detectIntervalSec": 30, "openCorpusSeq": ["1"]}',
     ['{"detectIntervalSec": 30, "openCorpusSeq": 1}'],
     [1]),
    ('B4 "openCorpusSeq": 1e999（Infinity）—— 0.3.43 的判据不许回归',
     '{"detectIntervalSec": 30, "openCorpusSeq": 1e999}',
     ['{"detectIntervalSec": 30, "openCorpusSeq": 1}'],
     [1]),
    ('B5 只改周期不算新请求；真新序号要打开',
     '{"detectIntervalSec": 30, "openCorpusSeq": 5}',
     ['{"detectIntervalSec": 10, "openCorpusSeq": 5}',
      '{"detectIntervalSec": 10, "openCorpusSeq": 6}'],
     [0, 1]),
    ('B6 半截 JSON 之后原样存回 —— 读不出来 ≠ 序号被抹掉',
     '{"detectIntervalSec": 30, "openCorpusSeq": 5}',
     ['{"detectIntervalSec": 30, "openCorpusSeq": 5',
      '{"detectIntervalSec": 30, "openCorpusSeq": 5}'],
     [0, 0]),
    ('B7 序号被整份覆盖抹掉后写回 —— 0.3.47 的判据不许回归',
     '{"detectIntervalSec": 30, "openCorpusSeq": 5}',
     ['{"detectIntervalSec": 30}',
      '{"detectIntervalSec": 30, "openCorpusSeq": 5}'],
     [0, 1]),
]


def compile_ext():
    outdir = os.path.join(TMP, 'out')
    os.makedirs(os.path.join(TMP, 'node_modules', 'vscode'), exist_ok=True)
    with io.open(os.path.join(TMP, 'node_modules', 'vscode', 'index.js'),
                 'w', encoding='utf-8') as fh:
        fh.write(VSCODE_STUB)
    r = subprocess.run(
        [NODE, TSC, EXT, '--module', 'commonjs', '--target', 'es2020',
         '--skipLibCheck', '--outDir', outdir],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    emitted = os.path.join(outdir, 'extension.js')
    if not os.path.isfile(emitted):
        return None, 'tsc emit failed: %s' % ((r.stdout or r.stderr or '')[:300])
    return emitted, ''


def run_scenario(emitted, name, init, steps, expect):
    import json as _json
    home = tempfile.mkdtemp(dir=TMP, prefix='home_')
    probe = os.path.join(TMP, 'probe.js')
    with io.open(probe, 'w', encoding='utf-8') as fh:
        fh.write(PROBE_JS)
    env = dict(os.environ)
    env['NODE_PATH'] = os.path.join(TMP, 'node_modules')
    env['PROBE_HOME'] = home
    env['PROBE_EXT'] = emitted
    env['PROBE_ROOT'] = REPO
    env['PROBE_SPEC'] = _json.dumps({'init': init, 'steps': steps})
    r = subprocess.run([NODE, probe], capture_output=True, text=True,
                       timeout=300, env=env, cwd=TMP)
    out = ((r.stdout or '') + (r.stderr or '')).strip()
    got = None
    for line in (r.stdout or '').splitlines():
        if line.startswith('DELTAS='):
            try:
                got = _json.loads(line.split('=', 1)[1])
            except ValueError:
                got = None
    return got, expect, out


try:
    emitted, err = compile_ext()
    if not emitted:
        check('B0 从 src 现编译', False, err)
    else:
        check('B0 从 src 现编译', True)
        for name, init, steps, expect in SCENARIOS:
            got, want, out = run_scenario(emitted, name, init, steps, expect)
            check(name, got == want,
                  'got=%r want=%r %s' % (got, want, '' if got is not None else out[-240:]))
except Exception as exc:
    check('B 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
section('C 旧入口 main.py 必须把设置里的检测周期传给页面')
# ===========================================================================
# 缺陷：`desktop_pet/main.py` 的 load_html(...) 没传 detect_sec，于是首帧右键
# 菜单永远显示 load_html 的默认 30 —— 用户把周期设成 5 秒，打开菜单却写着
# 30，要等 GET /settings 回来才纠正（standalone.py 那条路一直是传的）。
#
# 判据分两层，缺一不可：
#   C1 结构：main.py 的 load_html 调用必须**带上** detect_sec（关键字写法，
#      或 `**kwargs` 且那个 dict 里确实有 detect_sec 这个键、值取自
#      load_settings()）。两种合法写法都放过，旧码两者皆无 → 必红。
#   C2 行为：那条通路**真的改变渲染结果**（5 秒写进菜单、不传就是 30）——
#      否则 C1 只是"查语法"，谁把值写死也能过。
MAIN = os.path.join(REPO, 'desktop_pet', 'main.py')
try:
    import ast
    import re

    MSRC = io.open(MAIN, encoding='utf-8').read().lstrip('\ufeff')
    tree = ast.parse(MSRC)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'load_html']
    check('C0 main.py 里有 load_html 调用点', bool(calls), 'n=%d' % len(calls))
    has_kw = any(k.arg == 'detect_sec' for c in calls for k in c.keywords)
    has_unpack = any(k.arg is None for c in calls for k in c.keywords)
    feeds = re.search(r"\[['\"]detect_sec['\"]\]\s*=", MSRC) is not None
    check('C1 检测周期传到了 load_html（关键字或 kwargs 都算）',
          has_kw or (has_unpack and feeds),
          'kw=%s unpack=%s feeds=%s' % (has_kw, has_unpack, feeds))
    check('C1-2 值取自 load_settings()，不是另写一份默认',
          "load_settings()['detectIntervalSec']" in MSRC.replace('"', "'"))
except Exception as exc:
    check('C 段静态部分未崩', False, '%s: %s' % (type(exc).__name__, exc))

try:
    import json as _json
    _h = tempfile.mkdtemp(dir=TMP, prefix='cmain_')
    _settings = os.path.join(_h, 'settings.json')
    # ⚠️ 环境变量必须在 import standalone **之前**设（0.3.28 的教训：
    # 否则 SETTINGS_PATH / 日志路径在 import 时就定死了，测试会打到真实文件）
    os.environ['AIRI_PET_SETTINGS'] = _settings
    os.environ['AIRI_PET_LOG'] = os.path.join(_h, 'pet.log')
    io.open(_settings, 'w', encoding='utf-8').write(
        _json.dumps({'detectIntervalSec': 5, 'openCorpusSeq': 3}))
    sys.path.insert(0, os.path.join(REPO, 'desktop_pet'))
    import common          # noqa: E402
    import standalone      # noqa: E402

    _sec = standalone.load_settings()['detectIntervalSec']
    check('C2 设置文件里的 5 被读出来（未被 clamp 成别的值）', _sec == 5, repr(_sec))

    def _menu_value(html_text):
        m = re.search(r'parseInt\("(\d+)"', html_text or '')
        return m.group(1) if m else 'NONE'

    # 按 main.py 现在的通路渲染
    _html = common.load_html(19876, None, live2d=False, silhouette=False,
                             card='off', detect_sec=_sec)
    got = _menu_value(_html)
    check('C2-2 首帧菜单显示 5（旧码这里恒为 30）', got == '5', 'got=%s' % got)

    # 对照组：不传参数时是 load_html 的默认 30 —— 证明差异确实来自这个参数
    _html_d = common.load_html(19876, None, live2d=False, silhouette=False,
                               card='off')
    got_d = _menu_value(_html_d)
    check('C2-3 对照：不传 detect_sec 时是 30', got_d == '30', 'got=%s' % got_d)

    # 设置文件坏掉时 load_settings 自己回落，main.py 那条 except 不应被触发
    io.open(_settings, 'w', encoding='utf-8').write('{ not json')
    _bad = standalone.load_settings()['detectIntervalSec']
    check('C2-4 坏设置文件 → 回落 30 且不抛', _bad == 30, repr(_bad))
except Exception as exc:
    check('C 段运行时部分未崩', False, '%s: %s' % (type(exc).__name__, exc))


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_t049.txt'),
            'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
