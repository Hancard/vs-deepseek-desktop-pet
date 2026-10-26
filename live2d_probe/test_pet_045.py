# -*- coding: utf-8 -*-
"""0.3.45 回归测试。

覆盖本轮修的 16 处（每条都在 0.3.44 的代码上坐实过触发路径）：

  A 段  standalone.py 的设置口径
        A1 `clamp_detect_sec(True)` → 1 秒（bool 是 int 子类）；'0x10'/'1_0'/'  '
           与扩展侧 `Number()` 口径相反
        A2 `2.5` 两边差 1 秒（Math.round 是 3，round 是 2）
  B 段  standalone.py 的 GET /settings
        B1 只读内存快照 → 手改设置文件后菜单显示旧值，再点 + 把用户改的值覆盖掉
  C 段  standalone.py 的 SSE 帧
        C1 `{"emotion": 1e999}` → 帧里写出 Infinity → 前端 JSON.parse 抛错，整条气泡消失
        C2 `{"type": null}` / 未知类型 → 200 已回，前端 switch 落空，静默无气泡
        C3 text 传对象/数字 → 帧里念出 [object Object]
  D 段  standalone.py 的「编辑语料」
        D1 落盘失败回滚后重算同一个 seq → 槽位被顶掉 → 同一文件被打开两次
  E 段  corpus.py 的占位符归一（0.3.44 引入的回归）
        E1 `{ 待定 }` 这类字面花括号被整条丢弃（0.3.43 是原样显示的）
  F 段  extension.ts
        F1 `readDetectIntervalMs` 与 `clamp_detect_sec` 口径不一致（0x10 / 2.5 / '  '）
        F2 `seq === lastCorpusOpenSeq` 吞掉"重置后恰好同号"的请求（不打开也不回执）
        F3 spawn ENOENT 后仍等满 8 秒并补一条误导性的 pywebview 诊断
  G 段  response_generator.py
        G1 `error_count: 1e999` 抛 OverflowError → 兜底说成"干净了，零 bug"
  H 段  ui.html
        H1 非 Live2D 时 zoneAt 恒返回 'body' → head/desk 台词永远触发不到
        H2 Live2D 半初始化失败后 CSS 角色不再显示 → 看不见、点不到、退不出
        H3 情绪变体图的异步回调不认 live2d.ready → PNG 垫在模型下面
        H4 成功的 POST 被后发请求作废 → 回滚到磁盘上已经不存在的值
        H5 GET 在防抖窗口内回来 → 把刚点的乐观值打回旧值
        H6 corpusOpenBusy 700ms < 后端 2.5s 回执窗口 → 慢回执时仍双开

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 每段包在自己的 try 里 —— 旧版没有的符号会抛 AttributeError/NameError，
    把后面整段吞掉（用例数突然变少就是信号）。
  · 抠源码用**花括号配对**（grab_fn），别用 "\\n    }\\n" 当结束锚点。
  · 断言源码时先剥注释：本轮注释里写着"不能是 xxx"，会误伤自己。
  · 覆盖 setTimeout 一律走 globalThis（函数声明会 hoist）。
  · 判据用副作用（HTTP 状态、队列内容、磁盘内容、node 打出来的值）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='airi_test045_')
# ⚠️ 必须在 import standalone **之前**设：否则测试会往生产日志/设置里写
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

REPO = Path(r'S:\My event\projects\vscode-anime-assistent')
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'

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


HTML = REPO / 'desktop_pet' / 'ui.html'
EXTS = REPO / 'src' / 'extension.ts'
STANDALONE = REPO / 'desktop_pet' / 'standalone.py'
CORPUS = REPO / 'python_backend' / 'corpus.py'
RGEN = REPO / 'python_backend' / 'response_generator.py'

HSRC = io.open(str(HTML), encoding='utf-8').read()
ESRC = io.open(str(EXTS), encoding='utf-8').read()
SSRC = io.open(str(STANDALONE), encoding='utf-8').read()
PSRC = io.open(str(CORPUS), encoding='utf-8-sig').read()


def grab_fn(src, needle):
    """按花括号配对抠出一个函数体（比固定结束锚点稳）。"""
    i = src.find(needle)
    if i < 0:
        return ''
    j = src.find('{', i)
    if j < 0:
        return ''
    depth = 0
    k = j
    while k < len(src):
        ch = src[k]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    return ''


def strip_comments(code):
    """去掉整行 `//` 注释 —— 断言源码前必须做，否则注释里的反例原文会误伤。"""
    return '\n'.join(ln for ln in code.splitlines()
                     if not ln.strip().startswith('//'))


def grab_until_def(src, needle):
    """从 needle 开始截到**下一个**行首 `def ` —— 给带花括号的 docstring 用。

    ⚠️ 不能对这类函数用 grab_fn：它的 docstring 里就写着 `{ line }`，
    按花括号配对会从那个 `{` 开始数，切出来的东西根本不是函数体
    （本项目踩过：断言于是恒假，看起来像"代码没改"）。
    """
    i = src.find(needle)
    if i < 0:
        return ''
    j = src.find('\ndef ', i + len(needle))
    return src[i:j] if j > 0 else src[i:]


def ts_to_js(s):
    """把抠出来的 TS 函数体转成能跑的 JS（最小改写）。"""
    s = s.replace('(seq: number): string', '(seq)')
    s = s.replace('(n: number): number', '(n)')
    s = s.replace('(raw: unknown): number | undefined', '(raw)')
    s = s.replace('(): number | undefined', '()')
    s = s.replace('(sec: number, prev: number)', '(sec, prev)')
    s = s.replace('(prev: number, seq: number)', '(prev, seq)')
    s = s.replace('(seq: number)', '(seq)')
    s = re_sub_ret(s)
    s = s.replace('const ', 'var ').replace('let ', 'var ')
    # ⚠️ 变量声明的类型注解（`let n: number;`）不在"参数列表"里，上面那条
    #    正则管不到 —— 漏了就是 node 的 SyntaxError: Unexpected token ':'。
    s = re.sub(r'(\bvar\s+\w+)\s*:\s*[A-Za-z_$][\w$]*(?:<[^>]*>)?\s*(?=[;,=)])',
               r'\1', s)
    return s


def re_sub_ret(s):
    import re
    s = re.sub(r'\)\s*:\s*[^{;]*\{', ') {', s)      # 返回类型注解
    s = re.sub(r'\bas\s+(?:\{[^{}]*\}|\w+)', '', s)  # as T / as {...}
    return s


def run_node(js, timeout=60):
    d = Path(tempfile.mkdtemp(prefix='airi045_'))
    f = d / 't.js'
    f.write_text(js, encoding='utf-8')
    try:
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout, r.stderr, r.returncode
    except Exception as exc:
        return '', '%s: %s' % (type(exc).__name__, exc), -1


def kv_of(out):
    d = {}
    for line in (out or '').strip().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip()
    return d


def http_post(url, obj, timeout=8):
    body = json.dumps(obj).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b''
    except Exception as exc:
        return None, ('%s: %s' % (type(exc).__name__, exc)).encode()


# ===========================================================================
# A 段：设置值的口径
# ===========================================================================
section('A1 clamp_detect_sec 必须拒绝布尔/非十进制字符串')

try:
    import standalone  # noqa: E402

    c = standalone.clamp_detect_sec
    check('A1-1 True 不能变成 1 秒（bool 是 int 子类，旧代码 float(True)=1.0）',
          c(True) == standalone.DETECT_DEFAULT_SEC, 'got=%r' % (c(True),))
    check('A1-2 False 也一样',
          c(False) == standalone.DETECT_DEFAULT_SEC, 'got=%r' % (c(False),))
    check('A1-3 "0x10" 不算数字（旧代码 float 抛异常 → 30，扩展 Number 得 16）',
          c('0x10') == standalone.DETECT_DEFAULT_SEC, 'got=%r' % (c('0x10'),))
    check('A1-4 "1_0" 不算数字（Python 的 float 认下划线，JS 不认）',
          c('1_0') == standalone.DETECT_DEFAULT_SEC, 'got=%r' % (c('1_0'),))
    check('A1-5 纯空白串不算数字',
          c('  ') == standalone.DETECT_DEFAULT_SEC, 'got=%r' % (c('  '),))
    check('A1-6 有 _as_number 这个统一入口', hasattr(standalone, '_as_number'))
    check('A1-7 inf / nan / None 都是"不可用"',
          c(float('inf')) == 30 and c(float('nan')) == 30 and c(None) == 30,
          'got=%r/%r/%r' % (c(float('inf')), c(float('nan')), c(None)))
except Exception as exc:
    check('A1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A2 扩展与 Python 对同一份设置必须给出同一个秒数')

try:
    fn_parse = grab_fn(ESRC, 'function parseDetectSec(')
    fn_round = grab_fn(ESRC, 'function roundHalfEven(')
    fn_read = grab_fn(ESRC, 'function readDetectIntervalMs(')
    check('A2-0 三个函数都抠到了',
          bool(fn_parse) and bool(fn_round) and bool(fn_read),
          'len=%d/%d/%d' % (len(fn_parse), len(fn_round), len(fn_read)))

    import re
    consts = '\n'.join(re.findall(r'const (?:NUM_LITERAL_RE|DETECT_\w+) = [^;]+;',
                                  ESRC))
    check('A2-0b 抠到模块级常量（只抠函数会 ReferenceError）',
          'NUM_LITERAL_RE' in consts and 'DETECT_DEFAULT_SEC' in consts,
          'consts=%r' % consts[:160])

    # ⚠️ 同一份样例同时喂给 Python 与 JS，比对**结果必须相等**。
    CASES = [None, '', [], 0, False, True, 'abc', '0x10', '1_0', '  ', {},
             5, '5', ' 5 ', 2.5, 3.5, 999, 0.4, '1e1', -3]
    js = r"""
