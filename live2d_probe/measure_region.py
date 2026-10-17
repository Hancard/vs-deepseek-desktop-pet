# -*- coding: utf-8 -*-
"""measure_region.py — 从**正在运行的桌宠窗口**读出它的异形 region，反推轮廓行分布

为什么走这条路：zoneAt() 的分区比例（0.45 / 0.85）是当年拍脑袋定的，而本地
没有任何办法渲染 Live2D（无头浏览器不可用、模型没声明 HitAreas、预览图不透明）。
但桌宠**自己**早就量过轮廓了 —— 它每帧把「角色 alpha 形状」算成一组矩形，
交给 SetWindowRgn 裁窗口。用 GetWindowRgn + GetRegionData 把它读回来，
就是一份实测的角色行分布。

产物：live2d_probe/region_profile.txt
"""
import ctypes
import os
import sys
from ctypes import wintypes

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'region_profile.txt')
_lines = []


def out(s=''):
    _lines.append(str(s))
    print(s)


def flush():
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(_lines) + '\n')


u = ctypes.windll.user32
g = ctypes.windll.gdi32

EnumWindows = u.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

found = []


def _enum(hwnd, _lp):
    if not u.IsWindowVisible(hwnd):
        return True
    n = u.GetWindowTextLengthW(hwnd)
    if n == 0:
        return True
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    title = buf.value
    if title:
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, title, int(r.left), int(r.top),
                      int(r.right - r.left), int(r.bottom - r.top)))
    return True


EnumWindows(EnumWindowsProc(_enum), 0)

out('=== 可见窗口（找桌宠）===')
pet = None
for hwnd, title, x, y, w, h in found:
    mark = ''
    if title.strip() == 'deepseek':
        mark = '   <== 桌宠'
        pet = (hwnd, x, y, w, h)
    out('  hwnd=0x%X  %dx%d at (%d,%d)  %r%s' % (hwnd, w, h, x, y, title[:40], mark))

if pet is None:
    out('')
    out('!! 没找到标题为 deepseek 的窗口 —— 桌宠没在跑。')
    out('   请先启动桌宠（或让 VS Code 扩展拉起），再跑这个脚本。')
    flush()
    sys.exit(1)

hwnd, wx, wy, ww, wh = pet
# pywebview 会给出**两个**同名窗口（宿主 Form + 一个壳），region 挂在其中一个上
# —— 逐个试，挑第一个真能读到 region 的
cands = [(h, x, y, w, hh) for (h, t, x, y, w, hh) in found if t.strip() == 'deepseek']
for (h2, x2, y2, w2, h2h) in cands:
    _rgn = g.CreateRectRgn(0, 0, 0, 0)
    _res = u.GetWindowRgn(h2, _rgn)
    g.DeleteObject(_rgn)
    out('  候选 hwnd=0x%X %dx%d -> GetWindowRgn=%d' % (h2, w2, h2h, _res))
    if _res in (2, 3):
        hwnd, wx, wy, ww, wh = h2, x2, y2, w2, h2h
        break
out('桌宠窗口: hwnd=0x%X  %dx%d at (%d,%d)' % (hwnd, ww, wh, wx, wy))

hrgn = g.CreateRectRgn(0, 0, 0, 0)
res = u.GetWindowRgn(hwnd, hrgn)
# ⚠️ GDI 的区域类型常量：ERROR=0 / NULLREGION=1 / SIMPLEREGION=2 / COMPLEXREGION=3
#    （第一版把 3 当成错误，其实 3 = 复杂区域 = **读到了**）
out('GetWindowRgn -> %d（0=ERROR 1=NULLREGION 2=SIMPLE 3=COMPLEX）' % res)
if res in (0, 1):
    out('!! 这个窗口没有 region（桌宠可能刚启动/轮廓被关掉了）')
    flush()
    sys.exit(1)

size = g.GetRegionData(hrgn, 0, None)
out('GetRegionData 需要 %d 字节' % size)
buf = ctypes.create_string_buffer(size)


