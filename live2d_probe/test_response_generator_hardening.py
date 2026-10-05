# -*- coding: utf-8 -*-
"""test_response_generator_hardening.py — response_generator 外部输入加固验证（2026-10-05）

背景：审计发现 generate_response 对外部输入不设防（本机后端可用，
/push 会直接走进来）：
  * sample_errors 元素是非 dict（如字符串数组）-> categorize_errors 的
    err.get() AttributeError
  * error_count 是字符串（{"trigger": "diagnostics", "error_count": "3"}，
    走 standalone 的 trigger 直通路径）-> error_count >= 5 TypeError
两层验证：直接单元调用 + 真服务器真 HTTP 端到端。
"""
import io
import json
import sys
import urllib.request
import threading
import time

sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\desktop_pet')
sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\python_backend')

OUT = r'S:\My event\projects\vscode-anime-assistent\live2d_probe\rg_hardening_result.txt'
_lines = []


def out(s=''):
    _lines.append(str(s))


fail = 0

# ---------- 1. 单元层：generate_response 直调，畸形输入不许抛 ----------
from response_generator import generate_response  # noqa: E402

unit_cases = [
    ('items-str-array', {'trigger': 'diagnostics', 'error_count': 2,
                         'sample_errors': ["Cannot find name 'x'", 'bad indent']}),
    ('items-mixed', {'trigger': 'diagnostics', 'error_count': 1,
                     'sample_errors': [{'message': 'type error', 'line': 4}, 'str err']}),
    ('count-str', {'trigger': 'diagnostics', 'error_count': '3', 'sample_errors': []}),
    ('count-str-big', {'trigger': 'diagnostics', 'error_count': '7', 'sample_errors': []}),
    ('count-none-items-none', {'trigger': 'diagnostics', 'error_count': None,
                               'sample_errors': None}),
    ('all-clear-count-str', {'trigger': 'all_clear', 'error_count': '0'}),
]
for name, ctx in unit_cases:
    try:
        r = generate_response(ctx)
        ok = (isinstance(r, dict) and isinstance(r.get('payload', {}).get('text'), str)
              and r['payload']['text'] != '')
        out('[%s] unit:%s -> type=%s text=%r' % (
            'PASS' if ok else 'FAIL', name, r.get('type'), r['payload']['text'][:24]))
        if not ok:
            fail += 1
    except Exception as e:
        out('[FAIL] unit:%s raised %r' % (name, e))
        fail += 1

# 归一化必须真正生效：字符串错误要被消费成 name/syntax 类别，而不是被丢弃
r = generate_response({'trigger': 'diagnostics', 'error_count': 1,
                       'sample_errors': ["Cannot find name 'foo'"]})
ok = r['payload']['emotion'] == 'angry'
out('[%s] unit:str-error-classified (emotion=%s)' % ('PASS' if ok else 'FAIL', r['payload']['emotion']))
if not ok:
    fail += 1

# ---------- 2. 端到端：起真服务器，/push 走完整链路 ----------
import standalone  # noqa: E402

PORT = 19893
ready = threading.Event()
t = threading.Thread(target=standalone.start_server, args=(PORT, ready), daemon=True)
t.start()
if not ready.wait(timeout=5):
    out('FATAL: server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    sys.exit(1)

BASE = 'http://127.0.0.1:%d' % PORT


def post(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 'CONN:%r' % e, None


e2e_cases = [
    ('e2e:items-str-array', {'type': 'diagnostics', 'payload': {
        'count': 2, 'items': ["Cannot find name 'y'", 'unexpected indent']}}),
    ('e2e:trigger-count-str', {'trigger': 'diagnostics', 'error_count': '5',
                               'language': 'python'}),
    ('e2e:trigger-items-null', {'trigger': 'diagnostics', 'error_count': 1,
                                'sample_errors': None}),
]
for name, payload in e2e_cases:
    st, resp = post('/push', payload)
    ok = (st == 200 and resp == {'status': 'pushed'})
    out('[%s] %s status=%r resp=%r' % ('PASS' if ok else 'FAIL', name, st, resp))
    if not ok:
        fail += 1

time.sleep(0.3)
with standalone.queue_lock:
    tail = list(standalone.sse_queue)[-3:]
for seq, raw in tail:
    try:
        m = json.loads(raw)
        txt = m['payload']['text']
        ok = isinstance(txt, str) and txt != ''
        out('[%s] queue text=%r emotion=%s' % ('PASS' if ok else 'FAIL', txt[:28], m['payload']['emotion']))
        if not ok:
            fail += 1
    except Exception as e:
        out('[FAIL] queue unparsable: %r' % e)
        fail += 1

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