var FILE = '{}';
var fs = { readFileSync: function () { return FILE; } };
var PET_SETTINGS_PATH = 'x';
__CONSTS__
__PARSE__
__ROUND__
__READ__
var cases = CASES_JSON;
var out = [];
for (var i = 0; i < cases.length; i++) {
  FILE = JSON.stringify({ detectIntervalSec: cases[i] });
  var r = readDetectIntervalMs();
  out.push(i + '->' + (r === undefined ? 'undef' : r));
}
console.log(out.join('\n'));
""".replace('__CONSTS__', consts)\
   .replace('__PARSE__', ts_to_js(fn_parse))\
   .replace('__ROUND__', ts_to_js(fn_round))\
   .replace('__READ__', ts_to_js(fn_read))\
   .replace('CASES_JSON', json.dumps(CASES))
    out, err, rc = run_node(js)
    check('A2-1 node 跑通（TS→JS 转换没把函数削没）',
          rc == 0 and 'detectIntervalSec' in js, 'rc=%d err=%s' % (rc, err[:300]))
    got = {}
    for line in (out or '').strip().splitlines():
        if '->' in line:
            k, v = line.rsplit('->', 1)
            got[int(k)] = v

    bad = []
    for i, v in enumerate(CASES):
        py = standalone.clamp_detect_sec(v) * 1000
        js_val = got.get(i)
        if js_val != str(py):
            bad.append('%r: js=%r py=%r' % (v, js_val, py))
    check('A2-2 20 个样例两边完全一致（关键：0x10 / 1_0 / 2.5 / "  "）',
          not bad, '; '.join(bad[:6]))
    check('A2-3 2.5 必须落在 2 秒（银行家舍入，不是 Math.round 的 3）',
          got.get(CASES.index(2.5)) == '2000', 'got=%r' % got.get(CASES.index(2.5)))
    check('A2-4 3.5 落在 4 秒（.5 取偶，3 是奇数 → 进 4）',
          got.get(CASES.index(3.5)) == '4000', 'got=%r' % got.get(CASES.index(3.5)))
    check('A2-5 字段非法 → 默认 30 秒（不是 undefined 保持当前）',
          got.get(CASES.index('abc')) == '30000'
          and got.get(CASES.index(True)) == '30000',
          'abc=%r true=%r' % (got.get(CASES.index('abc')),
                              got.get(CASES.index(True))))
    check('A2-6 0 与负数按 [1,60] 夹（与 Python 一致，不是 30）',
          got.get(CASES.index(0)) == '1000' and got.get(CASES.index(-3)) == '1000',
          '0=%r -3=%r' % (got.get(CASES.index(0)), got.get(CASES.index(-3))))

    # JSON 半截 → undefined（保持当前周期），与"字段非法"区分开
    js2 = r"""
