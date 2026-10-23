# -*- coding: utf-8 -*-
"""test_drag_state.py — 拖拽状态机的回归（v0.3.37）

**bug**：dragging 只有 `document.mouseup` 一条复位路径。而 move_window 是
跨桥异步的（JS → pywebview → Python → ctypes.MoveWindow），窗口跟不上光标 ——
快速甩动 / 拖到屏幕边缘（Python 侧 clamp_to_virtual_screen 把窗口卡住而光标
继续走）/ 拖拽中 Alt+Tab —— 松手那一刻光标常常已经在窗口之外，mouseup 被
桌面或别的窗口吃掉，dragging 就**永远是 true**。之后不按任何鼠标键，只要
光标经过桌宠，mousemove 照常 move_window，桌宠像被磁铁吸住一样跟着鼠标跑
（"拖了一次就一直粘着，点一下才好"）。

**修法**：改用 pointer events，按下时 `setPointerCapture` 把后续指针事件锁到
petArea（光标甩出窗口也收得到 pointerup）；再叠三道兜底 —— pointermove 里
`buttons === 0` 复位、pointercancel 复位、window blur 复位。

A 段**行为级**：把这段状态机从 ui.html 抽出来，配最小 DOM stub 在 node 里真跑，
用"move_window 被调用几次"这个**副作用**判定状态机有没有收手。
B 段静态契约：钉死三道兜底都在。

用法：python test_drag_state.py → live2d_probe/drag_state_result.txt
"""
import io
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from node_finder import find_node  # noqa: E402

UI_HTML = os.path.join(_PROJ, 'desktop_pet', 'ui.html')
OUT = os.path.join(_HERE, 'drag_state_result.txt')

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

START = 'var dragging = false, dragStartX = 0'
# ⚠️ 锚点要跟着产品代码走（0.3.43）：blur 回调不再是单行，它多了一句
# "失焦也要收菜单"（菜单是 on_top 的，不收的话切走再回来点角色会误触
# 「退出」）。这里取到 blur 回调的收尾 `});` 为止，抽出的块仍是完整的
# 状态机 + 三道兜底。改产品代码时**必须**同步这个锚点，否则 block 为空、
# 整个 A 段 13 条一起假失败（看起来像拖拽彻底坏了）。
END = ("      if (contextMenu.classList.contains('show')) hideMenu();\n"
       "    });")

i0 = UI.find(START)
i1 = UI.find(END)
block = UI[i0:i1 + len(END)] if (i0 >= 0 and i1 > i0) else ''

# ===========================================================================
out('=' * 82)
out('A. 拖拽状态机 —— 行为级（抽出来配 DOM stub 在 node 里真跑）')

check('A0 能从 ui.html 抽出整段状态机', bool(block) and len(block) > 400,
      '抽出 %d 字符' % len(block))

STUB = r'''
// ---- 最小 DOM stub ----
var MOVES = [];      // move_window 每一次调用
var CLICKS = 0;      // handlePetClick 调用次数
var CAP = [];        // setPointerCapture 调用
var petArea = {
  _h: {}, _cap: null,
  addEventListener: function(t, f) { this._h[t] = f; },
  setPointerCapture: function(id) { this._cap = id; CAP.push(id); },
  releasePointerCapture: function(id) { this._cap = null; },
  hasPointerCapture: function(id) { return this._cap === id; }
};
var window = {
  screenX: 0, screenY: 0, _h: {},
  addEventListener: function(t, f) { this._h[t] = f; },
  pywebview: { api: {
    get_position: function() { return [10, 20]; },
    move_window: function(x, y) { MOVES.push([x, y]); }
  }}
};
function handlePetClick(e) { CLICKS++; }
// 0.3.43：blur 回调里还会调 hideMenu（失焦收右键菜单），补最小 stub
var contextMenu = { classList: { contains: function() { return false; } } };
function hideMenu() {}

'''

HARNESS = r'''
// ---- 场景 ----
function ev(o) { o.preventDefault = function () {}; return o; }
function pd(x, y) {
  return ev({ button: 0, clientX: x, clientY: y, screenX: x, screenY: y, pointerId: 1 });
}
function pm(x, y, buttons) {
  return ev({ buttons: (buttons === undefined ? 1 : buttons),
              clientX: x, clientY: y, screenX: x, screenY: y });
}
function up(x, y) { return ev({ button: 0, clientX: x, clientY: y }); }

var R = {};

// 1) 正常拖拽：按下 + 移动两次 -> 应该跟着动两次
MOVES.length = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointermove'](pm(200, 200, 1));
petArea._h['pointermove'](pm(300, 300, 1));
R.dragMoves = MOVES.length;

// 2) 【核心】松手在窗口外：根本不送 pointerup（模拟 mouseup 被别的窗口吃掉）。
//    之后光标回到窗口上、且左键没按（buttons=0）-> 必须收手，不许再动。
MOVES.length = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointermove'](pm(200, 200, 1));   // 这一次是正常拖拽
petArea._h['pointermove'](pm(400, 400, 0));   // 左键已松开（捕获失效路径）
petArea._h['pointermove'](pm(500, 500, 1));   // 之后"粘住"的话这里会继续动
petArea._h['pointermove'](pm(600, 600, 1));
R.afterRelease = MOVES.length;

// 3) pointerup 正常收手
MOVES.length = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointermove'](pm(200, 200, 1));
petArea._h['pointerup'](up(200, 200));
petArea._h['pointermove'](pm(300, 300, 1));
R.afterUp = MOVES.length;

// 4) 原地按下松开 = 点击（不是拖拽）
CLICKS = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointerup'](up(100, 100));
R.click = CLICKS;

// 5) 拖过再松开不算点击（否则每次拖窗口都会蹦一句台词）
CLICKS = 0;
MOVES.length = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointermove'](pm(300, 300, 1));
petArea._h['pointerup'](up(300, 300));
R.clickAfterDrag = CLICKS;
R.dragDidMove = MOVES.length;

// 6) pointercancel（系统抢走指针）也要收手
MOVES.length = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointermove'](pm(200, 200, 1));
petArea._h['pointercancel'](ev({}));
petArea._h['pointermove'](pm(300, 300, 1));
R.afterCancel = MOVES.length;

// 7) window blur（Alt+Tab 走开）也要收手
MOVES.length = 0;
petArea._h['pointerdown'](pd(100, 100));
petArea._h['pointermove'](pm(200, 200, 1));
window._h['blur']();
petArea._h['pointermove'](pm(300, 300, 1));
R.afterBlur = MOVES.length;

// 8) 捕获：按下时真的请求了 pointer capture，松开时真的放了
CAP.length = 0;
petArea._h['pointerdown'](pd(100, 100));
R.captured = CAP.length;
petArea._h['pointerup'](up(100, 100));
R.released = (petArea._cap === null);

console.log(JSON.stringify(R));
'''

