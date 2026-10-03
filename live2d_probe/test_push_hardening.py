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
import sys
import urllib.request
import threading
import time

sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\desktop_pet')
sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\python_backend')

OUT = r'S:\My event\projects\vscode-anime-assistent\live2d_probe\push_hardening_result.txt'
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

# ---------- 2. 畸形内容进队列后必须合法（text 可读、非崩源） ----------
time.sleep(0.2)
tail = last_n(9)
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

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