var FILE = '{"detectIntervalSec": 5';
var fs = { readFileSync: function () { return FILE; } };
var PET_SETTINGS_PATH = 'x';
__CONSTS__
__PARSE__
__ROUND__
__READ__
var r = readDetectIntervalMs();
console.log('R=' + (r === undefined ? 'undef' : r));
""".replace('__CONSTS__', consts)\
   .replace('__PARSE__', ts_to_js(fn_parse))\
   .replace('__ROUND__', ts_to_js(fn_round))\
   .replace('__READ__', ts_to_js(fn_read))
    out2, err2, rc2 = run_node(js2)
    check('A2-7 JSON 半截时返回 undefined（不因为编辑器正在写就改周期）',
          rc2 == 0 and kv_of(out2).get('R') == 'undef',
          'rc=%d out=%r' % (rc2, (out2 or err2)[:160]))
except Exception as exc:
    check('A2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# B 段：GET /settings 必须看磁盘
# ===========================================================================
section('B1 settings_payload 要先把磁盘读进内存')

try:
    spath = os.environ['AIRI_PET_SETTINGS']
    with io.open(spath, 'w', encoding='utf-8') as fh:
        json.dump({'detectIntervalSec': 5, 'openCorpusSeq': 3}, fh)
    standalone.settings.update(detectIntervalSec=30, openCorpusSeq=0)
    p = standalone.settings_payload()
    check('B1-1 手改成 5 秒后，payload 立刻是 5（旧代码给内存里的 30）',
          p.get('detectIntervalSec') == 5, 'got=%r' % (p.get('detectIntervalSec'),))
    check('B1-2 内存也一起对齐了（否则下一次 POST 用旧基准算）',
          standalone.settings.get('detectIntervalSec') == 5,
          'mem=%r' % (standalone.settings.get('detectIntervalSec'),))
    check('B1-3 序号同样以磁盘为准',
          p.get('openCorpusSeq') == 3, 'got=%r' % (p.get('openCorpusSeq'),))

    # 文件坏掉的那一刻：什么都不做，保持当前生效值
    with io.open(spath, 'w', encoding='utf-8') as fh:
        fh.write('{"detectIntervalSec": 5')
    p2 = standalone.settings_payload()
    check('B1-4 半截文件不能把内存里的 5 改回 30（_settings_from_disk 返回 None）',
          p2.get('detectIntervalSec') == 5,
          'got=%r' % (p2.get('detectIntervalSec'),))
    # 复原
    with io.open(spath, 'w', encoding='utf-8') as fh:
        json.dump({'detectIntervalSec': 30, 'openCorpusSeq': 0}, fh)
    standalone.settings.update(detectIntervalSec=30, openCorpusSeq=0)
except Exception as exc:
    check('B1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# C 段：SSE 帧必须是合法 JSON
# ===========================================================================
section('C1 push_message 的三个字段全部归一化')

try:
    def last_frame():
        return json.loads(standalone.sse_queue[-1][1])

    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', 'hi', float('inf'))
    # json.loads 默认接受 Infinity —— 必须用严格的解析器才判得出真假
    raw = standalone.sse_queue[-1][1]
    check('C1-1 帧里不许出现 Infinity / NaN 字面量（前端 JSON.parse 会抛）',
          'Infinity' not in raw and 'NaN' not in raw, 'raw=%r' % raw[:120])
    check('C1-2 emotion 归一成 idle', last_frame()['payload']['emotion'] == 'idle',
          'got=%r' % last_frame()['payload']['emotion'])

    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', 'hi', 123)
    check('C1-3 数字 emotion 也要变成 idle（否则前端拿它当类名拼）',
          last_frame()['payload']['emotion'] == 'idle')

    standalone.sse_queue.clear()
    standalone.push_message(None, 'hi')
    check('C1-4 type=None → chatMessage（旧代码原样写 null，前端第一行就 return）',
          last_frame()['type'] == 'chatMessage', 'got=%r' % last_frame()['type'])

    standalone.sse_queue.clear()
    standalone.push_message(1e999, 'hi')
    check('C1-5 type 是数字 → chatMessage',
          last_frame()['type'] == 'chatMessage', 'got=%r' % last_frame()['type'])

    standalone.sse_queue.clear()
    standalone.push_message('noSuchType', 'hi')
    check('C1-6 未知类型 → chatMessage（前端 switch 没有这个名字，会静默丢掉）',
          last_frame()['type'] == 'chatMessage')

    standalone.sse_queue.clear()
    standalone.push_message('errorAlert', 'hi', 'angry')
    check('C1-7 合法类型/情绪一个都不动',
          last_frame()['type'] == 'errorAlert'
          and last_frame()['payload']['emotion'] == 'angry')

    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', {'a': 1})
    check('C1-8 text 传对象 → 序列化成文本，不是 [object Object]',
          last_frame()['payload']['text'] == '{"a": 1}',
          'got=%r' % last_frame()['payload']['text'])

    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', None)
    check('C1-9 text=None → 空串（不是 null）',
          last_frame()['payload']['text'] == '',
          'got=%r' % last_frame()['payload']['text'])

    code = strip_comments(grab_fn(SSRC, 'def push_message('))
    check('C1-10 归一化写在唯一出口里（type/emotion/text 三个都在）',
          '_as_msg_type' in code and '_as_emotion' in code and '_as_text' in code,
          'code=%r' % code[:200])
    check('C1-11 用 allow_nan=False 把"帧必须能 JSON.parse"变成硬约束',
          'allow_nan=False' in code)
except Exception as exc:
    check('C1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# 起真实服务器（D 段要用）
# ===========================================================================
BASE = None
try:
    for _p in range(22200, 22300):
        _ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(_p, _ready),
                         daemon=True).start()
        if _ready.wait(3):
            BASE = _p
            break
    check('S0 测试服务器已起', BASE is not None, 'port=%r' % BASE)
except Exception as exc:
    check('S0 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D1 落盘失败后重算同一个 seq → 槽位被顶掉（同一文件开两次）')

try:
    if BASE:
        import corpus as _corpus_mod  # noqa: E402

        real_save = standalone.save_settings
        real_fallback = standalone._open_corpus_fallback
        real_timeout = standalone.CORPUS_OPEN_ACK_TIMEOUT
        real_ensure = _corpus_mod.ensure_corpus_file
        seqs = []
        try:
            # ① 落盘必失败；② 不真的去拉 code / 打开系统程序；
            # ③ 回执超时缩到 0.2s，别让测试等满 2.5s×N
            standalone.save_settings = lambda new: False
            standalone._open_corpus_fallback = lambda path: None
            standalone.CORPUS_OPEN_ACK_TIMEOUT = 0.2
            _corpus_mod.ensure_corpus_file = lambda: os.path.join(TMP, 'c.jsonc')
            for _ in range(2):
                st, body = http_post('http://127.0.0.1:%d/settings' % BASE,
                                     {'openCorpus': True})
                try:
                    seqs.append(json.loads(body).get('openCorpusSeq'))
                except Exception:
                    seqs.append(None)
                time.sleep(0.05)
        finally:
            standalone.save_settings = real_save
            standalone._open_corpus_fallback = real_fallback
            standalone.CORPUS_OPEN_ACK_TIMEOUT = real_timeout
            _corpus_mod.ensure_corpus_file = real_ensure
        check('D1-1 两次请求都拿到了序号', all(isinstance(s, int) for s in seqs),
              'seqs=%r' % (seqs,))
        check('D1-2 两次的序号必须**不同**（旧代码都是 0+1=1，槽位互相顶掉）',
              len(seqs) == 2 and seqs[0] != seqs[1], 'seqs=%r' % (seqs,))
        check('D1-3 第二个严格更大（序号只需单调，不必等于磁盘值）',
              len(seqs) == 2 and isinstance(seqs[0], int)
              and isinstance(seqs[1], int) and seqs[1] > seqs[0],
              'seqs=%r' % (seqs,))
        check('D1-4 有 _corpus_seq_floor 这个只增不减的地板',
              isinstance(getattr(standalone, '_corpus_seq_floor', None), int),
              'got=%r' % (getattr(standalone, '_corpus_seq_floor', None),))
    else:
        skip('D1', '测试服务器未起来')
except Exception as exc:
    check('D1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# E 段：corpus.py 的占位符归一
# ===========================================================================
section('E1 字面花括号不许被当成占位符')

try:
    import corpus  # noqa: E402

    def kept(scene, text):
        got, _dropped = corpus._clean_scene_lines(scene, [text])
        return got or []

    check('E1-1 「备注：{ 待定 }」要保留（0.3.44 把它整条丢了）',
          kept('all_clear', '备注：{ 待定 }') == ['备注：{ 待定 }'],
          'got=%r' % (kept('all_clear', '备注：{ 待定 }'),))
    check('E1-2 「这个 { 名字 } 我先记下了」要保留',
          kept('all_clear', '这个 { 名字 } 我先记下了') == ['这个 { 名字 } 我先记下了'])
    check('E1-3 代码片段「if (a > b) { return }」要保留',
          len(kept('file_scan', '代码里写 if (a > b) { return } 不行')) == 1)
    check('E1-4 「第 { line } 行」仍然要归一成 {line}（0.3.44 的修复不能丢）',
          kept('syntax_error', '第 { line } 行附近') == ['第 {line} 行附近'],
          'got=%r' % (kept('syntax_error', '第 { line } 行附近'),))
    check('E1-5 「{ file }」同样归一',
          kept('file_scan', '在 { file } 里') == ['在 {file} 里'],
          'got=%r' % (kept('file_scan', '在 { file } 里'),))
    check('E1-6 已知名字但在**本场景**白名单外 → 仍然丢弃（与 {count} 同口径）',
          kept('all_clear', '还有 { count } 个') == [],
          'got=%r' % (kept('all_clear', '还有 { count } 个'),))
    # 归一后的形态必须真的能被替换（不能只断言"字符串对了"）
    import response_generator as rg  # noqa: E402
    corpus._cache.update(key=None, scenes={}, clicks={}, error=None,
                         dropped=0, err_logged=None)
    with io.open(corpus.CORPUS_PATH, 'w', encoding='utf-8') as fh:
        json.dump({'scenes': {'syntax_error': ['第 { line } 行']}}, fh,
                  ensure_ascii=False)
    txt = rg.pick_corpus('syntax_error', {'line': 7, 'language': 'python'})
    check('E1-7 归一后的 {line} 能被真的替换（不是原样念出来）',
          txt == '第 7 行', 'txt=%r' % (txt,))

    code = grab_until_def(PSRC, 'def _norm_placeholders(')
    check('E1-8 只归一已知占位符名（_ALL_PLACEHOLDER_NAMES）',
          '_ALL_PLACEHOLDER_NAMES' in code, 'code=%r' % code[:200])
except Exception as exc:
    check('E1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# F 段：extension.ts
# ===========================================================================
section('F1 「编辑语料」用 (序号 + mtime) 当请求身份')

try:
    body = grab_fn(ESRC, 'async function openCorpusIfRequested(')
    check('F1-0 抠到 openCorpusIfRequested', bool(body), 'len=%d' % len(body))
    code = strip_comments(body)
    check('F1-1 判据是签名比较（不再是 `seq === lastCorpusOpenSeq`）',
          'sig === lastCorpusOpenSig' in code
          and 'seq === lastCorpusOpenSeq' not in code, 'code=%r' % code[:220])
    check('F1-2 仍然要挡 0 与 NaN',
          '!Number.isFinite(seq)' in code and 'seq <= 0' in code)
    check('F1-3 有 corpusOpenSig 这个身份函数',
          bool(grab_fn(ESRC, 'function corpusOpenSig(')))
    check('F1-4 启动时把当前签名记为"已处理"（重启后不重放上一次请求）',
          'lastCorpusOpenSig = corpusOpenSig(' in ESRC)

    sig_fn = grab_fn(ESRC, 'function corpusOpenSig(')
    js = r"""
