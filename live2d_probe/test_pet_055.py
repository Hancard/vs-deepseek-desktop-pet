# -*- coding: utf-8 -*-
"""0.4.1 回归：`/push` 不许把**请求体**（JSON 结构）当台词念出来。

## 缺陷

`do_POST` 最后一个 else 分支的兜底是 `msg.get('message', body)` —— `body`
是**原始请求体**。于是"结构化但没写内容"的推送会让桌宠念出一坨 JSON。
实测（旧代码，本次修复前）的真 SSE 帧：

    {"type":"errorAlert","payload":{"emotion":"angry"}}
      -> text = '{"type": "errorAlert", "payload": {"emotion": "angry"}}'
    {"type":"chatMessage","payload":"hi"}   -> 念整串
    {"type":"chatMessage","payload":{}}    -> 念整串
    {"foo": 1}                             -> 念 '{"foo": 1}'
    {}                                     -> 念 '{}'
    {"text": "..."}                        -> 念 '{"text": "..."}'（扁平写法也被套壳）

用户视角：桌宠对着你说出一串花括号引号，既不解决问题也像坏了。

## 判据为什么是"有没有帧"而不是"文本里有没有花括号"

台词库里本来就有带括号/引号的句子（颜文字、`(╯°□°)╯`），拿"文本含 {" 判会
误伤控制组。这里改成**副作用判据**：这几种输入到底有没有产生 SSE 帧。

## 修复后的口径

只认两种"调用方明确说了要说什么"的形态：顶层 `message`、顶层 `text`。
其余静默丢弃（HTTP 仍 200 + pushed），并在日志留一行 `push ignored`。
`payload.text` / `diagnostics` / `trigger` 三条正常分支一个字都没动 ——
它们有各自的控制组断言（A8/A9/A10）。
"""
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


TMP = tempfile.mkdtemp(prefix='airi_test055_')
# 环境变量必须在 import standalone **之前**设好（否则会写生产日志/设置）
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
os.environ.pop('DEEPSEEK_API_KEY', None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'desktop_pet'))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'python_backend'))

print('--- 0.4.1 /push 不许念请求体 ---')

frames = []
ready = threading.Event()
PORT = 19953
st = None

try:
    import standalone as st

    def _listen():
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/events' % PORT,
                                        timeout=40) as r:
                for raw in r:
                    ln = raw.decode('utf-8', 'replace').strip()
                    if ln.startswith('data:'):
                        try:
                            frames.append(json.loads(ln[5:].strip()))
                        except Exception:
                            pass
        except Exception:
            pass

    threading.Thread(target=st.start_server, args=(PORT, ready), daemon=True).start()
    check('A0 进程内服务器起来了', ready.wait(8.0))
    threading.Thread(target=_listen, daemon=True).start()
    time.sleep(0.5)
except Exception as exc:
    check('A0 服务器段未崩', False, '%s: %r' % (type(exc).__name__, exc))


