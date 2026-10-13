# -*- coding: utf-8 -*-
"""test_push_hardening.py — /push 畸形 payload 加固的端到端验证（2026-10-03）

背景：审计发现 do_POST 对外部 POST 的字段类型不设防：
  * items: null      -> None[:5] TypeError -> 500 断连
  * count: "3"       -> _builtin_reply 的 > 0 比较抛 TypeError
  * text: 123 / {}   -> 前端 typeText 的 charAt 崩（jsError 黑匣子报警）
本测试起真服务器打真 HTTP，逐条验证全部 200 且 SSE 队列内容合法。
"""
import io
import json
import os
import sys
import tempfile
import urllib.request
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PROJ, 'desktop_pet'))
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))

# ⚠️ 必须在 `import standalone` **之前**设置日志落点（v0.3.27）。
# standalone.LOG_PATH 是模块加载时算出来的；而本测试 import 之后直接调
# start_server() 起的是**进程内**生产服务器，它的 /event 分支会走模块级 log()。
# 不覆盖的话，下面那条故意投毒的非 UTF-8 用例，每跑一次就往开发者的
#     desktop_pet/.airi-pet.log
# 追加一行 "/event parse failed: JSONDecodeError(...)" —— 把测试噪声伪装成
# "产品里一条来路不明的 parse 失败"（0.3.26 就是被这个假线索带偏的）。
os.environ['AIRI_PET_LOG'] = os.path.join(tempfile.gettempdir(),
                                          'airi_test_push_hardening.log')

OUT = os.path.join(_HERE, 'push_hardening_result.txt')
_lines = []


def out(s=''):
    _lines.append(str(s))


import standalone  # noqa: E402