var MTIME = 100;
var fs = { statSync: function () { return { mtimeMs: MTIME }; } };
var PET_SETTINGS_PATH = 'x';
__SIG__
console.log('A=' + corpusOpenSig(1));
MTIME = 100;
console.log('B=' + corpusOpenSig(1));
MTIME = 200;
console.log('C=' + corpusOpenSig(1));
console.log('D=' + corpusOpenSig(2));
""".replace('__SIG__', ts_to_js(sig_fn))
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('F1-5 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:200]))
    check('F1-6 同一序号 + 同一 mtime → 同一个签名（挡住重复的 watch 事件）',
          kv.get('A') == kv.get('B') and kv.get('A') == '1@100',
          'A=%r B=%r' % (kv.get('A'), kv.get('B')))
    check('F1-7 序号相同但 mtime 变了 → 签名不同（放过"重置后恰好同号"）',
          kv.get('C') != kv.get('A'), 'A=%r C=%r' % (kv.get('A'), kv.get('C')))
    check('F1-8 序号变了 → 签名也不同', kv.get('D') != kv.get('C'))
except Exception as exc:
    check('F1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('F2 spawn 失败后不许再补一条误导性的 pywebview 诊断')

try:
    code = strip_comments(ESRC)
    check('F2-1 有 spawnFailed 这个标记', 'spawnFailed' in code)
    check('F2-2 error 事件里把它置真', 'spawnFailed = true' in code)
    check('F2-3 等待循环里提前退出（旧代码照跑满 8 秒）',
          'if (spawnFailed)' in code)
except Exception as exc:
    check('F2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# G 段：response_generator 的 error_count
# ===========================================================================
section('G1 error_count=1e999 不许让 generate_response 抛异常')

try:
    import response_generator as rg  # noqa: E402

    ctx = {'trigger': 'file_scan', 'error_count': float('inf'),
           'file': 'a.py', 'language': 'python',
           'sample_errors': [{'line': 3, 'message': 'x'}]}
    try:
        r = rg.generate_response(ctx)
        raised = None
    except Exception as exc:
        r, raised = None, exc
    check('G1-1 不抛异常（旧代码 int(inf) → OverflowError 从入口穿出）',
          raised is None, 'raised=%r' % (raised,))
    text = ((r or {}).get('payload') or {}).get('text', '')
    check('G1-2 有台词（异常路径下整条播报会变成空气泡）', bool(text),
          'text=%r' % (text or ''))
    # ⚠️ 不断言"不说干净了"：inf 收敛成 0 是**既有口径**（/push 侧的 _as_int
    # 同样给 0），本轮改的是"不许抛"，不是猜它到底几个错误。
    check('G1-3 回写的 error_count 是有限的 0（下游 str>int 比较不会再炸）',
          ctx.get('error_count') == 0, 'got=%r' % (ctx.get('error_count'),))
    src = strip_comments(io.open(str(RGEN), encoding='utf-8-sig').read())
    check('G1-4 源码里那一处 int() 连 OverflowError 一起兜了',
          'except (TypeError, ValueError, OverflowError):\n        error_count = 0'
          in src, 'not found')
except Exception as exc:
    check('G1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# H 段：ui.html
# ===========================================================================
section('H1 非 Live2D 时点击分区要按角色元素算')

try:
    zf = grab_fn(HSRC, 'function zoneFromRatio(')
    zb = grab_fn(HSRC, 'function zoneBox(')
    za = grab_fn(HSRC, 'function zoneAt(')
    check('H1-0 三个函数都抠到了', bool(zf) and bool(zb) and bool(za),
          'len=%d/%d/%d' % (len(zf), len(zb), len(za)))
    code = strip_comments(za)
    check('H1-1 不再"非 Live2D 就 return body"（关键）',
          "return 'body'" not in code, 'code=%r' % code[:200])
    check('H1-2 改走 zoneBox()', 'zoneBox()' in code)

    js = r"""
