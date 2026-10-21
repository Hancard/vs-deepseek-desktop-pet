"""0.3.40 回归测试。

覆盖四个面（每条都是 0.3.39 上实测坐实过的缺陷）：

  A 段  _generate_async 的三条不变量
        A1 生成抛异常 → 必须回落到内置台词（不能静默丢单）
        A2 生成返回空文本 → 同上
        A3 heartbeat 返回 None 是"故意不说"，兜底不许把它变成说话
        A4 同签名在途 → 第二次 /push 直接丢弃（不能说两遍）
        A5 过期结果作废（先报 bug、后报清零，慢的那条不许后到打脸）
  B 段  body 读预算必须是**整体截止时间**，不是 per-recv
        B1 慢速滴字节的跨源请求：5s 预算内必须释放连接
        B2 正常请求（一次发完）不受影响
  C 段  resolve_port() 不许在端口守卫之前写日志
  D 段  response_generator 的输入加固与去重
        D1 sample_errors 传标量不抛 TypeError
        D2 sample_errors 元素字段为 null 不许念出 "第None行：None"
        D3 error_count 为字符串必须回写 context（否则 deepseek 分支 str>int）
        D4 pick_corpus 不许连续两次逐字重复

⚠️ 测试自身撒谎的三种花样（0.3.39 全踩过，这里逐条防住）：
  ① 用例之间必须**等后台线程跑完**（生成是异步的），否则上一条的结果被下
     一条的快照吃掉；
  ② 抠源码 / 调旧版本没有的函数会抛 AttributeError，把后面整段吞掉 ——
     每段都包在自己的 try 里；
  ③ 判据用**副作用**（队列长度、日志字节数、连接释放时间）而不是状态码
     —— /push 在这条链路上恒返回 200。
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

TMP = tempfile.mkdtemp(prefix='airi_test040_')
# ⚠️ 必须在 import standalone **之前**设好：否则测试会往生产日志/设置里写
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def section(title):
    LINES.append('')
    LINES.append('--- %s ---' % title)


# ===========================================================================
# A 段：_generate_async
# ===========================================================================
section('A 后台生成的三条不变量')

try:
    import standalone

    BASE = None
    for _p in range(21500, 21600):
        _ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(_p, _ready),
                         daemon=True).start()
        if _ready.wait(3):
            BASE = _p
            break
    check('A0 测试服务器已起', BASE is not None, 'port=%r' % BASE)

    def post(body_obj, path='/push', timeout=3):
        data = json.dumps(body_obj).encode()
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (BASE, path),
                                     data=data, method='POST',
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except Exception as exc:
            return 'ERR:%s' % type(exc).__name__

    def qlen():
        return len(standalone.sse_queue)

    def texts_since(n):
        out = []
        for _s, m in list(standalone.sse_queue)[n:]:
            try:
                out.append(json.loads(m).get('payload', {}).get('text', ''))
            except Exception:
                out.append('')
        return out

    def settle(seconds):
        """等后台生成跑完。

        ⚠️ 不能用"等线程结束"来判断：0.3.39 的后台线程**没有名字**
        （`Thread(target=_run, daemon=True)`），按名字找会一次都找不到、
        立刻返回 —— 于是用例在结果还没产生时就下了结论（A5 因此误判成
        PASS）。固定等待是确定性的，代价只是几秒钟。
        """
        time.sleep(seconds)

    # --- A1 生成抛异常 ---
    n0 = qlen()
    standalone.generate_response = lambda ctx: (_ for _ in ()).throw(RuntimeError('boom'))
    st = post({'trigger': 'file_scan', 'error_count': 2, 'file': 'a.py'})
    settle(1.0)
    check('A1 生成抛异常时 /push 仍回 200', st == 200, 'status=%r' % st)
    check('A1 生成抛异常时必须回落到内置台词（不许静默丢单）',
          qlen() - n0 >= 1, 'queue_add=%d' % (qlen() - n0))

    # --- A2 生成返回空文本 ---
    n0 = qlen()
    standalone.generate_response = lambda ctx: {'type': 'chatMessage',
                                                'payload': {'text': '', 'emotion': 'idle'}}
    st = post({'trigger': 'file_scan', 'error_count': 4, 'file': 'b.py'})
    settle(1.0)
    check('A2 生成返回空文本时必须回落到内置台词', qlen() - n0 >= 1,
          'queue_add=%d' % (qlen() - n0))

    # --- A3 heartbeat 是"故意不说"，不能被兜底 ---
    n0 = qlen()
    standalone.generate_response = lambda ctx: None
    st = post({'trigger': 'heartbeat'})
    settle(1.0)
    check('A3 heartbeat 返回 None 时必须保持静默（兜底不许把它变成说话）',
          qlen() - n0 == 0, 'queue_add=%d' % (qlen() - n0))

    # --- A4 同签名在途去重 ---
    n0 = qlen()

    def slow_gen(ctx):
        time.sleep(1.0)
        return {'type': 'errorAlert', 'payload': {'text': '同一件事', 'emotion': 'angry'}}

    standalone.generate_response = slow_gen
    post({'trigger': 'file_scan', 'error_count': 7, 'file': 'c.py'})
    post({'trigger': 'file_scan', 'error_count': 7, 'file': 'c.py'})
    settle(2.5)
    added = qlen() - n0
    check('A4 同内容的重复 /push 只说一条（在途去重）', added == 1,
          'queue_add=%d' % added)

    # 对照组：内容不同就该各说一条（证明去重不是"一刀切"）
    n0 = qlen()
    post({'trigger': 'file_scan', 'error_count': 11, 'file': 'd.py'})
    post({'trigger': 'file_scan', 'error_count': 12, 'file': 'e.py'})
    settle(2.5)
    added = qlen() - n0
    check('A4b 内容不同的两次 /push 各说一条（去重没过头）', added == 2,
          'queue_add=%d' % added)

    # --- A5 过期结果作废 ---
    n0 = qlen()
    seq = [0]

    def gen_order(ctx):
        seq[0] += 1
        if seq[0] == 1:
            time.sleep(2.0)
            return {'type': 'errorAlert',
                    'payload': {'text': 'STALE_还有3个bug', 'emotion': 'angry'}}
        return {'type': 'chatMessage',
                'payload': {'text': 'FRESH_干净了', 'emotion': 'happy'}}

    standalone.generate_response = gen_order
    post({'trigger': 'file_scan', 'error_count': 3, 'file': 'f.py'})
    time.sleep(0.2)
    post({'trigger': 'all_clear', 'error_count': 0})
    settle(4.0)
    got = texts_since(n0)
    check('A5 过期的慢结果必须被丢弃（不许后到打脸）',
          'STALE_还有3个bug' not in got, 'got=%r' % got)
    check('A5b 新的那条照常说出来', 'FRESH_干净了' in got, 'got=%r' % got)

    # --- A6 线程有名字（排障用）与在途上限存在 ---
    check('A6 后台线程带 airi-gen 前缀名字',
          'airi-gen-' in open(os.path.join(ROOT, 'desktop_pet', 'standalone.py'),
                              encoding='utf-8').read())
    check('A6b 存在在途上限常量', hasattr(standalone, 'MAX_PENDING_GENERATIONS'),
          'missing MAX_PENDING_GENERATIONS')
except Exception as exc:
    check('A 段整体可执行', False, '%s: %s' % (type(exc).__name__, exc))

# ===========================================================================
# B 段：body 读预算是整体截止时间
# ===========================================================================
section('B 读预算必须是整体截止时间')

try:
    def drip(port, gap, total=200, origin='https://evil.example.com', observe=25):
        s = socket.create_connection(('127.0.0.1', port), timeout=30)
        s.sendall(('POST /push HTTP/1.0\r\nHost: 127.0.0.1\r\nOrigin: %s\r\n'
                   'Content-Length: %d\r\n\r\n' % (origin, total)).encode())
        stop = threading.Event()

        def feed():
            for _ in range(total):
                if stop.is_set():
                    return
                try:
                    s.sendall(b'x')
                except OSError:
                    return
                time.sleep(gap)

        threading.Thread(target=feed, daemon=True).start()
        s.settimeout(observe)
        t0 = time.time()
        try:
            res = s.recv(4096)
        except Exception as ex:
            res = ('EXC:%s' % type(ex).__name__).encode()
        el = time.time() - t0
        stop.set()
        try:
            s.close()
        except OSError:
            pass
        return el, res

    # 每 2 秒 1 字节：per-recv 超时永远不触发，只有整体截止时间能救
    el, _res = drip(BASE, gap=2.0)
    check('B1 慢速滴字节的跨源请求在 5s 预算内被释放',
          el <= 7.0, 'hold=%.1fs (budget=5s)' % el)

    el2, res2 = drip(BASE, gap=0.0, total=64)
    check('B2 正常请求（一次发完）立刻拿到 403', b'403' in res2,
          'hold=%.2fs resp=%r' % (el2, res2[:20]))
    check('B2b 正常请求没有被读预算拖慢', el2 < 2.0, 'hold=%.2fs' % el2)

    # 合法 body 仍能完整送达（读预算不能误伤正常大包）
    big = 'x' * 200000
    st = post({'type': 'chatMessage', 'payload': {'text': big, 'emotion': 'idle'}})
    check('B3 200KB 的合法 body 仍被接受（读预算没误伤）', st == 200,
          'status=%r' % st)
except Exception as exc:
    check('B 段整体可执行', False, '%s: %s' % (type(exc).__name__, exc))

# ===========================================================================
# C 段：resolve_port 不许在守卫之前写日志
# ===========================================================================
section('C resolve_port 的告警落盘时机')

try:
    log_path = os.environ['AIRI_PET_LOG']
    before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    os.environ['AIRI_STANDALONE_PORT'] = '0'
    port = standalone.resolve_port()
    after = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    check('C1 越界端口回落 19876', port == 19876, 'port=%r' % port)
    check('C2 resolve_port() 不直接写日志（留给守卫之后的 main）',
          after == before, 'log grew %d bytes' % (after - before))
    check('C3 告警被攒下来待落盘', len(standalone._port_warning) >= 1,
          'warnings=%r' % standalone._port_warning)
    # 落盘函数存在且能清空
    standalone.flush_port_warning()
    grown = (os.path.getsize(log_path) if os.path.exists(log_path) else 0) - after
    check('C4 flush_port_warning() 把告警写进日志', grown > 0,
          'log grew %d bytes' % grown)
    check('C5 落盘后队列清空（不重复写）', len(standalone._port_warning) == 0)
    del os.environ['AIRI_STANDALONE_PORT']
except Exception as exc:
    check('C 段整体可执行', False, '%s: %s' % (type(exc).__name__, exc))

# ===========================================================================
# D 段：response_generator 加固
# ===========================================================================
section('D response_generator 输入加固与去重')

try:
    import response_generator as rg

    # D1 sample_errors 传标量（数字）不该抛 TypeError
    try:
        r = rg.generate_response({'trigger': 'file_scan', 'error_count': 2,
                                  'file': 'a.py', 'sample_errors': 5})
        ok = isinstance(r, dict) and r.get('payload', {}).get('text')
        check('D1 sample_errors 传标量不抛 TypeError 且说得出话', bool(ok),
              'r=%r' % r)
    except Exception as exc:
        check('D1 sample_errors 传标量不抛 TypeError 且说得出话', False,
              '%s: %s' % (type(exc).__name__, exc))

    # D1b 布尔 / 浮点同样不该炸
    for bad in (True, 1.5, {'a': 1}):
        try:
            rg.generate_response({'trigger': 'file_scan', 'error_count': 1,
                                  'file': 'a.py', 'sample_errors': bad})
            ok = True
        except Exception as exc:
            ok = False
        check('D1b sample_errors=%r 不抛异常' % (bad,), ok)

    # D2 字段为 null 不许念出 None
    r = rg.generate_response({'trigger': 'file_scan', 'error_count': 2,
                              'file': 'a.py',
                              'sample_errors': [{'line': None, 'message': None}]})
    txt = (r or {}).get('payload', {}).get('text', '')
    check('D2 line/message 为 null 时不许出现 "None"', 'None' not in txt,
          'text=%r' % txt)

    # D2b 合法的行号 0 不能被当成假值吃掉
    r = rg.generate_response({'trigger': 'file_scan', 'error_count': 1,
                              'file': 'a.py',
                              'sample_errors': [{'line': 0, 'message': 'x'}]})
    txt = (r or {}).get('payload', {}).get('text', '')
    check('D2b 行号 0 是合法值（不许被 or 吃掉成 "?"）', '第0行' in txt,
          'text=%r' % txt)

    # D3 error_count 字符串必须回写进 context
    ctx = {'trigger': 'file_scan', 'error_count': '3', 'file': 'a.py'}
    rg.generate_response(ctx)
    check('D3 error_count="3" 被回写成 int', ctx.get('error_count') == 3,
          'ctx=%r' % ctx.get('error_count'))
    check('D3b files 缺失时被补成列表', isinstance(ctx.get('files'), list),
          'files=%r' % ctx.get('files'))

    # D4 pick_corpus 不连续重复
    seen = []
    for _ in range(60):
        seen.append(rg.pick_corpus('file_remind', {'file': 'a.py', 'count': 2,
                                                   'lines': '10'}))
    reps = sum(1 for i in range(1, len(seen)) if seen[i] == seen[i - 1])
    check('D4 pick_corpus 不连续两次逐字重复', reps == 0, 'repeats=%d' % reps)
    check('D4b 去重没有把池子缩小（仍在多条之间轮换）', len(set(seen)) >= 3,
          'pool=%d' % len(set(seen)))

    # D5 DeepSeek 客户端必须有 timeout（无 key 时走不到，直接查源码）
    src = open(os.path.join(ROOT, 'python_backend', 'response_generator.py'),
               encoding='utf-8').read()
    check('D5 OpenAI 客户端传了 timeout', 'timeout=8.0' in src)
    check('D5b OpenAI 客户端收了重试次数', 'max_retries=1' in src)
except Exception as exc:
    check('D 段整体可执行', False, '%s: %s' % (type(exc).__name__, exc))

LINES.append('')
LINES.append('== %d passed, %d failed ==' % (PASS, FAIL))
print('\n'.join(LINES))
sys.exit(1 if FAIL else 0)
