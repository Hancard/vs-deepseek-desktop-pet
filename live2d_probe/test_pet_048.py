# -*- coding: utf-8 -*-
"""0.3.48 回归测试。

覆盖本轮修的 3 处：

  A 段  端口解析的两端口径（`src/extension.ts` 的 STANDALONE_PORT 与
        `desktop_pet/standalone.py` 的 resolve_port）
        A1 同一个值两端算出同一个端口（逐值对撞 node 与 python）
        A2 特别守住三个历史上会分歧的写法：'0x10' / '1e3' / '12345.0'
        A3 越界与垃圾输入两端都回落 19876
  B 段  `desktop_pet/watcher.py` 单文件模式的过期 pending
        B1 pending 里是 B、而文件已撤回到已送达的 A 时，不许把 B 播出去
  C 段  `standalone.py` 的「编辑语料」在"没发布出去"时的行为
        C1 设置文件读坏 → 不许假装在等回执（立刻回落，而不是等满 2.5s）
        C2 气泡里要说明原因（设置文件坏了）
        C3 对照组：设置文件正常时不许提前回落（仍然要等扩展回执）
        C4 坏文件一个字节都不写；等待槽不残留

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 环境变量必须在 import standalone / watcher **之前**设，否则改的是用户的
    真实设置与语料。
  · 每段包在自己的 try 里：缺符号会抛 AttributeError，把后面整段吞掉。
  · A 段从 `src/extension.ts` 抠源码跑，不从 `out/` 取 —— out/ 是构建产物，
    判断力验证时 src 回滚了它还是新的，会假通过。
  · B 段必须真的起 HTTP 接收端：签名/状态这类东西只有"收到了什么"能证明。
"""
import ast
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix='airi_test048_')
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
os.environ.pop('AIRI_STANDALONE_PORT', None)

REPO = r'S:\My event\projects\vscode-anime-assistent'
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'
PET = os.path.join(REPO, 'desktop_pet')
sys.path.insert(0, PET)
sys.path.insert(0, os.path.join(REPO, 'python_backend'))

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


EXT = os.path.join(REPO, 'src', 'extension.ts')
WSRC = io.open(os.path.join(PET, 'watcher.py'), encoding='utf-8').read()


def run_node(js, env=None, timeout=60):
    d = tempfile.mkdtemp(dir=TMP, prefix='node_')
    f = os.path.join(d, 't.js')
    io.open(f, 'w', encoding='utf-8', newline='\n').write(js)
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run([NODE, f], capture_output=True, env=e, timeout=timeout)
    return (r.stdout.decode('utf-8', 'replace').strip(),
            r.stderr.decode('utf-8', 'replace')[:300], r.returncode)


