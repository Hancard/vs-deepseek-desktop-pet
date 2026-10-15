# -*- coding: utf-8 -*-
"""test_bubble_text.py — 气泡文案长度上限的回归（v0.3.34）

为什么必须有上限：气泡 CSS 是 `max-height:132px + overflow:hidden`，
13px/1.7 的行高只放得下 5~6 行 —— 多出来的字**用户一行都看不到**。
但 typeText 是按字定速的（最少 16ms/字），看不见的字照样要一字一字打完：

    5000 字  ->  80 秒
    50000 字 -> 13.3 分钟

这段时间内气泡被它占着（MAX_BUBBLES=2），后面的话排不上号。
⚠️ 这不是"外部输入才会踩"的理论风险：右键「编辑语料」是给用户自己用的，
   在里面写一条长台词保存，桌宠就会照着念（实测 5000 字 = 80 秒）。

本测试 A 段是**行为级** —— 把 ui.html 里的纯函数抽出来，在 node 里真跑
（与 test_alpha_orientation.py 抽 rowsBottomUp 同一套路）；B 段是静态契约。

用法：python test_bubble_text.py
      → live2d_probe/bubble_text_result.txt
      返回码 0 全通过 / 1 有失败
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from node_finder import find_node  # noqa: E402

UI_HTML = os.path.join(_PROJ, 'desktop_pet', 'ui.html')
OUT = os.path.join(_HERE, 'bubble_text_result.txt')

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
    out('  [%s] %-56s %s' % ('PASS' if ok else 'FAIL', name,
                             (':: ' + str(detail)) if detail else ''))


UI = io.open(UI_HTML, encoding='utf-8').read()


def extract_function(src, name):
    """按花括号配平抽出整个函数定义（缩进不定，不能靠缩进切）。"""
    m = re.search(r'function\s+%s\s*\(' % re.escape(name), src)
    if not m:
        return None
    i = src.index('{', m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    return None


# ===========================================================================
out('=' * 82)
out('A. clipBubbleText —— 行为级（抽出来在 node 里真跑）')
out('=' * 82)

NODE = find_node()
check('找到 node', bool(NODE), NODE or '')

FN = extract_function(UI, 'clipBubbleText')
check('ui.html 里有 clipBubbleText', FN is not None)

M_MAX = re.search(r'var MAX_BUBBLE_CHARS\s*=\s*(\d+)\s*;', UI)
check('ui.html 里有 MAX_BUBBLE_CHARS', M_MAX is not None)
MAXC = int(M_MAX.group(1)) if M_MAX else 0
out('    MAX_BUBBLE_CHARS = %d' % MAXC)
check('  上限是个合理的量级（50~600）', 50 <= MAXC <= 600, MAXC)

if NODE and FN and M_MAX:
    cases = [
        ('短文案', '错了一个字'),
        ('恰好等于上限', '啊' * MAXC),
        ('上限 + 1', '啊' * (MAXC + 1)),
        ('5000 字（用户语料可达）', '啊' * 5000),
        ('50000 字（外部 POST 可达）', '啊' * 50000),
        ('数字 123', 123),
        ('null', None),
        ('空串', ''),
    ]
    js = [
        'var MAX_BUBBLE_CHARS = %d;' % MAXC,
        FN,
        'var cases = %s;' % json.dumps(cases, ensure_ascii=False),
        'var out = cases.map(function (c) {',
        '  try { return { name: c[0], result: clipBubbleText(c[1]) }; }',
        '  catch (e) { return { name: c[0], error: String(e) }; }',
        '});',
        'console.log(JSON.stringify(out));',
    ]
    tmp = os.path.join(tempfile.gettempdir(), 'airi_clip_probe.js')
    io.open(tmp, 'w', encoding='utf-8').write('\n'.join(js))
    p = subprocess.run([NODE, tmp], capture_output=True)
    if p.returncode != 0:
        check('node 跑通', False, p.stderr.decode('utf-8', 'replace')[:200])
        results = []
    else:
        results = json.loads(p.stdout.decode('utf-8').strip())
        check('node 跑通', True)

    by_name = {r['name']: r for r in results}

    r = by_name.get('短文案', {})
    check('短文案原样返回', r.get('result') == '错了一个字', r.get('result') or r.get('error'))

    r = by_name.get('恰好等于上限', {})
    check('恰好等于上限：不动', r.get('result') == '啊' * MAXC,
          'len=%s' % (len(r.get('result') or '')))

    r = by_name.get('上限 + 1', {})
    rs = r.get('result') or ''
    check('超过上限：被裁', len(rs) < MAXC + 40 and len(rs) > MAXC, 'len=%d' % len(rs))
    check('  且带"已省略"提示', '…' in rs and '已省略' in rs, rs[-24:])

    for name, n in (('5000 字（用户语料可达）', 5000),
                    ('50000 字（外部 POST 可达）', 50000)):
        r = by_name.get(name, {})
        rs = r.get('result') or ''
        check('%s：裁到 %d 附近' % (name, MAXC), MAXC <= len(rs) <= MAXC + 40,
              'len=%d' % len(rs))
        # 打字机定速公式：最少 16ms/字
        secs = len(rs) * 16 / 1000.0
        check('  打完只需 %.1f 秒（原来 %.1f 秒）' % (secs, n * 16 / 1000.0),
              secs < 6.0, 'secs=%.1f' % secs)

    r = by_name.get('数字 123', {})
    check('数字不炸，转成字符串', r.get('result') == '123' and 'error' not in r,
          r.get('result') or r.get('error'))

    r = by_name.get('null', {})
    check('null 不炸（showBubble 另有 !text 守卫，这里只要求不抛）',
          'error' not in r, r.get('result') or r.get('error'))


# ===========================================================================
out('')
out('=' * 82)
out('B. 静态契约 —— typeText 与 showBubble')
out('=' * 82)

TYPE_FN = extract_function(UI, 'typeText') or ''
check('typeText 在气泡被移除后停止（isConnected 守卫）',
      'isConnected' in TYPE_FN,
      '否则长文案被打断后仍会往脱离文档的节点里空转')

SHOW_FN = extract_function(UI, 'showBubble') or ''
check('showBubble 在建立气泡前先裁文案',
      'clipBubbleText(' in SHOW_FN,
      '否则长文案会原样进打字机')

check('上限与 CSS 的可见能力匹配（max-height:132px + overflow:hidden 仍在）',
      'max-height: 132px' in UI and 'overflow: hidden' in UI)


# ===========================================================================
out('')
out('=' * 82)
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
out('=' * 82)
sys.exit(0 if _fail == 0 else 1)
