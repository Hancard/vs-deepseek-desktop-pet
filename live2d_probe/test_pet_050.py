# -*- coding: utf-8 -*-
"""0.3.50 回归测试。

本轮修的两条都属于"同一个值、两个实现、两个答案"，外加一条一直缺的**跨进程**
覆盖。

  A 段  气泡里的 `{language}` 占位符（真调 generate_response）
        A1 `language: null` 不许念出 "None的语法…"
        A2 空串 / 数字 / 数组同样不许（旧码实测：null → "None的语法不难吧？"、
           `["py"]` → "在写 ['py'] 吗？"）
        A3 合法的语言名必须原样保留（别为了堵畸形把正常值也吃了）

  B 段  Python 与 JS 的"数字形态"判据逐值对撞（真 node 跑同一张表）
        B1 `_as_number` 的接受集 == JS NUM_RE 的接受集
        B2 `_as_seq` 的"正整数"判据 == JS SEQ_RE + Number.isInteger + >0
        B3 `clamp_detect_sec` 的结果 == JS 侧 roundHalfEven + [1,60] 的结果
        旧码分歧值：`"٥"`(U+0665) / `"５"`(全角) / `"५"`(天城文) —— Python 的
        `\d` 与 `int()` 都认这些 Unicode 数字，`float()` 也真的能转（=5.0），
        而 JS 的 `\d` 只认 ASCII。于是设置文件里写 `"５"` 时桌宠按 5 秒跑、
        菜单也显示 5，扩展却按 30 秒跑。

  C 段  真·跨进程跑一遍「编辑语料」（覆盖，不是本轮修复的判据）
        真 standalone 服务器 + 从 src/ 现编译的扩展（真 activate）+ 真设置文件
        轮询 + 真 /event 回执。断言：扩展只开一次、**服务器没有走 code 回落**
        （那会在 VS Code 已开着时再拉一个窗口）、气泡是"在 VS Code 里打开了"、
        设置文件里确实落了新序号。这是本项目第一次把两半接起来跑 —— 之前
        Python 侧用假回执、扩展侧用不联网的桩，中间的 HTTP/文件约定没人验。

⚠️ 测试自身的坑（逐条防住）
  · `AIRI_PET_*` 环境变量必须在 `import standalone` / `import corpus` **之前**
    设好，否则会读写用户真实的设置文件和语料（0.3.28 踩过）。
  · C 段要挡住 `code` 回落：把 `_open_corpus_fallback` / `push_message` 换成
    记录器。否则测试会**真的**在开发机上再开一个 VS Code 窗口。
  · 一个场景一个 node 进程：`activate()` 启动时就把设置文件里的当前序号记成
    "已观察"，复用进程等于带上上一次的状态。
  · 断言只认副作用：`openTextDocument` 的次数、`_open_corpus_fallback` 有没有
    被调过、气泡文本 —— 不认日志、不认内部变量。
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'
TSC = os.path.join(REPO, 'node_modules', 'typescript', 'bin', 'tsc')
TMP = tempfile.mkdtemp(prefix='airi_test050_')

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


# 环境变量：必须在 import 之前
HOME = os.path.join(TMP, 'home')
os.makedirs(os.path.join(HOME, '.dsh', 'pets'), exist_ok=True)
SETTINGS = os.path.join(HOME, '.dsh', 'pets', 'airi-pet-settings.json')
CORPUS = os.path.join(HOME, '.dsh', 'pets', 'airi-pet-corpus.jsonc')
os.environ['AIRI_PET_SETTINGS'] = SETTINGS
os.environ['AIRI_PET_CORPUS'] = CORPUS
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
io.open(SETTINGS, 'w', encoding='utf-8').write('{"detectIntervalSec": 30}')

sys.path.insert(0, os.path.join(REPO, 'desktop_pet'))
sys.path.insert(0, os.path.join(REPO, 'python_backend'))


# ===========================================================================
section('A 气泡里的 {language} 占位符')
# ===========================================================================
try:
    import response_generator as rg

    BASE = {'trigger': 'diagnostics', 'error_count': 3,
            'sample_errors': [{'line': 2, 'message': 'invalid syntax'}]}

    def sample(language, n=20):
        ctx = dict(BASE)
        if language is not _MISSING:
            ctx['language'] = language
        return [rg.generate_response(ctx)['payload']['text'] for _ in range(n)]

    class _Miss(object):
        pass

    _MISSING = _Miss()
    def bad(texts):
        """畸形判据：出现 None / 列表 / 未替换的占位符 / 空的"的语法"。

        ⚠️ 数字那两条要用**有辨识度的值**（951753 / 'qqzz'）而不是 123：
        123 在台词里可能合法出现（"30 秒"之类），断言会退化成"偶然不出现"。
        实测过：用 123 时旧码也能"通过"，A2-2 等于没测。
        """
        bads = []
        for t in texts:
            if ('None' in t or '[' in t or '{language}' in t
                    or t.startswith('的语法') or '951753' in t or 'qqzz' in t):
                bads.append(t)
        return bads

    a_null = sample(None)
    check('A1 language=null 不念出 None', not bad(a_null), 'bad=%r' % bad(a_null)[:3])

    a_empty = sample('')
    check('A2-1 language="" 不念出空的"的语法…"', not bad(a_empty),
          'bad=%r' % bad(a_empty)[:3])

    a_num = sample(951753)
    check('A2-2 language=951753 不把数字当语言名', not bad(a_num),
          'bad=%r' % bad(a_num)[:3])

    a_arr = sample(['qqzz'])
    check('A2-3 language=["qqzz"] 不把数组塞进气泡', not bad(a_arr),
          'bad=%r' % bad(a_arr)[:3])

    # 合法值必须原样保留（别为了堵畸形把正常输入也吃了）
    kept = []
    for _ in range(30):
        t = rg.generate_response(dict(BASE, language='c++'))['payload']['text']
        if 'c++' in t:
            kept.append(t)
    check('A3 合法的语言名 c++ 原样出现在台词里', bool(kept), 'kept=%d' % len(kept))

    # 缺键与 null 必须给出同一个答案（这是本条修复的核心不变式）
    miss = sample(_MISSING)
    merged = list(miss) + list(a_null)
    check('A4 缺键与 null 同口径（都不出现畸形，都回落到同一个默认值）',
          not bad(merged), 'bad=%r' % bad(merged)[:3])
except Exception as exc:
    check('A 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
section('B 数字形态：Python 与 JS 逐值对撞')
# ===========================================================================
#: (标签, Python 侧要构造的 JSON 值, 给 node 的构造指令)
CASES = [
    ('整数字符串 "5"', '5', "'5'"),
    ('带空白 " 5 "', ' 5 ', "' 5 '"),
    ('正号 "+5"', '+5', "'+5'"),
    ('负号 "-5"', '-5', "'-5'"),
    ('零 "0"', '0', "'0'"),
    ('小数串 "5.5"', '5.5', "'5.5'"),
    ('半值串 "2.5"', '2.5', "'2.5'"),
    ('半值串 "3.5"', '3.5', "'3.5'"),
    ('科学计数 "1e2"', '1e2', "'1e2'"),
    ('十六进制串 "0x10"', '0x10', "'0x10'"),
    ('下划线 "1_0"', '1_0', "'1_0'"),
    ('空串 ""', '', "''"),
    ('空白串 " "', ' ', "' '"),
    ('阿拉伯-印度数字 "٥"', '\u0665', "'\\u0665'"),
    ('全角数字 "５"', '\uff15', "'\\uff15'"),
    ('天城文数字 "५"', '\u096b', "'\\u096b'"),
    ('垃圾串 "abc"', 'abc', "'abc'"),
    ('带单位 "5px"', '5px', "'5px'"),
    ('溢出串 "1e999"', '1e999', "'1e999'"),
    ('点开头 ".5"', '.5', "'.5'"),
    ('点结尾 "5."', '5.', "'5.'"),
    ('整数 5', 5, '5'),
    ('整数 0', 0, '0'),
    ('负数 -3', -3, '-3'),
    ('小数 2.9', 2.9, '2.9'),
    ('小数 3.0', 3.0, '3.0'),
    ('布尔 true', True, 'true'),
    ('布尔 false', False, 'false'),
    ('数组 [1]', [1], '[1]'),
    ('数组 ["1"]', ['1'], "['1']"),
    ('null', None, 'null'),
    ('超大整数 1e9', 1000000000, '1000000000'),
]

JS_HARNESS = r"""
const NUM = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;
const SEQ = /^[+-]?\d+$/;
function rhe(n) { const f = Math.floor(n), d = n - f; if (d > 0.5) return f + 1; if (d < 0.5) return f; return f % 2 === 0 ? f : f + 1; }
function asNumber(v) {
  let n;
  if (typeof v === 'number') { n = v; }
  else if (typeof v === 'string' && NUM.test(v.trim())) { n = Number(v.trim()); }
  else { return null; }
  if (!Number.isFinite(n)) return null;
  return n;
}
function clampSec(v) {
  const n = asNumber(v);
  if (n === null) return 30;             // DETECT_DEFAULT_SEC
  return Math.max(1, Math.min(60, rhe(n)));
}
function asSeq(v) {
  if (typeof v === 'boolean') return 0;
  if (typeof v === 'number') return Number.isInteger(v) && v > 0 ? v : 0;
  if (typeof v === 'string') {
    const t = v.trim();
    if (!/^[+-]?\d+$/.test(t)) return 0;
    const n = Number(t);
    return Number.isSafeInteger(n) && n > 0 ? n : 0;
  }
  return 0;
}
const vals = JSON.parse(process.env.PROBE_VALS);
console.log(JSON.stringify(vals.map((v) => ({
  num: asNumber(v) === null ? null : asNumber(v),
  clamp: clampSec(v),
  seq: asSeq(v),
}))));
"""

try:
    import standalone as sa

    # 构造同一张值表：JSON 往返当作"外部输入"
    vals = []
    for _label, pyval, _js in CASES:
        vals.append(pyval)

    js_path = os.path.join(TMP, 'numtest.js')
    io.open(js_path, 'w', encoding='utf-8').write(JS_HARNESS)
    env = dict(os.environ)
    env['PROBE_VALS'] = json.dumps(vals)
    r = subprocess.run([NODE, js_path], capture_output=True, text=True,
                       timeout=60, env=env)
    try:
        js = json.loads((r.stdout or '').strip().splitlines()[-1])
    except Exception:
        js = None
    check('B0 node 侧取到结果', js is not None,
          'rc=%d out=%r err=%r' % (r.returncode, r.stdout[:200], r.stderr[:200]))

    if js is not None:
        num_bad, seq_bad, clamp_bad = [], [], []
        for (label, pyval, _js), j in zip(CASES, js):
            # JSON 往返后 Python 侧看到的类型与 node 一致
            v = json.loads(json.dumps(pyval))
            py_num = sa._as_number(v)
            jn = j['num']
            if (py_num is None) != (jn is None):
                num_bad.append((label, py_num, jn))
            elif py_num is not None and abs(py_num - jn) > 1e-9:
                num_bad.append((label, py_num, jn))

            py_seq = sa._as_seq(v)
            if py_seq != j['seq']:
                seq_bad.append((label, py_seq, j['seq']))

            py_clamp = sa.clamp_detect_sec(v)
            if py_clamp != j['clamp']:
                clamp_bad.append((label, py_clamp, j['clamp']))

        check('B1 _as_number 的接受集与 JS NUM_RE 一致', not num_bad,
              'diff=%r' % num_bad[:4])
        check('B2 _as_seq 与 JS（isInteger && >0 && ASCII）一致', not seq_bad,
              'diff=%r' % seq_bad[:4])
        check('B3 clamp_detect_sec 与 JS 的 roundHalfEven+夹取一致', not clamp_bad,
              'diff=%r' % clamp_bad[:4])
except Exception as exc:
    check('B 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
section('C 真·跨进程「编辑语料」（覆盖）')
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
const HOME = process.env.PROBE_HOME;
process.env.USERPROFILE = HOME;
process.env.HOME = HOME;
const vscode = require('vscode');
const ext = require(process.env.PROBE_EXT);

function opens() { return vscode.__CALLS.filter((c) => c[0] === 'openTextDocument').length; }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

(async () => {
  ext.activate({ extensionPath: process.env.PROBE_ROOT, subscriptions: [] });
  await sleep(1400);
  fs.writeFileSync(process.env.PROBE_READY, 'ready');
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    await sleep(200);
    if (fs.existsSync(process.env.PROBE_DONE)) break;
  }
  console.log('OPENS=' + opens());
  console.log('PATHS=' + JSON.stringify(vscode.__CALLS.map((c) => c[1] || '')));
  process.exit(0);
})().catch((e) => { console.error('PROBE_FAIL', (e && e.stack) || e); process.exit(1); });
"""

