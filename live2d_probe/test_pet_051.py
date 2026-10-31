# -*- coding: utf-8 -*-
r"""0.3.51 回归测试。

本轮三条修复都属于同一族：**外部/跨进程数据把一条链路静默搞死**，而且
三条的症状一模一样 —— "配了 DEEPSEEK_API_KEY 却永远只说内置台词"，日志里
一行都没有。

  A 段  `/push` 的 `files` 里带非字符串 → LLM 那条路整条失效
        `try_deepseek_api()` 里是 `', '.join(files[:3])`，`str.join` 只吃
        字符串；`generate_response` 以前只判"是不是 list/tuple"，元素类型
        不管。JSON 的 `null` / 数字 / 对象进来就抛 TypeError，被那里裸的
        `except Exception: pass` 吞掉。

  B 段  提示词里的**孤立代理** → httpx 编码抛 UnicodeEncodeError → 同上
        Python 的 str 合法地可以含孤立代理（`'\ud83d'`），但它不能编码成
        utf-8。httpx 发送时是 `json.dumps(..., ensure_ascii=False)
        .encode('utf-8')`（本机实测 `httpx._content.encode_json`：
        `UnicodeEncodeError: surrogates not allowed`）。
        来源可以是任何本机客户端 / **更早版本的扩展**，所以桌宠侧必须
        自己在提示词出口兜住，不能假设发送方是新的。

  C 段  扩展侧 `clipMessage()` 把 emoji 切成两半（生产侧）
        `slice(0, maxLen-1)` 按 UTF-16 码元走，第 59 个码元正好是某个 emoji
        的高位代理时，切出来的是孤立代理 —— 于是 B 段的输入就有了。
        `ui.html` 的 `clipBubbleText` 0.3.43 就修过这个，扩展这一半漏了。

⚠️ 测试自身的坑（逐条防住）
  · 判据只认**副作用**：打桩的 client 有没有被 `create()` 过、返回值是不是
    桩里的那句标记。回落语料同样是正常中文，肉眼看不出区别。
  · 桩必须返回**真的能走通**的响应对象（`choices[0].message.content`）。
    0.3.51 第一版桩写成了 `choices=[_M()]`，缺 `.message` → 属性错误被
    `except Exception: pass` 吞成"生成失败" → 控制组也拿到回落语料，
    于是"LLM 被调用过"这条判据虽然还对，但"返回的是 LLM 的话"永远为假。
  · `src/extension.ts` 是 **CRLF**：按 `'\n}\n'` 切函数体会 indexOf=-1，
    `slice(0, -1+3)` 拿到文件末尾两个字（"fu"）→ ReferenceError。
  · 剥 TS 类型标注只能用窄正则；`/\(\w+: string\)/` 会把 `function` 也吃掉。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'
TMP = tempfile.mkdtemp(prefix='airi_test051_')

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


sys.path.insert(0, os.path.join(REPO, 'python_backend'))

LONE = 'x' * 58 + '\ud83d' + '…'          # clipMessage 旧版在 58 个 x 后切出来的形态
LLM_MARK = 'LLM-说了一句'

_old_key = os.environ.get('DEEPSEEK_API_KEY')
os.environ['DEEPSEEK_API_KEY'] = 'sk-fake-for-test'
_old_mods = {k: sys.modules.get(k) for k in ('openai', 'character')}

#: 真 httpx 的编码器（SDK 实际走的那一行）；没有 httpx 时用 stdlib 等价物
try:
    from httpx._content import encode_json as _encode_json
    _ENC = 'httpx'
except Exception:
    def _encode_json(payload):
        h = json.dumps(payload, ensure_ascii=False,
                       separators=(',', ':'), allow_nan=False).encode('utf-8')
        return {'Content-Length': str(len(h))}, h
    _ENC = 'stdlib(json.dumps+encode)'

EV = {'calls': 0, 'prompts': []}


class _Msg(object):
    content = LLM_MARK


class _Choice(object):
    message = _Msg()


class _Resp(object):
    choices = [_Choice()]


class _Completions(object):
    def create(self, **kw):
        # 与真实 SDK 的 httpx 传输层同一行代码：会真的做 utf-8 编码
        _encode_json({'model': kw.get('model'), 'messages': kw.get('messages')})
        EV['calls'] += 1
        EV['prompts'].append(kw['messages'][1]['content'])
        return _Resp()


class _Client(object):
    def __init__(self, *a, **kw):
        class _C(object):
            pass
        self.chat = _C()
        self.chat.completions = _Completions()


try:
    import response_generator as rg

    _openai = types.ModuleType('openai')
    _openai.OpenAI = lambda *a, **kw: _Client()
    sys.modules['openai'] = _openai
    _char = types.ModuleType('character')
    _char.SYSTEM_PROMPT = 'you are a tsundere'
    sys.modules['character'] = _char

    BASE = {'trigger': 'diagnostics', 'error_count': 2,
            'sample_errors': [{'line': 3, 'message': 'invalid syntax'}]}
    _MISSING = object()

    def call(**patch):
        """真调一次 generate_response。返回 (是否走到 LLM, 提示词里的文件名)。"""
        ctx = dict(BASE)
        ctx.update(patch)
        n0 = EV['calls']
        out = rg.generate_response(ctx)
        called = EV['calls'] > n0
        text = (out or {}).get('payload', {}).get('text', '')
        prompt = EV['prompts'][-1] if called else ''
        part = ''
        if '涉及文件: ' in prompt:
            part = prompt.split('涉及文件: ')[1].split('\n')[0]
        return called and text == LLM_MARK, part, ctx, text

    # =======================================================================
    section('A `files` 里的非字符串不许把 LLM 那条路整条吞掉')
    # =======================================================================
    ok, part, _ctx, _t = call(files=[None])
    check('A1 files=[null] 时 LLM 仍被调用', ok, 'ok=%r' % (ok,))

    ok, part, _ctx, _t = call(files=[123])
    check('A2 files=[123] 时 LLM 仍被调用', ok, 'ok=%r' % (ok,))

    ok, part, _ctx, _t = call(files=['a.py', None])
    check('A3 files=["a.py", null] 时 LLM 仍被调用且提示词里只有 a.py',
          ok and part == 'a.py', 'ok=%r part=%r' % (ok, part))

    ok, part, _ctx, _t = call(files=['a.py'])
    check('A4 files=["a.py"] 原样进提示词（正常值不许被过滤）',
          ok and part == 'a.py', 'ok=%r part=%r' % (ok, part))

    ok, part, _ctx, _t = call(files=['a.py', {}, '   ', 5])
    check('A5 files 里混对象/空白/数字时只剩 a.py',
          ok and part == 'a.py', 'ok=%r part=%r' % (ok, part))

    ok, _part, ctx, _t = call(files=['a.py', None, 7])
    check('A6 归一化结果回写进 context（其余消费者看到同一份）',
          ctx.get('files') == ['a.py'], 'files=%r' % (ctx.get('files'),))

    ok_all, detail = True, []
    for weird in ('a.py', {'x': 1}, 5, None, True, []):
        try:
            _ok, _p, ctx, _t = call(files=weird)
        except Exception as exc:
            ok_all = False
            detail.append('%r -> EXC %r' % (weird, exc))
            continue
        if ctx.get('files') != []:
            ok_all = False
            detail.append('%r -> files=%r' % (weird, ctx.get('files')))
    check('A7 files 是标量/对象/布尔时归一成 []（不抛）', ok_all,
          ' '.join(detail[:3]))

    big = ['x' * 500] + ['f%d.py' % i for i in range(20)]
    _ok, _p, ctx, _t = call(files=big)
    files = ctx.get('files') or []
    check('A8 条数与单条长度都有上限（≤8 条、单条 ≤200 字）',
          len(files) <= 8 and all(len(f) <= 200 for f in files),
          'n=%d maxlen=%d' % (len(files), max([len(f) for f in files] or [0])))

    # =======================================================================
    section('B 提示词里的孤立代理不许让 LLM 静默失效（编码器=%s）' % _ENC)
    # =======================================================================
    ok, _p, _ctx, _t = call(sample_errors=[{'line': 1, 'message': LONE}])
    check('B1 sample_errors 带孤立代理时 LLM 仍被调用',
          ok, 'ok=%r（httpx 编码器在孤立代理上抛 UnicodeEncodeError）' % (ok,))

    ok, _p, _ctx, _t = call(language='py\ud83d')
    check('B2 language 带孤立代理时 LLM 仍被调用', ok, 'ok=%r' % (ok,))

    ok, _p, _ctx, _t = call(trigger='file_scan', file=LONE,
                            sample_errors=[{'line': 1, 'message': LONE}])
    check('B3 file_scan + file/样本都带孤立代理时 LLM 仍被调用',
          ok, 'ok=%r' % (ok,))

    ok, _p, _ctx, _t = call()
    check('B4 干净输入照样走 LLM（别为了堵畸形把正常路也堵了）',
          ok, 'ok=%r' % (ok,))

    # B5：提示词本身必须已经不带孤立代理（不是靠 try/except 兜过去的）
    call(sample_errors=[{'line': 1, 'message': LONE}])
    prompt = EV['prompts'][-1] if EV['prompts'] else ''
    lone_in_prompt = 0
    for ch in prompt:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            lone_in_prompt += 1
    check('B5 发出去的提示词里没有孤立代理（真的被替换掉，不是绕开）',
          lone_in_prompt == 0, 'lone=%d' % lone_in_prompt)
    try:
        prompt.encode('utf-8')
        enc_ok = True
    except UnicodeEncodeError:
        enc_ok = False
    check('B6 提示词可 utf-8 编码（httpx 那一行不会抛）', enc_ok,
          'prompt[-40:]=%r' % prompt[-40:])

    # =======================================================================
    section('C 扩展侧 clipMessage 不许切在代理对中间（真跑源码）')
    # =======================================================================
    js_path = os.path.join(TMP, 'clip_probe.js')
    io.open(js_path, 'w', encoding='utf-8', newline='\n').write(r"""
