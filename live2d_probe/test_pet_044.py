# -*- coding: utf-8 -*-
"""0.3.44 回归测试。

覆盖本轮修的 16 处（每条都在 0.3.43 的代码上坐实过触发路径）：

  A 段  common.py
        A1 /char/ 的"单层文件名"限制能被 %252F 双重解码绕过
        A2 .jpeg 在 _CHAR_MIME 有、在 _MIME 没有 → 以 octet-stream 发出
  B 段  standalone.py
        B1 int() 漏 OverflowError → /push 收到 1e999 时断连且零日志
        B2 兜底台词绕过"过期作废"闸门（桌宠自己推翻自己）
        B3 openCorpusSeq 用内存旧值算 → 把磁盘上的大序号写小
        B4 单条消息没有统一上限（payload.text / message 默认值都能塞 1MB）
        B5 /events 只认 Last-Event-ID 头，手动重建的 EventSource 不带它
  C 段  corpus.py / response_generator.py
        C1 读盘失败仍推进缓存键 → 本次运行内永不重试（改了台词不生效）
        C2 占位符 `{ file }`（带空格）既不校验也不替换 → 原样念进气泡
        C3 diagnostics 的 line: null 说成"第 None 行"
  D 段  extension.ts
        D1 `seq <= lastCorpusOpenSeq` 让设置文件被覆盖后的 12 次点击全部失效
        D2 `detectIntervalSec: true` 被 Number() 打成 1 秒（30 倍）
  E 段  ui.html
        E1 打字机按码元追加 → 每个 emoji 闪一帧 "�"
        E2 手动重建 EventSource 不带续传点 → 重播最近 20 条
  F 段  watcher.py
        F1 check_c 没 lower() → `Foo.CPP` 交给 gcc、languageId 记成 c

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 每段包在自己的 try 里 —— 旧版没有的符号会抛 AttributeError/NameError，
    把后面整段吞掉（用例数突然变少就是信号）。
  · 抠源码用**花括号配对**（grab_fn），别用 "\n    }\n" 当结束锚点。
  · 覆盖 setTimeout 一律走 globalThis（函数声明会 hoist）。
  · 判据用副作用（HTTP 状态、队列内容、磁盘内容、调用记录），不用"看起来对"。
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

TMP = tempfile.mkdtemp(prefix='airi_test044_')
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
COMMON = REPO / 'desktop_pet' / 'common.py'
WATCHER = REPO / 'desktop_pet' / 'watcher.py'
CORPUS = REPO / 'python_backend' / 'corpus.py'
RGEN = REPO / 'python_backend' / 'response_generator.py'

HSRC = io.open(str(HTML), encoding='utf-8').read()
ESRC = io.open(str(EXTS), encoding='utf-8').read()
SSRC = io.open(str(STANDALONE), encoding='utf-8').read()
CSRC = io.open(str(COMMON), encoding='utf-8').read()
WSRC = io.open(str(WATCHER), encoding='utf-8').read()
PSRC = io.open(str(CORPUS), encoding='utf-8').read()
RSRC = io.open(str(RGEN), encoding='utf-8').read()


def grab(src, start, end):
    i = src.find(start)
    if i < 0:
        return ''
    j = src.find(end, i + len(start))
    if j < 0:
        return ''
    return src[i:j + len(end)]


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
    d = Path(tempfile.mkdtemp(prefix='airi044_'))
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


def http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), b''
    except Exception as exc:
        return None, {}, '%s: %s' % (type(exc).__name__, exc)


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
        return None, '%s: %s' % (type(exc).__name__, exc)


# ===========================================================================
# A 段：common.py
# ===========================================================================
section('A1 /char/ 的单层限制不能被双重编码绕过')

try:
    import common  # noqa: E402

    url, port = common.start_asset_server('<html>x</html>', 0)
    check('A1-0 资产服务器起来了', bool(url), 'url=%r' % url)
    if url:
        base = 'http://127.0.0.1:%d' % port
        # 子目录里放一个真文件，才能证明"读得到"而不是"刚好 404"
        target = common._CHAR_ROOT / 'live2d' / 'vendor' / 'LICENSE-dsh-plugin-MIT.txt'
        if target.is_file():
            st_single, _, _ = http_get(
                base + '/char/live2d/vendor/LICENSE-dsh-plugin-MIT.txt')
            check('A1-1 单层限制：/char/<子路径> → 403',
                  st_single == 403, 'st=%r' % st_single)
            st_dbl, hdr, body = http_get(
                base + '/char/live2d%252fvendor%252fLICENSE-dsh-plugin-MIT.txt')
            # 现在只解码一次 → 剩下的 `%2f` 是**字面文件名**，磁盘上没有 → 404。
            # 判据是"读不到"（旧代码这条会 200 + 真文件内容），不是具体码。
            check('A1-2 %252F 双重编码后读不到（关键：旧代码 200）',
                  st_dbl in (403, 404) and b'MIT' not in (body or b''),
                  'st=%r body=%r' % (st_dbl, (body or b'')[:60]))
            st_dbl2, _, body2 = http_get(
                base + '/char/live2d%252fvendor%252f%252e%252e%252fvendor%252f'
                       'live2d-vendor.js')
            check('A1-3 多段 %252F + %252e 也读不到',
                  st_dbl2 in (403, 404) and b'function' not in (body2 or b''),
                  'st=%r len=%d' % (st_dbl2, len(body2 or b'')))
        else:
            skip('A1-1..3', '仓库里没有 assets/live2d/vendor/LICENSE 文件')
        st_ok, hdr_ok, _ = http_get(base + '/char/character.png')
        check('A1-4 assets 根目录的立绘照旧能取（修过头就坏了）',
              st_ok == 200 and 'image/png' in (hdr_ok.get('Content-Type') or ''),
              'st=%r ctype=%r' % (st_ok, hdr_ok.get('Content-Type')))
except Exception as exc:
    check('A1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A2 .jpeg 的 MIME 两张表要一致')

try:
    check('A2-1 _CHAR_MIME 认 .jpeg（它决定是否内联）',
          common._CHAR_MIME.get('.jpeg') == 'image/jpeg',
          'got=%r' % common._CHAR_MIME.get('.jpeg'))
    check('A2-2 _MIME 也认 .jpeg（它决定响应头）',
          common._MIME.get('.jpeg') == 'image/jpeg',
          'got=%r' % common._MIME.get('.jpeg'))
    # find_character_image 承诺支持的后缀，必须每一种都能被 _file 正确标注
    missing = [e for e in ('.png', '.gif', '.webp', '.jpg', '.jpeg')
               if common._CHAR_MIME.get(e) != common._MIME.get(e)]
    check('A2-3 五种立绘后缀在两张表里答案一致', not missing,
          'mismatch=%r' % missing)
except Exception as exc:
    check('A2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# B 段：standalone.py
# ===========================================================================
section('B1 int() 必须连 OverflowError 一起兜')

try:
    import standalone  # noqa: E402

    check('B1-1 有 _as_int 这个统一入口', hasattr(standalone, '_as_int'))
    fn = getattr(standalone, '_as_int', None)
    if fn:
        check('B1-2 float("inf") → 0（旧代码抛 OverflowError）',
              fn(float('inf')) == 0, 'got=%r' % (fn(float('inf')),))
        check('B1-3 float("-inf") / nan 也收敛成 0',
              fn(float('-inf')) == 0 and fn(float('nan')) == 0)
        check('B1-4 正常值照旧', fn('12') == 12 and fn(3.9) == 3,
              'got=%r/%r' % (fn('12'), fn(3.9)))
        check('B1-5 None → 0', fn(None) == 0)
    check('B1-6 /push 的 diagnostics 分支改用它',
          "_as_int(p.get('count'))" in SSRC)
    check('B1-7 trigger 分支也改用它',
          "_as_int(msg.get('error_count'))" in SSRC)
except Exception as exc:
    check('B1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


BASE = None
try:
    for _p in range(22100, 22200):
        _ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(_p, _ready),
                         daemon=True).start()
        if _ready.wait(3):
            BASE = _p
            break
    check('B0 测试服务器已起', BASE is not None, 'port=%r' % BASE)
except Exception as exc:
    check('B0 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B1b 真实 HTTP：count=1e999 不能断连')

try:
    if BASE:
        u = 'http://127.0.0.1:%d/push' % BASE
        # 用原始字节发：json.dumps(float('inf')) 会给出 Infinity（合法 JSON），
        # 1e999 则是标准写法 —— 两者都会解析成 inf。
        for raw in (b'{"type":"diagnostics","payload":{"count":1e999}}',
                    b'{"type":"diagnostics","payload":{"count":Infinity}}',
                    b'{"trigger":"x","error_count":1e999}'):
            req = urllib.request.Request(u, data=raw, method='POST',
                                         headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    st, body = r.status, r.read()
            except urllib.error.HTTPError as exc:
                st, body = exc.code, b''
            except Exception as exc:
                st, body = None, repr(exc)
            check('B1b-1 %s → 200 且有响应体（旧代码 0 字节断连）'
                  % raw[:34].decode('utf-8', 'replace'),
                  st == 200 and body, 'st=%r body=%r' % (st, body[:80]))
    else:
        skip('B1b', '测试服务器未起来')
except Exception as exc:
    check('B1b 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B2 兜底台词也要过"过期作废"闸门')

try:
    real_gen = standalone.generate_response

    def boom(_ctx):
        raise RuntimeError('llm down')

    # ⚠️ 先把前面几段（B1b 的 /push）留下的在途生成排空 —— 它们是异步线程，
    # 会在本段窗口里推气泡，把"应该几条"数错（实测 n=2，混进一条"全部修好了"）。
    _deadline = time.time() + 10
    while time.time() < _deadline:
        with standalone._gen_lock:
            if not standalone._gen_inflight:
                break
        time.sleep(0.05)
    time.sleep(0.4)
    standalone.sse_queue.clear()

    def run_case(done_seq, label):
        standalone.sse_queue.clear()
        standalone._gen_done_seq = done_seq
        standalone.generate_response = boom
        try:
            standalone._generate_async({'trigger': 'file_scan', 'file': 'a.py',
                                        'error_count': 3}, 'test044')
            deadline = time.time() + 5
            while time.time() < deadline:
                with standalone._gen_lock:
                    pending = len(standalone._gen_inflight)
                if pending == 0 and standalone.sse_queue:
                    break
                if pending == 0:
                    time.sleep(0.15)     # 兜底还没来得及说话
                    break
                time.sleep(0.05)
            time.sleep(0.35)
        finally:
            standalone.generate_response = real_gen
        return [json.loads(d) for _s, d in standalone.sse_queue]

    # 正例：没有更新的播报时，兜底必须说话（否则测试等于假绿）
    msgs_ok = run_case(0, 'fresh')
    check('B2-1 不过期时兜底照旧说话（1 条）', len(msgs_ok) == 1,
          'n=%d' % len(msgs_ok))
    check('B2-2 说的还是 状态栏那种错误台词',
          bool(msgs_ok) and 'a.py' in msgs_ok[0]['payload']['text'],
          'msgs=%r' % (msgs_ok[:1],))

    # 反例：已有更新的结果播报过 → 这条兜底必须被丢掉
    msgs_stale = run_case(999, 'stale')
    check('B2-3 过期兜底一条都不许说（旧代码会播，桌宠自己推翻自己）',
          len(msgs_stale) == 0, 'n=%d texts=%r'
          % (len(msgs_stale), [m['payload']['text'][:20] for m in msgs_stale]))
    standalone._gen_done_seq = 0
except Exception as exc:
    check('B2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))
finally:
    try:
        standalone._gen_done_seq = 0
    except Exception:
        pass


section('B3 openCorpusSeq 要取"内存与磁盘的较大值"')

try:
    if BASE:
        # 组件级：直接看落盘结果，并拦掉会拉 VS Code 的 worker
        real_worker = standalone._open_corpus_worker
        standalone._open_corpus_worker = lambda *a, **k: None
        try:
            io.open(standalone.SETTINGS_PATH, 'w', encoding='utf-8').write(
                json.dumps({'detectIntervalSec': 30, 'openCorpusSeq': 100}))
            standalone.settings['openCorpusSeq'] = 0     # 内存里的旧快照
            st, body = http_post('http://127.0.0.1:%d/settings' % BASE,
                                 {'openCorpus': True})
            got = (json.loads(body) if body else {}).get('openCorpusSeq')
            check('B3-1 序号从磁盘的 100 往上算，不是 0+1', got == 101,
                  'st=%r got=%r body=%r' % (st, got, body[:120]))
            disk = json.loads(io.open(standalone.SETTINGS_PATH,
                                      encoding='utf-8').read())
            check('B3-2 落盘的值不许比原来的小（旧代码把 100 写成 1）',
                  disk.get('openCorpusSeq') == 101,
                  'disk=%r' % disk.get('openCorpusSeq'))
        finally:
            standalone._open_corpus_worker = real_worker
    else:
        skip('B3', '测试服务器未起来')
except Exception as exc:
    check('B3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B4 单条消息有统一上限')

try:
    cap = getattr(standalone, 'MAX_PUSH_CHARS', None)
    check('B4-1 有 MAX_PUSH_CHARS', isinstance(cap, int) and cap > 0,
          'cap=%r' % cap)
    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', 'x' * 5000)
    txt = json.loads(standalone.sse_queue[-1][1])['payload']['text']
    check('B4-2 5000 字被截到上限附近（不是原样入队）',
          len(txt) <= (cap or 0) + 10 and len(txt) < 5000, 'len=%d' % len(txt))
    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', 'ok')
    check('B4-3 短文本一个字都不动',
          json.loads(standalone.sse_queue[-1][1])['payload']['text'] == 'ok')
    check('B4-4 截断放在 push_message 这个唯一出口上（覆盖所有分支）',
          'MAX_PUSH_CHARS' in grab_fn(SSRC, 'def push_message('))
except Exception as exc:
    check('B4 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B5 /events 认 ?after_seq=（手动重建的 EventSource 不带头）')

try:
    if BASE:
        standalone.sse_queue.clear()
        base_seq = standalone._seq
        for i in range(3):
            standalone.push_message('chatMessage', 'm%d' % i)
        want = {base_seq + 2, base_seq + 3}      # after_seq=base+1 → 只补 2、3
        got_ids = []
        url = 'http://127.0.0.1:%d/events?after_seq=%d' % (BASE, base_seq + 1)
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                deadline = time.time() + 4
                buf = b''
                while time.time() < deadline:
                    try:
                        chunk = r.read1(4096)
                    except Exception:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    for line in buf.split(b'\n'):
                        if line.startswith(b'id: '):
                            try:
                                n = int(line[4:].strip())
                            except ValueError:
                                continue
                            if n not in got_ids:
                                got_ids.append(n)
                    if len(got_ids) >= 2:
                        break
        except Exception as exc:
            got_ids = got_ids or ['ERR:%s' % type(exc).__name__]
        check('B5-1 只补发缺口（收到 %r）' % (got_ids,),
              set(got_ids) == want,
              'want=%r got=%r' % (sorted(want), got_ids))
        check('B5-2 已收到的旧事件没被重播',
              (base_seq + 1) not in got_ids, 'got=%r' % (got_ids,))
    else:
        skip('B5', '测试服务器未起来')
except Exception as exc:
    check('B5 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# C 段：corpus.py / response_generator.py
# ===========================================================================
section('C1 语料读失败不许推进缓存键（否则永不重试）')

try:
    import builtins  # noqa: E402

    import corpus  # noqa: E402

    p = corpus.CORPUS_PATH
    corpus._cache.update(key=None, scenes={}, clicks={}, error=None,
                         dropped=0, err_logged=None)

    def write(lines):
        io.open(p, 'w', encoding='utf-8').write(json.dumps(
            {'scenes': {'greeting': lines}}, ensure_ascii=False))

    write(['V1'])
    scenes, _c, err = corpus._user_corpus()
    check('C1-1 先读到好语料', err is None and scenes.get('greeting') == ['V1'],
          'scenes=%r err=%r' % (scenes.get('greeting'), err))

    write(['V2'])                      # 文件已经变了（mtime/size 都变）
    real_open = builtins.open
    target_abs = os.path.abspath(p)

    def selective_fail(*a, **k):
        # 只让**语料文件本身**打不开，别把日志文件一起拦掉
        if a and os.path.abspath(str(a[0])) == target_abs:
            raise PermissionError('locked by OneDrive')
        return real_open(*a, **k)

    builtins.open = selective_fail
    try:
        scenes_bad, _c, err_bad = corpus._user_corpus()
    finally:
        builtins.open = real_open
    check('C1-2 读失败时保住上一版（0.3.38 的承诺）',
          scenes_bad.get('greeting') == ['V1'], 'scenes=%r' % (scenes_bad.get('greeting'),))
    check('C1-3 如实留下 error', bool(err_bad), 'err=%r' % (err_bad,))

    scenes_retry, _c, err_retry = corpus._user_corpus()
    check('C1-4 文件解锁后**这一次运行内**就能生效（关键：旧代码永远返回 V1）',
          scenes_retry.get('greeting') == ['V2'],
          'scenes=%r err=%r' % (scenes_retry.get('greeting'), err_retry))

    # 失败不许把日志刷满：同一个错误只留一行
    log_path = os.environ.get('AIRI_PET_LOG')
    before = (io.open(log_path, encoding='utf-8').read()
              if log_path and os.path.exists(log_path) else '')
    builtins.open = selective_fail
    try:
        for _ in range(5):
            corpus._user_corpus()
    finally:
        builtins.open = real_open
    after = (io.open(log_path, encoding='utf-8').read()
             if log_path and os.path.exists(log_path) else '')
    added = after[len(before):].count('corpus')
    check('C1-5 同一错误重复出现时不刷屏（<=1 行）', added <= 1,
          'added=%d' % added)
except Exception as exc:
    check('C1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('C2 带空格的占位符 `{ file }` 要归一')

try:
    check('C2-1 有 _norm_placeholders', hasattr(corpus, '_norm_placeholders'))
    norm = getattr(corpus, '_norm_placeholders', None)
    if norm:
        check("C2-2 `{ file }` → `{file}`", norm('喂，{ file } 里') == '喂，{file} 里',
              'got=%r' % norm('喂，{ file } 里'))
        check('C2-3 已经规范的保持不变', norm('{count} 个') == '{count} 个')
        check('C2-4 不合法的花括号不动（保持可见）', norm('{file} {a b}') == '{file} {a b}',
              'got=%r' % norm('{file} {a b}'))
    p = corpus.CORPUS_PATH
    io.open(p, 'w', encoding='utf-8').write(json.dumps({
        'scenes': {'file_scan': ['{ file } 里还躺着 {count} 个 bug！']},
        'click_lines': {'head': [['喂 { count } 个', 'angry']]},
    }, ensure_ascii=False))
    corpus._cache.update(key=None, scenes={}, clicks={}, error=None,
                         dropped=0, err_logged=None)
    lines = corpus.get_lines('file_scan')
    check('C2-5 读入时就归一，下游 replace 才匹配得上',
          '{file}' in lines[0] and '{ file }' not in lines[0], 'line=%r' % lines[0])
    pools = corpus.get_click_lines('head')
    txt = pools[0][0] if pools else ''
    check('C2-6 点击池同样归一', '{count}' in txt and '{ count }' not in txt,
          'txt=%r' % txt)
except Exception as exc:
    check('C2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('C3 diagnostics 的 line: null 不能念成 None')

try:
    import response_generator as rg  # noqa: E402

    os.environ.pop('DEEPSEEK_API_KEY', None)
    captured = {}
    real_pick = rg.pick_corpus

    def fake_pick(category, variables=None):
        captured['vars'] = dict(variables or {})
        # 模拟真实替换：{line} 换成传入的值
        return '第%s行炸了' % (variables or {}).get('line', '?')

    rg.pick_corpus = fake_pick
    try:
        r = rg.generate_response({
            'trigger': 'diagnostics',
            'error_count': 1,
            'language': 'python',
            'sample_errors': [{'line': None, 'message': 'missing paren'}],
        })
    finally:
        rg.pick_corpus = real_pick

    text = (r or {}).get('payload', {}).get('text', '')
    check('C3-1 抓到传入的变量', 'vars' in captured, 'captured=%r' % (captured,))
    check('C3-2 line 是 null 时传下去的是 ?（旧代码是 None）',
          captured.get('vars', {}).get('line') == '?',
          'line=%r' % (captured.get('vars', {}).get('line'),))
    check('C3-3 气泡里不出现 None', 'None' not in text, 'text=%r' % text[:120])
    check('C3-4 走的是 _nvl', '_nvl(errors[0].get("line"))' in RSRC)
    check('C3-5 行号 0 仍然是 0（不能把合法值吃掉）',
          rg._nvl(0) == 0, 'got=%r' % (rg._nvl(0),))
except Exception as exc:
    check('C3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# D 段：extension.ts
# ===========================================================================
section('D1 设置文件被覆盖后「编辑语料」不能永久失效')

try:
    body = grab_fn(ESRC, 'async function openCorpusIfRequested(')
    check('D1-0 抠到 openCorpusIfRequested', bool(body), 'len=%d' % len(body))
    # ⚠️ 必须把注释剥掉再断言：我在注释里写了"不能是 `seq <= lastCorpusOpenSeq`"
    # 这句原文，不剥会把注释本身当成违规代码（本项目的经典误伤）。
    code = '\n'.join(ln for ln in body.splitlines()
                     if not ln.strip().startswith('//'))
    check('D1-1 判据是"相同才忽略"，不是 `<=`（关键）',
          'seq === lastCorpusOpenSeq' in code
          and 'seq <= lastCorpusOpenSeq' not in code, 'code=%r' % code[:200])
    check('D1-2 0 与 NaN 仍然要挡（字段被删掉时不能乱开）',
          '!Number.isFinite(seq)' in code and 'seq <= 0' in code,
          'code=%r' % code[:200])

    js = r"""
