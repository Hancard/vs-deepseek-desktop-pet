# -*- coding: utf-8 -*-
"""test_region_handle_leak.py — 异形窗口早退路径的 GDI 句柄泄漏（v0.3.31）

被测：common._apply_window_region() —— 窗口 region 的底层实现。

缺陷：`if not kept:` 这条**所有矩形都被裁没**的早退路径上，h_total
（CreateRectRgn 出来的那个区域句柄）没有交给系统（SetWindowRgn 没被调到），
也没有自己 DeleteObject —— 每调用一次泄漏 **1 个 GDI 区域句柄**。

为什么这很致命而不是"一点点泄漏"：
  * region 是 ui.html **500ms 一拍**推上来的（collectUiRects -> set_window_region），
    也就是每秒 2 个句柄；
  * 进程 GDI 句柄默认上限 **10000**（GDIProcessHandleQuota），到顶之后
    CreateRectRgn 开始失败 -> h_total 为 0 -> "异形窗口"这个功能**永久坏掉**
    （不是变慢，是彻底不工作，且重启也只在重启后那段时间内还能用）；
  * 这条早退路径本身还特别容易进 —— 窗口最小化、DPR 变化的一瞬间、
    拖到屏幕外把矩形全裁掉，都会得到 kept == 0。

判据是**增量随 N 的线性度**，不是单次绝对数值（上一轮踩过：GetGuiResources
有 ±7 的环境噪声，看单次会误判）。

两个测量陷阱（上一轮踩过，都写进断言/注释了）：
  1. `GetGuiResources(GetCurrentProcess(), ...)` **恒返回 0** —— 伪句柄
     (-1) 不被接受。必须 `OpenProcess(PROCESS_QUERY_INFORMATION)` 拿真句柄。
  2. 泄漏会把句柄顶到上限，之后的测量全部失真 —— 所以 N 阶梯要小，
     且在**同一次进程**里做（跨进程没法比较增量）。

安全：本测试**绝不真的去裁任何窗口**。
  · _get_hwnd 换成 GetDesktopWindow() 只是为了通过 `if not hwnd` 那道门；
  · 所有用例都走 **kept == 0 的早退分支**（矩形全被裁掉），SetWindowRgn
    根本不会被调到 —— 所以桌面 region 不变（末尾有断言复核）。
  · _set_backdrop_none 换成 no-op，不碰 DWM。
  · 花掉的那 2000 多个句柄随进程退出自动回收。

用法：python test_region_handle_leak.py
      → live2d_probe/region_handle_leak_result.txt
      返回码 0 全通过 / 1 有失败
"""
import ctypes
import io
import os
import sys

PROBE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PROBE)
PET = os.path.join(ROOT, 'desktop_pet')
sys.path.insert(0, PET)

OUT = os.path.join(PROBE, 'region_handle_leak_result.txt')

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
    out('  [%s] %-62s %s' % ('PASS' if ok else 'FAIL', name,
                             (':: ' + str(detail)) if detail else ''))


if sys.platform != 'win32':
    out('SKIP: 非 Windows（region 是 Win32 专属）')
    out('RESULT: ALL PASS (0 failures)')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    sys.exit(0)

import common  # noqa: E402

# ---------------------------------------------------------------------------
# GDI 句柄计数
# ---------------------------------------------------------------------------
PROCESS_QUERY_INFORMATION = 0x0400
GR_GDIOBJECTS = 0

_k32 = ctypes.WinDLL('kernel32')
_u32 = ctypes.WinDLL('user32')
_gdi = ctypes.WinDLL('gdi32')
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_u32.GetGuiResources.restype = ctypes.c_uint32
_u32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_u32.GetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_u32.GetWindowRgn.restype = ctypes.c_int
_gdi.CreateRectRgn.restype = ctypes.c_void_p
_gdi.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi.DeleteObject.restype = ctypes.c_int

# ⚠️ 必须 OpenProcess 拿真句柄 —— GetCurrentProcess() 是伪句柄 -1，
#    GetGuiResources 对它恒返回 0（上一轮实测）。
_hproc = _k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, os.getpid())
if not _hproc:
    out('FATAL: OpenProcess failed (err=%d)' % ctypes.get_last_error())
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    sys.exit(1)


def gdi_count():
    return int(_u32.GetGuiResources(_hproc, GR_GDIOBJECTS))


