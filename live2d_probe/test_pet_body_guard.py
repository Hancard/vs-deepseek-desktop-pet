"""回归：本地 HTTP 服务对畸形请求的两道护栏（0.3.37）

背景（两个 bug 都是**实测**坐实的，不是推理）：

**Bug A —— 畸形 Content-Length 把 handler 线程永久挂死**
    `self.rfile.read(int(Content-Length))`，int() 只兜了 TypeError/ValueError：
      · 负数 → read(-1) 是 **readall** 语义，一直读到 EOF；而客户端发完请求头
        是在**等响应**、不会关连接 ⇒ 线程永久阻塞。
      · 虚大（声明 1000 只发 20）→ read(1000) 阻塞等收满 ⇒ 同样挂死。
    两者**都不抛异常**（socket 没设读超时），handle_error 不会被调用、日志零
    痕迹 —— 是可静默累积的线程 + socket 泄漏。
    实测（修复前）：连发 5 次 `Content-Length: -1` 且保持连接 → 进程线程数
    从 2 涨到 7，之后再也没退过。

**Bug B —— 超大 Last-Event-ID 让 SSE 连接永久饿死**
    after_seq 只做 int()，不钳上界。_seq 是进程级单调计数且**重启归零**，
    客户端带一个比当前 _seq 更大的 id 时 replay 必然为空，last_seq 就停在那个
    巨大值上 ⇒ 此后 _seq 得涨到 after_seq+1 才可能有消息被选出，其间一条都
    收不到；而循环外层是 `except Exception: pass`，日志一行都没有。
    实测（修复前）：同样推 3 条消息，全新连接收到 14 条，带
    `Last-Event-ID: 99999999` 的那条收到 **0 条**。

判据一律用**副作用**：真的收到 400 响应、线程数没涨、日志里有留痕、连接真的
收到消息 —— 而不是"看起来没崩"。

端口 19892（19891 已被 test_push_hardening.py 占用）。
"""

import io
import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PET = os.path.join(REPO, 'desktop_pet')
TMP = os.path.join(os.environ.get('TEMP', '/tmp'), 'airi_body_guard_test')

# 测试隔离：必须在 import standalone 之前设（0.3.27 的教训）
os.makedirs(TMP, exist_ok=True)
LOG_PATH = os.path.join(TMP, 'run.log')
os.environ['AIRI_PET_LOG'] = LOG_PATH
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

sys.path.insert(0, PET)
import standalone  # noqa: E402

PORT = 19892
LINES = []
_fail = 0


def out(s):
    print(s)
    LINES.append(s)


def check(label, ok, detail=''):
    global _fail
    if not ok:
        _fail += 1
    out('  [%s] %s%s' % ('PASS' if ok else 'FAIL', label,
                         ('  -- ' + detail) if detail else ''))


def flush():
    with io.open(os.path.join(HERE, 'body_guard_result.txt'), 'w',
                 encoding='utf-8') as f:
        f.write('\n'.join(LINES) + '\n')


# ---------------------------------------------------------------- A. 静态契约
out('=== A. 静态契约：护栏必须真的在每一处 ===')
SRC = io.open(os.path.join(PET, 'standalone.py'), encoding='utf-8').read()

check('A1 有统一的 _read_body()', 'def _read_body(self):' in SRC)
check('A2 有 MAX_BODY_BYTES 上限常量', 'MAX_BODY_BYTES' in SRC)
check('A3 有 BODY_READ_TIMEOUT_SEC 读超时常量', 'BODY_READ_TIMEOUT_SEC' in SRC)
check('A4 _read_body 拒绝负数', re_neg := ('length < 0' in SRC))
check('A5 _read_body 拒绝超限', 'length > MAX_BODY_BYTES' in SRC)
check('A6 _read_body 设了 socket 读超时',
      'settimeout(BODY_READ_TIMEOUT_SEC)' in SRC)
check('A7 读完后把超时恢复成阻塞（SSE 不能带着超时跑）',
      'settimeout(None)' in SRC)
# 穷举：三处调用点必须全部换成 _read_body，不能有漏网的裸读
check('A8 三个有 body 的路径都走 _read_body（/push、/event、/settings）',
      SRC.count('self._read_body()') >= 3,
      '实测调用点 %d 处' % SRC.count('self._read_body()'))
check('A9 调用方没有漏网的裸读（_read_body 自身那处不算 —— 它已受范围'
      '校验 + 读超时保护）',
      not [ln for ln in SRC.splitlines()
           if 'rfile.read(length)' in ln and 'raw = ' not in ln],
      '实测残留 %r' % [ln.strip() for ln in SRC.splitlines()
                       if 'rfile.read(length)' in ln and 'raw = ' not in ln])