const fs = require('fs');
const path = process.argv[2];
// ⚠️ 源文件是 CRLF：先归一再按 '\n}\n' 切，否则 indexOf=-1
const src = fs.readFileSync(path, 'utf8').replace(/\r\n/g, '\n');
const i = src.indexOf('function clipMessage');
if (i < 0) { console.log('NOT_FOUND'); process.exit(0); }
let body = src.slice(i);
const j = body.indexOf('\n}\n');
body = body.slice(0, j + 3);
body = body.replace(/\(message: string, maxLen = 60\): string/,
                    '(message, maxLen = 60)');
body = body.replace(/: string\b/g, '').replace(/: number\b/g, '');
const clipMessage = new Function(body + '\nreturn clipMessage;')();

function loneCount(s) {
  let n = 0;
  for (let k = 0; k < s.length; k++) {
    const c = s.charCodeAt(k);
    if (c >= 0xD800 && c <= 0xDBFF) {
      const nx = s.charCodeAt(k + 1);
      if (!(nx >= 0xDC00 && nx <= 0xDFFF)) n++;
    } else if (c >= 0xDC00 && c <= 0xDFFF) {
      const pv = s.charCodeAt(k - 1);
      if (!(pv >= 0xD800 && pv <= 0xDBFF)) n++;
    }
  }
  return n;
}