# ---------------------------------------------------------------------------
# 把 _apply_window_region 引到"不会真的动任何窗口"的环境里
# ---------------------------------------------------------------------------
_desktop = int(_u32.GetDesktopWindow())
_real_get_hwnd = common._get_hwnd
_real_backdrop = common._set_backdrop_none
common._get_hwnd = lambda window: _desktop
common._set_backdrop_none = lambda hwnd: False


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


_gdi.GetRgnBox.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
_gdi.GetRgnBox.restype = ctypes.c_int


def desktop_region_sig():
    """桌面窗口 region 的**签名**（类型 + 外接框）。

    ⚠️ 不能用 "类型 == 0" 当判据 —— 实测桌面窗口**本来就有** region
    （Win11 上返回 3 = COMPLEXREGION，大概与多显示器布局有关）。这里真正
    要守的不变量是"**没有被本测试改动**"，所以比较前后签名。

    用一段临时区域句柄去读（读完立刻删）；不要传 NULL —— 传 NULL 的语义
    文档里写作"返回类型"，但各家实现不一致，给个真句柄最稳。
    """
    h = _gdi.CreateRectRgn(0, 0, 0, 0)
    if not h:
        return None
    try:
        t = int(_u32.GetWindowRgn(_desktop, h))
        box = _RECT()
        _gdi.GetRgnBox(h, ctypes.byref(box))
        return (t, box.left, box.top, box.right, box.bottom)
    finally:
        _gdi.DeleteObject(h)


# 这批矩形**全部会被裁没**：负坐标 -> _clip_x/y 夹到 0 -> r<=l -> continue
# => kept == 0 => 走那条早退分支（正是被修的那条）
#
# ⚠️ 只走这一条：正常矩形会真的调到 SetWindowRgn，而我们的 hwnd 是**桌面窗口**
# —— 那会真的把用户的桌面裁掉，绝对不能在这里试。正常路径由 test_window_dpi /
# test_window_clamp 与下面 D 段的静态不变量覆盖。
CLIPPED = [{'l': -50, 't': -50, 'r': -10, 'b': -10}]

out('=' * 82)
out('环境')
out('=' * 82)
out('  desktop hwnd       = %s' % hex(_desktop))
out('  桌面 region 签名   = %r（类型,外接框；测试前后必须一致）'
    % (desktop_region_sig(),))
out('  起始 GDI 句柄       = %d' % gdi_count())
out('')

_region_before = desktop_region_sig()

# ---------------------------------------------------------------------------
out('=' * 82)
out('A. 早退路径（所有矩形被裁没）不得泄漏句柄')
out('=' * 82)

# 先做一次热身 —— 排除"首次调用会懒加载/建立缓存"这类一次性开销
for _ in range(5):
    common._apply_window_region(None, CLIPPED, 300.0, 450.0)
_warm = gdi_count()

N1 = 200
for _ in range(N1):
    common._apply_window_region(None, CLIPPED, 300.0, 450.0)
_after_n1 = gdi_count()
d1 = _after_n1 - _warm

N2 = 2000          # 10 倍
for _ in range(N2):
    common._apply_window_region(None, CLIPPED, 300.0, 450.0)
_after_n2 = gdi_count()
d2 = _after_n2 - _after_n1

out('  热身 5 次后            = %d' % _warm)
out('  +%d 次 -> %d   增量 = %d' % (N1, _after_n1, d1))
out('  +%d 次 -> %d   增量 = %d   （N 翻 10 倍）' % (N2, _after_n2, d2))
out('  对照：修复前实测这里分别是 N1=200 / N2=2000（每次恰好 +1）')
out('  对照：N=30000 时修复前会把句柄顶到 10000 上限')

REAL_ROWS = 2      # 上一轮实测的环境噪声量级（±7），所以阈值给到 50/200
check('N=%d 次调用增量 <= 50（修复前是 %d）' % (N1, N1), d1 <= 50, 'delta=%d' % d1)
check('N=%d 次调用增量 <= 200（修复前是 %d）' % (N2, N2), d2 <= 200,
      'delta=%d' % d2)
