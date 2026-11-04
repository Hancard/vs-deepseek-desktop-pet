# -*- coding: utf-8 -*-
"""0.4.2 回归：三个"契约只在一条路径上兑现"的缺口。

## 缺陷（都是同一类：判断写对了，但只写在其中一个出口上）

### ① `heartbeat` 的"故意不说话"只在一个出口生效

`generate_response` 里 `elif trigger == "heartbeat": return None` —— 返回 None
的意思是"我**决定**不说"。可是 standalone 那边要判断"这个 None 是决定不说，
还是生成失败该说兜底台词"，判据是自已写的一份裸比较：

    if r is None and str(ctx.get('trigger') or '') == 'heartbeat':

而生成器是**先 strip 再比对**的（`_as_trigger`）。同一个值在两个进程里被解释
成两件事。实测（真服务端 + 真 `/push` + 真 SSE 帧，修复前）：

    {"trigger":"heartbeat"}    → 无帧（对）
    {"trigger":" heartbeat"}   → chatMessage/happy「哼，全部修好了…算你厉害。」
    {"trigger":"heartbeat "}   → 同上

多打一个空格，心跳就变成"全部清零"的喜报。

而且缺口有**两半**：后端不可用时（`_backend_available=False`）`/push` 的
trigger 分支根本不走 `_generate_async`，直接 `_builtin_reply(...)`，而
`_builtin_reply` 不看 trigger 是否"该沉默" —— 于是连**原样**的
`{"trigger":"heartbeat"}` 也会播报同一句"全部修好了"。判据只写在
`_generate_async._run` 里，兜底出口没有。

### ② `all_clear` 与 `error_count` 打架时按 trigger 走

`all_clear` 的语义是"一个错误都没有"，但它和 `error_count` 是两个独立字段。
`{"trigger":"all_clear","error_count":3}` 旧口径照 trigger 直走 → 类型
chatMessage（不是红色告警）、情绪 happy、台词「哼，终于修好了」。明明 3 个
错误，桌宠在道喜。`_as_trigger` 的注释写着"'报错还是干净'由那个数决定"——
同一个原则不能只在"值不认识"时生效。

### ③ 有键但没内容的文本照样发帧

`{"payload":{"text":""}}` / `{"payload":{"text":"   "}}` 会入队一条帧。
前端 `showBubble('')` 遇到空串直接 return（不建气泡），但紧跟着的
`setEmotion(emotion || 'idle')` 照常执行 —— 一条"没有话"的推送把表情打回
idle，屏幕上毫无动静。全空白更糟：`'   '` 在 JS 里是真值，会建出一个
**看不见却占着 region** 的空气泡挂满 8 秒，还会把已有气泡顶上去。
0.4.1 挡的是"没有 message/text 键"，这边是"有键但没内容"，同一个口径的另一半。

## 判据为什么这么选

- ①② 用**副作用**（有没有 SSE 帧、帧的 type/emotion、台词归属），不看
  "代码里有没有这个函数"。
- ① 的后半（无后端）用 `_builtin_reply(..., _push=收集器)` 直接打兜底出口，
  不依赖服务端 —— 那才是这条路径的真实入口。
- ② 除了 type/emotion，还要求台词**不属于** all_clear 池（抽 30 次）：光看
  情绪不牢，池子里的句子才是用户实际看到的。
"""
import ast
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

PASS = FAIL = 0


def check(name, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print('PASS  %s' % name)
    else:
        FAIL += 1
        print('FAIL  %s  %s' % (name, detail))


def section(title):
    print('\n--- %s ---' % title)


TMP = tempfile.mkdtemp(prefix='airi_test056_')
# 环境变量必须在 import standalone **之前**设好（否则会写生产日志/设置）
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
os.environ.pop('DEEPSEEK_API_KEY', None)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'desktop_pet'))
sys.path.insert(0, os.path.join(ROOT, 'python_backend'))

print('--- 0.4.2 静默契约 / all_clear 口径 / 空白文本 ---')

