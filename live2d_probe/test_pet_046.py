# -*- coding: utf-8 -*-
"""0.3.46 回归测试。

覆盖本轮修的 8 处：

  A 段  desktop_pet/watcher.py 与 standalone.py 的**本地回环请求**
        A1 watcher / _ping_ok 走 urllib.request.urlopen → 读 http_proxy，
           而 CPython 的 proxy_bypass 不认 Windows 上常见的 `127.*` 写法。
           实测：代理指向一个无人在听的端口时，默认 urlopen 到 127.0.0.1
           抛 URLError(WinError 10061)，而显式 ProxyHandler({}) 得 200
        A2 代码里不许再有裸 urlopen 残留（两处推送 + /ping 探测）
  B 段  desktop_pet/watcher.py 的推送签名
        B1 `last_signature = signature` 写在推送**之前** → 桌宠没起来时
           那条播报永久消失（除非错误内容再变）
  C 段  standalone.py / response_generator.py 的"布尔不是数量"
        C1 `_as_int(True)` / `_as_seq(True)` 应为 0
        C2 `{"error_count": true}` 不许把一条聊天念成 errorAlert
  D 段  standalone.py 的 _persist_locked
        D1 设置文件存在但读不出来时，一个字节都不许写（旧代码用
           load_settings() 的默认值把整份文件覆盖掉）
        D2 文件不存在时仍要能创建（别修过头）
  E 段  ui.html 的检测周期
        E1 `syncDetectSec` 的闸门必须同时覆盖"待发"和"在途"两种状态
        E2 运行时：POST 在途时 GET 回来，界面不许被打回旧值
  F 段  src/extension.ts 的面板单例
        F1 dispose 回调必须认领自己那个实例，不能无条件 `panel = undefined`
  G 段  desktop_pet/watcher.py 的单文件诊断
        G1 payload 要带 language（目录模式一直有，单文件模式漏了）

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 每段包在自己的 try 里 —— 旧版没有的符号会抛 AttributeError，
    把后面整段吞掉（用例数突然变少就是信号）。
  · 断言源码前先剥注释：本轮注释里写着"不能是 xxx"，会误伤自己。
  · 覆盖 setTimeout 要留原版引用，否则等微任务的代码自己也被劫持。
  · 判据用副作用（HTTP 状态、磁盘字节、node 打出来的值），不判"有没有这段代码"。
"""
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='airi_test046_')
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


HTML = REPO / 'desktop_pet' / 'ui.html'
EXTS = REPO / 'src' / 'extension.ts'
STANDALONE = REPO / 'desktop_pet' / 'standalone.py'
WATCHER = REPO / 'desktop_pet' / 'watcher.py'
RGEN = REPO / 'python_backend' / 'response_generator.py'

HSRC = io.open(str(HTML), encoding='utf-8').read()
ESRC = io.open(str(EXTS), encoding='utf-8').read()
SSRC = io.open(str(STANDALONE), encoding='utf-8-sig').read()
WSRC = io.open(str(WATCHER), encoding='utf-8').read()


def strip_comments(code):
    return '\n'.join(ln for ln in code.splitlines()
                     if not ln.strip().startswith('//'))


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


def run_node(js, timeout=60):
    d = Path(tempfile.mkdtemp(prefix='airi046_'))
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


# ===========================================================================
# A 段：本地回环请求必须绕开系统代理
# ===========================================================================
section('A1 本地回环请求不许被 http_proxy 劫持')

import http.server  # noqa: E402


class _Echo(http.server.BaseHTTPRequestHandler):
    hits = []

    def _ok(self):
        _Echo.hits.append(self.path)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _ok
    do_POST = _ok

    def log_message(self, *a):
        pass


_srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Echo)
_srv_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_local_url = 'http://127.0.0.1:%d/ping' % _srv_port

# 保存并改写代理环境变量：指向一个**确定**没人监听的端口
_PROXY_KEYS = ('http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY',
               'no_proxy', 'NO_PROXY')
_saved_env = {k: os.environ.get(k) for k in _PROXY_KEYS}
for _k in _PROXY_KEYS:
    os.environ.pop(_k, None)
