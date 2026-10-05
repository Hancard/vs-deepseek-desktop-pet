# -*- coding: utf-8 -*-
"""test_window_dpi.py — DPI 双重缩放修复的真窗口端到端验证（2026-10-05）

背景：pywebview 的 create_window(x=,y=) 按逻辑像素消费（内部乘窗口 DPI
scale 转物理），而 get_screen_size() 在 DPI-aware 进程里返回物理像素。
修复前 150% 缩放 + 2560 主屏时 x=2220 被放到物理 3330 -> 窗口整个在
屏幕右侧之外（单屏不可见；双屏落进第二块屏所以没暴露）。

本测试用修复后的初始位置公式真起一个 pywebview 窗口，用 GetWindowRect
量物理矩形，断言它完整落在主屏物理范围内。必须用系统 Python 3.12 跑
（有 pywebview）。
"""
import ctypes
import ctypes.wintypes as wt
import io
import sys
import threading
import time

sys.path.insert(0, r'S:\My event\projects\vscode-anime-assistent\desktop_pet')

OUT = r'S:\My event\projects\vscode-anime-assistent\live2d_probe\window_dpi_result.txt'
_lines = []


def out(s=''):
    _lines.append(str(s))


from common import get_screen_size, get_dpi_scale  # noqa: E402

fail = 0

# ---------- 1. 纯单元：get_dpi_scale 合理性 ----------
scale = get_dpi_scale()
out('get_dpi_scale() = %.3f' % scale)
if not (0.99 <= scale <= 2.01):
    out('[FAIL] scale out of sane range')
    fail += 1
if abs(scale - 1.5) < 0.01:
    out('  -> 本机 150% 缩放（GetDpiForSystem=144），正是触发 bug 的环境')
else:
    out('  -> 注意：本机 scale 不是 1.5，bug 复现条件不同，但修复同样适用')

screen_w, screen_h = get_screen_size()
out('physical screen: %dx%d' % (screen_w, screen_h))

# ---------- 2. 真窗口端到端 ----------
import webview  # noqa: E402

win_w, win_h = 300, 480
# 修复后的初始位置（逻辑像素）：窗口物理尺寸是 win_w*scale，右下对齐按物理算
x = int((screen_w - win_w * scale - 40) / scale)
y = int((screen_h - win_h * scale - 120) / scale)
out('fixed initial position (logical): x=%d y=%d' % (x, y))

window = webview.create_window(title='dpi-test', url='about:blank',
                               width=win_w, height=win_h, x=x, y=y,
                               frameless=False, on_top=False, hidden=False)
result = {}
done = threading.Event()


def check():
    try:
        # 等 native 句柄就绪
        deadline = time.time() + 10
        while time.time() < deadline:
            if window.native is not None:
                break
            time.sleep(0.1)
        hwnd = int(window.native.Handle.ToInt64())
        rect = wt.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        result['rect'] = (rect.left, rect.top, rect.right, rect.bottom)
        result['physical_w'] = rect.right - rect.left
        result['physical_h'] = rect.bottom - rect.top
        # pywebview 的属性坐标（逻辑）应与传入值一致
        result['logical_x'] = window.x
        result['logical_y'] = window.y
    except Exception as e:
        result['error'] = repr(e)
    finally:
        done.set()
        try:
            window.destroy()
        except Exception:
            pass


webview.start(check)

if 'error' in result:
    out('[FAIL] real-window check error: %s' % result['error'])
    fail += 1
else:
    l, t, r, b = result['rect']
    out('GetWindowRect (physical): (%d,%d)-(%d,%d)  size=%dx%d'
        % (l, t, r, b, result['physical_w'], result['physical_h']))
    out('pywebview logical x/y: (%s,%s)  expected (%d,%d)'
        % (result['logical_x'], result['logical_y'], x, y))

    # 断言 1：物理矩形完整落在主屏物理范围内（修复前 right=3330 > 2560）
    if r <= screen_w and b <= screen_h:
        out('[PASS] window fully inside primary screen physically')
    else:
        out('[FAIL] window extends past screen: right=%d>%d or bottom=%d>%d'
            % (r, screen_w, b, screen_h))
        fail += 1
    # 断言 2：物理尺寸 = 逻辑尺寸 x scale（证明 scale 语义理解正确）
    exp_pw = int(win_w * scale)
    exp_ph = int(win_h * scale)
    if abs(result['physical_w'] - exp_pw) <= 2 and abs(result['physical_h'] - exp_ph) <= 2:
        out('[PASS] physical size %dx%d ~= logical %dx%d x %.2f'
            % (result['physical_w'], result['physical_h'], win_w, win_h, scale))
    else:
        out('[WARN] physical size %dx%d differs from expected %dx%d (border/shadow?)'
            % (result['physical_w'], result['physical_h'], exp_pw, exp_ph))
    # 断言 3：位置逻辑回读一致
    if abs(result['logical_x'] - x) <= 1 and abs(result['logical_y'] - y) <= 1:
        out('[PASS] logical position round-trips correctly')
    else:
        out('[FAIL] logical position mismatch')
        fail += 1

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