rg = st = corpus = None
frames = []
PORT = 0

try:
    import response_generator as rg
    import corpus
    import standalone as st
    from http.server import ThreadingHTTPServer
    import urllib.request as _ur
    OPENER = _ur.build_opener(_ur.ProxyHandler({}))   # 本机不走系统代理
    srv = ThreadingHTTPServer(('127.0.0.1', 0), st.AiriHandler)   # 0 = 内核分配，不撞端口
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _listen():
        try:
            with OPENER.open('http://127.0.0.1:%d/events' % PORT, timeout=60) as r:
                buf = b''
                while True:
                    chunk = r.read(1)
                    if not chunk:
                        return
                    buf += chunk
                    while b'\n\n' in buf:
                        ev, buf = buf.split(b'\n\n', 1)
                        data = None
                        for ln in ev.split(b'\n'):
                            if ln.startswith(b'data: '):
                                data = ln[6:]
                        if data:
                            try:
                                frames.append(json.loads(data.decode('utf-8')))
                            except Exception:
                                pass
        except Exception:
            pass

    threading.Thread(target=_listen, daemon=True).start()
    time.sleep(0.6)
except Exception as exc:
    check('A0 导入与服务端段未崩', False, '%s: %r' % (type(exc).__name__, exc))


def post(obj):
    req = urllib.request.Request(
        'http://127.0.0.1:%d/push' % PORT,
        data=json.dumps(obj, ensure_ascii=False).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status
    except Exception as exc:
        return 'ERR %r' % (exc,)


def take(n):
    """返回第 n 条之后新增的帧（type, emotion, text）三元组。

    ⚠️ 别写成 `[ (…for f in …) ]` —— 那是"一个只装着生成器的列表"，
    `got == []` 恒假、`got[0][0]` 抛 TypeError，判据就全废了。
    """
    return [(f.get('type'), (f.get('payload') or {}).get('emotion'),
             (f.get('payload') or {}).get('text') or '') for f in frames[n:]]


# ===========================================================================
section('A is_silent_trigger —— 判据只能有一份')
# ===========================================================================
try:
    # ⚠️ 旧码里没有这个函数：直接 `rg.is_silent_trigger` 会抛 AttributeError，
    # 被最外层 except 吞掉 → 后面整段不执行（"只崩一处"是假象）。必须 getattr。
    _silent = getattr(rg, 'is_silent_trigger', None)
    check('A0 response_generator.is_silent_trigger 存在且可调用',
          callable(_silent), 'missing')
    _at = getattr(rg, '_as_trigger', None)

    def silent(v):
        return bool(_silent(v)) if callable(_silent) else None

    check('A1 原样 heartbeat 算静默', silent('heartbeat') is True, repr(silent('heartbeat')))
    check('A2 前后带空白的 heartbeat 也算静默（strip 口径）',
          silent(' heartbeat ') is True and silent('heartbeat\n') is True,
          repr((silent(' heartbeat '), silent('heartbeat\n'))))
    check('A3 其余四个已知 trigger 都不静默',
          [silent(t) for t in ('diagnostics', 'file_scan', 'all_clear', 'greeting')]
          == [False] * 4,
          repr([silent(t) for t in ('diagnostics', 'file_scan', 'all_clear', 'greeting')]))
    check('A4 畸形值不静默',
          [silent(v) for v in (None, 123, [], {}, True, 'HEARTBEAT', '')]
          == [False] * 7,
          repr([silent(v) for v in (None, 123, [], {}, True, 'HEARTBEAT', '')]))
    if callable(_at):
        vals = ['heartbeat', ' heartbeat', 'heartbeat ', 'diagnostics', 'file_scan',
                'all_clear', 'greeting', None, 123, [], {}, 'HEARTBEAT', '', '  ']
        same = all(silent(v) == (rg._as_trigger(v) == 'heartbeat') for v in vals)
        check('A5 与 _as_trigger 逐值对撞：silent 当且仅当归一化后是 heartbeat',
              same, repr([(v, silent(v), rg._as_trigger(v)) for v in vals]))
    else:
        check('A5 与 _as_trigger 逐值对撞：silent 当且仅当归一化后是 heartbeat',
              False, '_as_trigger missing')
except Exception as exc:
    check('A 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
section('B 真服务端：心跳的四种写法都不许出声（控制组必须出声）')
# ===========================================================================
try:
    for name, trig in (('B1 原样', 'heartbeat'), ('B2 前空格', ' heartbeat'),
                       ('B3 后空格', 'heartbeat '), ('B4 换行', 'heartbeat\n')):
        n = len(frames)
        code = post({'trigger': trig, 'error_count': 0})
        time.sleep(1.2)
        got = take(n)
        check(name + ' → 一条帧都不许有', code == 200 and got == [],
              'code=%r got=%r' % (code, got))

    n = len(frames)
    code = post({'trigger': 'diagnostics', 'error_count': 2,
                 'sample_errors': [{'line': 1, 'message': 'x'}]})
    time.sleep(1.2)
    got = take(n)
    check('B5 控制组：diagnostics 2 错必须开口',
          code == 200 and len(got) == 1 and got[0][0] == 'errorAlert',
          'code=%r got=%r' % (code, got))
except Exception as exc:
    check('B 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
section('C 无后端那条出口：_builtin_reply 也不许替心跳说话')
# ===========================================================================
try:
    def builtin(trigger, count=0, items=None, reason='changed'):
        seen = []
        st._builtin_reply(count, trigger, '', items, reason,
                          lambda *a, **k: seen.append(a))
        return seen

    check('C1 后端不可用时，原样 heartbeat 也不出声',
          builtin('heartbeat') == [], repr(builtin('heartbeat')))
    check('C2 带空白的 heartbeat 同样不出声',
          builtin(' heartbeat ') == [] and builtin('heartbeat ') == [],
          repr((builtin(' heartbeat '), builtin('heartbeat '))))

    ctrl = builtin('file_scan', 2, [{'line': 3, 'message': 'x'}])
    check('C3 控制组：file_scan 2 错必须出声且是 errorAlert',
          len(ctrl) == 1 and ctrl[0][0] == 'errorAlert', repr(ctrl))
    ctrl2 = builtin('diagnostics', 0)
    check('C4 控制组：diagnostics 0 错仍要出声（说"干净了"）',
          len(ctrl2) == 1 and ctrl2[0][0] == 'chatMessage', repr(ctrl2))
except Exception as exc:
    check('C 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
section('D all_clear 与 error_count 打架：以计数为准')
# ===========================================================================
try:
    d0 = rg.generate_response({'trigger': 'all_clear', 'error_count': 0})
    check('D0 控制组：all_clear + 0 错 = chatMessage/happy',
          d0['type'] == 'chatMessage' and d0['payload']['emotion'] == 'happy',
          repr((d0['type'], d0['payload']['emotion'])))

    d1 = rg.generate_response({'trigger': 'all_clear', 'error_count': 3,
                               'sample_errors': [{'line': 4, 'message': 'boom'}]})
    check('D1 all_clear + 3 错必须改成 errorAlert（不是红色之外的气泡）',
          d1['type'] == 'errorAlert', repr(d1['type']))
    check('D2 all_clear + 3 错时情绪不许是 happy',
          d1['payload']['emotion'] != 'happy', repr(d1['payload']['emotion']))

    # 台词归属：抽 30 次，全部不能落在 all_clear 池里
    calm = set(corpus.get_lines('all_clear'))
    hits = 0
    texts = []
    for _ in range(30):
        d = rg.generate_response({'trigger': 'all_clear', 'error_count': 3,
                                  'sample_errors': [{'line': 4, 'message': 'boom'}]})
        t = d['payload']['text']
        texts.append(t)
        if t in calm:
            hits += 1
    check('D3 30 次抽样没有一句来自 all_clear 池（不能道喜）',
          hits == 0 and all(texts), 'hits=%d sample=%r' % (hits, texts[:3]))
except Exception as exc:
    check('D 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
section('E 有键但没内容：一张帧都不许发')
# ===========================================================================
try:
    for name, obj in (('E1 payload.text 空串', {'payload': {'text': ''}}),
                      ('E2 payload.text 三个空格', {'payload': {'text': '   '}}),
                      ('E3 payload.text 换行', {'payload': {'text': '\n\n'}}),
                      ('E4 message 三个空格', {'message': '   '}),
                      ('E5 扁平 text 空串', {'text': ''})):
        n = len(frames)
        code = post(obj)
        time.sleep(1.2)
        got = take(n)
        check(name + ' → 不许有帧（看不见的空气泡）', code == 200 and got == [],
              'code=%r got=%r' % (code, got))

    n = len(frames)
    code = post({'payload': {'text': '正常一句话_zz9'}})
    time.sleep(1.2)
    got = take(n)
    check('E6 控制组：有内容的照念',
          code == 200 and len(got) == 1 and got[0][2] == '正常一句话_zz9',
          'code=%r got=%r' % (code, got))
except Exception as exc:
    check('E 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
section('F 静态判据（剥注释后再判，规避"新码注释里本来就有它"）')
# ===========================================================================
try:
    def strip_comments(path):
        src = io.open(path, encoding='utf-8-sig').read()
        out = []
        for ln in src.split('\n'):
            # ⚠️ 只按行剥 `#`：本文件的旧写法都被搬进了注释（"这里以前是…"），
            # 不剥就会让判据恒 FAIL（0.4.1 踩过）
            i = ln.find('#')
            out.append(ln if i < 0 else ln[:i])
        return '\n'.join(out)

    st_src = strip_comments(os.path.join(ROOT, 'desktop_pet', 'standalone.py'))
    rg_src = strip_comments(os.path.join(ROOT, 'python_backend', 'response_generator.py'))

    check('F1 standalone 不再自己写 `str(ctx.get(trigger)) == heartbeat`',
          "str(ctx.get('trigger') or '')" not in st_src)
    check('F2 standalone 里 _is_silent_trigger 至少 3 处（导入 / 兜底 / None 分支）',
          st_src.count('_is_silent_trigger') >= 3,
          'count=%d' % st_src.count('_is_silent_trigger'))
    check('F3 response_generator 定义了 is_silent_trigger 且建在 _as_trigger 上',
          'def is_silent_trigger(' in rg_src and '_as_trigger(value) == "heartbeat"' in rg_src)
    check('F4 push_message 里挡掉全空白文本',
          'if not text.strip():' in st_src and 'MAX_PUSH_CHARS' in st_src)
    check('F5 generate_response 里 all_clear 与计数打架时改为 diagnostics',
          'trigger == "all_clear" and error_count > 0' in rg_src)

    # 结构判据：`_run` 的 `not isinstance(r, dict)` 分支里调用的是 _is_silent_trigger
    tree = ast.parse(io.open(os.path.join(ROOT, 'desktop_pet', 'standalone.py'),
                             encoding='utf-8-sig').read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_generate_async':
            for sub in ast.walk(node):
                if isinstance(sub, ast.If) and isinstance(sub.test, ast.UnaryOp):
                    seg = ast.get_source_segment(
                        io.open(os.path.join(ROOT, 'desktop_pet', 'standalone.py'),
                                encoding='utf-8-sig').read(), sub.test) or ''
                    if 'isinstance(r, dict)' in seg:
                        body = ast.get_source_segment(
                            io.open(os.path.join(ROOT, 'desktop_pet', 'standalone.py'),
                                    encoding='utf-8-sig').read(), sub) or ''
                        found.append('_is_silent_trigger' in body)
    check('F6 `_run` 的 None 分支走 _is_silent_trigger（结构判据）',
          found == [True], repr(found))
except Exception as exc:
    check('F 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ===========================================================================
print('\n==== %d PASS / %d FAIL ====' % (PASS, FAIL))
if FAIL:
    sys.exit(1)
