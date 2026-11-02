# -*- coding: utf-8 -*-
r"""0.3.54 回归：`/push` 的 `trigger` / `emotion` 两个字段必须归一化。

`trigger` 是 `dict.get` 默认值陷阱的**第五个**入口（前四个：`error_count`
0.3.40、`line` 0.3.44、`language` 0.3.50、`file`/`message` 0.3.52）。它比
前面几个都重 —— 错的不是某个字，而是**整件事的性质**：

    trigger='diagnostics'  count=5  → errorAlert / surprised / "天哪 5 个错误！…"
    trigger=null           count=5  → chatMessage / idle      / "…你的代码确实有进步了。"
    trigger=["file_scan"]  count=5  → chatMessage / idle      / "慢慢来嘛，谁一开始就写得好？"
    trigger=123            count=5  → chatMessage / idle      / "累了就喝口水休息一下。"

三条畸形输入都是 HTTP 200、"已推送"、日志零行；用户看到的是**明明有 5 个
bug，桌宠却在夸他**，连气泡颜色都不是红的。

`emotion` 是同一个"白名单缺失"的另一半：`_as_msg_type` 早就有 `_PUSH_TYPES`
了（注释写着"不认识的名字则在 switch 里落空"），`_as_emotion` 只做了类型
检查。陌生值会被前端拿去做两件事，两件都出事：`data-emotion` 让四条 CSS
表情规则**全部失配**（角色表情停住 8 秒），`CHAR_IMG.replace(...)` 拼出一个
必然 404 的图片地址。

## 段说明
  A 段  `_as_trigger` 逐值（纯函数，表驱动）
  B 段  `diagnostics` + 0 错不再落进 syntax_error 池 —— 判据是**台词归属**：
        抽样结果必须全在 encourage 池里（旧码必然产出 syntax_error 的 9 条之一）
  C 段  起**真** standalone，走真 `/push` + 真 SSE，用帧里的 type 判定
        （副作用判据，不是读代码）
  D 段  `_as_emotion` 白名单，并与语料 / 前端三处常量对撞
  E 段  静态判据：不许改回裸 `.get("trigger", ...)`

⚠️ 测试自身的坑：
  · 环境变量必须在 import standalone **之前**设，否则污染生产日志/设置
  · C 段读 SSE 必须在单独线程里（主线程 readline 会等到 socket 超时）
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request

TMP = tempfile.mkdtemp(prefix='airi_test054_')
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
os.environ['DEEPSEEK_API_KEY'] = ''

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


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_sse(port, wait=1.5):
    """连一次 /events，返回 data 帧列表。

    ⚠️ 读必须在单独线程里做：主线程 readline 会一直等到 socket 超时
    （服务端空闲时 15 秒才发一次心跳注释行）。
    """
    bodies = []
    try:
        resp = OPENER.open('http://127.0.0.1:%d/events' % port, timeout=10)
    except Exception as exc:
        return bodies

    def _reader():
        try:
            while True:
                raw = resp.readline()
                if not raw:
                    return
                line = raw.decode('utf-8', 'replace').strip()
                if line.startswith('data:'):
                    try:
                        bodies.append(json.loads(line[5:].strip()))
                    except Exception:
                        pass
        except Exception:
            return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    time.sleep(wait)
    try:
        resp.close()
    except Exception:
        pass
    t.join(1.2)
    return bodies


try:
    import response_generator as rg
    import standalone as st
    import corpus

    rg.try_deepseek_api = lambda *a, **k: None      # 绝不走网络

    # ======================================================================
    section('A 段：_as_trigger 逐值')
    # ======================================================================
    # ⚠️ 用 getattr 取而不是直接 `rg._as_trigger`：本用例必须能在**旧码**上跑
    # 出一份可读的失败清单。直接属性访问在旧码上是 AttributeError，会被最外层
    # 那个 except 吞掉，后面 B/C/D/E 四段**一条都不会执行** —— 看起来像"只
    # 崩了一个地方"，实际是判据失效（本项目 0.3.45 踩过：用例数变少就是信号）。
    _as_trigger = getattr(rg, '_as_trigger', None)
    check('A0 response_generator 暴露了 _as_trigger（0.3.54 新增）',
          callable(_as_trigger), '模块里没有这个函数')

    if callable(_as_trigger):
        A_CASES = [
            ('file_scan', 'file_scan'), ('diagnostics', 'diagnostics'),
            ('all_clear', 'all_clear'), ('greeting', 'greeting'),
            ('heartbeat', 'heartbeat'), ('  file_scan  ', 'file_scan'),
        ]
        bad = [(v, _as_trigger(v)) for v, want in A_CASES
               if _as_trigger(v) != want]
        check('A1 五个已知 trigger（含前后空白）原样返回', not bad, repr(bad))

        A_FALLBACK = [None, '', '   ', 'hello', 123, 3.5, True, False,
                      ['file_scan'], {'t': 'file_scan'}, b'file_scan']
        bad = [(v, _as_trigger(v)) for v in A_FALLBACK
               if _as_trigger(v) != 'diagnostics']
        check('A2 未知 / 非字符串一律回落 diagnostics', not bad, repr(bad))

    check('A3 _TRIGGERS 与前端/主入口的实际取值集合一致',
          getattr(rg, '_TRIGGERS', None) == {'file_scan', 'diagnostics',
                                             'all_clear', 'greeting',
                                             'heartbeat'},
          repr(sorted(getattr(rg, '_TRIGGERS', ()))))

    # ======================================================================
    section('B 段：diagnostics + 0 错不再念「第 ? 行少了个括号」')
    # ======================================================================
    encourage = set(corpus.get_lines('encourage'))
    syntax = set(corpus.get_lines('syntax_error'))
    check('B0 两个池都非空且不重叠',
          bool(encourage) and bool(syntax) and not (encourage & syntax),
          'enc=%d syn=%d 交集=%r' % (len(encourage), len(syntax),
                                     sorted(encourage & syntax)[:2]))

    samples = []
    for _ in range(20):
        d = rg.generate_response({'trigger': 'diagnostics', 'error_count': 0,
                                  'sample_errors': [], 'language': 'python'})
        samples.append((d or {}).get('payload', {}).get('text', ''))

    stray = [t for t in samples if t not in encourage]
    check('B1 20 次抽样全部来自 encourage 池（旧码会落在 syntax_error）',
          not stray, '越界 %d 条，例：%r' % (len(stray), stray[:2]))

    d = rg.generate_response({'trigger': 'diagnostics', 'error_count': 0,
                              'sample_errors': []})
    emo = (d or {}).get('payload', {}).get('emotion')
    typ = (d or {}).get('type')
    check('B2 0 错时情绪是 idle、类型是 chatMessage', emo == 'idle' and typ == 'chatMessage',
          'emotion=%r type=%r' % (emo, typ))

    # 控制组：有错时必须仍然是 errorAlert + 错误类台词
    d = rg.generate_response({'trigger': 'diagnostics', 'error_count': 3,
                              'sample_errors': [{'line': 3, 'message': 'x'}],
                              'language': 'python'})
    typ = (d or {}).get('type')
    check('B3 控制组：count=3 仍然是 errorAlert', typ == 'errorAlert', 'type=%r' % typ)

    # ======================================================================
    section('C 段：真服务端 + 真 /push —— 畸形 trigger 也必须报错')
    # ======================================================================
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(('127.0.0.1', 0), st.AiriHandler)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    frames = []
    stop = threading.Event()

    def _collect():
        try:
            resp = OPENER.open('http://127.0.0.1:%d/events' % PORT, timeout=10)
        except Exception:
            return
        try:
            while not stop.is_set():
                raw = resp.readline()
                if not raw:
                    return
                line = raw.decode('utf-8', 'replace').strip()
                if line.startswith('data:'):
                    try:
                        frames.append(json.loads(line[5:].strip()))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    threading.Thread(target=_collect, daemon=True).start()
    time.sleep(0.4)

    ITEMS = [{'line': 3, 'message': 'expected ;'},
             {'line': 9, 'message': 'undefined name'}]
    TRIG_VALUES = [None, ['file_scan'], 123, {'a': 1}]
    for v in TRIG_VALUES:
        body = {'type': 'diagnostics',
                'payload': {'trigger': v, 'count': 5, 'items': ITEMS,
                            'language': 'python'}}
        req = urllib.request.Request('http://127.0.0.1:%d/push' % PORT,
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        with OPENER.open(req, timeout=10) as r:
            code = r.status
        check('C1 /push(trigger=%r) 回 200' % (v,), code == 200, 'code=%d' % code)
        time.sleep(0.9)

    time.sleep(1.2)
    stop.set()
    time.sleep(0.2)

    types = [f.get('type') for f in frames]
    check('C2 四条畸形 trigger 全部收到 errorAlert（0.3.54 前是 chatMessage）',
          types.count('errorAlert') >= 3 and 'chatMessage' not in types,
          'types=%r' % types)
    emos = [(f.get('payload') or {}).get('emotion') for f in frames]
    check('C3 情绪不再是 idle（0.3.54 前四组都是 idle）',
          'idle' not in emos and bool(emos), 'emotions=%r' % emos)
    texts = [(f.get('payload') or {}).get('text', '') for f in frames]
    check('C4 文本里不出现 encourage 池的台词',
          not [t for t in texts if t in encourage], repr(texts[:2]))

    # ======================================================================
    section('D 段：_as_emotion 白名单')
    # ======================================================================
    ok_vals = ['idle', 'greeting', 'angry', 'happy', 'surprised', ' happy ']
    bad = [(v, st._as_emotion(v)) for v in ok_vals
           if st._as_emotion(v) != v.strip()]
    check('D1 五个合法情绪原样返回（含前后空白）', not bad, repr(bad))

    bad_vals = ['nope', '../evil', 'HAPPY', 'a"b', '', '   ', None, 123, True,
                ['happy'], {'e': 'happy'}]
    bad = [(v, st._as_emotion(v)) for v in bad_vals
           if st._as_emotion(v) != 'idle']
    check('D2 陌生 / 非字符串一律回落 idle（0.3.54 前原样放行）', not bad, repr(bad))

    _EMOTIONS = getattr(st, '_EMOTIONS', None)
    check('D3 _EMOTIONS 与语料 EMOTION_MAP 的取值集合完全一致',
          _EMOTIONS is not None and _EMOTIONS == set(corpus.EMOTION_MAP.values()),
          '后端=%r' % (sorted(_EMOTIONS) if _EMOTIONS else '模块里没有 _EMOTIONS'))
    check('D4 _EMOTIONS 与语料 CLICK_EMOTIONS 完全一致',
          _EMOTIONS is not None and _EMOTIONS == set(corpus.CLICK_EMOTIONS),
          '后端=%r' % (sorted(_EMOTIONS) if _EMOTIONS else '模块里没有 _EMOTIONS'))

    ui = io.open(os.path.join(REPO, 'desktop_pet', 'ui.html'),
                 encoding='utf-8').read()
    blk = ui.split('var EMOTION_EXPRESSION = {')[1].split('};')[0]
    ui_keys = set(re.findall(r'(\w+)\s*:', blk))
    check('D5 _EMOTIONS 与前端 EMOTION_EXPRESSION 的键完全一致',
          _EMOTIONS is not None and _EMOTIONS == ui_keys,
          '前端=%r 后端=%r' % (sorted(ui_keys),
                              sorted(_EMOTIONS) if _EMOTIONS else '(无)'))

    # ======================================================================
    section('E 段：静态判据（防回退）')
    # ======================================================================
    gsrc = io.open(os.path.join(REPO, 'python_backend', 'response_generator.py'),
                   encoding='utf-8-sig').read()
    ssrc = io.open(os.path.join(REPO, 'desktop_pet', 'standalone.py'),
                   encoding='utf-8-sig').read()

    check('E1 generate_response / try_deepseek_api 都走 _as_trigger',
          gsrc.count('_as_trigger(context.get("trigger"))') == 2,
          '出现 %d 次' % gsrc.count('_as_trigger(context.get("trigger"))'))
    check('E2 不再有裸的 context.get("trigger", "diagnostics")',
          'context.get("trigger", "diagnostics")' not in gsrc)
    check('E3 diagnostics 分支有 error_count <= 0 的出口',
          bool(re.search(r'elif trigger == "diagnostics":\s*\n\s*if error_count <= 0:',
                         gsrc)))
    check('E4 _as_emotion 走白名单而不是「非空就放行」',
          'text in _EMOTIONS' in ssrc)
    check('E5 push_message 仍是唯一出口（两处归一化都在里面）',
          'msg_type = _as_msg_type(msg_type)' in ssrc
          and 'emotion = _as_emotion(emotion)' in ssrc)

except Exception as exc:
    check('整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(HERE, '_t054.txt'), 'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