# ===========================================================================
section('A 端口解析：两端必须同一口径')
# ===========================================================================
try:
    extsrc = io.open(EXT, encoding='utf-8').read()
    start = 'const STANDALONE_PORT = (() => {'
    i = extsrc.find(start)
    check('A0-1 抠到 STANDALONE_PORT 那段', i >= 0, '找不到锚点')
    j = extsrc.find('})();', i + len(start))
    seg = extsrc[i:j + 5] if i >= 0 and j > i else ''
    check('A0-2 抠到结尾', bool(seg) and 'AIRI_STANDALONE_PORT' in seg,
          'seg=%r' % seg[:80])

    import standalone  # noqa: E402  （环境变量已设好）

    harness = seg + '\nprocess.stdout.write(String(STANDALONE_PORT));\n'
    cases = ['', ' ', '19876', '19999', ' 23456 ', '+12345', '-1', '0',
             '70000', 'abc', '0x10', '1e3', '12345.0', '1_0', '20 000',
             '19876abc']
    rows = []
    for v in cases:
        env = {'AIRI_STANDALONE_PORT': v}
        js, jerr, rc = run_node(harness, env)
        os.environ['AIRI_STANDALONE_PORT'] = v
        try:
            py = standalone.resolve_port()
        finally:
            os.environ.pop('AIRI_STANDALONE_PORT', None)
        rows.append((v, js, py, rc, jerr))
    bad = [r for r in rows if str(r[2]) != r[1] or r[3] != 0]
    LINES.append('      %-12s %-8s %-8s %s' % ('env', 'ext', 'pet', 'ok'))
    for v, js, py, rc, _e in rows:
        LINES.append('      %-12r %-8s %-8s %s'
                     % (v, js, py, 'OK' if (str(py) == js and rc == 0) else '** 分歧 **'))
    check('A1 所有取值两端一致', not bad,
          '不一致: %r' % [(b[0], b[1], b[2]) for b in bad])

    # 三个历史分歧点必须**两端都回落**（不是"碰巧一致"）
    d = {r[0]: r for r in rows}
    for v in ('0x10', '1e3', '12345.0', '1_0'):
        check('A2 %r 两端都回落 19876' % v,
              d[v][1] == '19876' and d[v][2] == 19876,
              'ext=%s pet=%s' % (d[v][1], d[v][2]))
    for v in ('', 'abc', '0', '70000', '-1', '20 000', '19876abc'):
        check('A3 %r 回落 19876' % v,
              d[v][1] == '19876' and d[v][2] == 19876,
              'ext=%s pet=%s' % (d[v][1], d[v][2]))
    check('A3-b 合法值两端都认', d['19999'][1] == '19999'
          and d['19999'][2] == 19999, d['19999'])
    check('A3-c 前后空白也认（23456）', d[' 23456 '][1] == '23456'
          and d[' 23456 '][2] == 23456, d[' 23456 '])
except Exception as exc:
    check('A 段整体未崩', False, '%s: %r' % (type(exc).__name__, exc))


# ===========================================================================
section('B 单文件 watcher：过期的 pending 不许播出去')
# ===========================================================================
try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []
    ERR_A = 'def f(:\n    pass\n'                  # 错误在第 1 行
    ERR_B = 'x = 1\n\n\ndef g(:\n    pass\n'      # 错误在第 4 行
    src = os.path.join(TMP, 'demo.py')

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n)
            try:
                received.append(json.loads(body))
            except Exception:
                received.append({'raw': body[:40]})
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *a):
            pass

    def write(text):
        io.open(src, 'w', encoding='utf-8', newline='\n').write(text)

    def sink_on(port):
        srv = ThreadingHTTPServer(('127.0.0.1', port), Sink)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def line_of(msg):
        items = (msg.get('payload') or {}).get('items') or []
        return sorted({it.get('line') for it in items})

    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()

    write(ERR_A)
    time.sleep(0.3)
    srv = sink_on(port)
    threading.Thread(target=__import__('watcher').watch_single_file,
                     args=(src, port), daemon=True).start()
    end = time.time() + 8
    while time.time() < end and not received:
        time.sleep(0.05)
    check('B1 第一版（错误A）送达，行号=1',
          bool(received) and line_of(received[0]) == [1],
          '%r' % (line_of(received[0]) if received else None))

    srv.shutdown()
    srv.server_close()
    time.sleep(0.4)

    write(ERR_B)          # 推送必然失败 -> pending = B
    time.sleep(2.0)
    write(ERR_A)          # 撤回：signature == last_signature
    time.sleep(2.0)

    before = len(received)
    srv2 = sink_on(port)
    end = time.time() + 8
    while time.time() < end and len(received) <= before:
        time.sleep(0.05)
    time.sleep(1.8)       # 再多等一轮，看会不会补发 A

    new = received[before:]
    # 修好之后**什么都可以不发**：A 在那次成功推送时桌宠就已经知道了。
    # 唯一不允许的是把已撤回的 B（行 4）当成最新状态播出去。
    check('B2 不许把已撤回的 B（行 4）播出去',
          all(line_of(m) != [4] for m in new),
          '新增: %r' % [line_of(m) for m in new])
    check('B3 最多补一条（不许"先播 B 再播 A"两条）', len(new) <= 1,
          '新增 %d 条: %r' % (len(new), [line_of(m) for m in new]))
    check('B4 若补发了，必须补的是当前内容（行 1）',
          (not new) or line_of(new[0]) == [1],
          '新增: %r' % [line_of(m) for m in new])
    srv2.shutdown()