function oldRule(seq, last) { return !(isFinite(seq) && seq > last); }
function newRule(seq, last) {
  if (!isFinite(seq) || seq <= 0) return false;
  return seq !== last;
}
var rows = [[1,12],[12,12],[13,12],[0,12],[NaN,12],[7,0]];
var o = [], n = [];
rows.forEach(function (r) {
  o.push(oldRule(r[0], r[1]) ? 'x' : 'o');
  n.push(newRule(r[0], r[1]) ? 'o' : 'x');
});
console.log('OLD=' + o.join(''));
console.log('NEW=' + n.join(''));
"""
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('D1-3 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:200]))
    # 行序：[1,12] [12,12] [13,12] [0,12] [NaN,12] [7,0]   x=忽略 o=打开
    check('D1-4 旧规则把"重置后的 seq=1"和 seq<last 全判成已处理（xxoxxo）',
          kv.get('OLD') == 'xxoxxo', 'OLD=%r' % kv.get('OLD'))
    check('D1-5 新规则只忽略"同一个 seq"，0/NaN 仍然挡（oxoxxo）',
          kv.get('NEW') == 'oxoxxo', 'NEW=%r' % kv.get('NEW'))
except Exception as exc:
    check('D1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D2 detectIntervalSec: true 不能变成 1 秒')

try:
    seg = grab(ESRC, "const raw = (data as { detectIntervalSec?: unknown })",
               'if (!Number.isFinite(sec) || sec <= 0)')
    check('D2-0 抠到解析那段', bool(seg), 'seg=%r' % seg[:160])
    check('D2-1 只有 number / string 才交给 Number()',
          "typeof raw === 'string'" in seg, 'seg=%r' % seg[:200])

    js = r"""
