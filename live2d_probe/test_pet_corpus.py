# -*- coding: utf-8 -*-
"""test_pet_corpus.py — 「编辑语料」功能（v0.3.29）的离线回归。

被测的东西：右键菜单「编辑语料」→ 生成/打开 ~/.dsh/pets/airi-pet-corpus.jsonc
→ 改完保存立刻生效（热重载）→ 写坏了自动回落到内置台词。

覆盖：
  A. strip_jsonc 状态机（注释 / 尾随逗号 / 字符串里的 //）
  B. 导出：结构、占位符提示、幂等（不覆盖用户已有文件）
  C. 回落：坏 JSON / 场景缺失 / 空数组 / 非法占位符 / 非法情绪词
  D. 热重载：改文件立刻生效
  E. 接线：response_generator.pick_corpus 走 corpus.get_lines
  F. 接线：standalone._pick_click_line 走 corpus.get_click_lines
  G. /settings 的 openCorpus 请求 + /event 的 corpusOpened 回执 + 三级回落
  H. **用户真实文件零改动**（测试必须把 AIRI_PET_* 全隔离到 %TEMP%）
  I. 内置语料自洽（占位符 ⊆ 白名单、情绪词前端认识、场景表对齐）
  J. 三端接线（ui.html 菜单项 / extension.ts 的打开逻辑）

用法：用带 pywebview 的那个解释器跑（Python312），结果写
      live2d_probe/pet_corpus_result.txt
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

PROBE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PROBE)
PET = os.path.join(ROOT, 'desktop_pet')
BACKEND = os.path.join(ROOT, 'python_backend')

# --- 隔离：**必须在 import standalone / corpus 之前** ---------------------------
# corpus.CORPUS_PATH / standalone.SETTINGS_PATH / LOG_PATH 都是模块加载时算出来的。
# 不隔离的话，下面那一串 POST 与导出动作改的是**用户真实的**语料与设置。
TMP_CORPUS = os.path.join(tempfile.gettempdir(), 'airi_test_pet_corpus.jsonc')
TMP_SETTINGS = os.path.join(tempfile.gettempdir(), 'airi_test_pet_settings2.json')
TMP_LOG = os.path.join(tempfile.gettempdir(), 'airi_test_pet_corpus.log')
os.environ['AIRI_PET_CORPUS'] = TMP_CORPUS
os.environ['AIRI_PET_SETTINGS'] = TMP_SETTINGS
os.environ['AIRI_PET_LOG'] = TMP_LOG

sys.path.insert(0, PET)
sys.path.insert(0, BACKEND)

OUT = os.path.join(PROBE, 'pet_corpus_result.txt')
PORT = 19901

_lines = []
_fail = 0


def out(s=''):
    _lines.append(str(s))
    try:
        io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    except OSError:
        pass


def check(name, ok, detail=''):
    global _fail
    if not ok:
        _fail += 1
    out('  [%s] %-58s %s' % ('PASS' if ok else 'FAIL', name,
                             (':: ' + str(detail)) if detail else ''))


# 真实文件（用来在最后断言"一个字节都没动过"）
REAL_CORPUS = os.path.join(os.path.expanduser('~'), '.dsh', 'pets',
                           'airi-pet-corpus.jsonc')
REAL_SETTINGS = os.path.join(os.path.expanduser('~'), '.dsh', 'pets',
                             'airi-pet-settings.json')


def snapshot(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


REAL_BEFORE = {p: snapshot(p) for p in (REAL_CORPUS, REAL_SETTINGS)}

# H 段要用：测试**开始前**真实设置文件里有没有 openCorpusSeq。
# 它是「编辑语料」功能的正常产物 —— 用户用过一次就会写在里面（0.3.36 实锤：
# 跑测试时用户文件里就有 "openCorpusSeq": 1，把"文件里不许有"这条断言打红了）。
# 所以正确的断言是"测试没让它**新出现**"，不是"文件里不许有"。
_real_had_seq_before = False
if os.path.exists(REAL_SETTINGS):
    try:
        _real_had_seq_before = ('openCorpusSeq' in
                                io.open(REAL_SETTINGS, encoding='utf-8').read())
    except OSError:
        pass

for p in (TMP_CORPUS, TMP_SETTINGS, TMP_LOG, TMP_CORPUS + '.tmp',
          TMP_SETTINGS + '.tmp'):
    try:
        os.remove(p)
    except OSError:
        pass

import corpus            # noqa: E402
import response_generator as rg   # noqa: E402
import standalone        # noqa: E402

UI = io.open(os.path.join(PET, 'ui.html'), encoding='utf-8').read()
EXT = io.open(os.path.join(ROOT, 'src', 'extension.ts'), encoding='utf-8').read()

out('=' * 78)
out('A. strip_jsonc 状态机')
out('=' * 78)

check('剥掉行注释与块注释',
      json.loads(corpus.strip_jsonc('{"a": 1} // 尾注\n/* 块 */')) == {"a": 1})
check('字符串里的 // 不被当成注释',
      json.loads(corpus.strip_jsonc('{"a": "http://x // y"}'))["a"]
      == 'http://x // y')
check('字符串里的 /* */ 不被当成注释',
      json.loads(corpus.strip_jsonc('{"a": "/* keep */"}'))["a"] == '/* keep */')
check('转义引号不会提前结束字符串',
      json.loads(corpus.strip_jsonc('{"a": "x\\"// still string"}'))["a"]
      == 'x"// still string')
check('剥掉尾随逗号（对象与数组）',
      json.loads(corpus.strip_jsonc('{"a": [1, 2,],}')) == {"a": [1, 2]})
check('BOM 被吃掉', corpus.strip_jsonc('\ufeff{"a":1}').lstrip().startswith('{'))
check('注释里的引号不影响后续解析',
      json.loads(corpus.strip_jsonc('// he said "hi"\n{"a": 1}')) == {"a": 1})

out('')
out('=' * 78)
out('B. 导出：结构 / 幂等')
out('=' * 78)

ok, target, err = corpus.export_default_corpus()
check('export_default_corpus() 成功', ok and err is None, 'target=%s err=%s' % (target, err))
check('导出目标 == CORPUS_PATH（测试隔离后的临时文件）', target == TMP_CORPUS,
      '%s vs %s' % (target, TMP_CORPUS))
check('文件存在', os.path.exists(TMP_CORPUS))

raw = io.open(TMP_CORPUS, encoding='utf-8').read()
check('导出的文件带中文说明注释（JSONC 的价值就在这里）', '//' in raw and '占位符' in raw)
check('导出的文件里没有未替换的模板标记', '{{' not in raw and '}}' not in raw)

exported = json.loads(corpus.strip_jsonc(raw))
check('顶层是 object', isinstance(exported, dict))
check('scenes 覆盖全部 %d 个场景' % len(corpus.SCENE_ORDER),
      isinstance(exported.get('scenes'), dict)
      and set(exported['scenes']) == set(corpus.SCENE_ORDER),
      'scenes=%d' % len(exported.get('scenes', {})))
check('click_lines 覆盖三个分区',
      set(exported.get('click_lines', {})) == set(corpus.CLICK_ZONES),
      sorted(exported.get('click_lines', {})))
check('导出内容 == 内置语料（逐场景）',
      all(exported['scenes'][s] == list(corpus.BUILTIN_CORPUS[s])
          for s in corpus.SCENE_ORDER))
check('导出的点击台词 == 内置（文本+情绪都一致）',
      all([list(x) for x in exported['click_lines'][z]]
          == [list(x) for x in corpus.CLICK_LINES[z]]
          for z in corpus.CLICK_ZONES))
check('文件末尾是换行（POSIX 风格，diff 友好）', raw.endswith('\n'))
check('导出用的是 LF 而不是 CRLF', '\r\n' not in raw)

stat1 = os.stat(TMP_CORPUS)
io.open(TMP_CORPUS, 'w', encoding='utf-8').write('{"scenes": {"greeting": ["用户改过的"]}}')
changed = io.open(TMP_CORPUS, encoding='utf-8').read()
again, _t2, _e2 = corpus.export_default_corpus()
check('已存在时**不覆盖**（用户的修改比出厂内容值钱）',
      not again and io.open(TMP_CORPUS, encoding='utf-8').read() == changed,
      'returned %s' % again)
check('ensure_corpus_file() 也尊重已有文件',
      io.open(corpus.ensure_corpus_file(), encoding='utf-8').read() == changed)

out('')
out('=' * 78)
out('C. 回落：写坏了不许崩，也不许把 {xxx} 说进气泡')
out('=' * 78)


def write_corpus(obj_or_text):
    """写语料文件并**强制**让缓存键变掉（mtime_ns + size）"""
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(
        obj_or_text, ensure_ascii=False)
    if len(text) % 2 == 0:      # 长度奇偶都覆盖，避免"size 恰好相同"的偶合
        text += ' '
    io.open(TMP_CORPUS, 'w', encoding='utf-8').write(text)
    corpus._cache['key'] = None      # 保险：不让缓存掩盖真实读数


write_corpus('{ this is not json at all')
check('坏 JSON → 整个回落内置（greeting 是内置的）',
      corpus.get_lines('greeting') == corpus.GREETING,
      corpus.get_lines('greeting')[:1])
check('坏 JSON → 点击台词也回落内置',
      list(corpus.get_click_lines('head')) == list(corpus.CLICK_LINES['head']))
check('坏 JSON → corpus_status 报出 error 且场景数为 0',
      corpus.corpus_status()['error'] and
      corpus.corpus_status()['scenes_from_user'] == 0)

write_corpus({'scenes': {'greeting': ['自定义问候']}})
check('只写了一个场景 → 该场景用用户的',
      corpus.get_lines('greeting') == ['自定义问候'])
check('没写的场景 → 回落内置', corpus.get_lines('encourage') == corpus.ENCOURAGE)

write_corpus({'scenes': {'greeting': []}})
check('场景写成空数组 → 回落内置（不是"不说话"）',
      corpus.get_lines('greeting') == corpus.GREETING)

write_corpus({'scenes': {'greeting': [123, None, '  ', 'ok 的一句']}})
check('非字符串/空白元素被丢掉，留下合法的',
      corpus.get_lines('greeting') == ['ok 的一句'],
      corpus.get_lines('greeting'))

write_corpus({'scenes': {'greeting': ['你好 {line}']}})
check('用了该场景**不允许**的占位符 → 整条丢弃 → 回落内置',
      corpus.get_lines('greeting') == corpus.GREETING,
      'greeting 白名单为空，{line} 无处可换')
check('丢弃计数被记下来（排障用）', corpus.corpus_status()['dropped'] >= 1)

write_corpus({'scenes': {'syntax_error': ['第 {line} 行有问题（{language}）'],
                         'greeting': ['你好 {line}']}})
check('合法的占位符台词被采纳',
      corpus.get_lines('syntax_error') == ['第 {line} 行有问题（{language}）'])
check('非法的那条只影响它自己所在场景，不牵连别的',
      corpus.get_lines('syntax_error') != corpus.SYNTAX_ERROR)

write_corpus({'click_lines': {'head': [['摸摸头', 'bogus_emotion']]}})
check('点击台词情绪词非法 → 丢弃该条 → 回落内置',
      list(corpus.get_click_lines('head')) == list(corpus.CLICK_LINES['head']))
write_corpus({'click_lines': {'head': [['摸摸头', 'happy'], ['怪', 'x']]}})
check('点击台词只丢坏的那条、留好的',
      list(corpus.get_click_lines('head')) == [('摸摸头', 'happy')],
      corpus.get_click_lines('head'))
write_corpus({'click_lines': {'head': [['摸摸头']]}})
check('点击台词元素不是二元组 → 丢弃', 
      list(corpus.get_click_lines('head')) == list(corpus.CLICK_LINES['head']))

write_corpus({'scenes': {'unknown_scene_xyz': ['whatever']}})
check('未知场景名被忽略（不改用户文件、也不报错）',
      corpus.corpus_status()['scenes_from_user'] == 0)

write_corpus({'scenes': 'not a dict', 'click_lines': 42})
check('scenes/click_lines 类型不对 → 全部回落，不崩',
      corpus.get_lines('greeting') == corpus.GREETING
      and list(corpus.get_click_lines('body')) == list(corpus.CLICK_LINES['body']))

out('')
out('=' * 78)
out('D. 热重载')
out('=' * 78)

write_corpus({'scenes': {'greeting': ['AAAA']}})
first = corpus.get_lines('greeting')
write_corpus({'scenes': {'greeting': ['BBBBBBBB']}})   # 长度不同，缓存键必变
second = corpus.get_lines('greeting')
check('改完文件立刻读到新内容（不用重启桌宠）',
      first == ['AAAA'] and second == ['BBBBBBBB'], '%s -> %s' % (first, second))

os.remove(TMP_CORPUS)
check('文件被删掉 → 回落内置（不会崩）',
      corpus.get_lines('greeting') == corpus.GREETING)
check('删除后 corpus_status().exists 为 False',
      corpus.corpus_status()['exists'] is False)

out('')
out('=' * 78)
out('E/F. 接线：诊断台词与点击台词都经过 corpus')
out('=' * 78)

write_corpus({'scenes': {'greeting': ['来自用户语料的问候'],
                         'syntax_error': ['第 {line} 行坏掉了']},
              'click_lines': {'head': [['来自用户语料的摸头', 'happy']]}})
check('response_generator.pick_corpus 用上了用户语料',
      rg.pick_corpus('greeting') == '来自用户语料的问候',
      rg.pick_corpus('greeting'))
check('pick_corpus 的占位符替换照旧生效',
      rg.pick_corpus('syntax_error', {'line': 42}) == '第 42 行坏掉了',
      rg.pick_corpus('syntax_error', {'line': 42}))
# 边界（已知、且被白名单挡住了大半）：调用方**没传**变量时占位符会原样残留。
# 用户语料的合法占位符受 ALLOWED_PLACEHOLDERS 限制（= 所有调用路径的交集），
# 所以"有调用路径拿不到它"这件事不会因为用户写语料而变多。
check('调用方不传变量时占位符残留（边界，已被白名单约束在交集内）',
      '{line}' in rg.pick_corpus('syntax_error'))
check('standalone._pick_click_line 用上了用户语料',
      standalone._pick_click_line('head') == ('来自用户语料的摸头', 'happy'),
      standalone._pick_click_line('head'))
check('点击未知分区 → 回落 body 池',
      standalone._pick_click_line('nope')[0] in
      [t for t, _e in corpus.get_click_lines('body')])
check('standalone.CLICK_LINES 与 corpus.CLICK_LINES 是同一份（单一真值）',
      standalone.CLICK_LINES is corpus.CLICK_LINES)
check('corpus 不可用时有兜底台词（点了不至于没反应）',
      standalone._CLICK_FALLBACK and standalone._CLICK_FALLBACK[0][0])

out('')
out('=' * 78)
out('G. /settings 的 openCorpus 与 /event 的 corpusOpened 回执')
out('=' * 78)

if os.path.exists(TMP_CORPUS):
    os.remove(TMP_CORPUS)
ready = threading.Event()
threading.Thread(target=standalone.start_server, args=(PORT, ready),
                 daemon=True).start()
check('进程内服务器起来了', ready.wait(5.0))

# 真正的打开动作在**后台线程**里跑（它要等扩展回执）。这一节把回执超时压到
# 最短、并把回落换成桩 —— 否则用例会真去拉起一个 VS Code / 弹"选择打开方式"。
fallback_calls = []
_saved_timeout = standalone.CORPUS_OPEN_ACK_TIMEOUT
_saved_fallback = standalone._open_corpus_fallback
standalone._open_corpus_fallback = lambda p: (fallback_calls.append(p) or 'code-cli')
standalone.CORPUS_OPEN_ACK_TIMEOUT = 0.2


def drain(timeout=4.0):
    """等所有后台 worker 收工（等待槽被摘空）"""
    end = time.time() + timeout
    while time.time() < end:
        with standalone._corpus_open_lock:
            if not standalone._corpus_open_wait:
                return True
        time.sleep(0.05)
    return False


def req(method, path, payload=None, raw=None):
    url = 'http://127.0.0.1:%d%s' % (PORT, path)
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers['Content-Type'] = 'application/json'
    elif payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


standalone.settings['openCorpusSeq'] = 0
st, txt = req('POST', '/settings', {'openCorpus': True})
d = json.loads(txt)
check('POST /settings {openCorpus:true} → 200', st == 200, st)
check('响应里带回了递增后的序号', d.get('openCorpusSeq') == 1, d)
on_disk = json.load(io.open(TMP_SETTINGS, encoding='utf-8'))
check('序号落到了设置文件里（扩展就是这么看到的）',
      on_disk.get('openCorpusSeq') == 1, on_disk)
check('检测周期没被这次请求动过（只改该改的键）',
      on_disk.get('detectIntervalSec') == standalone.DETECT_DEFAULT_SEC, on_disk)

st2, txt2 = req('POST', '/settings', {'openCorpus': True})
check('再点一次 → 序号变 2（扩展靠"变大"识别，不是靠值本身）',
      json.loads(txt2).get('openCorpusSeq') == 2)
check('GET /settings 也回带 openCorpusSeq',
      'openCorpusSeq' in json.loads(req('GET', '/settings')[1]))

st_bad, _t = req('POST', '/settings', raw=b'\xff\xfe\xfd not utf8')
check('非 UTF-8 body 不再炸（400 或 200，反正不能断连）',
      st_bad in (200, 400), st_bad)
st_list, _t = req('POST', '/settings', raw=b'[1,2,3]')
check('合法 JSON 但不是对象 → 400，不是 500', st_list == 400, st_list)

# ---- /event 回执：手工注册一个等待槽，走真实的 HTTP 路径 ----
slot = {'event': threading.Event(), 'ok': False, 'method': None}
with standalone._corpus_open_lock:
    standalone._corpus_open_wait[777] = slot
st, _t = req('POST', '/event',
             {'type': 'corpusOpened', 'payload': {'seq': 777, 'ok': True,
                                                  'method': 'vscode'}})
check('/event corpusOpened → 200', st == 200, st)
check('等待槽被唤醒且标记为成功',
      slot['event'].wait(1.0) and slot['ok'] is True and slot['method'] == 'vscode',
      slot)
# 回执只负责"唤醒" —— 摘槽是 worker 的活：它被唤醒后还要读 ok/method 才能决定
# 要不要往下回落，槽先被摘掉的话这两个字段就丢了。
check('回执不摘槽（摘槽归 worker，它还要读 ok/method）',
      777 in standalone._corpus_open_wait)
with standalone._corpus_open_lock:
    standalone._corpus_open_wait.pop(777, None)

# 无人等待的回执（迟到的）不能 500，也不能把 _corpus_open_wait 弄脏
before_keys = set(standalone._corpus_open_wait)
st, _t = req('POST', '/event',
             {'type': 'corpusOpened', 'payload': {'seq': 88888, 'ok': True}})
check('迟到/陌生的回执不报错、不污染等待表',
      st == 200 and set(standalone._corpus_open_wait) == before_keys, st)
check('缺 seq 的回执也不报错', req('POST', '/event',
                                  {'type': 'corpusOpened', 'payload': {}})[0] == 200)

# ---- 三级回落：直接调 worker，回落函数是桩（绝不真去拉起 VS Code） ----
# 上面那两个 POST 各起了一个后台 worker：等它们收工，顺带验证"等待槽不泄漏"。
check('后台 worker 全部收工、等待槽不泄漏', drain())
fallback_calls.clear()

def bubble_texts(msgs):
    """从 SSE 队列里取出气泡文本。
    不能直接对原始 JSON 串做子串匹配：路径在 JSON 里是 `C:\\\\Users\\\\...`
    （反斜杠被转义成两个），拿原始路径去 `in` 一定不中。"""
    texts = []
    for m in msgs:
        try:
            texts.append(json.loads(m)['payload']['text'])
        except Exception:
            pass
    return texts


qlen0 = len(standalone.sse_queue)
standalone._open_corpus_worker(1001)      # 没人回执 → 应当走回落
check('等不到回执 → 走 second-level 回落（code 命令）',
      fallback_calls == [TMP_CORPUS], fallback_calls)
check('回落后语料文件确实被生成了', os.path.exists(TMP_CORPUS))
new_msgs = [m for _s, m in standalone.sse_queue[qlen0:]]
check('说了一句结果（推给气泡）',
      any('台词库' in t for t in bubble_texts(new_msgs)), bubble_texts(new_msgs))
check('worker 跑完把等待槽摘干净（不泄漏）',
      not standalone._corpus_open_wait, sorted(standalone._corpus_open_wait))

# 有回执但失败 → 也要回落（扩展接了单却没打开）
fallback_calls.clear()
def ack_fail():
    time.sleep(0.15)
    standalone._corpus_open_ack(1002, False, 'vscode')
threading.Thread(target=ack_fail, daemon=True).start()
standalone._open_corpus_worker(1002)
check('扩展接了单但打开失败 → 仍然回落', fallback_calls == [TMP_CORPUS],
      fallback_calls)

# 有回执且成功 → **不许**再回落（否则会多开一个 VS Code）
fallback_calls.clear()
def ack_ok():
    time.sleep(0.15)
    standalone._corpus_open_ack(1003, True, 'vscode')
threading.Thread(target=ack_ok, daemon=True).start()
standalone._open_corpus_worker(1003)
check('扩展成功打开 → 不回落到 code 命令（避免多开一个 VS Code）',
      fallback_calls == [], fallback_calls)

# 全都打不开 → 说一句带路径的错话，而不是静默
fallback_calls.clear()
standalone._open_corpus_fallback = lambda p: None
qlen1 = len(standalone.sse_queue)
standalone._open_corpus_worker(1004)
tail = bubble_texts([m for _s, m in standalone.sse_queue[qlen1:]])
check('三级全部失败 → 报错并把路径告诉用户（不许静默）',
      any(TMP_CORPUS in t for t in tail), tail)

standalone.CORPUS_OPEN_ACK_TIMEOUT = 0.2

# 端到端：走 HTTP 把 openCorpus 打进去，worker 会真的跑；
# 这里确认"HTTP 那条路不会因为后台线程而卡住/500"，并且收工后不残留等待槽。
st, txt = req('POST', '/settings', {'openCorpus': True})
check('openCorpus 的 HTTP 路径立刻返回（打开动作在后台线程里）',
      st == 200 and json.loads(txt).get('openCorpusSeq') == 3, txt)
check('HTTP 触发的后台 worker 收工后也不残留等待槽', drain())

# ---- K. v0.3.30 修的两处：code 拼串引号 / 等待槽"先登记再发布" ----
out('')
out('K. code 命令拼串引号 + 等待槽的登记时机')

# K1：拼出来的命令行必须把 **exe 也**引起来。
#     本机 shutil.which('code') 返回 `S:\Microsoft VS Code\bin\code.CMD`——含
#     空格。旧写法 exe 裸拼，cmd.exe 会拿 `S:\Microsoft` 当命令名 → 找不到；
#     而 Popen 不抛错（stderr 丢进 DEVNULL），于是**谎报成功**、第三级
#     os.startfile 永远轮不到。这一条只看字符串形状，行为验证在 K2。
captured = []
_real_subproc = standalone.subprocess


class _FakePopen:
    def __init__(self, cmd, **kw):
        captured.append((cmd, kw))
        self.returncode = 0


class _FakeSubproc:
    Popen = _FakePopen
    DEVNULL = _real_subproc.DEVNULL
    # _launch_code_cli 在 try **之外**调 list2cmdline 拼串 —— 替身必须带着它
    list2cmdline = staticmethod(_real_subproc.list2cmdline)


_real_shutil = standalone.shutil


class _FakeShutil:
    @staticmethod
    def which(name):
        return r'C:\Program Files\Some Editor\bin\code.CMD' if name == 'code' else None


try:
    standalone.subprocess = _FakeSubproc
    standalone.shutil = _FakeShutil
    got_k1 = standalone._launch_code_cli(
        r'C:\Users\x y\.dsh\pets\airi-pet-corpus.jsonc')
finally:
    standalone.subprocess = _real_subproc
    standalone.shutil = _real_shutil

_cmd = captured[0][0] if captured else ''
check('_launch_code_cli 返回 code-cli', got_k1 == 'code-cli', got_k1)
check('命令行里 exe 也带引号（含空格路径不会被 cmd 拆开）',
      _cmd.startswith('"'), _cmd)
check('两个参数各自带引号（共 4 个）', _cmd.count('"') == 4, _cmd)
check('仍然 shell=True（.cmd 必须经 cmd.exe）',
      bool(captured) and captured[0][1].get('shell') is True, captured[:1])

# K2：**行为**验证 —— 这才是当初 bug 的直接复现。
#     用一个同形状的替身（含空格目录下的 .cmd）走真实 subprocess，断言它
#     **真的被执行**（往自己旁边写一个 marker）。旧写法这里 marker 不会生成，
#     却照样返回 'code-cli' + 冒一句"台词库打开了"。
STUB_DIR = os.path.join(tempfile.gettempdir(), 'airi corpus space dir')
STUB_MARK = os.path.join(STUB_DIR, 'marker.txt')
STUB_CMD = os.path.join(STUB_DIR, 'stub.cmd')
os.makedirs(STUB_DIR, exist_ok=True)
if os.path.exists(STUB_MARK):
    os.remove(STUB_MARK)
with io.open(STUB_CMD, 'w', encoding='ascii', newline='\r\n') as _fh:
    _fh.write('@echo off\r\n')
    _fh.write('> "%~dp0marker.txt" echo ran\r\n')


class _StubShutil:
    @staticmethod
    def which(name):
        return STUB_CMD if name == 'code' else None


_real_shutil2 = standalone.shutil
try:
    standalone.shutil = _StubShutil
    got_k2 = standalone._launch_code_cli(
        os.path.join(STUB_DIR, 'does-not-need-to-exist.jsonc'))
finally:
    standalone.shutil = _real_shutil2

_deadline = time.time() + 10.0
while time.time() < _deadline and not os.path.exists(STUB_MARK):
    time.sleep(0.05)
check('含空格路径的 code 替身**真的被执行了**（旧写法在这里静默失败）',
      os.path.exists(STUB_MARK), STUB_MARK)
check('返回值仍是 code-cli', got_k2 == 'code-cli', got_k2)

# K3：code 命令整个找不到时，必须落到第 3 级 os.startfile，而不是静默返回 None
_started = []
_real_startfile = os.startfile
_real_fallback_fn = _saved_fallback          # 第 304 行存的**真身**（此时全局的是桩）


class _NoCodeShutil:
    @staticmethod
    def which(name):
        return None


_real_shutil3 = standalone.shutil
os.startfile = lambda p: _started.append(p)
try:
    standalone.shutil = _NoCodeShutil
    _m = _real_fallback_fn(r'X:\nope\airi-pet-corpus.jsonc')
finally:
    standalone.shutil = _real_shutil3
    os.startfile = _real_startfile
check('code 命令不存在 → 落到 os.startfile（第 3 级）', _m == 'system-default', _m)
check('os.startfile 拿到的是语料路径',
      _started == [r'X:\nope\airi-pet-corpus.jsonc'], _started)

# K4：等待槽必须在"落盘发布"**之前**登记好。
#     旧写法先落盘、由后台线程稍后登记 → 扩展可能在下一次 500ms 轮询就回执，
#     回执先到会被当成"迟到回执"丢掉 → worker 白等 2.5s 后走 code 回落 →
#     同一个文件被打开两次。把超时放大到 5s 让窗口可观测。
_saved_timeout_k = standalone.CORPUS_OPEN_ACK_TIMEOUT
standalone.CORPUS_OPEN_ACK_TIMEOUT = 5.0
_st, _tx = req('POST', '/settings', {'openCorpus': True})
_seq_k = json.loads(_tx).get('openCorpusSeq') if _st == 200 else None
with standalone._corpus_open_lock:
    _present = _seq_k in standalone._corpus_open_wait
check('POST 一返回，等待槽就已经登记好了（登记早于发布）', _present, _seq_k)
# 收尾：给个回执，别让 drain 等满 5 秒
standalone._corpus_open_ack(_seq_k, True, 'vscode')
standalone.CORPUS_OPEN_ACK_TIMEOUT = _saved_timeout_k
check('给回执后 worker 摘槽、不残留', drain())

# K5：静态不变量 —— 把上面几条钉在源码上（改回去就红）
_src = io.open(os.path.join(PET, 'standalone.py'), encoding='utf-8').read()
_blk_i = _src.find("if msg.get('openCorpus'):")
_blk = _src[_blk_i:_blk_i + 3000]
_reg = _blk.find('_corpus_open_wait[seq] = slot')
# 0.3.43 起落盘统一走 _persist_locked（它内部才 save_settings）：
# 这个键也要"落盘失败回滚 + 写前合并磁盘"，见 standalone._persist_locked。
_sav = _blk.find('saved = _persist_locked(')
check('源码里"登记等待槽"排在"落盘"之前（顺序不变量）',
      _blk_i >= 0 and 0 <= _reg < _sav, (_blk_i, _reg, _sav))
check('code 回落用 list2cmdline 拼串（exe 自带引号）',
      'list2cmdline' in _src)
check('worker 提前返回路径也会摘槽（不泄漏）', '_corpus_release(seq, slot)' in _src)

standalone.CORPUS_OPEN_ACK_TIMEOUT = _saved_timeout
standalone._open_corpus_fallback = _saved_fallback

out('')
out('=' * 78)
out('H. 用户真实文件零改动')
out('=' * 78)

for p in (REAL_CORPUS, REAL_SETTINGS):
    check('未被创建/修改：%s' % os.path.basename(p),
          snapshot(p) == REAL_BEFORE[p],
          'before=%s after=%s' % (REAL_BEFORE[p], snapshot(p)))
check('真实设置文件里没有**新出现** openCorpusSeq（测试前就有 = 用户正常状态）',
      _real_had_seq_before
      or (not os.path.exists(REAL_SETTINGS))
      or 'openCorpusSeq' not in io.open(REAL_SETTINGS, encoding='utf-8').read())

out('')
out('=' * 78)
out('I. 内置语料自洽')
out('=' * 78)

check('SCENE_ORDER 与 BUILTIN_CORPUS 的键一一对应',
      set(corpus.SCENE_ORDER) == set(corpus.BUILTIN_CORPUS))
check('ALLOWED_PLACEHOLDERS 的键与场景表一致',
      set(corpus.ALLOWED_PLACEHOLDERS) == set(corpus.SCENE_ORDER))

bad_ph = []
for scene, lines in corpus.BUILTIN_CORPUS.items():
    allowed = corpus.ALLOWED_PLACEHOLDERS[scene]
    for line in lines:
        for ph in corpus._PLACEHOLDER_RE.findall(line):
            if ph not in allowed:
                bad_ph.append('%s:{%s}' % (scene, ph))
check('内置台词只用了该场景白名单内的占位符', not bad_ph, bad_ph[:6])

check('内置点击台词的情绪词都在 CLICK_EMOTIONS 里',
      all(e in corpus.CLICK_EMOTIONS
          for pool in corpus.CLICK_LINES.values() for _t, e in pool))
check('点击台词池覆盖三个分区且都非空',
      all(corpus.CLICK_LINES.get(z) for z in corpus.CLICK_ZONES))

# 前端 EMOTION_EXPRESSION 的键（情绪切不过去就是表情不动）
import re as _re
_m = _re.search(r'EMOTION_EXPRESSION\s*=\s*\{(.*?)\}', UI, _re.S)
_frontend_emotions = set(_re.findall(r'(\w+)\s*:', _m.group(1))) if _m else set()
check('前端 EMOTION_EXPRESSION 解析成功', bool(_frontend_emotions),
      sorted(_frontend_emotions))
check('内置语料的情绪词前端全认识',
      set(corpus.EMOTION_MAP.values()) <= _frontend_emotions,
      sorted(set(corpus.EMOTION_MAP.values()) - _frontend_emotions))

out('')
out('=' * 78)
out('J. 三端接线')
out('=' * 78)

check('ui.html 主菜单有「编辑语料」条目（且只有一个）',
      UI.count('data-action="edit-corpus"') == 1,
      UI.count('data-action="edit-corpus"'))
check('ui.html 用 SETTINGS_URL 发 openCorpus（不是相对路径）',
      'openCorpus: true' in UI and 'fetch(SETTINGS_URL' in UI)
check('ui.html 点击「编辑语料」后收起菜单（走 hideMenu 那条路）',
      "a === 'edit-corpus'" in UI)
check('ui.html 里 still 没有相对路径的 /settings 请求',
      "fetch('/settings'" not in UI)
check('extension.ts 里有语料路径常量（.jsonc）',
      "'airi-pet-corpus.jsonc'" in EXT and 'PET_CORPUS_PATH' in EXT)
check('extension.ts 监听 openCorpusSeq 并去重（lastCorpusOpenSeq）',
      'lastCorpusOpenSeq' in EXT and 'openCorpusSeq' in EXT)
check('extension.ts 回执用 /event + corpusOpened',
      "'/event'" in EXT and "'corpusOpened'" in EXT)
check('extension.ts 的 watch 回调里挂上了打开逻辑',
      'openCorpusIfRequested()' in EXT)

_standalone_src = io.open(os.path.join(PET, 'standalone.py'),
                          encoding='utf-8').read()
check('standalone.py 有三级回落与回执入口',
      '_open_corpus_fallback' in _standalone_src
      and '_corpus_open_ack' in _standalone_src)
check('standalone.py 不再自带点击台词副本（单一真值在 corpus.py）',
      "'head': (" not in _standalone_src and '别摸头啦' not in _standalone_src)
check('standalone.py 里的 CLICK_LINES 只是指向 corpus 的别名',
      'CLICK_LINES = corpus.CLICK_LINES' in _standalone_src)

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')

for p in (TMP_CORPUS, TMP_SETTINGS, TMP_LOG, TMP_CORPUS + '.tmp',
          TMP_SETTINGS + '.tmp'):
    try:
        os.remove(p)
    except OSError:
        pass

sys.exit(0 if _fail == 0 else 1)