try:
    import standalone
    import corpus  # noqa: F401  （确保语料模块可用）

    bubbles = []
    fallbacks = []
    orig_push = standalone.push_message
    orig_fallback = standalone._open_corpus_fallback
    standalone.push_message = lambda typ, txt, emo='idle': bubbles.append((typ, txt, emo))
    standalone._open_corpus_fallback = lambda path: (fallbacks.append(path), None)[1]
    try:
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        PORT = s.getsockname()[1]
        s.close()
        ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(PORT, ready),
                         daemon=True).start()
        check('C0 服务器起来了', ready.wait(timeout=5.0), 'port=%d' % PORT)

        stub_dir = os.path.join(TMP, 'node_modules', 'vscode')
        os.makedirs(stub_dir, exist_ok=True)
        io.open(os.path.join(stub_dir, 'index.js'), 'w', encoding='utf-8').write(VSCODE_STUB)

        outdir = os.path.join(TMP, 'out')
        rc = subprocess.run([NODE, TSC, os.path.join(REPO, 'src', 'extension.ts'),
                             '--module', 'commonjs', '--target', 'es2020',
                             '--skipLibCheck', '--outDir', outdir],
                            cwd=REPO, capture_output=True, text=True, timeout=300)
        emitted = os.path.join(outdir, 'extension.js')
        check('C1 从 src 现编译扩展', os.path.isfile(emitted),
              (rc.stdout or rc.stderr or '')[:300])

        if os.path.isfile(emitted):
            probe = os.path.join(TMP, 'probe.js')
            io.open(probe, 'w', encoding='utf-8').write(PROBE_JS)
            READY_MARK = os.path.join(TMP, 'ready.flag')
            DONE_MARK = os.path.join(TMP, 'done.flag')
            env = dict(os.environ)
            env['NODE_PATH'] = os.path.join(TMP, 'node_modules')
            env['PROBE_HOME'] = HOME
            env['PROBE_EXT'] = emitted
            env['PROBE_ROOT'] = REPO
            env['PROBE_READY'] = READY_MARK
            env['PROBE_DONE'] = DONE_MARK
            env['AIRI_STANDALONE_PORT'] = str(PORT)   # 两端都用这一个（0.3.48 修过）
            env['AIRI_PET_SETTINGS'] = SETTINGS

            proc = subprocess.Popen([NODE, probe], cwd=TMP, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            t0 = time.time()
            while not os.path.exists(READY_MARK) and time.time() - t0 < 25:
                time.sleep(0.1)
            check('C2 扩展 activate 完成', os.path.exists(READY_MARK),
                  '%.1fs' % (time.time() - t0))

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(
                'http://127.0.0.1:%d/settings' % PORT,
                data=json.dumps({'openCorpus': True}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST')
            with opener.open(req, timeout=5) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            seq = body.get('openCorpusSeq')
            check('C3 服务器受理了「编辑语料」', bool(body.get('ok')) and bool(seq),
                  repr(body))

            # worker 最多等 2.5s 回执；再给气泡一点时间
            deadline = time.time() + 6.0
            while time.time() < deadline:
                if any('VS Code 里打开了' in t for _ty, t, _e in bubbles):
                    break
                if fallbacks:
                    break
                time.sleep(0.05)

            io.open(DONE_MARK, 'w').write('done')
            try:
                out, _ = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            text = (out or b'').decode('utf-8', 'replace')

            opens = 0
            for line in text.splitlines():
                if line.startswith('OPENS='):
                    opens = int(line.split('=', 1)[1])

            check('C4 扩展真的打开了语料文件（只开一次）', opens == 1,
                  'opens=%d out=%r' % (opens, text[-260:]))
            check('C5 没有走 code 回落（否则会多开一个 VS Code 窗口）',
                  not fallbacks, 'fallbacks=%r' % (fallbacks,))
            check('C6 气泡说的是"在 VS Code 里打开了"',
                  any('VS Code 里打开了' in t for _ty, t, _e in bubbles),
                  'bubbles=%r' % (bubbles[-2:],))
            try:
                disk = json.load(io.open(SETTINGS, encoding='utf-8'))
            except Exception:
                disk = {}
            check('C7 新序号落到了设置文件里', disk.get('openCorpusSeq') == seq,
                  'seq=%r disk=%r' % (seq, disk.get('openCorpusSeq')))
            log_txt = ''
            try:
                log_txt = io.open(os.environ['AIRI_PET_LOG'], encoding='utf-8').read()
            except Exception:
                pass
            check('C8 回执被认领（日志里没有 late ack）', 'late ack' not in log_txt,
                  log_txt[-300:] if 'late ack' in log_txt else '')
    finally:
        standalone.push_message = orig_push
        standalone._open_corpus_fallback = orig_fallback
except Exception as exc:
    check('C 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(HERE, '_t050.txt'), 'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