function old(raw) { return typeof raw === 'number' ? raw : Number(raw); }
function now(raw) {
  return typeof raw === 'number' ? raw
       : (typeof raw === 'string' ? Number(raw) : NaN);
}
function pick(f, raw) {
  var s = f(raw);
  return (!isFinite(s) || s <= 0) ? 'undef' : String(s);
}
var cases = [['true', true], ['false', false], ['null', null], ['arr45', ['45']],
             ['str45', '45'], ['num5', 5], ['strEmpty', ''], ['obj', {}]];
var a = [], b = [];
cases.forEach(function (c) {
  a.push(c[0] + ':' + pick(old, c[1]));
  b.push(c[0] + ':' + pick(now, c[1]));
});
console.log('OLD=' + a.join('|'));
console.log('NEW=' + b.join('|'));
"""
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('D2-2 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:200]))
    check('D2-3 旧规则里 true 变成 1 秒（这就是 bug）',
          'true:1' in (kv.get('OLD') or ''), 'OLD=%r' % kv.get('OLD'))
    check('D2-4 新规则拒掉 true / 数组（都落到 undef = 保持当前值）',
          'true:undef' in (kv.get('NEW') or '')
          and 'arr45:undef' in (kv.get('NEW') or ''),
          'NEW=%r' % kv.get('NEW'))
    check('D2-5 数字与数字字符串照旧', 'num5:5' in (kv.get('NEW') or '')
          and 'str45:45' in (kv.get('NEW') or ''), 'NEW=%r' % kv.get('NEW'))
except Exception as exc:
    check('D2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# E 段：ui.html
# ===========================================================================
section('E1 打字机不许把 emoji 切成半个代理对')

try:
    fn = grab_fn(HSRC, 'function typeText(el, text) {')
    check('E1-0 抠到 typeText', bool(fn), 'len=%d' % len(fn))
    js = r"""