def post_raw(raw: bytes):
    req = urllib.request.Request('http://127.0.0.1:%d/push' % PORT, data=raw,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()


def post(obj):
    return post_raw(json.dumps(obj, ensure_ascii=False).encode())


def texts_since(n):
    return [((f.get('payload') or {}).get('text') or '') for f in frames[n:]]


# ---------------------------------------------------------------------------
print('\n--- A 结构化但没内容：不许念 JSON ---')
SILENT = [
    ('A1 结构化推送但没 text', {'type': 'errorAlert', 'payload': {'emotion': 'angry'}}),
    ('A2 payload 是字符串', {'type': 'chatMessage', 'payload': 'hi'}),
    ('A3 payload 空对象', {'type': 'chatMessage', 'payload': {}}),
    ('A4 无关对象', {'foo': 1}),
    ('A5 空对象', {}),
]
try:
    for name, obj in SILENT:
        before = len(frames)
        code, resp = post(obj)
        time.sleep(1.2)
        got = texts_since(before)
        # 判据 1：HTTP 仍受理（兼容老客户端）
        # 判据 2：一条帧都不许产生
        check(name, code == 200 and 'pushed' in resp and not got,
              'code=%r texts=%r' % (code, [t[:60] for t in got]))
except Exception as exc:
    check('A1~A5 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ---------------------------------------------------------------------------
print('\n--- B 明确说了内容：必须照念（控制组，防收口过头） ---')
try:
    before = len(frames)
    post({'message': '纯文本控制组_A'})
    time.sleep(1.2)
    got = texts_since(before)
    check('B1 message 键照念', got == ['纯文本控制组_A'], repr([t[:40] for t in got]))

    before = len(frames)
    post({'text': '扁平控制组_B'})
    time.sleep(1.2)
    got = texts_since(before)
    check('B2 扁平 text 键照念（脚本手敲 POST 的写法）',
          got == ['扁平控制组_B'], repr([t[:40] for t in got]))

    before = len(frames)
    post({'message': {'a': 1}})
    time.sleep(1.2)
    got = texts_since(before)
    check('B3 message 是对象时序列化成文本（0.3.42 的既有口径）',
          len(got) == 1 and '"a": 1' in got[0], repr([t[:40] for t in got]))

    before = len(frames)
    post({'message': ''})
    time.sleep(1.2)
    got = texts_since(before)
    check('B4 空 message 仍照旧推送（不扩大本次改动范围）',
          got == [''], repr([t[:20] for t in got]))
except Exception as exc:
    check('B 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ---------------------------------------------------------------------------
print('\n--- C 三条正常分支一个字都没动 ---')
try:
    before = len(frames)
    post({'payload': {'text': 'payload 分支_OK', 'emotion': 'happy'}})
    time.sleep(1.2)
    got = texts_since(before)
    check('C1 payload.text 分支照念', got == ['payload 分支_OK'], repr([t[:40] for t in got]))

    before = len(frames)
    post({'type': 'diagnostics', 'payload': {'count': 2, 'items': [
        {'file': 'a.py', 'line': 3, 'message': "SyntaxError: '(' was never closed"}],
        'language': 'python'}})
    time.sleep(2.6)
    new = frames[before:]
    types = sorted({f.get('type') for f in new})
    check('C2 diagnostics 分支仍出 errorAlert', types == ['errorAlert'], repr(types))
    check('C2-b diagnostics 的文本不是请求体',
          all('"count"' not in ((f.get('payload') or {}).get('text') or '') for f in new),
          repr([((f.get('payload') or {}).get('text') or '')[:50] for f in new]))

    before = len(frames)
    post({'trigger': 'all_clear', 'error_count': 0, 'language': 'unknown',
          'files': [], 'sample_errors': []})
    time.sleep(2.6)
    got = texts_since(before)
    check('C3 all_clear 分支仍有话说', len(got) >= 1 and all(t.strip() for t in got),
          repr([t[:30] for t in got]))
except Exception as exc:
    check('C 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ---------------------------------------------------------------------------
print('\n--- D 完全不是 JSON 的 body 仍按纯文本念（curl 风格） ---')
try:
    before = len(frames)
    code, resp = post_raw(b'hello from curl')
    time.sleep(1.2)
    got = texts_since(before)
    check('D1 非 JSON body 照念原文', code == 200 and got == ['hello from curl'],
          'code=%r texts=%r' % (code, [t[:30] for t in got]))
except Exception as exc:
    check('D 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

# ---------------------------------------------------------------------------
print('\n--- E 静态判据（防回退） ---')
try:
    src = io.open(os.path.join(os.path.dirname(HERE), 'desktop_pet',
                               'standalone.py'), encoding='utf-8-sig').read()
    # ⚠️ 静态判据必须先剥注释：旧写法本身留在新代码的注释里当"此处以前是…"，
    # 不剥的话 E1 恒 FAIL —— 这是"断言某串不存在，而新码里它本来就在"的老坑。
    code = '\n'.join(ln for ln in src.splitlines()
                     if not ln.strip().startswith('#'))
    check('E1 不再拿请求体当兜底',
          "msg.get('message', body)" not in code
          and 'msg.get("message", body)' not in code)
    check('E2 只认 message / text 两个键',
          "content = msg.get('message')" in src and "content = msg.get('text')" in src)
    check('E3 丢弃时留痕', 'push ignored: object without' in src)
except Exception as exc:
    check('E 段未崩', False, '%s: %r' % (type(exc).__name__, exc))

print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(HERE, '_t055.txt'), 'w', encoding='utf-8').write(
        '%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