check('A10 _cors_guard 里那处 while 循环仍在（它用 while remaining>0，'
      '负数天然进不去，本来就是安全的 —— 别被误删）',
      'while remaining > 0:' in SRC)

# ---------------------------------------------------------------- 起服务器
ready = threading.Event()
threading.Thread(target=standalone.start_server, args=(PORT, ready),
                 daemon=True).start()
if not ready.wait(timeout=8):
    out('FATAL: 服务器没起来')
    flush()
    sys.exit(1)
time.sleep(0.5)


def raw_request(payload: bytes, timeout=3.0, hold=False):
    """发裸 HTTP 请求。返回 (结果, 响应头首行)。

    hold=True 时**不关连接**（模拟真实客户端：发完请求头就等响应）。
    """
    try:
        s = socket.create_connection(('127.0.0.1', PORT), timeout=timeout)
        s.sendall(payload)
        s.settimeout(timeout)
        try:
            data = s.recv(200)
            r = ('RESPONSED', data.decode('utf-8', 'replace').split('\r\n')[0])
        except socket.timeout:
            r = ('NO_RESPONSE', '')
        except (ConnectionResetError, ConnectionAbortedError):
            r = ('RESET', '')
        if hold:
            HELD.append(s)
        else:
            s.close()
        return r
    except Exception as exc:
        return ('ERR:%s' % type(exc).__name__, '')


HELD = []


def push_text(text, timeout=3.0) -> str:
    body = json.dumps({'type': 'chatMessage',
                       'payload': {'text': text, 'emotion': 'idle'}}).encode()
    req = (b'POST /push HTTP/1.1\r\nHost: 127.0.0.1\r\n'
           b'Content-Type: application/json\r\n'
           b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body)) + body
    return raw_request(req, timeout)[0]


# ---------------------------------------------------------------- B. Bug A
out('')
out('=== B. 行为：畸形 Content-Length 必须被拒，线程不许累积 ===')
out('  --- 基线 ---')
check('B0 正常 POST /push 有响应', push_text('hello') == 'RESPONSED')

NEG = (b'POST /push HTTP/1.1\r\nHost: 127.0.0.1\r\n'
       b'Content-Type: application/json\r\n'
       b'Content-Length: -1\r\n\r\n{"a":1}')

out('  --- Content-Length: -1 ---')
r, head = raw_request(NEG, timeout=3.0)
out('    结果=%s  响应头=%r' % (r, head))
check('B1 负数 Content-Length 被**当场拒绝**（拿到 400，不是挂死）',
      r == 'RESPONSED' and '400' in head, '实测=%s %r' % (r, head))

out('  --- 连发 5 次且**客户端不关连接**（真实场景）---')
# ⚠️ 关掉连接等于替服务端解围（读到 EOF 线程就退了），测不出泄漏
base_threads = threading.active_count()
for _ in range(5):
    raw_request(NEG, timeout=2.0, hold=True)
time.sleep(1.5)
thr = threading.active_count()
out('    线程数 %d -> %d' % (base_threads, thr))
check('B2 5 次畸形请求后线程数**没有累积**（修复前会从 2 涨到 7）',
      thr <= base_threads + 1, '实测 %d -> %d' % (base_threads, thr))
check('B3 服务器仍可用', push_text('after neg') == 'RESPONSED')
for s in HELD:
    try:
        s.close()
    except Exception:
        pass
HELD.clear()
time.sleep(0.8)

out('  --- Content-Length 虚大（声明 1000 只发 20）：靠读超时兜底 ---')
SHORT = (b'POST /push HTTP/1.1\r\nHost: 127.0.0.1\r\n'
         b'Content-Type: application/json\r\n'
         b'Content-Length: 1000\r\n\r\n{"a":1,"b":2,"c":3}')
t0 = time.time()
r2, head2 = raw_request(SHORT, timeout=8.0, hold=True)
cost = time.time() - t0
out('    结果=%s  响应头=%r  耗时 %.1fs' % (r2, head2, cost))
check('B4 虚大 Content-Length 最终**收到 400**（不是永久挂死）',
      r2 == 'RESPONSED' and '400' in head2, '实测=%s %r' % (r2, head2))
check('B5 兜底发生在读超时附近（1~8 秒，说明超时真生效了）',
      1.0 <= cost <= 8.0, '实测 %.1fs' % cost)
for s in HELD:
    try:
        s.close()
    except Exception:
        pass
HELD.clear()
time.sleep(0.8)

