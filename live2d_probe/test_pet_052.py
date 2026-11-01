# -*- coding: utf-8 -*-
r"""0.3.52 回归：`file` / `line` / `message` 的非标量不许被 repr 念进气泡。

本轮修的是一条**同族**的残留：`_as_language`（0.3.50）收的是 `{language}`，
`_as_file_list`（0.3.51）收的是 `files`（只喂提示词），而 `file` / `line` /
`message` 三个字段一直只有 `_nvl` 兜着 —— 它只认 `None` 和空串，**非标量
原样放行**，于是 `%s` / `str()` 把 Python 的 repr 直接念出来。

在 0.3.51 的代码上实测（真 HTTP `/push`，看 SSE 里最终那段文字）：

    {"file": ["a.py"], "error_count": 3, ...}
      → "['a.py'] 的 bug 有 3 个…你是打算留着过年吗？"
    {"file": {"a": 1}, ...}
      → "喂，{'a': 1} 里还躺着 3 个 bug，别装没看见！"
    {"sample_errors": [{"line": {"a": 1}, "message": "x"}]}
      → "第 {'a': 1} 行附近有问题…第{'a': 1}行：x"
    {"sample_errors": [{"line": 3, "message": {"k": "v"}}]}
      → "从第 3 行开始修吧。 第3行：{'k': 'v'}"
    {"file": "x" * 5000, ...}
      → 5000 个字全进气泡（文件名语义）

提示词那条路同样被污染：`- ['a.py']:1 → x`、`- :{'a': 1} → x`。

## 段说明
  A 段  真调 `generate_response`，每个用例抽多次（台词是随机池）
  B 段  两份 `_as_label`（后端 / 降级）逐值对撞 —— 本项目已经因为
        "两实现口径不一致"修过五次，重复实现必须带对撞
  C 段  真调降级路径 `_builtin_reply`（没装 openai 的机器走这条）
  D 段  真 HTTP `/push` → 真 SSE 队列，端到端一条
  E 段  静态判据，防止有人把 `str(...)` 改回去

⚠️ 测试自身的坑（本项目踩过）：
  · 断言"不含 repr 特征"必须同时验证**控制组**含正常值，否则判据可能恒真；
  · 台词池是随机的，单个用例要抽多次，只抽一次会因为抽到不含 `{file}` 的
    台词而"恰好通过"（0.3.44 的教训）；
  · 环境变量必须在 import standalone **之前**设，否则污染生产日志/设置。
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix='airi_test052_')
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'desktop_pet'))
sys.path.insert(0, os.path.join(REPO, 'python_backend'))

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


#: repr 的指纹：Python 的 list / dict 字面量带这两种括号，正常人不会这么写注释
REPR_MARKS = ("['", "{'")

#: 唯一可识别的标记 —— 中文台词里绝不可能出现，旧码却一定会把它念出来。
#: ⚠️ 不能用 `[1, 2]` 这种"常见的畸形值"当判据（0.3.50 的教训）：
#: 旧码输出 `[1, 2]` 时我的第一版判据（只认 `['`）根本没命中，用例假通过。
MARK = 'zz9'
NUM = '951753'


def has_repr(text):
    return any(m in (text or '') for m in REPR_MARKS)


import response_generator as rg  # noqa: E402


def sample(ctx, n=25):
    """抽 n 次，返回 (命中畸形特征的次数, 问题样本, 抛异常次数, 全部文本)。

    ⚠️ `texts` 必须单独收集：只在"出问题"时才记样本的话，控制组拿不到
    任何文本，`'a.py' in joined` 恒假 —— 看起来像回归，其实是断言自己空转
    （本项目踩过：断言"某串不存在"时新码里它本来就在）。
    """
    bad, seen, boom, texts = 0, [], 0, []
    for _ in range(n):
        try:
            d = rg.generate_response(dict(ctx))
            text = d['payload']['text']
        except Exception as exc:
            boom += 1
            seen.append('%s: %s' % (type(exc).__name__, exc))
            continue
        if not text:
            boom += 1
            seen.append('(空文本)')
            continue
        texts.append(text)
        if has_repr(text):
            bad += 1
            seen.append(text)
    return bad, seen, boom, texts


# ===========================================================================
# A 段：气泡文字里不许出现 repr
# ===========================================================================
section('A 非标量字段不许把 repr 念进气泡')

try:
    BASE_ERR = [{'line': 1, 'message': 'boom'}]

    # (名字, 上下文, 旧码一定会念出来、新码一定念不出来的标记)
    # ⚠️ 标记必须"只有 repr 里才有"：中文台词池里没有 zz9 / 951753 / True
    cases = [
        ('file 是数组', {'trigger': 'file_scan', 'error_count': 3,
                       'file': [MARK + '.py'], 'sample_errors': BASE_ERR}, MARK),
        ('file 是对象', {'trigger': 'file_scan', 'error_count': 3,
                       'file': {MARK: 1}, 'sample_errors': BASE_ERR}, MARK),
        ('file 是布尔', {'trigger': 'file_scan', 'error_count': 3,
                       'file': True, 'sample_errors': BASE_ERR}, 'True'),
        ('line 是对象', {'trigger': 'file_scan', 'error_count': 3, 'file': 'a.py',
                       'sample_errors': [{'line': {MARK: 1}, 'message': 'x'}]}, MARK),
        ('line 是数组', {'trigger': 'diagnostics', 'error_count': 2,
                       'sample_errors': [{'line': [951753, 2], 'message': 'x'}]}, NUM),
        ('message 是对象', {'trigger': 'file_scan', 'error_count': 2, 'file': 'a.py',
                          'sample_errors': [{'line': 3, 'message': {MARK: 'v'}}]}, MARK),
        ('message 是数组', {'trigger': 'file_scan', 'error_count': 2, 'file': 'a.py',
                          'sample_errors': [{'line': 3, 'message': [MARK, 'y']}]}, MARK),
    ]
    for i, (name, ctx, leaked) in enumerate(cases, 1):
        # A5 走 diagnostics 分支：`{line}` 只在 9 条 syntax_error 台词里的 2 条
        # 出现，25 抽样有 ~0.04% 的概率一条都没抽到（判据会假通过）。
        # 抬到 60 抽样把偶发压到 1e-6 量级 —— 回归套件不许自己掷骰子。
        bad, seen, boom, texts = sample(ctx, 60 if 'line 是数组' in name else 25)
        # ⚠️ 必须在**全部**文本里找标记，不能在 `seen`（只有判定为坏的）里找：
        # `has_repr` 认的是 `['` / `{'` 特征，而 "瞄了一眼 True…"、
        # "第 [951753, 2] 行…" 两种泄漏都**不含**这两个特征 —— 只在 seen 里
        # 找的话这两条旧码会假通过（实测踩过）。
        hit = [t for t in texts if leaked in t]
        check('A%d %s —— 抽样里一次都不念出 %r' % (i, name, leaked),
              not hit and boom == 0 and bad == 0,
              'hit=%d bad=%d boom=%d n=%d 例=%r'
              % (len(hit), bad, boom, len(texts), (hit or seen or texts)[:2]))

    # 控制组：正常值必须还在（否则"把这些字段整个丢掉"也能过上面 7 条）
    bad, seen, boom, texts = sample({'trigger': 'file_scan', 'error_count': 2,
                                     'file': 'a.py',
                                     'sample_errors': [{'line': 3, 'message': 'boom'}]})
    joined = ' | '.join(texts)
    check('A8 控制组：正常 file/line/message 仍然念出来（不许为了堵畸形全丢）',
          bad == 0 and boom == 0 and 'a.py' in joined and '第3行' in joined,
          'bad=%d boom=%d n=%d 例=%r' % (bad, boom, len(texts), texts[:2]))

    # 超长 file：语义是"文件名"，5KB 不该整段进气泡
    d = rg.generate_response({'trigger': 'file_scan', 'error_count': 1,
                              'file': 'x' * 5000,
                              'sample_errors': [{'line': 1, 'message': 'm'}]})
    text = d['payload']['text']
    check('A9 5KB 的 file 被截断（旧码 5000 字全进气泡）',
          len(text) < 400 and 'x' * 500 not in text,
          'len=%d' % len(text))

    # 各类标量都不许炸
    boom_cases = [
        {'trigger': 'diagnostics', 'error_count': 1, 'sample_errors': [None]},
        {'trigger': 'diagnostics', 'error_count': 1, 'sample_errors': [b'oops']},
        {'trigger': 'diagnostics', 'error_count': 1,
         'sample_errors': [{'line': float('nan'), 'message': float('inf')}]},
        {'trigger': 'file_scan', 'error_count': 2, 'file': float('nan'),
         'sample_errors': [{'line': True, 'message': None}]},
    ]
    errs = []
    for ctx in boom_cases:
        try:
            t = rg.generate_response(ctx)['payload']['text']
            if not t or has_repr(t):
                errs.append(t)
        except Exception as exc:
            errs.append('%s: %s' % (type(exc).__name__, exc))
    check('A10 None/bytes/nan/inf/布尔 都不抛且没有 repr', not errs, repr(errs[:2]))

    # 提示词侧：桩 client 抓 user_prompt
    class _Msg(object):
        def __init__(self, c):
            self.content = c

    class _Choice(object):
        def __init__(self, c):
            self.message = _Msg(c)

    class _Resp(object):
        def __init__(self, c):
            self.choices = [_Choice(c)]

    class _Comp(object):
        def __init__(self, rec):
            self.rec = rec

        def create(self, **kw):
            self.rec.append(kw['messages'][1]['content'])
            return _Resp('好的')

    class _Chat(object):
        def __init__(self, rec):
            self.completions = _Comp(rec)

    class _Client(object):
        def __init__(self, rec):
            self.chat = _Chat(rec)

    rec = []
    ok_stub = False
    old_key = os.environ.get('DEEPSEEK_API_KEY')
    try:
        import openai
        real_openai = openai.OpenAI
        openai.OpenAI = lambda *a, **k: _Client(rec)
        os.environ['DEEPSEEK_API_KEY'] = 'sk-fake-052'
        ok_stub = True

        rg.generate_response({'trigger': 'file_scan', 'error_count': 1,
                              'file': ['a.py'],
                              'sample_errors': [{'line': {'a': 1}, 'message': 'x'}]})
        prompt = rec[0] if rec else ''
        check('A11 提示词里也没有 repr（旧码 "- [\'a.py\']:1 → x"）',
              bool(prompt) and not has_repr(prompt), 'prompt=%r' % prompt[:160])
        check('A12 提示词里 file 为空时读得通（不留"打开的文件  有"双空格）',
              '打开的文件  有' not in prompt, 'prompt=%r' % prompt[:160])

        rec.clear()
        rg.generate_response({'trigger': 'file_scan', 'error_count': 1,
                              'file': 'a.py',
                              'sample_errors': [{'line': 3, 'message': 'boom'}]})
        prompt2 = rec[0] if rec else ''
        check('A13 控制组：正常值照旧进提示词', 'a.py' in prompt2 and '3' in prompt2,
              'prompt=%r' % prompt2[:140])
    except Exception as exc:
        check('A11-13 提示词段未崩', False, '%s: %s' % (type(exc).__name__, exc))
    finally:
        if ok_stub:
            openai.OpenAI = real_openai
        if old_key is None:
            os.environ.pop('DEEPSEEK_API_KEY', None)
        else:
            os.environ['DEEPSEEK_API_KEY'] = old_key

except Exception as exc:
    check('A 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# B 段：两份 _as_label 必须同一个口径
# ===========================================================================
section('B 两份 _as_label（后端 / 降级）逐值对撞')

try:
    import standalone  # noqa: E402

    a1 = getattr(rg, '_as_label', None)
    a2 = getattr(standalone, '_as_label', None)
    check('B1 两边都有 _as_label', callable(a1) and callable(a2),
          'rg=%r st=%r' % (a1, a2))
    if callable(a1) and callable(a2):
        vals = [['a.py'], {'a': 1}, ('t',), set(), 3, 3.0, 3.7, 0, -2,
                True, False, None, '', '  ', 'a.py', float('nan'),
                float('inf'), float('-inf'), b'x', 'x' * 999]
        diff = [(v, a1(v), a2(v)) for v in vals if a1(v) != a2(v)]
        check('B2 20 个输入两边答案完全相同（同一请求在"装了 openai"与'
              '"没装"的机器上气泡必须一样）', not diff,
              'diff=%r' % diff[:3])

        check('B3 非标量一律回落占位符，绝不 str()',
              a1(['a.py']) == '?' and a1({'a': 1}) == '?'
              and a1(True) == '?' and a1(None) == '?',
              'got=%r' % [a1(['a.py']), a1({'a': 1}), a1(True), a1(None)])
        check('B4 合法的 0 / 整数 / 整数 float 照旧可用',
              a1(0) == '0' and a1(3) == '3' and a1(3.0) == '3',
              'got=%r' % [a1(0), a1(3), a1(3.0)])
        check('B5 长度上限生效', len(a1('x' * 999)) == 200
              and len(a1('y' * 999, '?', 12)) == 12,
              'got=%d/%d' % (len(a1('x' * 999)), len(a1('y' * 999, '?', 12))))
except Exception as exc:
    check('B 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# C 段：降级路径 _builtin_reply（没装 openai 的机器走这条）
# ===========================================================================
section('C 降级台词 _builtin_reply 同样不许念 repr')

try:
    got = []
    fn = getattr(standalone, '_builtin_reply', None)
    check('C0 有 _builtin_reply', callable(fn))
    if callable(fn):
        fn(2, 'file_scan', ['a.py'],
           [{'line': {'a': 1}, 'message': {'k': 'v'}}], 'changed',
           _push=lambda t, tx, em: got.append(tx))
        txt = got[0] if got else ''
        check('C1 file 数组 / line 对象 / message 对象 都不念 repr',
              bool(txt) and not has_repr(txt), 'text=%r' % txt[:120])
        got[:] = []
        fn(2, 'file_scan', 'a.py', [{'line': 3, 'message': 'boom'}], 'changed',
           _push=lambda t, tx, em: got.append(tx))
        check('C2 控制组：正常值照旧念出来',
              'a.py' in (got[0] if got else '') and '第3行' in (got[0] if got else ''),
              'text=%r' % (got[0] if got else '')[:100])
except Exception as exc:
    check('C 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# D 段：真 HTTP /push → 真 SSE 队列
# ===========================================================================
section('D 真 HTTP /push 端到端')

try:
    BASE = None
    for port in range(22440, 22450):
        ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(port, ready),
                         daemon=True).start()
        if ready.wait(3):
            BASE = port
            break
    check('D0 测试服务器已起', BASE is not None, 'port=%r' % BASE)

    if BASE:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def push(obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(
                'http://127.0.0.1:%d/push' % BASE, data=body, method='POST',
                headers={'Content-Type': 'application/json'})
            try:
                with opener.open(req, timeout=8) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code
            except Exception as exc:
                return repr(exc)

        def drain(timeout=6.0):
            t0 = time.time()
            while time.time() - t0 < timeout:
                with standalone.queue_lock:
                    items = list(standalone.sse_queue)
                if items:
                    time.sleep(0.15)
                    with standalone.queue_lock:
                        items = list(standalone.sse_queue)
                        standalone.sse_queue.clear()
                    return [json.loads(d) for _s, d in items]
                time.sleep(0.05)
            return []

        bad = []
        for _ in range(2):
            st = push({'trigger': 'file_scan', 'error_count': 2,
                       'file': ['a.py'],
                       'sample_errors': [{'line': [1, 2], 'message': {'k': 'v'}}]})
            for m in drain():
                t = m.get('payload', {}).get('text', '')
                if st != 200 or not t or has_repr(t):
                    bad.append((st, t))
        check('D1 /push（file 数组 + line 数组 + message 对象）→ 气泡干净',
              not bad, repr(bad[:2]))
except Exception as exc:
    check('D 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# E 段：静态判据
# ===========================================================================
section('E 静态：归一化入口还在被用')

try:
    with io.open(os.path.join(REPO, 'python_backend', 'response_generator.py'),
                 encoding='utf-8-sig') as f:
        gsrc = f.read()
    with io.open(os.path.join(REPO, 'desktop_pet', 'standalone.py'),
                 encoding='utf-8-sig') as f:
        ssrc = f.read()

    check('E1 generate_response 用 _as_label 收敛 file',
          '_as_label(context.get("file"), "", 120)' in gsrc)
    check('E2 sample_errors 的 line / message / file 逐个归一化',
          '_as_label(item.get("line")' in gsrc
          and '_as_label(item.get("message")' in gsrc
          and '_as_label(item.get("file")' in gsrc)

    # ⚠️ 判"旧写法已消失"前必须先剥掉**注释**：本轮的修复注释里就引用了那句
    # 旧代码，直接 `in` 会命中注释 —— 判据变成恒假（本项目踩过，见 E3 注释）
    def strip_line_comments(src):
        return '\n'.join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith('#'))

    gcode = strip_line_comments(gsrc)
    check('E3 旧的 `str(context.get("file", "") or "当前文件")` 已消失（注释不算）',
          'str(context.get("file", "") or "当前文件")' not in gcode)
    check('E4 降级路径不再用 str(it.get(\'message\')) 拼气泡',
          "str(it.get('message'" not in ssrc
          and "_as_label(it.get('message'), '', 40)" in ssrc)
    check('E5 降级路径的 fname 不再用 `file or \'当前文件\'`',
          "fname = file or '当前文件'" not in ssrc
          and "_as_label(file, '当前文件', 120)" in ssrc)

    # 结构性质：函数体最后一行必须是 `return fallback` —— 只有这样，
    # 走到末尾的 dict / list 才会回落占位符而**不是**被 str() 念出去。
    # （不能断言"函数里没有 str(" ：int / float 分支本来就该用 str()。）
    if 'def _as_label' not in gsrc:
        check('E6 _as_label 的兜底分支是 `return fallback`（dict/list 绝不 str()）',
              False, '函数不存在')
    else:
        body = gsrc.split('def _as_label')[1].split('def _encodable')[0]
        tail_code = [ln.strip() for ln in body.splitlines()
                     if ln.strip() and not ln.lstrip().startswith('#')]
        check('E6 _as_label 的兜底分支是 `return fallback`（dict/list 绝不 str()）',
              bool(tail_code) and tail_code[-1] == 'return fallback',
              'tail=%r' % tail_code[-1:])
except Exception as exc:
    check('E 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(HERE, '_t052.txt'), 'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
