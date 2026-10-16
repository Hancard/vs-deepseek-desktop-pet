# -*- coding: utf-8 -*-
"""test_pet_origin.py — 本地 HTTP 服务的「跨源护栏」端到端验证（0.3.35）

背景（实测取证，非推测）：桌宠主服务监听 127.0.0.1，而 ui.html 是从**资产
服务器**（127.0.0.1:<动态端口>）加载的 —— 端口不同即跨源，所以响应必须带
CORS 头，否则前端一个请求都发不出去。但 CORS 头写成了 '*'，实测结果是：

    POST /push   Origin: https://evil.example.com -> 200，桌宠照着念任意文案
    GET  /settings                                -> 200，设置被读走
    OPTIONS 预检                                   -> 204 一路放行

即：用户随手打开的一个网页就能 ① 冒充桌宠说话（"构建失败，点这里修复"这类
钓鱼文案）② 开 /events 偷听 —— SSE 流里有 file_scan 的文件名、代码行和诊断
内容。

修法：只有 Origin 是回环地址（或根本没有 Origin —— 扩展 / curl 这类非浏览器
客户端）才放行，其余一律 403 且**不执行**任何动作。

本测试起真服务器打真 HTTP：既验"恶意来源被挡且没有副作用"，也验"合法来源
照旧工作"（护栏最容易写歪成连自己人也挡了）。
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PROJ, 'desktop_pet'))
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))

# ⚠️ 必须在 `import standalone` **之前**设置（v0.3.27 的教训）：
# standalone 的这些路径是模块加载时算出来的，import 之后再改有竞态。
# 本测试会真实触发 /settings 写盘与被拒请求，不隔离就会污染开发者的
# ~/.dsh/pets/ 与 desktop_pet/.airi-pet.log。
_TMP = tempfile.gettempdir()
os.environ['AIRI_PET_LOG'] = os.path.join(_TMP, 'airi_test_origin.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(_TMP, 'airi_test_origin_set.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(_TMP, 'airi_test_origin_corpus.jsonc')
for _p in (os.environ['AIRI_PET_LOG'], os.environ['AIRI_PET_SETTINGS'],
           os.environ['AIRI_PET_CORPUS']):
    try:
        os.remove(_p)
    except OSError:
        pass

OUT = os.path.join(_HERE, 'origin_result.txt')
_lines = []
_fail = 0


def out(s=''):
    _lines.append(str(s))


def check(name, ok, detail=''):
    global _fail
    out('[%s] %s%s' % ('PASS' if ok else 'FAIL', name,
                       ('  ' + detail) if detail else ''))
    if not ok:
        _fail += 1


import standalone  # noqa: E402

PORT = 19882
_ready = threading.Event()
threading.Thread(target=standalone.start_server, args=(PORT, _ready),
                 daemon=True).start()
if not _ready.wait(timeout=5):
    out('FATAL: server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    sys.exit(1)

BASE = 'http://127.0.0.1:%d' % PORT
EVIL = 'https://evil.example.com'


def req(method, path, origin=None, body=b'', ctype='application/json'):
    """返回 (status, ACAO 头, 响应体)。连不上/被重置时 status 是异常名。"""
    headers = {}
    if origin is not None:
        headers['Origin'] = origin
    if body:
        headers['Content-Type'] = ctype
    r = urllib.request.Request(BASE + path, data=body or None,
                               headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.headers.get('Access-Control-Allow-Origin'), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Access-Control-Allow-Origin'), e.read()
    except Exception as e:
        return type(e).__name__, None, b''


def queue_len():
    with standalone.queue_lock:
        return len(standalone.sse_queue)


def read_settings_file():
    try:
        with io.open(os.environ['AIRI_PET_SETTINGS'], encoding='utf-8') as fh:
            return json.loads(fh.read())
    except Exception:
        return None


PUSH_BODY = json.dumps({'type': 'chatMessage',
                        'payload': {'text': 'origin-probe', 'emotion': 'idle'}}).encode()

out('=== A. 非本机来源：一律 403，且不产生任何副作用 ===')

# A1 —— 最要紧的一条：恶意网页能不能让桌宠说话。
# 判据不是"返回了 403"，而是**队列有没有变** —— 403 但消息照样进队列的话
# 护栏等于没装。
for label, ctype, body in (
        ('json', 'application/json', PUSH_BODY),
        ('text-plain（简单请求，浏览器不预检）', 'text/plain', b'\xe6\x81\xb6\xe6\x84\x8f'),
):
    before = queue_len()
    st, acao, _ = req('POST', '/push', origin=EVIL, body=body, ctype=ctype)
    after = queue_len()
    check('A1 push-%s 被拒且队列不增长' % label,
          st == 403 and after == before,
          'status=%r 队列 %d->%d' % (st, before, after))

# A2 —— 读设置
st, acao, _ = req('GET', '/settings', origin=EVIL)
check('A2 GET /settings 被拒', st == 403 and acao is None, 'status=%r ACAO=%r' % (st, acao))

# A3 —— 偷听 SSE
st, _, _ = req('GET', '/events', origin=EVIL)
check('A3 GET /events（SSE 偷听）被拒', st == 403, 'status=%r' % (st,))

# A4 —— 预检。放行了预检，浏览器才会把真正的 POST 发出去，所以这里必须挡。
st, acao, _ = req('OPTIONS', '/push', origin=EVIL)
check('A4 OPTIONS 预检被拒且不带 ACAO', st == 403 and acao is None,
      'status=%r ACAO=%r' % (st, acao))

# A5 —— 改设置（写操作）。除了 403，还要看**磁盘上的文件有没有被动过**。
# ⚠️ 必须先用合法来源把文件建出来：否则读到的两个值都是 None，
#    `before == after` 是条空洞断言（啥也没验）。
req('POST', '/settings', origin='http://127.0.0.1:54321',
    body=json.dumps({'detectIntervalSec': 15}).encode())
before_file = read_settings_file()
st, _, _ = req('POST', '/settings', origin=EVIL,
               body=json.dumps({'detectIntervalSec': 7}).encode())
after_file = read_settings_file()
check('A5 POST /settings 被拒且设置文件未被改写', st == 403 and before_file == after_file,
      'status=%r 文件 %r -> %r' % (st, before_file, after_file))

# A6 —— 各种"看起来像本机"其实不是的来源
for label, origin in (
        ('null（沙箱 iframe / file://）', 'null'),
        ('局域网地址', 'http://192.168.1.5:8080'),
        ('后缀欺骗 127.0.0.1.evil.com', 'http://127.0.0.1.evil.com:80'),
        ('公网 https', 'https://attacker.io'),
        ('畸形字符串', 'not-a-url'),
):
    before = queue_len()
    st, _, _ = req('POST', '/push', origin=origin, body=PUSH_BODY)
    check('A6 拒绝 %s' % label, st == 403 and queue_len() == before,
          'status=%r' % (st,))

out('')
out('=== B. 合法来源：必须照旧能用（护栏别把自己人也挡了）===')

# B1 —— 桌宠自己的页面（资产服务器，动态端口）
for label, origin in (
        ('127.0.0.1 任意端口', 'http://127.0.0.1:54321'),
        ('localhost', 'http://localhost:54321'),
        ('IPv6 回环', 'http://[::1]:54321'),
):
    before = queue_len()
    st, acao, _ = req('POST', '/push', origin=origin, body=PUSH_BODY)
    ok = (st == 200 and queue_len() == before + 1
          and acao == origin)   # 回显而不是 '*'
    check('B1 %s 放行且 ACAO 回显' % label, ok, 'status=%r ACAO=%r' % (st, acao))

# B2 —— 非浏览器客户端（扩展 / curl / 探针）根本不发 Origin，必须照旧
before = queue_len()
st, acao, _ = req('POST', '/push', body=PUSH_BODY)
check('B2 无 Origin（VS Code 扩展）放行', st == 200 and queue_len() == before + 1,
      'status=%r ACAO=%r' % (st, acao))

# B3 —— 扩展要读 /settings 来同步"编辑语料"序号，两种形态都得通
st, acao, body = req('GET', '/settings')           # 无 Origin
st2, acao2, _ = req('GET', '/settings', origin='http://127.0.0.1:54321')
check('B3 GET /settings 无 Origin / 回环 Origin 都通',
      st == 200 and st2 == 200 and acao2 == 'http://127.0.0.1:54321',
      'status=%r/%r' % (st, st2))

# B4 —— 预检合法来源要放行，否则浏览器连 POST 都不会发
st, acao, _ = req('OPTIONS', '/push', origin='http://127.0.0.1:54321')
check('B4 OPTIONS 放行回环来源', st == 204 and acao == 'http://127.0.0.1:54321',
      'status=%r ACAO=%r' % (st, acao))

# B5 —— SSE 合法来源能连上（只读前几个字节就撤，不留长连接）
def sse_head(origin):
    try:
        s = socket.create_connection(('127.0.0.1', PORT), timeout=5)
        req_line = ('GET /events HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n' % PORT)
        if origin:
            req_line += 'Origin: %s\r\n' % origin
        s.sendall((req_line + '\r\n').encode())
        data = s.recv(64)
        s.close()
        return data.split(b'\r\n')[0].decode('latin-1')
    except Exception as e:
        return 'ERR:%r' % e


head_ok = sse_head('http://127.0.0.1:54321')
check('B5 SSE 回环来源能连上', ' 200 ' in head_ok, 'status-line=%r' % head_ok)

out('')
out('=== C. 端到端副作用：被拒的写操作确实没生效 ===')
# 先用合法来源把周期改成 42（在 1~60 范围内），再用恶意来源改 7，
# 文件里必须还是 42 —— 只验状态码的话，"403 但照样写了盘"看不出来。
st, _, _ = req('POST', '/settings', origin='http://127.0.0.1:54321',
               body=json.dumps({'detectIntervalSec': 42}).encode())
set_after_legal = read_settings_file()
st2, _, _ = req('POST', '/settings', origin=EVIL,
                body=json.dumps({'detectIntervalSec': 7}).encode())
set_after_evil = read_settings_file()
check('C1 合法来源能改设置', st == 200 and
      (set_after_legal or {}).get('detectIntervalSec') == 42,
      'status=%r 值=%r' % (st, (set_after_legal or {}).get('detectIntervalSec')))
check('C2 恶意来源改不动', st2 == 403 and
      (set_after_evil or {}).get('detectIntervalSec') == 42,
      'status=%r 值=%r' % (st2, (set_after_evil or {}).get('detectIntervalSec')))

out('')
out('=== D. 护栏本身 + 留痕 ===')
# D1 —— 纯函数单测（含大小写/方括号/端口等写法差异）
fn = getattr(standalone, '_origin_allowed', None)
if fn is None:
    check('D1 _origin_allowed 存在', False, 'standalone 里找不到该函数')
else:
    cases = [
        (None, True), ('', True),                              # 非浏览器客户端
        ('http://127.0.0.1:54321', True),
        ('http://127.0.0.1', True),
        ('http://LOCALHOST:8080', True),                       # 大小写
        ('http://[::1]:8080', True),
        ('http://localhost', True),
        ('null', False),                                       # 沙箱 iframe
        ('https://evil.example.com', False),
        ('http://127.0.0.1.evil.com', False),                  # 后缀欺骗
        ('http://192.168.0.10:3000', False),
        ('not-a-url', False),
    ]
    bad = [(o, fn(o), e) for o, e in cases if fn(o) != e]
    check('D1 _origin_allowed 判定（%d 例）' % len(cases), not bad, '不符：%r' % (bad,))

# D2 —— 代码里不许再出现写死的 '*' CORS 头
src = io.open(os.path.join(_PROJ, 'desktop_pet', 'standalone.py'),
              encoding='utf-8').read()
check('D2 源码里不再有写死的 ACAO: *',
      "'Access-Control-Allow-Origin', '*'" not in src)

# D3 —— 拒绝必须留痕（静默拒绝会无从查起）
try:
    logtext = io.open(os.environ['AIRI_PET_LOG'], encoding='utf-8',
                      errors='replace').read()
except Exception:
    logtext = ''
check('D3 拒绝在日志里留痕', 'blocked cross-origin' in logtext,
      '日志 %d 字节' % len(logtext))

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if _fail == 0 else 1)
