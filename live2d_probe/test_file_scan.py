# -*- coding: utf-8 -*-
"""test_file_scan.py — 当前文件 bug 播报（file_scan）端到端验证（2026-10-05）

背景：新增功能——扩展每 10s 盯当前激活文件，bug 变化立刻播报（含行号内容）、
未变化定期提醒（reason=remind）、修到 0 播报"干净了"。
本测试起真服务器打真 HTTP，验证：
  1. file_scan 有 bug → 200 且队列文本含文件名/数量/行号细节
  2. file_scan remind → 换提醒台词（不与首播相同句式）
  3. file_scan 清零 → file_clear 台词
  4. 畸形 file_scan（count 字符串/items 字符串/缺 file）不炸
"""
import io
import json
import sys
import urllib.request
import threading
import time

sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\desktop_pet')
sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\python_backend')

OUT = r'S:\My event\projects\vscode-anime-assistent\live2d_probe\file_scan_result.txt'
_lines = []


def out(s=''):
    _lines.append(str(s))


import standalone  # noqa: E402

PORT = 19893
ready = threading.Event()
t = threading.Thread(target=standalone.start_server, args=(PORT, ready), daemon=True)
t.start()
if not ready.wait(timeout=5):
    out('FATAL: server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines))
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


def snapshot():
    with standalone.queue_lock:
        return list(standalone.sse_queue)


fail = 0

# ---------- 1. 有 bug 的播报：文本必须含文件名、数量、行号细节 ----------
st, resp = post('/push', {
    'trigger': 'file_scan', 'reason': 'changed', 'file': 'main.py',
    'error_count': 2, 'prev_count': 0, 'language': 'python',
    'sample_errors': [
        {'line': 42, 'message': "name 'x' is not defined"},
        {'line': 87, 'message': 'invalid syntax'},
    ],
})
ok = st == 200 and resp == {'status': 'pushed'}
out('[%s] file-scan-2bugs status=%r' % ('PASS' if ok else 'FAIL', st))
if not ok:
    fail += 1

# ---------- 2. 未变化的重复提醒（reason=remind） ----------
st, resp = post('/push', {
    'trigger': 'file_scan', 'reason': 'remind', 'file': 'main.py',
    'error_count': 2, 'prev_count': 2, 'language': 'python',
    'sample_errors': [
        {'line': 42, 'message': "name 'x' is not defined"},
        {'line': 87, 'message': 'invalid syntax'},
    ],
})
out('[%s] file-scan-remind status=%r' % ('PASS' if st == 200 else 'FAIL', st))
if st != 200:
    fail += 1

# ---------- 3. 清零播报 ----------
st, resp = post('/push', {
    'trigger': 'file_scan', 'reason': 'changed', 'file': 'main.py',
    'error_count': 0, 'prev_count': 2, 'language': 'python',
    'sample_errors': [],
})
out('[%s] file-scan-clear status=%r' % ('PASS' if st == 200 else 'FAIL', st))
if st != 200:
    fail += 1

# ---------- 4. 畸形 file_scan：类型兜底不炸 ----------
malformed = [
    ('count-str', {'trigger': 'file_scan', 'file': 'a.py',
                   'error_count': '3', 'sample_errors': 'oops'}),
    ('count-none', {'trigger': 'file_scan', 'file': 'a.py',
                    'error_count': None, 'sample_errors': None}),
    ('no-file', {'trigger': 'file_scan', 'error_count': 1,
                 'sample_errors': [{'line': '7', 'message': 123}]}),
    ('items-str-elems', {'trigger': 'file_scan', 'file': 'a.py',
                         'error_count': 2,
                         'sample_errors': ['plain string', 42, None]}),
]
for name, payload in malformed:
    st, resp = post('/push', payload)
    ok = st == 200 and resp == {'status': 'pushed'}
    out('[%s] malformed-%s status=%r' % ('PASS' if ok else 'FAIL', name, st))
    if not ok:
        fail += 1

# ---------- 5. 队列内容断言 ----------
time.sleep(0.3)
tail = snapshot()[-7:]
texts = []
for seq, raw in tail:
    try:
        m = json.loads(raw)
        texts.append((m['type'], m['payload']['text'], m['payload']['emotion']))
    except Exception as e:
        out('[FAIL] queue entry unparsable: %r (%r)' % (raw[:80], e))
        fail += 1

for i, (mtype, text, emo) in enumerate(texts):
    out('  q[%d] type=%s emo=%s text=%r' % (i, mtype, emo, text[:60]))

# 5.1 首播：含文件名 + 数量 + 行号细节
first = texts[0][1] if texts else ''
if 'main.py' not in first or '2' not in first:
    out('[FAIL] first report missing file/count: %r' % first[:60])
    fail += 1
else:
    out('[PASS] first report has file+count')
if '42' not in first:
    out('[FAIL] first report missing line detail: %r' % first[:80])
    fail += 1
else:
    out('[PASS] first report names line 42')

# 5.2 remind 台词与首播不同（file_remind 语料 vs file_scan 语料）
if len(texts) >= 2 and texts[1][1] == texts[0][1]:
    out('[FAIL] remind repeats the same line as first report')
    fail += 1
else:
    out('[PASS] remind uses a different line')

# 5.3 清零：happy 情绪 + 文件名
if len(texts) >= 3:
    mtype, text, emo = texts[2]
    if emo != 'happy' or 'main.py' not in text:
        out('[FAIL] clear msg wrong: emo=%s text=%r' % (emo, text[:60]))
        fail += 1
    else:
        out('[PASS] clear msg happy + filename')
else:
    out('[FAIL] clear message missing from queue')
    fail += 1

# 5.4 所有畸形样本都要进队列且是字符串
for mtype, text, emo in texts[3:]:
    if not isinstance(text, str) or not text:
        out('[FAIL] malformed produced bad queue text: %r' % (text,))
        fail += 1
out('[PASS] malformed all queued as strings' if fail == 0 else '[NOTE] see above')

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
