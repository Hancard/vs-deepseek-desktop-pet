# -*- coding: utf-8 -*-
r"""0.3.53 回归：SSE 续传基线（lastSSESeq）必须能跟上服务端重启。

服务端的 `_seq` 是**进程级**计数器（`standalone.py: _seq = 0`，只在
push_message 里 +1），桌宠一重启就归零；而页面里的 `lastSSESeq` 活得更久
（桌宠崩溃后由扩展重新拉起，页面不一定跟着重建）。旧写法是

    if (isFinite(eid) && eid > lastSSESeq) { lastSSESeq = eid; }

只增不减，于是旧进程的最大 id 被永久当成续传点。两个方向都错：

  · 新进程 `_seq` 还没涨过旧基线时 —— `?after_seq=<旧基线>` 被服务端钳到
    当前 `_seq`（/events 里那段"钳上界否则永久饿死"的注释），replay 恒空，
    断线期间的消息**一条都收不到**；
  · 涨过旧基线之后 —— after_seq 重新"有效"，一次重连就把上一代的旧对话
    当新消息重播一遍，正是 0.3.44 加 `id:` 要治的复读机。

实测（真 standalone + 真 /push + 真 /events，见 A 段）：
    `?after_seq=500`（当时 _seq=3）→ 收到 0 条
    `?after_seq=3`（当时 _seq=8）→ 重播 5 条旧消息

## 段说明
  A 段  起**真** standalone，验证服务端在两种 after_seq 下的行为 ——
        这是前端那条判据必须双向的理由，也顺便锁住服务端的钳位契约
  B 段  从 ui.html 里抠出 adoptSSESeq，用 node 真跑九组输入
  C 段  静态判据：防止有人把它改回"只增不减"
  D 段  `_run_all.py` 的**退出码**（本轮的第二条 bug）。它以前从不
        `sys.exit`，于是一律 rc=0 —— 而它自己文件头写着"真实结论只能看 rc"。
        实测：表里印着"通过 39，异常 1"，`rc` 还是 0，差点据此宣布全绿。
        这一段把 runner 复制到临时目录、喂假测试进去，用真 rc 判定。

⚠️ 测试自身的坑：
  · A 段用的是**另一个进程里的真服务**吗？不是 —— 是同一进程的另一个线程，
    但走的是真 HTTP + 真 SSE，判据是"收到几条 id"这个副作用；
  · SSE 读取不能直接在主线程 readline（会挂死到 socket 超时）→ 单独线程读、
    主线程到点 close 再 join；收集到的 id 才是判据；
  · 环境变量必须在 import standalone **之前**设，否则污染生产日志/设置
    （0.3.38 的教训）。
"""
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
import urllib.request

TMP = tempfile.mkdtemp(prefix='airi_test053_')
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'desktop_pet'))
sys.path.insert(0, os.path.join(REPO, 'python_backend'))

NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'

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


def _free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_sse(port, query, wait=1.6):
    """连一次 /events，返回 (id 列表, data 里解析出来的对象列表)。

    ⚠️ 读必须在单独线程里做：主线程 readline 会一直等到 socket 超时
    （服务端空闲时 15 秒才发一次心跳注释行），整个用例被拖成"看起来挂了"。
    """
    url = 'http://127.0.0.1:%d/events%s' % (port, query)
    ids, bodies = [], []
    resp = OPENER.open(url, timeout=10)

    def _reader():
        try:
            while True:
                raw = resp.readline()
                if not raw:
                    return
                line = raw.decode('utf-8', 'replace').strip()
                if line.startswith('id:'):
                    try:
                        ids.append(int(line[3:].strip()))
                    except ValueError:
                        ids.append(line[3:].strip())
                elif line.startswith('data:'):
                    try:
                        bodies.append(json.loads(line[5:].strip()))
                    except Exception:
                        bodies.append(line[5:].strip())
        except Exception:
            return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    time.sleep(wait)
    try:
        resp.close()
    except Exception:
        pass
    t.join(1.5)
    return ids, bodies


def texts(bodies):
    out = []
    for b in bodies:
        if isinstance(b, dict):
            p = b.get('payload') or {}
            out.append(str(p.get('text') or p.get('message') or ''))
    return out