os.environ['http_proxy'] = 'http://127.0.0.1:1'
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:1'

try:
    # 对照：默认 urlopen 必须失败（证明这个环境真的会劫持）
    baseline_ok = None
    try:
        with urllib.request.urlopen(_local_url, timeout=3) as r:
            baseline_ok = (r.status == 200)
    except Exception as exc:
        baseline_ok = False
        LINES.append('       baseline urlopen -> %s: %s'
                     % (type(exc).__name__, exc))
    check('A1-0 对照：默认 urlopen 确实被代理劫持（失败）', baseline_ok is False,
          'baseline_ok=%r' % (baseline_ok,))

    # ① watcher._LOCAL_OPENER
    try:
        import watcher  # noqa: E402
        got = None
        try:
            with watcher._LOCAL_OPENER.open(_local_url, timeout=3) as r:
                got = r.status
        except Exception as exc:
            got = '%s: %s' % (type(exc).__name__, exc)
        check('A1-1 watcher._LOCAL_OPENER 能直连 127.0.0.1', got == 200,
              'got=%r' % (got,))
    except AttributeError as exc:
        check('A1-1 watcher._LOCAL_OPENER 能直连 127.0.0.1', False,
              'AttributeError: %s' % exc)
    except Exception as exc:
        check('A1-1 watcher._LOCAL_OPENER 能直连 127.0.0.1', False,
              '%s: %s' % (type(exc).__name__, exc))

    # ② standalone._LOCAL_OPENER
    try:
        import standalone  # noqa: E402
        got = None
        try:
            with standalone._LOCAL_OPENER.open(_local_url, timeout=3) as r:
                got = r.status
        except Exception as exc:
            got = '%s: %s' % (type(exc).__name__, exc)
        check('A1-2 standalone._LOCAL_OPENER 能直连 127.0.0.1', got == 200,
              'got=%r' % (got,))
    except AttributeError as exc:
        check('A1-2 standalone._LOCAL_OPENER 能直连 127.0.0.1', False,
              'AttributeError: %s' % exc)
    except Exception as exc:
        check('A1-2 standalone._LOCAL_OPENER 能直连 127.0.0.1', False,
              '%s: %s' % (type(exc).__name__, exc))

    # ③ standalone._ping_ok 本身就是端到端判据（它自己会 new 请求）
    try:
        check('A1-3 _ping_ok 在代理不可用时仍能认出本机服务器',
              standalone._ping_ok(_srv_port) is True,
              'got=%r' % (standalone._ping_ok(_srv_port),))
    except Exception as exc:
        check('A1-3 _ping_ok 在代理不可用时仍能认出本机服务器', False,
              '%s: %s' % (type(exc).__name__, exc))

    # ④ 单文件模式的一次真实推送（POST）也要能到
    try:
        import standalone as _sa  # noqa: E402
        body = json.dumps({'trigger': 'all_clear', 'error_count': 0}).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:%d/push' % _srv_port, data=body,
            headers={'Content-Type': 'application/json'})
        with _sa._LOCAL_OPENER.open(req, timeout=3) as r:
            check('A1-4 POST 也能直连（不是只有 GET 特例）', r.status == 200,
                  'status=%r' % (r.status,))
    except Exception as exc:
        check('A1-4 POST 也能直连（不是只有 GET 特例）', False,
              '%s: %s' % (type(exc).__name__, exc))
finally:
    for _k, _v in _saved_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    try:
        _srv.shutdown()
    except Exception:
        pass


section('A2 代码里不许再有指向 127.0.0.1 的裸 urlopen')

_wcode = strip_comments(WSRC) if WSRC else ''
_scode_body = SSRC

