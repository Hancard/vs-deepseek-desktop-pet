# -*- coding: utf-8 -*-
"""test_click_zones.py — 点击分区边界的回归（v0.3.36）

为什么改边界：zoneAt() 的 desk 带旧定义是 ry>=0.85，而实测（0.3.36，
live2d_probe/measure_region.py 读运行中桌宠的窗口 region）——

  * 角色（含桌子）实际画在 canvas ry 0.109~0.825（角色卡就是按这个
    外接框贴的：卡 y 202~353 / canvas y 179~390，窗口 286x443）；
  * 所以 desk 带整条落在角色卡**下缘之外**的缝隙里，缝隙不在窗口
    region 内 → 点击直接穿透到下层窗口；
  * 结论：desk 台词一条都触发不了，点桌子只会得到 body 的台词
    （用户 09:03:05 的 pet_click #1 zone=body 就是这么来的）。

新边界：head < 0.45（不变）、body < 0.62、desk >= 0.62 —— desk 带回到
可点击范围内（0.62 < 0.825）。

A 段**行为级**：把 zoneFromRatio 从 ui.html 抽出来在 node 里真跑（与
test_bubble_text 抽 clipBubbleText 同套路）；B 段静态契约 + 用实测数字
钉死"desk 带必须在可点击范围内"，防止以后又把边界挪出角色外。

用法：python test_click_zones.py → live2d_probe/click_zones_result.txt
"""
import io
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
OUT = os.path.join(_HERE, 'click_zones_result.txt')

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
out('A. zoneFromRatio —— 行为级（抽出来在 node 里真跑）')

fn = extract_function(UI, 'zoneFromRatio')
check('A0 能从 ui.html 抽出 zoneFromRatio', fn is not None)
consts = re.findall(r'var\s+(ZONE_HEAD_MAX|ZONE_DESK_MIN)\s*=\s*([0-9.]+)\s*;', UI)
cmap = dict(consts)
check('A0 两个边界常量都存在', set(cmap) == {'ZONE_HEAD_MAX', 'ZONE_DESK_MIN'},
      '找到 %r' % (consts,))

js = 'var ZONE_HEAD_MAX=%s, ZONE_DESK_MIN=%s;\n%s\n' % (
    cmap.get('ZONE_HEAD_MAX', '0.45'), cmap.get('ZONE_DESK_MIN', '0.62'), fn or '')
js += '''
var cases = [[0.0,'head'],[0.10,'head'],[0.449,'head'],[0.45,'body'],[0.5,'body'],
             [0.619,'body'],[0.62,'desk'],[0.7,'desk'],[0.825,'desk'],[1.0,'desk'],
             [1.5,'desk'],[-0.2,'head']];
var bad = [];
for (var i = 0; i < cases.length; i++) {
  var got = zoneFromRatio(cases[i][0]);
  if (got !== cases[i][1]) bad.push(cases[i][0] + '->' + got + '(want ' + cases[i][1] + ')');
}
console.log(bad.length ? 'FAIL ' + bad.join(', ') : 'PASS');
'''
if fn:
    node = find_node()
    check('A0 找到 node', bool(node))
    tf = os.path.join(tempfile.gettempdir(), 'airi_zone_ratio_test.js')
    io.open(tf, 'w', encoding='utf-8').write(js)
    p = subprocess.run([node, tf], capture_output=True, timeout=30)
    tail = (p.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
    verdict = tail[-1] if tail else '(无输出)'
    check('A1 边界判定 12 例全对', verdict.startswith('PASS'), verdict)
else:
    check('A1 边界判定 12 例全对', False, '抽不出函数，无法跑')

# ===========================================================================
out('')
out('B. 静态契约')

# B1 —— zoneAt 必须走 zoneFromRatio（别再写回内联的 if 链，否则两处边界会漂移）
za = extract_function(UI, 'zoneAt')
check('B1 zoneAt 调 zoneFromRatio', bool(za) and 'zoneFromRatio(ry)' in za)

# B2 —— 旧边界 0.85 不许再出现（它就是这次"desk 点不到"的元凶）
check('B2 旧边界 0.85 已移除', '0.85' not in (za or ''))

# B3 —— pet_click 必须带 ry（校准闭环靠它：点一下桌宠，日志里就有 ry）
check('B3 handlePetClick 给 pet_click 传 ry',
      re.search(r'pet_click\(.+ryLog', UI) is not None)
common_py = io.open(os.path.join(_PROJ, 'desktop_pet', 'common.py'),
                    encoding='utf-8').read()
check('B4 桥接 pet_click(zone, ry) 收 ry', 'def pet_click(self, zone, ry=None)' in common_py)
check('B5 日志打印 ry', 'ry=%s' in common_py)

# B6 —— ★ 用实测数字钉死：desk 带必须落在**可点击范围**内。
#    实测来源：live2d_probe/measure_region.py 读运行中桌宠的窗口 region
#    （0.3.36，窗口 286x443、canvas cssBox 161x211@62,179、角色卡 y 202~353）。
#    换算成 canvas ry：卡 = 0.109 ~ 0.825。桌子贴图（texture_01）就是两条
#    宽梯形桌面，占角色下半 —— 只要 ZONE_DESK_MIN < 0.825，desk 带就有点得到。
CARD_TOP_RY = 0.109
CARD_BOTTOM_RY = 0.825
desk_min = float(cmap.get('ZONE_DESK_MIN', '1'))
head_max = float(cmap.get('ZONE_HEAD_MAX', '0'))
check('B6 desk 带起点在角色卡内（<%.3f）' % CARD_BOTTOM_RY,
      desk_min < CARD_BOTTOM_RY, 'ZONE_DESK_MIN=%s' % desk_min)
check('B7 desk 带有实际高度（与 head 边界拉开）',
      desk_min - head_max >= 0.10, '带宽 %.2f' % (desk_min - head_max))
check('B8 head 边界在角色卡上缘之下',
      head_max > CARD_TOP_RY, 'ZONE_HEAD_MAX=%s' % head_max)

# B9 —— 前端三个分区名与语料侧 CLICK_ZONES 一致（多/少一个都是死台词）
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))
import corpus  # noqa: E402
zones_js = set(re.findall(r"return\s+'(head|body|desk)'", fn or ''))
check('B9 分区名与 CLICK_ZONES 对齐',
      zones_js == set(corpus.CLICK_ZONES),
      'js=%r py=%r' % (sorted(zones_js), list(corpus.CLICK_ZONES)))

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if _fail == 0 else 1)