PORT = 19891
ready = threading.Event()
t = threading.Thread(target=standalone.start_server, args=(PORT, ready), daemon=True)
t.start()
if not ready.wait(timeout=5):
    out('FATAL: server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines))
    sys.exit(1)

BASE = 'http://127.0.0.1:%d' % PORT


def post(path, payload):
    """返回 (http_status, response_json)；连接被断开时返回 ('CONN', None)。"""
    data = payload if isinstance(payload, (bytes,)) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 'CONN:%r' % e, None


def snapshot():
    with standalone.queue_lock:
        return list(standalone.sse_queue)


def last_n(n):
    return snapshot()[-n:]


fail = 0

# ---------- 1. 畸形 payload 全部必须活着回来 ----------
cases = [
    ('items-null', {'type': 'diagnostics', 'payload': {'count': 2, 'items': None}}),
    ('count-str', {'type': 'diagnostics', 'payload': {'count': '3', 'items': []}}),
    ('count-null', {'type': 'diagnostics', 'payload': {'count': None, 'items': []}}),
    ('text-number', {'payload': {'text': 123, 'emotion': 'idle'}}),
    ('text-dict', {'payload': {'text': {'a': 1}, 'emotion': 'idle'}}),
    ('text-null', {'payload': {'text': None, 'emotion': 'idle'}}),
    ('all-clear-count-str', {'trigger': 'all_clear', 'error_count': '0', 'language': 'unknown'}),
    ('all-clear-count-null', {'trigger': 'all_clear', 'error_count': None}),
    ('items-str', {'type': 'diagnostics', 'payload': {'count': 1, 'items': 'oops'}}),
]
for name, payload in cases:
    st, resp = post('/push', payload)
    ok = (st == 200 and resp == {'status': 'pushed'})
    out('[%s] %s status=%r resp=%r' % ('PASS' if ok else 'FAIL', name, st, resp))
    if not ok:
        fail += 1

# ---------- 1.5 body 本身不是 JSON 对象：合法 JSON 数组/标量/null、非 UTF-8 ----------
# json.loads 成功 ≠ dict：'payload' in msg / msg.get 在 list/int/None 上
# 抛 TypeError/AttributeError，此前未捕获直接断连；decode 也可能 UnicodeDecodeError。
raw_cases = [
    ('body-array', b'[1,2,3]'),
    ('body-int', b'123'),
    ('body-null', b'null'),
    ('body-str', b'"just a string"'),
    ('body-binary', b'\xff\xfe\xfd not utf8 \xc3\x28'),
]
for name, raw in raw_cases:
    st, resp = post('/push', raw)
    ok = (st == 200 and resp == {'status': 'pushed'})
    out('[%s] %s status=%r resp=%r' % ('PASS' if ok else 'FAIL', name, st, resp))
    if not ok:
        fail += 1

# ---------- 2. 畸形内容进队列后必须合法（text 可读、非崩源） ----------
time.sleep(0.2)
tail = last_n(14)
texts = []
for seq, raw in tail:
    try:
        m = json.loads(raw)
        texts.append(m['payload']['text'])
    except Exception as e:
        out('[FAIL] queue entry unparsable: %r (%r)' % (raw[:80], e))
        fail += 1
out('queue texts: %r' % (texts,))

if not any(t == '123' for t in texts):
    out('[FAIL] text=123 was not coerced to "123"')
    fail += 1
if not any('"a": 1' in t for t in texts):
    out('[FAIL] text=dict was not serialized')
    fail += 1

# ---------- 2.5 payload 本身不是 dict：null / 数字 / 布尔 / 含 'text' 的列表 ----------
# 0.3.25：body 层的加固没管到 payload 层。`'text' in msg.get('payload', {})`
#   * None/int/bool  -> TypeError（不可迭代）
#   * ['text']       -> 返回 True，紧接着 msg['payload']['text'] -> TypeError
# 两条都是未捕获异常 → 500 断连（实测复现）。这里放在队列内容断言之后，
# 免得新推的垃圾把上面 last_n(14) 的窗口挤出。
payload_cases = [
    ('payload-null', {'type': 'chatMessage', 'payload': None}),
    ('payload-int', {'type': 'chatMessage', 'payload': 123}),
    ('payload-bool', {'type': 'chatMessage', 'payload': True}),
    ('payload-list-text', {'type': 'chatMessage', 'payload': ['text']}),
    ('diag-payload-null', {'type': 'diagnostics', 'payload': None}),
    ('diag-payload-int', {'type': 'diagnostics', 'payload': 7}),
]
for name, payload in payload_cases:
    st, resp = post('/push', payload)
    ok = (st == 200 and resp == {'status': 'pushed'})
    out('[%s] %s status=%r resp=%r' % ('PASS' if ok else 'FAIL', name, st, resp))
    if not ok:
        fail += 1

# ---------- 2.6 /event 的 body 非 UTF-8 ----------
# decode 原先没带 errors=（且在 try 之外）：非 UTF-8 body 抛
# UnicodeDecodeError → 连 'parse failed' 都写不进日志，请求直接断连。
st, resp = post('/event', b'\xff\xfe\xfd not utf8 \xc3\x28')
ok = (st == 200 and resp == {'status': 'ok'})
out('[%s] event-binary-body status=%r resp=%r' % ('PASS' if ok else 'FAIL', st, resp))
if not ok:
    fail += 1

# ---------- 2.7 /event 的入参类型加固（v0.3.31）----------
# `/event` 与 `/push` 是**同一个坑的两个入口**：payload 是外部写的，而这里
# 以前写的是
#     p = msg.get('payload') or {}
# —— 只兜 falsy。`payload=5`（真值非 dict）会在 p.get 上抛 AttributeError，
# 被外层 except 记成 "/event parse failed"，把一条**良性**的畸形输入伪装成
# 解析错误（0.3.26 那条"来路不明的 parse 失败"假线索就是这么来的）。
# /push 侧早就有 isinstance 守卫（见 2.5），这次是补 /event 侧。
#
# 第二处：corpusOpened 的 seq 走 int() 收敛，而 Python 的 json **默认接受**
# 字面量 Infinity —— int(float('inf')) 抛 OverflowError，与 _as_seq /
# clamp_detect_sec 同源。三处 int() 收敛点必须都兜 OverflowError。
#
# 断言方式：这批输入全是**合法 JSON**，所以它们一个都不该产生
# "/event parse failed" —— 那条日志只该留给真正无法解析的 body（如 2.6）。
_LOG_PATH = standalone.LOG_PATH


def parse_failed_count():
    """数日志里 /event parse failed 的行数；日志还没建时算 0（不是 -1）。

    ⚠️ 返回 -1 会让**第一条**用例永远假失败（上一轮踩过）。
    """
    try:
        with io.open(_LOG_PATH, encoding='utf-8') as fh:
            return sum(1 for ln in fh if '/event parse failed' in ln)
    except OSError:
        return 0


def log_tail(n=8):
    try:
        with io.open(_LOG_PATH, encoding='utf-8') as fh:
            return [ln.rstrip() for ln in fh.readlines()][-n:]
    except OSError:
        return []


_before_pf = parse_failed_count()

event_cases = [
    ('event-bridgepayload-int', {'type': 'bridgePing', 'payload': 5}),
    ('event-bridgepayload-null', {'type': 'bridgePing', 'payload': None}),
    ('event-bridgepayload-str', {'type': 'bridgePing', 'payload': 'ok'}),
    ('event-bridgepayload-bool', {'type': 'bridgePing', 'payload': True}),
    ('event-bridgepayload-list', {'type': 'bridgePing', 'payload': ['ok']}),
    ('event-corpuspayload-int', {'type': 'corpusOpened', 'payload': 5}),
    ('event-corpuspayload-null', {'type': 'corpusOpened', 'payload': None}),
    ('event-corpus-seq-str', {'type': 'corpusOpened', 'payload': {'seq': 'abc'}}),
    ('event-corpus-seq-null', {'type': 'corpusOpened', 'payload': {'seq': None}}),
    ('event-top-int', 5),
    ('event-top-str', 'hello'),
    ('event-top-list', [1, 2, 3]),
    ('event-normal-bridge',
     {'type': 'bridgePing', 'payload': {'ok': True, 'at': 1, 'pid': 2}}),
]
for name, payload in event_cases:
    st, resp = post('/event', payload)
    ok = (st == 200 and resp == {'status': 'ok'})
    out('[%s] %s status=%r resp=%r' % ('PASS' if ok else 'FAIL', name, st, resp))
    if not ok:
        fail += 1

# Infinity / NaN 只在字面量形式下出现 —— 手工给字节，不绕 json.dumps
for name, raw in [
    ('event-corpus-seq-inf',
     b'{"type":"corpusOpened","payload":{"seq":Infinity,"ok":true}}'),
    ('event-top-inf', b'Infinity'),
]:
    st, resp = post('/event', raw)
    ok = (st == 200 and resp == {'status': 'ok'})
    out('[%s] %s status=%r resp=%r' % ('PASS' if ok else 'FAIL', name, st, resp))
    if not ok:
        fail += 1

time.sleep(0.3)
_after_pf = parse_failed_count()
ok = (_after_pf == _before_pf)
out('[%s] 合法 JSON 的畸形 /event 不产生 "parse failed" 假线索 (%d -> %d)'
    % ('PASS' if ok else 'FAIL', _before_pf, _after_pf))
if not ok:
    fail += 1
    for ln in log_tail(12):
        out('    log: %s' % ln)

_tail = '\n'.join(log_tail(40))
m_ignored = 'ignored non-object body' in _tail
out('[%s] 顶层非对象留痕 "/event ignored non-object body"（不静默、不误报）'
    % ('PASS' if m_ignored else 'FAIL'))
if not m_ignored:
    fail += 1

m_late = 'late ack for seq=' in _tail
out('[%s] corpusOpened 的坏 seq 走"迟到回执"分支而非崩（含 Infinity）'
    % ('PASS' if m_late else 'FAIL'))
if not m_late:
    fail += 1

m_ping = 'bridge ping OK' in _tail
out('[%s] bridgePing 正常 payload 仍被正确解析' % ('PASS' if m_ping else 'FAIL'))
if not m_ping:
    fail += 1

# ---------- 3. 正常路径不受影响 ----------
st, resp = post('/push', {'type': 'chatMessage',
                          'payload': {'text': '正常消息', 'emotion': 'happy'}})
ok = st == 200
out('[%s] normal-chat status=%r' % ('PASS' if ok else 'FAIL', st))
if not ok:
    fail += 1

st, resp = post('/push', {'type': 'diagnostics', 'payload': {
    'count': 1, 'language': 'python',
    'items': [{'file': 'a.py', 'message': 'bad indent', 'line': 3}]}})
ok = st == 200
out('[%s] normal-diagnostics status=%r' % ('PASS' if ok else 'FAIL', st))
if not ok:
    fail += 1

time.sleep(0.3)
tail2 = last_n(2)
for seq, raw in tail2:
    m = json.loads(raw)
    out('  final queue: type=%s text=%r emotion=%s'
        % (m['type'], m['payload']['text'][:30], m['payload']['emotion']))

# ---------- 3.5 _builtin_reply 兜底：remind 与 changed 台词必须区分 ----------
before = len(snapshot())
standalone._builtin_reply(2, 'file_scan', 'x.py', [], 'remind')
standalone._builtin_reply(2, 'file_scan', 'x.py', [], 'changed')
time.sleep(0.2)
pair = snapshot()[before:]
remind_texts = []
changed_texts = []
for seq, raw in pair:
    m = json.loads(raw)
    (remind_texts if '记着账' in m['payload']['text'] else changed_texts).append(m['payload']['text'])
ok = len(remind_texts) == 1 and len(changed_texts) == 1
out('[%s] builtin-reply-remind-vs-changed remind=%r changed=%r' % (
    'PASS' if ok else 'FAIL', remind_texts[:1], changed_texts[:1]))
if not ok:
    fail += 1

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