out('  --- 超限（>1MB）---')
BIG = (b'POST /push HTTP/1.1\r\nHost: 127.0.0.1\r\n'
       b'Content-Length: 99999999\r\n\r\nx')
r3, head3 = raw_request(BIG, timeout=3.0)
check('B6 声明超过 1MB 直接 400（不等它真的发完）',
      r3 == 'RESPONSED' and '400' in head3, '实测=%s %r' % (r3, head3))

# ---------------------------------------------------------------- C. 留痕
out('')
out('=== C. 拒绝必须留痕（否则以后查不到"谁在打我"）===')
try:
    logtxt = io.open(LOG_PATH, encoding='utf-8', errors='replace').read()
except OSError:
    logtxt = ''
check('C1 日志里有 rejected body 记录', 'rejected body' in logtxt,
      '日志 %d 字节' % len(logtxt))
check('C2 留痕里带了具体的 Content-Length 值',
      'Content-Length=-1' in logtxt)

# ---------------------------------------------------------------- D. Bug B
out('')
out('=== D. 行为：SSE 连接不能被超大 Last-Event-ID 饿死 ===')


def sse_recv(last_event_id, wait=6.0):
    """开一条 SSE 连接，期间推 3 条消息，返回 [(id, data)]。

    连 id 一起收：续传语义只能靠 id 判定（"有没有收到比续传点更旧的消息"），
    数条数是靠不住的 —— 队列里攒着历史消息，条数会随测试进度漂移。
    """
    hdr = b'GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n'
    if last_event_id is not None:
        hdr += b'Last-Event-ID: %d\r\n' % last_event_id
    hdr += b'\r\n'
    got = []
    cur_id = [None]
    try:
        s = socket.create_connection(('127.0.0.1', PORT), timeout=wait)
        s.sendall(hdr)
        s.settimeout(0.4)
        stop = [False]

        def reader():
            buf = b''
            while not stop[0]:
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    ln, buf = buf.split(b'\n', 1)
                    if ln.startswith(b'id: '):
                        try:
                            cur_id[0] = int(ln[4:])
                        except ValueError:
                            cur_id[0] = None
                    elif ln.startswith(b'data: '):
                        got.append((cur_id[0], ln[6:].decode('utf-8', 'replace')))
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for i in range(3):
            time.sleep(wait / 4.0)
            push_text('sse-probe-%d' % i)
        time.sleep(wait / 4.0)
        stop[0] = True
        time.sleep(0.3)
        s.close()
    except Exception as exc:
        out('  (SSE 探针异常 %r)' % (exc,))
    return got


out('  --- 对照组：不带 Last-Event-ID（全新连接）---')
normal = sse_recv(None)
out('    收到 %d 条' % len(normal))
check('D1 全新连接能收到新推送', len(normal) >= 1, '收到 %d 条' % len(normal))

out('  --- 实验组：Last-Event-ID: 99999999（远大于当前 seq）---')
starved = sse_recv(99999999)
out('    收到 %d 条: %r' % (len(starved), starved[:3]))
# ⚠️ 修复前这里收到 0 条（被饿死）。修复后 after_seq 被钳到当前 _seq。
check('D2 超大 Last-Event-ID 的连接**照样能收到新消息**（修复前是 0 条）',
      len(starved) >= 1, '收到 %d 条' % len(starved))

out('  --- 对照组 2：合法续传点（当前 seq - 1）---')
cur = getattr(standalone, '_seq', None)
resumed = sse_recv(max(0, (cur or 0) - 1))
out('    当前 _seq=%r  收到 %d 条' % (cur, len(resumed)))
check('D3 合法续传点不受影响', len(resumed) >= 1, '收到 %d 条' % len(resumed))

out('  --- 对照组 3：续传语义必须仍然正确（只补缺口，不重播旧的）---')
cur = getattr(standalone, '_seq', 0) or 0
resume_from = max(0, cur - 3)
mid = sse_recv(resume_from)
mid_ids = [i for i, _d in mid if i is not None]
out('    续传点=%d  收到 %d 条  id=%r' % (resume_from, len(mid), mid_ids))
check('D4 中间续传点能收到后续消息', len(mid) >= 1, '收到 %d 条' % len(mid))
check('D5 续传只补缺口：收到的每条 id 都 > 续传点（没有把旧消息重播一遍）',
      bool(mid_ids) and all(i > resume_from for i in mid_ids),
      '续传点=%d 收到 id=%r' % (resume_from, mid_ids))

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'HAS FAILURE',
                                  _fail))
flush()
sys.exit(0 if _fail == 0 else 1)