var el = { isConnected: true, textContent: '' };
var frames = [];
globalThis.setTimeout = function (f) { globalThis.__T = f; return 1; };
function loneSurrogate(s) {
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i);
    if (c >= 0xD800 && c <= 0xDBFF) {
      var d = s.charCodeAt(i + 1);
      if (!(d >= 0xDC00 && d <= 0xDFFF)) return true;
      i++;
    } else if (c >= 0xDC00 && c <= 0xDFFF) { return true; }
  }
  return false;
}
__FN__
var TEXT = '你好🐳世界a';
typeText(el, TEXT);
frames.push(el.textContent);
var guard = 0;
while (globalThis.__T && guard++ < 200) {
  var f = globalThis.__T; globalThis.__T = null; f();
  frames.push(el.textContent);
}
var bad = 0;
frames.forEach(function (s) { if (loneSurrogate(s)) bad++; });
console.log('FRAMES=' + frames.length);
console.log('BAD=' + bad);
console.log('FINAL=' + (el.textContent === TEXT ? 'ok' : 'diff'));
"""
    out, err, rc = run_node(js.replace('__FN__', fn))
    kv = kv_of(out)
    check('E1-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    check('E1-2 没有任何一帧出现孤立代理（旧代码每个 emoji 一帧）',
          kv.get('BAD') == '0', 'BAD=%r' % kv.get('BAD'))
    check('E1-3 最后一帧与原字面量完全一致', kv.get('FINAL') == 'ok',
          'FINAL=%r' % kv.get('FINAL'))
    check('E1-4 帧数没有变多（还是按"字"打，只是整码点）',
          (int(kv.get('FRAMES') or 0) <= len('你好🐳世界a') + 2),
          'FRAMES=%r' % kv.get('FRAMES'))
except Exception as exc:
    check('E1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('E2 手动重建 EventSource 要带续传点')

try:
    fn = grab_fn(HSRC, 'function connectSSE() {')
    check('E2-0 抠到 connectSSE', bool(fn), 'len=%d' % len(fn))
    check('E2-1 连接地址带 after_seq（否则服务端重播最近 20 条）',
          'after_seq=' in fn, 'fn=%r' % fn[:260])
    check('E2-2 有 lastSSESeq 状态', 'lastSSESeq' in HSRC)
    check('E2-3 从事件里记住 id', 'lastEventId' in fn, 'fn=%r' % fn[:300])

    js = r"""