const CASES = [
  'x'.repeat(58) + '\u{1F4A2}' + 'tail',
  'x'.repeat(57) + '\u{1F4A2}' + 'tail',
  'x'.repeat(58) + '中文' + 'tail',
  'x'.repeat(10) + '\u{1F4A2}',
  'x'.repeat(58) + '\u{1F433}',
  'x'.repeat(20) + '\u{1F4A2}\u{1F433}'.repeat(20),
];
let lone = 0;
for (const c of CASES) { lone += loneCount(clipMessage(c)); }
const long = clipMessage('x'.repeat(500));
console.log('LONE=' + lone + ';MAXLEN=' + long.length +
            ';ELLIPSIS=' + (long.endsWith('…') ? 1 : 0) +
            ';CTRL=' + JSON.stringify(clipMessage('x'.repeat(10) + '\u{1F4A2}')));
""")

    out = ''
    try:
        proc = subprocess.run([NODE, js_path,
                               os.path.join(REPO, 'src', 'extension.ts')],
                              capture_output=True, text=True, timeout=60,
                              encoding='utf-8', errors='replace')
        out = (proc.stdout or '').strip()
    except Exception as exc:
        out = 'EXC %r' % (exc,)

    def kv(text, key, default=None):
        for seg in text.split(';'):
            if seg.startswith(key + '='):
                return seg[len(key) + 1:]
        return default

    check('C0 从 src 里真跑到了 clipMessage（不是重写一份）',
          out.startswith('LONE='), 'out=%r' % out[:120])
    check('C1 六种排布里一个孤立代理都不留（旧码 58x+emoji 与连排各出 1 个）',
          kv(out, 'LONE') == '0', 'out=%r' % out[:120])
    check('C2 长度上限仍是 60 且以省略号收尾',
          kv(out, 'MAXLEN') == '60' and kv(out, 'ELLIPSIS') == '1',
          'out=%r' % out[:120])
    # 控制组：没到上限的短文本必须原样返回（含完整 emoji）
    check('C3 未到上限的文本原样返回（emoji 不许被截成半个）',
          kv(out, 'CTRL', '') == '"' + 'x' * 10 + '\U0001F4A2' + '"',
          'ctrl=%r' % kv(out, 'CTRL', ''))

    # =======================================================================
    section('D 静态：三处修复的判据都在')
    # =======================================================================
    esrc = io.open(os.path.join(REPO, 'src', 'extension.ts'),
                   encoding='utf-8-sig').read().replace('\r\n', '\n')
    i = esrc.find('function clipMessage')
    cbody = esrc[i:i + 1600] if i >= 0 else ''
    check('D1 clipMessage 里显式判了高位代理并退一个码元',
          bool(cbody) and '0xD800' in cbody and 'cut -= 1' in cbody,
          'cbody=%r' % cbody[:160])
    check('D2 clipMessage 不再裸 slice(0, maxLen - 1) 直接返回',
          'slice(0, maxLen - 1)' not in cbody, 'still there')

    rsrc = io.open(os.path.join(REPO, 'python_backend', 'response_generator.py'),
                   encoding='utf-8-sig').read()
    check('D3 generate_response 用 _as_file_list 归一化 files 并回写',
          'context["files"] = _as_file_list(context.get("files"))' in rsrc,
          'not found')
    check('D4 try_deepseek_api 仍在 ' + "join(files[:3])" + ' 上拼提示词',
          "', '.join(files[:3])" in rsrc, 'not found')
    check('D5 提示词出口过了一遍 _encodable（唯一出口，新增拼接不用再记）',
          'user_prompt = _encodable(user_prompt)' in rsrc, 'not found')
    i = rsrc.find('def _as_file_list')
    abody = rsrc[i:i + 2000] if i >= 0 else ''
    check('D6 _as_file_list 只保留 str 元素，不做 str() 强转',
          bool(abody) and 'isinstance(item, str)' in abody
          and 'str(item)' not in abody, 'body=%r' % abody[:160])

except Exception as exc:
    check('整体未崩', False, '%s: %s' % (type(exc).__name__, exc))
finally:
    if _old_key is None:
        os.environ.pop('DEEPSEEK_API_KEY', None)
    else:
        os.environ['DEEPSEEK_API_KEY'] = _old_key
    for _k, _v in _old_mods.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


print('\n'.join(LINES))
print()
print('==== %d PASS / %d FAIL ====' % (PASS, FAIL))
try:
    io.open(os.path.join(HERE, '_t051.txt'), 'w', encoding='utf-8').write(
        '\n'.join(LINES) + '\n\n%d PASS / %d FAIL\n' % (PASS, FAIL))
except Exception:
    pass
sys.exit(1 if FAIL else 0)