try:
    # watcher：两处推送都必须是 _LOCAL_OPENER.open
    n_urlopen_w = len(re.findall(r'urllib\.request\.urlopen\s*\(', _wcode))
    n_opener_w = len(re.findall(r'_LOCAL_OPENER\.open\s*\(', _wcode))
    check('A2-1 watcher 没有裸 urlopen 调用', n_urlopen_w == 0,
          'urlopen=%d' % n_urlopen_w)
    check('A2-2 watcher 的两处推送都走 _LOCAL_OPENER', n_opener_w == 2,
          'opener=%d' % n_opener_w)

    # standalone：_ping_ok 不许再用 urlopen
    ping_fn = grab_fn(_scode_body, 'def _ping_ok(')
    check('A2-3 _ping_ok 内部没有 urllib.request.urlopen',
          'urllib.request.urlopen' not in ping_fn,
          'ping_fn 里仍有 urlopen')
    check('A2-4 _ping_ok 用的是 _LOCAL_OPENER',
          '_LOCAL_OPENER.open' in ping_fn,
          'ping_fn 里没有 _LOCAL_OPENER.open')

    # 公网下载（Cubism Core）**应该**经过代理 —— 别修过头
    _l2d = io.open(str(REPO / 'desktop_pet' / 'live2d_assets.py'),
                   encoding='utf-8').read()
    check('A2-5 Cubism Core 公网下载仍走默认 opener（不能被一起改掉）',
          'urllib.request.urlopen(req' in _l2d and '_LOCAL_OPENER' not in _l2d,
          '公网下载被误改')