class RGNDATAHEADER(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('iType', wintypes.DWORD),
                ('nCount', wintypes.DWORD), ('nRgnSize', wintypes.DWORD),
                ('rcBound', wintypes.RECT)]


class RGNDATA(ctypes.Structure):
    _fields_ = [('rdh', RGNDATAHEADER), ('Buffer', ctypes.c_byte * 1)]


got = g.GetRegionData(hrgn, size, ctypes.byref(buf))
if not got:
    out('!! GetRegionData 失败')
    flush()
    sys.exit(1)

rd = ctypes.cast(ctypes.byref(buf), ctypes.POINTER(RGNDATA)).contents
n = rd.rdh.nCount
out('矩形个数: %d   bounds=(%d,%d)-(%d,%d)'
    % (n, rd.rdh.rcBound.left, rd.rdh.rcBound.top,
       rd.rdh.rcBound.right, rd.rdh.rcBound.bottom))

RECTS = wintypes.RECT * n
rects_off = ctypes.sizeof(RGNDATAHEADER)
rects = ctypes.cast(ctypes.byref(buf, rects_off), ctypes.POINTER(RECTS)).contents

rs = [(int(r.left), int(r.top), int(r.right), int(r.bottom)) for r in rects]
minx = min(r[0] for r in rs)
miny = min(r[1] for r in rs)
maxx = max(r[2] for r in rs)
maxy = max(r[3] for r in rs)
out('实际范围 x %d..%d   y %d..%d' % (minx, minx and maxx or maxx, miny, maxy))
out('（若 x/y 接近窗口屏幕坐标，说明 GetWindowRgn 给的是绝对坐标）')

# 归一化到窗口内：优先按"相对窗口左上角"解释
if minx >= wx - 5 and miny >= wy - 5:
    ox, oy = wx, wy          # 绝对坐标
    out('坐标类型: 绝对（减去窗口左上角）')
else:
    ox, oy = 0, 0
    out('坐标类型: 相对窗口左上角')

# 行分布：把 y 按窗口高度分 40 段，统计被矩形覆盖的宽度占比
N = 40
rows = []
for b in range(N):
    y0 = oy + int(b * wh / N)
    y1 = oy + int((b + 1) * wh / N)
    covered = [0] * ww
    for (l, t, rr, bb) in rs:
        lt, tt, rr2, bb2 = l - ox, t - oy, rr - ox, bb - oy
        if bb2 <= y0 or tt >= y1:
            continue
        for x in range(max(0, lt), min(ww, rr2)):
            covered[x] = 1
    rows.append(100.0 * sum(covered) / ww)

out('')
out('=== 窗口纵向覆盖（40 段，0.00=窗口顶 1.00=窗口底）===')
for i, pct in enumerate(rows):
    ry0 = i / float(N)
    out('  ry %.3f-%.3f  %5.1f%% %s' % (ry0, ry0 + 1.0 / N, pct, '#' * int(pct / 5)))

out('')
out('=== 字符画（40 行 x 60 列，# = region 覆盖）===')
CH, CW = 40, 60
for ry_i in range(CH):
    y0 = oy + int(ry_i * wh / CH)
    y1 = oy + int((ry_i + 1) * wh / CH)
    line = ''
    for rx in range(CW):
        x0 = int(rx * ww / CW)
        x1 = int((rx + 1) * ww / CW)
        hit = False
        for (l, t, rr, bb) in rs:
            lt, tt, rr2, bb2 = l - ox, t - oy, rr - ox, bb - oy
            if bb2 <= y0 or tt >= y1:
                continue
            if rr2 > x0 and lt < x1:
                hit = True
                break
        line += '#' if hit else '.'
    out('%5.2f |%s|' % (ry_i / float(CH), line))

out('')
out('注：region 里除角色外还可能有气泡/名牌（若当时有气泡，会多出一块）。')
out('    当前 zoneAt: ry<0.45 = head, ry<0.85 = body, 其余 = desk')
flush()