try:
    import standalone  # noqa: E402

    # ======================================================================
    # A 段：真服务端的两种行为
    # ======================================================================
    section('A 段  真 standalone：after_seq 的两种失效方向')
    PORT = _free_port()
    ready = threading.Event()
    threading.Thread(target=standalone.start_server,
                     args=(PORT, ready), daemon=True).start()
    if not ready.wait(10):
        check('A0 服务器能在测试端口上起来', False, 'ready 未置位')
    else:
        check('A0 服务器能在测试端口上起来', True)

        # 不推送就无法验证；用真出口（push_message 是唯一出口）
        for i in (1, 2, 3):
            standalone.push_message('chatMessage', 'A%d' % i)
        time.sleep(0.2)
        check('A0b 三条消息进了队列（_seq 应是 3）', standalone._seq == 3,
              '实测 _seq=%r' % (standalone._seq,))

        ids_fresh, b_fresh = read_sse(PORT, '')
        check('A1 全新连接（不带 after_seq）能拿到全部 3 条',
              ids_fresh == [1, 2, 3],
              'id=%r text=%r' % (ids_fresh, texts(b_fresh)))

        ids_high, b_high = read_sse(PORT, '?after_seq=500')
        check('A2 基线**高于**当前 _seq 时一条都收不到（旧窗口的短期失效）',
              ids_high == [],
              'id=%r text=%r' % (ids_high, texts(b_high)))

        for i in (4, 5, 6, 7, 8):
            standalone.push_message('chatMessage', 'B%d' % i)
        time.sleep(0.2)
        ids_low, b_low = read_sse(PORT, '?after_seq=3')
        check('A3 基线**低于**当前 _seq 时把旧消息重播出来（旧窗口的长期复读）',
              ids_low == [4, 5, 6, 7, 8],
              'id=%r text=%r' % (ids_low, texts(b_low)))
        check('A4 重播出来的是上一代那批旧台词（不是新消息）',
              [t for t in texts(b_low) if t.startswith('B')] ==
              ['B4', 'B5', 'B6', 'B7', 'B8'],
              'text=%r' % (texts(b_low),))

    # ======================================================================
    # B 段：真跑 ui.html 里的 adoptSSESeq
    # ======================================================================
    section('B 段  adoptSSESeq（从 ui.html 抠出来用 node 跑）')
    ui_path = os.path.join(REPO, 'desktop_pet', 'ui.html')
    src = io.open(ui_path, encoding='utf-8').read()      # 通用换行：CRLF -> LF

    start = src.find('function adoptSSESeq(')
    if start < 0:
        check('B0 ui.html 里能找到 adoptSSESeq', False, '找不到函数定义')
        func_src = ''
    else:
        # 括号配平取函数体（ui.html 里字符串中含 `}` 的可能性不为零，
        # 但这个函数体内只有比较和返回，不涉及字符串 —— 配平足够可靠）
        i, depth, seen = start, 0, False
        end = -1
        while i < len(src):
            ch = src[i]
            if ch == '{':
                depth += 1
                seen = True
            elif ch == '}':
                depth -= 1
                if seen and depth == 0:
                    end = i + 1
                    break
            i += 1
        func_src = src[start:end] if end > 0 else ''
        check('B0 ui.html 里能找到完整的 adoptSSESeq', bool(func_src),
              'end=%r' % (end,))

    CASES = [
        # (名称, eid, lastSeq, isFirst, 期望)
        ('B1 稳态递增 5->6', 6, 5, False, 6),
        ('B2 同 id 不变', 5, 5, False, 5),
        ('B3 重启对齐：500 -> 1（首条允许回退）', 1, 500, True, 1),
        ('B4 重启后继续 1->2', 2, 1, False, 2),
        ('B5 全新页重播首条 0 -> 481', 481, 0, True, 481),
        ('B6 坏 id 0 保持原值', 0, 500, True, 500),
        ('B7 非首条不许回退 5 -> 3', 3, 5, False, 5),
        ('B8 非首条不回退 but 更大则前进 5 -> 9', 9, 5, False, 9),
        ('B9 首条大于基线时正常前进 5 -> 9', 9, 5, True, 9),
    ]
    if func_src:
        js_lines = ['const cases = %s;' % json.dumps(
            [[c[0], c[1], c[2], c[3], c[4]] for c in CASES], ensure_ascii=False)]
        js_lines.append(func_src)
        js_lines.append("""
for (const c of cases) {
  const [name, eid, last, first, want] = c;
  let got;
  try { got = adoptSSESeq(eid, last, first); }
  catch (e) { got = 'ERR:' + (e && e.message || e); }
  console.log('RESULT\\t' + name + '\\t' + got + '\\t' + want);
}
console.log('RESULT\\tB10 NaN 保持原值\\t' + adoptSSESeq(NaN, 7, true) + '\\t7');
console.log('RESULT\\tB11 undefined 保持原值\\t' + adoptSSESeq(undefined, 7, false) + '\\t7');
console.log('RESULT\\tB12 负数视为无效\\t' + adoptSSESeq(-3, 7, true) + '\\t7');
""")
        js_path = os.path.join(TMP, 'adopt_probe.js')
        io.open(js_path, 'w', encoding='utf-8').write('\n'.join(js_lines))
        try:
            proc = subprocess.run([NODE, js_path], capture_output=True,
                                  timeout=60)
            out = proc.stdout.decode('utf-8', 'replace')
            err = proc.stderr.decode('utf-8', 'replace')
            rows = [ln.split('\t') for ln in out.splitlines()
                    if ln.startswith('RESULT\t')]
            check('B0b node 真的跑出了结果（不是脚本报错）',
                  len(rows) >= 12,
                  'rows=%d rc=%d err=%r' % (len(rows), proc.returncode,
                                            err[:160]))
            for r in rows:
                if len(r) < 4:
                    continue
                name, got, want = r[1], r[2], r[3]
                check(name, got == want, 'got=%r want=%r' % (got, want))
        except Exception as exc:
            check('B 段跑通 node', False, '%s: %s' % (type(exc).__name__, exc))

    # ======================================================================
    # C 段：静态判据
    # ======================================================================
    section('C 段  静态判据（不许改回"只增不减"）')
    check('C1 旧的 `eid > lastSSESeq` 单行赋值已消失',
          'isFinite(eid) && eid > lastSSESeq' not in src,
          '还留着（会被服务端重启永久卡住）')
    check('C2 onmessage 走 adoptSSESeq（而不是自己算）',
          'lastSSESeq = adoptSSESeq(Number(event && event.lastEventId),' in src)
    check('C3 基线的"回退"只允许发生在连接的第一条事件上',
          bool(re.search(r'isFirst\s*&&\s*eid\s*<\s*lastSeq', func_src or '')),
          'func=%r' % (func_src[:80],))
    check('C4 connectSSE 里为每条连接备了 firstEvent 初值',
          bool(re.search(r'var firstEvent = true;', src)),
          '缺少 per-connection 标记 → 回退永远不生效')
    check('C5 收到事件后把 firstEvent 置 false（否则每条事件都能回退）',
          bool(re.search(r'lastSSESeq = adoptSSESeq\([\s\S]{0,160}?firstEvent = false;',
                         src)))
    check('C6 非数字 / 非正的 id 不许改写基线',
          bool(re.search(r'!isFinite\(eid\)\s*\|\|\s*eid <= 0', func_src or '')))

    # ======================================================================
    # D 段：_run_all.py 的退出码
    # ======================================================================
    section('D 段  _run_all.py 的退出码必须反映结论')
    run_all_src = os.path.join(HERE, '_run_all.py')

    def _probe_runner(files):
        """把真 _run_all.py 复制到临时目录，配上假测试跑一遍，返回 rc。"""
        d = tempfile.mkdtemp(prefix='airi_runall_')
        io.open(os.path.join(d, '_run_all.py'), 'w', encoding='utf-8').write(
            io.open(run_all_src, encoding='utf-8').read())
        for name, body in files.items():
            io.open(os.path.join(d, name), 'w', encoding='utf-8').write(body)
        r = subprocess.run([sys.executable, os.path.join(d, '_run_all.py')],
                           capture_output=True, timeout=180)
        return r.returncode, (r.stdout or b'').decode('utf-8', 'replace')

    GOOD = "print('PASS  ok')\n"
    BAD_EXIT = "print('FAIL  boom')\nraise SystemExit(1)\n"
    BAD_MARK = "print('FAIL  only-marker')\n"      # 自己 rc=0，但打了 FAIL 标记
    NOMARK = "print('RESULT: ALL PASS')\n"         # 第三代写法：只有 rc 有意义

    rc_ok, out_ok = _probe_runner({'test_a_ok.py': GOOD,
                                  'test_d_zeromark.py': NOMARK})
    check('D1 全绿时退出码是 0（别矫枉过正）', rc_ok == 0,
          'rc=%d out=%r' % (rc_ok, out_ok[-200:]))

    rc_bad, out_bad = _probe_runner({'test_a_ok.py': GOOD,
                                     'test_b_bad.py': BAD_EXIT})
    check('D2 有测试非零退出时 runner 也必须非零（旧码恒 0）',
          rc_bad != 0, 'rc=%d out=%r' % (rc_bad, out_bad[-200:]))

    rc_mark, out_mark = _probe_runner({'test_c_mark.py': BAD_MARK})
    check('D3 测试自己 rc=0 却打了 FAIL 标记，runner 也必须非零',
          rc_mark != 0, 'rc=%d out=%r' % (rc_mark, out_mark[-200:]))

except Exception as exc:
    check('整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(HERE, '_t053.txt'), 'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