except Exception as exc:
    check('A2 watcher/standalone 的 opener 口径', False,
          '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# B 段：watcher 的签名只在送达后提交
# ===========================================================================
section('B1 推送失败时签名不许被提交')


def _is_push_call(node):
    """这个 Call 是不是"往桌宠推一条"的那个调用。

    `_LOCAL_OPENER.open(...)` → Attribute(attr='open')；
    `urllib.request.urlopen(...)` / `urlopen(...)` → 旧写法。
    ⚠️ 等价 API 必须穷举：只认一种写法会让依赖它的判据退化成恒真/恒假。
    """
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in ('open', 'urlopen'):
        return True
    return isinstance(f, ast.Name) and f.id == 'urlopen'


def _is_sig_assign(node):
    """`last_signature = <signature|p_sig>` 这种提交语句。

    0.3.47 的值来自 pending 元组（`p_sig`），不是 `signature` —— 等价写法要穷举。
    """
    if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
        return False
    if node.value.id not in ('signature', 'p_sig'):
        return False
    return any(isinstance(t, ast.Name) and t.id == 'last_signature'
               for t in node.targets)


def _sig_assigns(fn_name):
    """返回 (有签名赋值的行号列表, 有 open/urlopen 调用的行号列表)。"""
    tree = ast.parse(WSRC)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            fn = node
            break
    if fn is None:
        return None, None
    assigns = [n.lineno for n in ast.walk(fn) if _is_sig_assign(n)]
    opens = [n.lineno for n in ast.walk(fn) if _is_push_call(n)]
    return sorted(assigns), sorted(opens)


def _sig_assigns_guarded(fn_name):
    """`last_signature = ...` 中**受推送异常保护**的那些行号。

    这才是"送达才提交"的精确判据：赋值被一个**包含 open/urlopen 调用**的 try
    覆盖，且在那个 open 之后 —— 推送抛异常时它执行不到。两种合法实现都命中：
      · 0.3.46：`try: ... open(...) ; last_signature = signature`（异常直接跳出）
      · 0.3.47：`try: ... open(...) except: pass else: last_signature = p_sig`
    0.3.45 及更早是"先提交、再推送"，赋值在 try 外面/前面 → 这里必然为空。
    """
    tree = ast.parse(WSRC)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            fn = node
            break
    if fn is None:
        return None
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        opens = [n.lineno for n in ast.walk(node) if _is_push_call(n)]
        if not opens:
            continue
        lo = min(opens)
        for inner in ast.walk(node):
            if _is_sig_assign(inner) and inner.lineno > lo:
                out.append(inner.lineno)
    return sorted(out)


try:
    # watch_directory：干净启动那条分支也会赋值（不推但要记住），
    # 所以判据是"至少有一处赋值在 open 之后"。
    a_d, o_d = _sig_assigns('watch_directory')
    check('B1-0 能定位 watch_directory 的签名赋值', a_d is not None,
          'AST 没找到函数')
    if a_d is not None and o_d:
        after = [ln for ln in a_d if ln > min(o_d)]
        check('B1-1 目录模式：签名赋值出现在推送之后（送达才提交）',
              len(after) >= 1,
              'assigns=%r opens=%r' % (a_d, o_d))
    else:
        check('B1-1 目录模式：签名赋值出现在推送之后（送达才提交）', False,
              'assigns=%r opens=%r' % (a_d, o_d))

    a_s, o_s = _sig_assigns('watch_single_file')
    if a_s is not None and o_s:
        after = [ln for ln in a_s if ln > min(o_s)]
        check('B1-2 单文件模式：签名赋值出现在推送之后', len(after) >= 1,
              'assigns=%r opens=%r' % (a_s, o_s))
    else:
        check('B1-2 单文件模式：签名赋值出现在推送之后', False,
              'assigns=%r opens=%r' % (a_s, o_s))

    # 0.3.47：上一条只看"行号在后面"，太粗。精确判据是**受推送异常保护** ——
    # 推送抛异常时那条赋值执行不到（无论放在 try 体末尾还是 else 分支）。
    # 0.3.45 及更早"先提交再推送"必然为空。
    guarded = _sig_assigns_guarded('watch_single_file')
    check('B1-2b 单文件模式：提交受推送异常保护（抛异常时不许提交）',
          bool(guarded), 'guarded_assigns=%r' % (guarded,))

    # 反向：不许有"推送前无条件提交"那种写法残留
    check('B1-3 没有 `if signature != last_signature: last_signature = signature` 的旧写法',
          not re.search(r'if\s+signature\s*!=\s*last_signature\s*:\s*\n\s*last_signature\s*=\s*signature',
                        _wcode),
          '旧写法还在')
except Exception as exc:
    check('B1 watcher 签名提交时机', False,
          '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# C 段：布尔不是数量
# ===========================================================================
section('C1 _as_int / _as_seq 必须拒绝布尔')

try:
    import standalone as sa  # noqa: E402

    cases = [
        ('_as_int(True)', sa._as_int(True), 0),
        ('_as_int(False)', sa._as_int(False), 0),
        ('_as_int(7)', sa._as_int(7), 7),
        ('_as_seq(True)', sa._as_seq(True), 0),
        ('_as_seq(False)', sa._as_seq(False), 0),
        ('_as_seq(42)', sa._as_seq(42), 42),
        ('_as_int(1e999)', sa._as_int(float('inf')), 0),   # 0.3.44 的老契约
    ]
    for label, got, want in cases:
        check('C1 %s == %r' % (label, want), got == want, 'got=%r' % (got,))
except Exception as exc:
    check('C1 _as_int/_as_seq 拒绝布尔', False,
          '%s: %s' % (type(exc).__name__, exc))


section('C2 {"error_count": true} 不许念成"有 1 个错误"')

try:
    import response_generator as rg  # noqa: E402

    r_true = rg.generate_response({
        'trigger': 'diagnostics', 'error_count': True,
        'language': 'python', 'sample_errors': [],
    })
    r_one = rg.generate_response({
        'trigger': 'diagnostics', 'error_count': 1,
        'language': 'python', 'sample_errors': [],
    })
    check('C2-1 error_count=true → chatMessage（不是 errorAlert）',
          isinstance(r_true, dict) and r_true.get('type') == 'chatMessage',
          'r=%r' % (r_true,))
    check('C2-2 error_count=1 → errorAlert（真数字不受影响）',
          isinstance(r_one, dict) and r_one.get('type') == 'errorAlert',
          'r=%r' % (r_one,))
    check('C2-3 两条分支的文本必须不同（说明真的走了不同分支）',
          (r_true or {}).get('payload', {}).get('text')
          != (r_one or {}).get('payload', {}).get('text'),
          'both=%r' % ((r_true or {}).get('payload'),))
except Exception as exc:
    check('C2 布尔不是错误数', False,
          '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# D 段：设置有文件但读不出来时，不许覆盖
# ===========================================================================
section('D1 设置文件坏掉时 _persist_locked 必须拒绝覆盖')

try:
    import standalone as sa2  # noqa: E402

    bad_path = sa2.SETTINGS_PATH
    # 一份"用户正在编辑"的坏文件：少了一个右括号
    bad_text = '{\n  "detectIntervalSec": 5,\n  "openCorpusSeq": 17\n'
    Path(bad_path).write_text(bad_text, encoding='utf-8')
    before = Path(bad_path).read_bytes()

    # 内存里保持一个"上一次成功读到的值"
    sa2.settings['detectIntervalSec'] = 5
    sa2.settings['openCorpusSeq'] = 17

    with sa2.settings_lock:
        ok = sa2._persist_locked('detectIntervalSec', 10)

    after = Path(bad_path).read_bytes()
    check('D1-1 坏文件时返回 False（不谎报成功）', ok is False, 'ok=%r' % (ok,))
    check('D1-2 磁盘内容逐字节不变（用户正在编辑的内容没被吞）',
          before == after,
          'before=%r after=%r' % (before[:60], after[:60]))
    check('D1-3 内存被回滚到调用前的值', sa2.settings['detectIntervalSec'] == 5,
          'mem=%r' % (sa2.settings['detectIntervalSec'],))
except Exception as exc:
    check('D1 坏文件不许覆盖', False, '%s: %s' % (type(exc).__name__, exc))


section('D2 文件不存在时仍要能创建（别修过头）')

try:
    import standalone as sa3  # noqa: E402

    fresh = os.path.join(TMP, 'fresh_dir', 'nested', 'settings.json')
    old_path = sa3.SETTINGS_PATH
    sa3.SETTINGS_PATH = fresh
    try:
        sa3.settings['detectIntervalSec'] = 30
        sa3.settings['openCorpusSeq'] = 0
        with sa3.settings_lock:
            ok = sa3._persist_locked('detectIntervalSec', 12)
        check('D2-1 首次落盘返回 True', ok is True, 'ok=%r' % (ok,))
        got = json.loads(Path(fresh).read_text(encoding='utf-8')) \
            if Path(fresh).is_file() else None
        check('D2-2 文件内容是新值', isinstance(got, dict)
              and got.get('detectIntervalSec') == 12, 'got=%r' % (got,))
        check('D2-3 另一个键被保留（不是只写一个键）',
              isinstance(got, dict) and 'openCorpusSeq' in got,
              'got=%r' % (got,))
    finally:
        sa3.SETTINGS_PATH = old_path
except Exception as exc:
    check('D2 文件不存在时能创建', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# E 段：ui.html 的检测周期
# ===========================================================================
section('E1 syncDetectSec 的闸门要覆盖"待发 + 在途"两种状态')

try:
    _h = strip_comments(HSRC)
    fn_sync = grab_fn(_h, 'function syncDetectSec(')
    fn_post = grab_fn(_h, 'function postDetectSec(')
    check('E1-0 两个函数都能抠出来', bool(fn_sync) and bool(fn_post),
          'sync=%d post=%d' % (len(fn_sync), len(fn_post)))

    check('E1-1 syncDetectSec 的判据包含 pendingDetectSec',
          'pendingDetectSec !== null' in fn_sync, fn_sync[:80])
    check('E1-2 syncDetectSec 的判据包含 postInFlight > 0',
          'postInFlight > 0' in fn_sync,
          '旧代码只判 pendingDetectSec —— POST 在途时 GET 会把界面打回旧值')
    check('E1-3 postDetectSec 里有在途计数 +1',
          'postInFlight++' in fn_post, fn_post[:80])
    check('E1-4 postDetectSec 里有统一的收尾 -1',
          'postInFlight--' in fn_post, fn_post[:80])
except Exception as exc:
    check('E1 检测周期闸门', False, '%s: %s' % (type(exc).__name__, exc))


section('E2 运行时：POST 在途时 GET 回来不许打回旧值')

try:
    _h2 = strip_comments(HSRC)
    fn_clamp = grab_fn(_h2, 'function clampDetect(')
    fn_labels = grab_fn(_h2, 'function renderDetectLabels(')
    fn_failed = grab_fn(_h2, 'function detectFailed(')
    fn_set = grab_fn(_h2, 'function setDetectSec(')
    fn_post2 = grab_fn(_h2, 'function postDetectSec(')
    fn_sync2 = grab_fn(_h2, 'function syncDetectSec(')

    js = r'''
var SETTINGS_URL = '/settings';
var detectSec = 30, detectMin = 1, detectMax = 60;
var settingsSeq = 0, detectPostTimer = null, pendingDetectSec = null;
var postInFlight = 0, diskDetectSec = 30;
var bubbles = [];

var realSetTimeout = globalThis.setTimeout;
var pendingFetch = [];   // {url, resolve, reject}

// renderDetectLabels 会摸 DOM（真实页面里是 #tickValue / #tickChips）
globalThis.document = {
  getElementById: function() { return null; },
  querySelectorAll: function() { return []; }
};

globalThis.fetch = function(url, opts) {
  return new Promise(function(res, rej) {
    pendingFetch.push({url: url, opts: opts, res: res, rej: rej});
  });
};

''' + fn_clamp + '\n' + fn_labels + '\n' + fn_failed + '\n' + fn_set + '\n' + fn_post2 + '\n' + fn_sync2 + r'''

function showBubble(t) { bubbles.push(t); }
function repushMenuRegion() {}

// 防抖那一次立刻执行，其余 setTimeout 走原版（后面要等微任务）
globalThis.setTimeout = function(fn, ms) {
  if (ms === 150) { fn(); return 0; }
  return realSetTimeout(fn, ms);
};

globalThis.runScenario = async function() {
  // ① 点一下「+」：30 → 31。防抖立即触发 → 发出 POST（挂起）
  setDetectSec(31);
  var afterClick = detectSec;
  var postInflight = postInFlight;
  var pendingCleared = (pendingDetectSec === null);

  // ② 立刻右键（syncDetectSec）：GET 也挂起
  syncDetectSec();

  var getReq = null, postReq = null;
  for (var i = 0; i < pendingFetch.length; i++) {
    if (pendingFetch[i].opts && pendingFetch[i].opts.method === 'POST') postReq = pendingFetch[i];
    else getReq = pendingFetch[i];
  }

  // ③ 让 GET 先回来，且返回**旧值** 30（磁盘上还没落盘）
  if (getReq) {
    getReq.res({json: function() {
      return Promise.resolve({detectIntervalSec: 30, min: 1, max: 60, openCorpusSeq: 0});
    }});
  }
  await new Promise(function(r) { realSetTimeout(r, 20); });
  var afterGet = detectSec;

  // ④ 再让 POST 回来（落盘成功 = 31）
  if (postReq) {
    postReq.res({json: function() {
      return Promise.resolve({detectIntervalSec: 31, min: 1, max: 60, saved: true});
    }});
  }
  await new Promise(function(r) { realSetTimeout(r, 20); });
  var afterPost = detectSec;
  var inflightEnd = postInFlight;

  return {
    afterClick: afterClick, postInflight: postInflight,
    pendingCleared: pendingCleared, afterGet: afterGet,
    afterPost: afterPost, inflightEnd: inflightEnd,
    hasGet: !!getReq, hasPost: !!postReq
  };
};

runScenario().then(function(r) {
  console.log('afterClick=' + r.afterClick);
  console.log('postInflight=' + r.postInflight);
  console.log('pendingCleared=' + r.pendingCleared);
  console.log('afterGet=' + r.afterGet);
  console.log('afterPost=' + r.afterPost);
  console.log('inflightEnd=' + r.inflightEnd);
  console.log('hasGet=' + r.hasGet);
  console.log('hasPost=' + r.hasPost);
}).catch(function(e) { console.log('ERR=' + (e && e.stack || e)); });
'''
    out, err, rc = run_node(js, timeout=60)
    kv = kv_of(out)
    LINES.append('       node rc=%s out=%r' % (rc, out.strip()[:300]))
    if err.strip():
        LINES.append('       node stderr=%r' % err.strip()[:300])

    check('E2-0 场景跑起来了（没抛异常）',
          kv.get('hasGet') == 'true' and kv.get('hasPost') == 'true',
          'kv=%r' % (kv,))
    check('E2-1 点击后界面立刻是 31（乐观值）',
          kv.get('afterClick') == '31', 'got=%r' % kv.get('afterClick'))
    check('E2-2 防抖到点后 pendingDetectSec 已清空（这正是漏洞窗口）',
          kv.get('pendingCleared') == 'true', 'got=%r' % kv.get('pendingCleared'))
    check('E2-3 POST 已发出、在途计数为 1',
          kv.get('postInflight') == '1', 'got=%r' % kv.get('postInflight'))
    check('E2-4 GET 回来（磁盘值 30）时界面**不许**被打回 30',
          kv.get('afterGet') == '31',
          'got=%r（旧代码这里是 30）' % kv.get('afterGet'))
    check('E2-5 POST 回来后界面与磁盘一致（31）',
          kv.get('afterPost') == '31', 'got=%r' % kv.get('afterPost'))
    check('E2-6 在途计数最终归零（没有泄漏）',
          kv.get('inflightEnd') == '0', 'got=%r' % kv.get('inflightEnd'))
except Exception as exc:
    check('E2 运行时检测周期竞态', False,
          '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# F 段：面板 dispose 认领自己的实例
# ===========================================================================
section('F1 dispose 回调不许无条件清空模块级 panel')

try:
    _e = strip_comments(ESRC)
    fn_panel = grab_fn(_e, 'function createOrShowAssistantPanel(')
    check('F1-0 能抠出 createOrShowAssistantPanel', bool(fn_panel),
          'len=%d' % len(fn_panel))

    disp = grab_fn(fn_panel, 'created.onDidDispose(') or \
        grab_fn(fn_panel, '.onDidDispose(')
    check('F1-1 onDidDispose 回调能定位', bool(disp), 'len=%d' % len(disp))

    check('F1-2 摘 subscriptions 用的是自己的实例（created）',
          'indexOf(created)' in disp,
          '旧代码写的是 indexOf(panel ...) —— 会摘错人')
    check('F1-3 置空必须带 `panel === created` 守卫',
          re.search(r'if\s*\(\s*panel\s*===\s*created\s*\)', disp) is not None,
          '旧代码是无条件 panel = undefined')
    # 反向：把 `if (panel === created) { ... }` 整段剥掉之后，不许还剩裸赋值。
    # ⚠️ 不能只查"有没有出现 panel = undefined" —— 新代码里那一句本来就在，
    #    区别只在于它被守卫包住了。判据必须是"剥掉守卫之后还剩不剩"。
    stripped = re.sub(r'if\s*\(\s*panel\s*===\s*created\s*\)\s*\{[^{}]*\}',
                      '', disp)
    check('F1-4 去掉守卫后没有剩余的 `panel = undefined`',
          re.search(r'\bpanel\s*=\s*undefined\s*;', stripped) is None,
          '仍有裸赋值：%r' % (stripped[:200],))
    check('F1-5 创建时就绑定了局部变量 created',
          'const created =' in fn_panel and 'panel = created' in fn_panel,
          fn_panel[:120])
except Exception as exc:
    check('F1 面板 dispose 认领', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# G 段：单文件诊断也要带 language
# ===========================================================================
section('G1 watch_single_file 的 diagnostics payload 要带 language')

try:
    tree = ast.parse(WSRC)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'watch_single_file':
            fn = node
    keys = set()
    if fn is not None:
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in ('language', 'count', 'items', 'timestamp'):
                    keys.add(node.value)
    check('G1-1 单文件 payload 含 language',
          'language' in keys,
          'keys=%r（旧代码只有 count/items/timestamp）' % (sorted(keys),))
    check('G1-2 其它三个键仍在（没有替换掉原字段）',
          {'count', 'items', 'timestamp'} <= keys, 'keys=%r' % (sorted(keys),))
except Exception as exc:
    check('G1 单文件 language', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
print('\n'.join(LINES))
print('\nPASS=%d FAIL=%d' % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
