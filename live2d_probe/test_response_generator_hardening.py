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

# file_scan 且 sample_errors 为空：语料占位符 {line}/{lines} 不许原样残留
# （pick_corpus 用 str.replace，缺变量会留着 "{line}" 进气泡）
r = generate_response({'trigger': 'file_scan', 'file': 'a.py',
                       'error_count': 3, 'sample_errors': []})
ok = ('{line}' not in r['payload']['text'] and '{lines}' not in r['payload']['text']
      and '{file}' not in r['payload']['text'] and '{count}' not in r['payload']['text'])
out('[%s] unit:file_scan-empty-errors-no-placeholder text=%r' % (
    'PASS' if ok else 'FAIL', r['payload']['text'][:36]))
if not ok:
    fail += 1

# diagnostics 且 sample_errors 为空：categorize_errors([]) 恒返回 syntax_error，
# 而 SYNTAX_ERROR 语料里有一条含 {line} —— variables 缺 line 键时占位符会原样
# 说进气泡（与 file_scan 分支同源，上一轮只补了 file_scan 那半）。
# pick_corpus 是随机抽，单次抽不中，必须多抽几次才能把这条语句逼出来。
leak = None
for _ in range(80):
    r = generate_response({'trigger': 'diagnostics', 'error_count': 2, 'sample_errors': []})
    if '{' in r['payload']['text']:
        leak = r['payload']['text']
        break
ok = leak is None
out('[%s] unit:diagnostics-empty-errors-no-placeholder%s' % (
    'PASS' if ok else 'FAIL', '' if ok else ' text=%r' % leak[:36]))
if not ok:
    fail += 1

# remind 与 changed 在 many_errors 之外的语料分支要能区分（都不许抛、不许留占位符）
for reason in ('remind', 'changed'):
    r = generate_response({'trigger': 'file_scan', 'file': 'b.py',
                           'error_count': 2, 'reason': reason, 'sample_errors': []})
    ok = '{' not in r['payload']['text']
    out('[%s] unit:file_scan-%s text=%r' % ('PASS' if ok else 'FAIL', reason, r['payload']['text'][:30]))
    if not ok:
        fail += 1

# ---------- 1.5 语料占位符全量回归（语料扩容后防回归） ----------
# 规则：每条语料里出现的 {占位符} 必须落在"该场景实际会被传入的变量键"集合内，
# 否则 pick_corpus 的 str.replace 缺键就会把占位符原样说进气泡（0.3.18/0.3.20 踩过）。
import re  # noqa: E402
import corpus  # noqa: E402

CORPUS_LISTS = {
    'syntax_error': (corpus.SYNTAX_ERROR, {'line', 'language'}),
    'type_error': (corpus.TYPE_ERROR, {'line', 'language'}),
    'import_error': (corpus.IMPORT_ERROR, {'line', 'language'}),
    'name_error': (corpus.NAME_ERROR, {'line', 'language'}),
    # many_errors 走两条路：diagnostics 只给 count/language，
    # file_scan 给 file/count/language/line/lines —— 并集允许
    'many_errors': (corpus.MANY_ERRORS, {'count', 'language', 'file', 'line', 'lines'}),
    # 下面四个永远是 pick_corpus(category) 裸调 —— 一个占位符都不许有
    'all_clear': (corpus.ALL_CLEAR, set()),
    'greeting': (corpus.GREETING, set()),
    'idle': (corpus.IDLE, set()),
    'encourage': (corpus.ENCOURAGE, set()),
    'file_scan': (corpus.FILE_SCAN, {'file', 'count', 'line', 'lines', 'language'}),
    'file_remind': (corpus.FILE_REMIND, {'file', 'count', 'line', 'lines', 'language'}),
    'file_clear': (corpus.FILE_CLEAR, {'file'}),
}
for cat, (lst, allowed) in CORPUS_LISTS.items():
    for idx, line in enumerate(lst):
        bad = set(re.findall(r'\{(\w+)\}', line)) - allowed
        if bad:
            out('[FAIL] corpus:%s[%d] 非法占位符 %s -> %r' % (cat, idx, sorted(bad), line[:36]))
            fail += 1
out('[PASS] corpus:placeholder-sweep (12 scenes / %d lines)'
    % sum(len(v[0]) for v in CORPUS_LISTS.values()))

# 动态抽样：generate_response 实际会用到的每一种 (场景, 变量) 组合，
# 抽 120 次确认没有一条残留 '{'。以后往语料里加新句子，这条会自动兜住。
from response_generator import pick_corpus  # noqa: E402

FS_VARS = {'file': 'a.py', 'count': 3, 'language': 'python', 'line': '9', 'lines': '第9行'}
DYNAMIC_COMBOS = [
    ('syntax_error', {'language': 'python', 'line': '?'}),
    ('type_error', {'language': 'python', 'line': '?'}),
    ('import_error', {'language': 'python', 'line': '?'}),
    ('name_error', {'language': 'python', 'line': '?'}),
    ('many_errors', {'count': 7, 'language': 'python'}),
    ('many_errors', dict(FS_VARS, count=7)),
    ('file_scan', FS_VARS),
    ('file_remind', FS_VARS),
    ('file_clear', {'file': 'a.py'}),
    ('greeting', {}), ('all_clear', {}), ('encourage', {}), ('idle', {}),
]
for cat, variables in DYNAMIC_COMBOS:
    leaked = None
    for _ in range(120):
        t = pick_corpus(cat, dict(variables))
        if '{' in t:
            leaked = t
            break
    if leaked:
        out('[FAIL] dynamic:%s 残留占位符 -> %r' % (cat, leaked[:40]))
        fail += 1
out('[PASS] dynamic:pick-corpus-sampling (%d combos x120)' % len(DYNAMIC_COMBOS))

# ---------- 2. 端到端：起真服务器，/push 走完整链路 ----------
import standalone  # noqa: E402

# 点击台词的情绪词必须是 ui.html EMOTION_EXPRESSION 认识的五个，否则表情切不过去
_VALID_EMOTIONS = {'idle', 'greeting', 'angry', 'happy', 'surprised'}
for zone, pool in standalone.CLICK_LINES.items():
    for text, emo in pool:
        if emo not in _VALID_EMOTIONS:
            out('[FAIL] click:%s 情绪词 %r 非法 -> %r' % (zone, emo, text[:24]))
            fail += 1
out('[PASS] click:emotion-words (%d zones)' % len(standalone.CLICK_LINES))

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