check('增量随 N **不**线性增长（不是"每次 +1"那条曲线）',
      d2 <= max(50, d1 * 3), 'd1=%d d2=%d（噪声量级参考 %d）'
      % (d1, d2, REAL_ROWS))

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('B. 对照：空矩形列表走的是另一条更早的返回（连 CreateRectRgn 都不做）')
out('=' * 82)

_b = gdi_count()
for _ in range(500):
    r = common._apply_window_region(None, [], 300.0, 450.0)
_a = gdi_count()
check('rects=[] 时 500 次调用增量 <= 20（且 why=empty rects (refused)）',
      (_a - _b) <= 20 and r.get('why') == 'empty rects (refused)',
      'delta=%d why=%r' % (_a - _b, r.get('why')))

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('C. 早退分支的返回值形状没被改坏（不是"不泄漏就行"）')
out('=' * 82)

res = common._apply_window_region(None, CLIPPED, 300.0, 450.0)
check('返回 ok=False、why="all rects clipped away"',
      res.get('ok') is False and res.get('why') == 'all rects clipped away',
      res.get('why'))
check('meta 里带 kept=0 与 client 尺寸（排障要靠它）',
      res.get('meta', {}).get('kept') == 0
      and 'x' in str(res.get('meta', {}).get('client', '')),
      res.get('meta'))

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('D. 静态不变量：三条退出路径都必须归还 h_total')
out('=' * 82)

_src = io.open(os.path.join(PET, 'common.py'), encoding='utf-8').read()

# 只取 _apply_window_region 这一个函数的源码 —— 用 "文件里最后一个 except"
# 那种写法会串到别的函数上（本测试第一版就是这么误报的）
_region_src = _src[_src.index('def _apply_window_region('):]
_region_src = _region_src[:_region_src.index(
    '\n# ---------------------------------------------------------------------------')]

# 定位 `if not kept:` 那段，检查它的第一条可执行语句就是 DeleteObject
_lines_src = _region_src.splitlines()
_anchor = None
for i, ln in enumerate(_lines_src):
    if ln.strip() == 'if not kept:':
        _anchor = i
        break
check('common.py 里存在 `if not kept:` 早退分支', _anchor is not None)

if _anchor is not None:
    body = []
    for ln in _lines_src[_anchor + 1:_anchor + 16]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith('#'):
            continue
        body.append(s)
        if s.startswith('return'):
            break
    check('  该分支在 return 之前先 gdi32.DeleteObject(h_total)',
          bool(body) and body[0] == 'gdi32.DeleteObject(h_total)',
          '首条可执行语句 = %r' % (body[0] if body else None))
    check('  且确实在 DeleteObject 之后才 return',
          any(b.startswith('return') for b in body[1:]),
          ' | '.join(body[:3]))

check('SetWindowRgn 失败那条分支也 DeleteObject',
      'if not user32.SetWindowRgn(hwnd, h_total, True):' in _region_src
      and _region_src.count('gdi32.DeleteObject(h_total)') == 3,
      'DeleteObject(h_total) 出现 %d 次'
      % _region_src.count('gdi32.DeleteObject(h_total)'))
check('异常兜底分支也 DeleteObject（外层 except 的 try 里就是它）',
      'gdi32.DeleteObject(h_total)'
      in _region_src.split('except Exception:')[1],
      '函数内 except Exception 次数 = %d'
      % _region_src.count('except Exception:'))

# 每次合并进来的 piece 也必须当场删掉（否则是另一个泄漏源）
check('_merge() 合并后立刻 DeleteObject(h_piece)（piece 泄漏源）',
      'gdi32.DeleteObject(h_piece)' in _region_src)

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('E. 本测试没有真的动过任何窗口')
out('=' * 82)

_region_after = desktop_region_sig()
check('桌面窗口 region 签名未被改动（类型 + 外接框）',
      _region_after == _region_before,
      '%r -> %r' % (_region_before, _region_after))
check('  —— 结构上早退发生在 SetWindowRgn 之前（所以 A/B 确实没碰到窗口）',
      _region_src.index('if not kept:') < _region_src.index('user32.SetWindowRgn('),
      'if not kept @%d  SetWindowRgn @%d'
      % (_region_src.index('if not kept:'),
         _region_src.index('user32.SetWindowRgn(')))

common._get_hwnd = _real_get_hwnd
common._set_backdrop_none = _real_backdrop
_k32.CloseHandle(_hproc)

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if _fail == 0 else 1)