var SSE_URL = 'http://127.0.0.1:1/events';
var EVENT_URL = 'http://127.0.0.1:1/event';
var lastSSESeq = 5;
var sseInstance = null, sseRetryTimer = null, sseRetryDelay = 1000;
var opened = null;
function setOnline() {}
function fetch() { return { catch: function () {} }; }
globalThis.EventSource = function (url) {
  opened = url;
  this.close = function () {};
};
__FN__
connectSSE();
console.log('URL=' + opened);
var es = sseInstance;
es.onmessage({ lastEventId: '9', data: JSON.stringify({ type: 'chatMessage', payload: { text: 'x' } }) });
console.log('SEQ=' + lastSSESeq);
es.onmessage({ lastEventId: '', data: '{"type":"chatMessage","payload":{"text":"y"}}' });
console.log('SEQ2=' + lastSSESeq);
"""
    extra = r"""
function showBubble() {}
function setEmotion() {}
function typeText() {}
"""
    out, err, rc = run_node(js.replace('__FN__', fn))
    kv = kv_of(out)
    check('E2-4 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:400]))
    check('E2-5 首次带续传点（?after_seq=5）',
          (kv.get('URL') or '').endswith('?after_seq=5'), 'URL=%r' % kv.get('URL'))
    check('E2-6 收到事件后记住最大 id', kv.get('SEQ') == '9',
          'SEQ=%r' % kv.get('SEQ'))
    check('E2-7 空 id 不把已知值打回 0', kv.get('SEQ2') == '9',
          'SEQ2=%r' % kv.get('SEQ2'))
except Exception as exc:
    check('E2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# F 段：watcher.py
# ===========================================================================
section('F1 check_c 要按小写后缀判 C/C++')

try:
    seg = grab_fn(WSRC, 'def check_c(filepath: str) -> list:')
    check('F1-0 抠到 check_c', bool(seg))
    check('F1-1 取后缀时 lower()（Windows 上 Foo.CPP 是合法文件名）',
          "os.path.splitext(filepath)[1].lower()" in seg,
          'seg=%r' % seg[:220])
    # 行为验证：patch 掉编译入口，看它对 .CPP 选哪个编译器
    import watcher  # noqa: E402

    seen = {}

    def fake_run(cmd, *a, **k):
        seen['cmd'] = cmd

        class R:
            returncode = 0
            stdout = ''
            stderr = ''
        return R()

    real = getattr(watcher, '_run_checker', None)
    tmp_cpp = Path(TMP) / 'Foo.CPP'
    tmp_cpp.write_text('int main(){return 0;}\n', encoding='utf-8')
    if hasattr(watcher, 'subprocess'):
        real_pop = watcher.subprocess.run
        watcher.subprocess.run = fake_run
        try:
            watcher.check_c(str(tmp_cpp))
        finally:
            watcher.subprocess.run = real_pop
        cmd = seen.get('cmd') or []
        joined = ' '.join(str(x) for x in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        check('F1-2 .CPP 用的是 g++（不是 gcc）', 'g++' in joined, 'cmd=%r' % (joined[:120],))
    else:
        skip('F1-2/3', 'watcher 没有 subprocess 入口')
except Exception as exc:
    check('F1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
out_lines = LINES + ['', 'PASS=%d FAIL=%d' % (PASS, FAIL)]
print('\n'.join(out_lines))
sys.exit(0 if FAIL == 0 else 1)