res = None
if block:
    js = STUB + block + '\n' + HARNESS
    tmp = os.path.join(os.environ.get('TEMP', '/tmp'), 'airi_drag_state.js')
    io.open(tmp, 'w', encoding='utf-8').write(js)
    try:
        p = subprocess.run([find_node(), tmp], capture_output=True, timeout=60)
        res = p.stdout.decode('utf-8', 'replace').strip()
        err = p.stderr.decode('utf-8', 'replace').strip()
        if p.returncode != 0:
            out('  (node 退出码 %d) %s' % (p.returncode, err[:400]))
    except Exception as exc:
        out('  (跑 node 失败: %r)' % (exc,))

import json  # noqa: E402
R = {}
if res:
    try:
        R = json.loads(res.splitlines()[-1])
    except Exception as exc:
        out('  (解析 node 输出失败: %r) 原始=%r' % (exc, res[:300]))

check('A1 node 真的跑起来了', bool(R), '输出 %r' % (res or '')[:120])
check('A2 正常拖拽：按下+移动两次 -> move_window 被调用 2 次',
      R.get('dragMoves') == 2, '实测 %r' % R.get('dragMoves'))
check('A3 【核心】松手在窗口外后必须收手：之后的移动只应算 1 次'
      '（修复前会一直跟着鼠标动）',
      R.get('afterRelease') == 1, '实测 %r' % R.get('afterRelease'))
check('A4 pointerup 之后不再移动', R.get('afterUp') == 1,
      '实测 %r' % R.get('afterUp'))
check('A5 原地按下松开 = 点击（handlePetClick 被调用 1 次）',
      R.get('click') == 1, '实测 %r' % R.get('click'))
check('A6 拖过之后松开不算点击（否则拖一次窗口就蹦一句台词）',
      R.get('clickAfterDrag') == 0, '实测 %r' % R.get('clickAfterDrag'))
check('A7 拖过确实移动了窗口（A6 不是因为压根没动）',
      R.get('dragDidMove') == 1, '实测 %r' % R.get('dragDidMove'))
check('A8 pointercancel 后收手', R.get('afterCancel') == 1,
      '实测 %r' % R.get('afterCancel'))
check('A9 window blur 后收手（Alt+Tab 走开不留半路状态）',
      R.get('afterBlur') == 1, '实测 %r' % R.get('afterBlur'))
check('A10 按下时真的请求了 pointer capture',
      R.get('captured') == 1, '实测 %r' % R.get('captured'))
check('A11 松开时真的释放了 capture',
      R.get('released') is True, '实测 %r' % R.get('released'))

# ===========================================================================
out('')
out('=' * 82)
out('B. 静态契约：三道兜底必须都在（同类问题要穷举）')

check('B1 用 pointer 事件而不是 mouse 事件',
      "'pointerdown'" in UI and "'pointermove'" in UI and "'pointerup'" in UI)
check('B2 按下时 setPointerCapture（光标甩出窗口也收得到 up）',
      'setPointerCapture' in UI)
check('B3 pointermove 里有 buttons===0 兜底复位',
      re.search(r'e\.buttons\s*===\s*0', UI) is not None)
check('B4 有 pointercancel 复位', "'pointercancel'" in UI)
check('B5 有 window blur 复位', "window.addEventListener('blur'" in UI)
check('B6 收手时释放 capture', 'releasePointerCapture' in UI)
check('B7 拖拽不再挂在 document 上（挂在 document 上就收不到窗口外的 up）',
      "document.addEventListener('mousemove'" not in UI
      and "document.addEventListener('mouseup'" not in UI,
      '残留 mousemove=%s mouseup=%s' % (
          "document.addEventListener('mousemove'" in UI,
          "document.addEventListener('mouseup'" in UI))
check('B8 抽出块里有 endDrag 这个统一的收手入口',
      'function endDrag(' in block)

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'HAS FAILURE',
                                  _fail))
out('=' * 82)
sys.exit(0 if _fail == 0 else 1)