except Exception as exc:
    check('B 段整体未崩', False, '%s: %r' % (type(exc).__name__, exc))


# ===========================================================================
section('C 设置文件读坏时，「编辑语料」不许假装在等回执')
# ===========================================================================
try:
    import urllib.request
    import standalone as st2

    PORT = 19951
    SETTINGS = st2.SETTINGS_PATH
    bubbles = []
    st2.push_message = lambda t, x, e='idle': bubbles.append((t, x, e))
    fallback = []
    st2._open_corpus_fallback = lambda p: (fallback.append(time.time()), 'code-cli')[1]

    ready = threading.Event()
    threading.Thread(target=st2.start_server, args=(PORT, ready), daemon=True).start()
    check('C0 进程内服务器起来了', ready.wait(6.0))

    def post_open():
        data = json.dumps({'openCorpus': True}).encode()
        r = urllib.request.Request('http://127.0.0.1:%d/settings' % PORT, data=data,
                                   headers={'Content-Type': 'application/json'},
                                   method='POST')
        t0 = time.time()
        with urllib.request.urlopen(r, timeout=15) as resp:
            return json.loads(resp.read().decode()), t0

    # --- C1/C2：坏设置文件 ---
    io.open(SETTINGS, 'w', encoding='utf-8').write(
        '{"detectIntervalSec": 12 "openCorpusSeq": 3}')
    size_before = os.path.getsize(SETTINGS)
    bubbles.clear()
    fallback.clear()
    d, t0 = post_open()
    check('C1-1 回执带 saved=false（没落盘就是没落盘）', d.get('saved') is False, d)
    end = time.time() + 2.0
    while time.time() < end and not fallback:
        time.sleep(0.02)
    dt = (fallback[0] - t0) if fallback else None
    check('C1-2 立刻回落（不等满 2.5s 回执窗口）',
          dt is not None and dt < 1.0, '耗时=%r' % (dt,))
    check('C1-3 坏设置文件一个字节都没动',
          os.path.getsize(SETTINGS) == size_before, os.path.getsize(SETTINGS))
    texts = ' '.join(b[1] for b in bubbles)
    check('C2 气泡里说明了原因（设置文件坏了）',
          '设置文件' in texts, '气泡=%r' % [b[1][:40] for b in bubbles])
    with st2._corpus_open_lock:
        check('C2-b 等待槽不残留', not st2._corpus_open_wait,
              st2._corpus_open_wait)

    # --- C3：对照，正常设置文件仍要等扩展回执 ---
    io.open(SETTINGS, 'w', encoding='utf-8').write(
        json.dumps({'detectIntervalSec': 30, 'openCorpusSeq': 0}))
    bubbles.clear()
    fallback.clear()
    d2, t1 = post_open()
    time.sleep(0.6)
    check('C3-1 正常文件时 saved=true', d2.get('saved') is True, d2)
    check('C3-2 正常文件时 0.6s 内**不许**提前回落（还在等回执）',
          not fallback, 'fallback=%r' % fallback)
    end = time.time() + (st2.CORPUS_OPEN_ACK_TIMEOUT + 3)
    while time.time() < end:
        with st2._corpus_open_lock:
            if not st2._corpus_open_wait:
                break
        time.sleep(0.05)
    with st2._corpus_open_lock:
        left = len(st2._corpus_open_wait)
    check('C3-3 超时后仍然回落、槽收干净', bool(fallback) and left == 0,
          'fallback=%d left=%d' % (len(fallback), left))
except Exception as exc:
    check('C 段整体未崩', False, '%s: %r' % (type(exc).__name__, exc))


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_t048.txt'),
            'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