var ZONE_HEAD_MAX = 0.45, ZONE_DESK_MIN = 0.62;
var live2d = { ready: false, app: null };
var FB = null;
function getBoundingClientRect() { return { top: 100, height: 100 }; }
function fallbackCharacterEl() { return FB; }
__ZONE_FROM__
__ZONE_BOX__
__ZONE_AT__
var out = [];
// ① 非 Live2D，有 CSS 角色（外接框 100~200）
FB = { getBoundingClientRect: getBoundingClientRect };
out.push('a' + zoneAt(110));   // ry .1  → head
out.push('b' + zoneAt(150));   // ry .5  → body
out.push('c' + zoneAt(180));   // ry .8  → desk
// ② 非 Live2D 且没有角色元素 → 退化成 body（不能抛）
FB = null;
out.push('d' + zoneAt(110));
// ③ Live2D 就绪 → 用 canvas 的框（这里 0~500），同一个 clientY 结论不同
live2d.ready = true;
live2d.app = { canvas: { getBoundingClientRect:
  function () { return { top: 0, height: 500 }; } } };
out.push('e' + zoneAt(110));   // ry .22 → head
out.push('f' + zoneAt(340));   // ry .68 → desk
console.log('R=' + out.join(','));
""".replace('__ZONE_FROM__', ts_to_js(zf))\
   .replace('__ZONE_BOX__', ts_to_js(zb))\
   .replace('__ZONE_AT__', ts_to_js(za))
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('H1-3 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    check('H1-4 兜底角色上点头顶 → head（旧代码恒 body）',
          kv.get('R', '').startswith('ahead'), 'R=%r' % kv.get('R'))
    check('H1-5 点下半 → desk（旧代码恒 body）',
          ',cdesk' in (kv.get('R') or ''), 'R=%r' % kv.get('R'))
    check('H1-6 没有角色元素时不抛、退成 body',
          ',dbody' in (kv.get('R') or ''), 'R=%r' % kv.get('R'))
    check('H1-7 Live2D 就绪时仍按 canvas 的框算（不能把老路径改坏）',
          ',fdesk' in (kv.get('R') or '') and ',ehead' in (kv.get('R') or ''),
          'R=%r' % kv.get('R'))
except Exception as exc:
    check('H1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('H2 Live2D 半初始化失败后不许变成"没有角色"')

try:
    i = HSRC.find('live2d.failed = true;')
    seg = HSRC[i:i + 1800] if i >= 0 else ''
    check('H2-1 找到 catch 段', bool(seg))
    code = strip_comments(seg)
    check('H2-2 清场里把 cssChar 的 visible 加回来了（关键）',
          "cssChar.classList.add('visible')" in code, 'code=%r' % code[:300])
    check('H2-3 且仍在调用 showImageCharacter()（有图时由它接管）',
          'showImageCharacter()' in code)
    # 顺序：先加回 visible，再 showImageCharacter
    a = code.find("cssChar.classList.add('visible')")
    b = code.find('showImageCharacter()')
    check('H2-4 顺序正确（先放回 CSS 角色，再让图片接管）',
          0 <= a < b, 'a=%d b=%d' % (a, b))
except Exception as exc:
    check('H2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('H3 情绪变体图的回调要认 live2d.ready')

try:
    i = HSRC.find('var myEmotion = emotion;')
    seg = HSRC[i:i + 900] if i >= 0 else ''
    code = strip_comments(seg)
    check('H3-1 找到回调段', bool(code))
    check('H3-2 onload 里判了 ready（否则 PNG 会垫到模型下面）',
          code.count('live2d.ready') >= 2,
          'count=%d' % code.count('live2d.ready'))
    check('H3-3 情绪认领那份判断仍然在（0.3.42 的修复不能丢）',
          code.count('currentEmotion !== myEmotion') >= 2,
          'count=%d' % code.count('currentEmotion !== myEmotion'))
except Exception as exc:
    check('H3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('H4/H5 检测周期的两个竞态')

try:
    post = strip_comments(grab_fn(HSRC, 'function postDetectSec('))
    a = post.find('diskDetectSec = isFinite')
    b = post.find('if (seq !== settingsSeq) return;')
    check('H4-1 postDetectSec 里磁盘事实先于序号闸门（关键顺序）',
          0 <= a < b, 'a=%d b=%d' % (a, b))
    check('H4-2 落盘失败时不许更新基准（d.saved !== false 守卫）',
          'd.saved !== false' in post, 'post=%r' % post[:200])

    sync = strip_comments(grab_fn(HSRC, 'function syncDetectSec('))
    check('H5-1 有待发的点击时不覆盖界面值',
          'pendingDetectSec !== null' in sync, 'sync=%r' % sync[:220])
    check('H5-2 但基准照更新（它是磁盘事实）',
          'diskDetectSec = v' in sync)
except Exception as exc:
    check('H4/H5 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('H6 编辑语料的在途窗口必须盖住后端 2.5s 回执')

try:
    i = HSRC.find('setTimeout(function() { corpusOpenBusy = false; }')
    seg = HSRC[i:i + 60] if i >= 0 else ''
    check('H6-1 找到解锁定时器', bool(seg), 'seg=%r' % seg)
    ms = 0
    try:
        ms = int(seg.split('}, ', 1)[1].split(')', 1)[0])
    except Exception:
        ms = -1
    check('H6-2 窗口 >= 2500ms（后端 CORPUS_OPEN_ACK_TIMEOUT 是 2.5s）',
          ms >= 2500, 'ms=%r seg=%r' % (ms, seg))
    check('H6-3 后端那个常量还是 2.5（改小了这条断言就失效了）',
          abs(float(standalone.CORPUS_OPEN_ACK_TIMEOUT) - 2.5) < 0.001,
          'got=%r' % (standalone.CORPUS_OPEN_ACK_TIMEOUT,))
except Exception as exc:
    check('H6 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
out = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'test_pet_045_result.txt'), 'w', encoding='utf-8')
out.write('\n'.join(LINES) + '\n')
out.write('\nPASS=%d FAIL=%d\n' % (PASS, FAIL))
out.close()
print('\n'.join(LINES))
print('\nPASS=%d FAIL=%d' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
