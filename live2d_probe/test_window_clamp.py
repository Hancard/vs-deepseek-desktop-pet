"""test_window_clamp.py — clamp_to_virtual_screen 边界用例测试（离线可跑）。

用法: python test_window_clamp.py  （结果写 desktop_pet/_clamp_test.txt —— 
      注意是 desktop_pet/ 不是本目录，ROOT 指向被 import 的 common.py 所在处）
覆盖: 屏内不动 / 四边出界 / 角落极限 / 双屏负坐标虚拟桌面 / 真机度量。
"""
import io
import os
import sys

ROOT = r"S:\My event\projects\vscode-anime-assistent\desktop_pet"
sys.path.insert(0, ROOT)
lines = []

import common

# 虚拟桌面: 单屏 (0,0)-(1920,1080)，窗口 284x441，margin 60
VS = (0, 0, 1920, 1080)
W, H, M = 284, 441, 60

cases = [
    # (x, y, 期望 x, 期望 y, 说明)
    (960, 540, 960, 540, '屏内正常位置不动'),
    (100, 100, 100, 100, '屏内左上不动'),
    (-500, 100, -224, 100, '左边几乎完全拖出 -> 留60px'),
    (100, -600, 100, -381, '顶上完全拖出 -> 留60px'),
    (2100, 540, 1860, 540, '右边完全拖出 -> 留60px'),
    (100, 1300, 100, 1020, '底下完全拖出 -> 留60px'),
    (-9999, -9999, -224, -381, '左上角外极限'),
    (99999, 99999, 1860, 1020, '右下角外极限'),
]
ok = True
for x, y, ex, ey, desc in cases:
    rx, ry = common.clamp_to_virtual_screen(x, y, W, H, VS, margin=M)
    good = (rx == ex and ry == ey)
    ok = ok and good
    lines.append('%-24s (%d,%d) -> (%d,%d) 期望 (%d,%d) %s'
                 % (desc, x, y, rx, ry, ex, ey, 'OK' if good else 'FAIL'))

# 多显示器负坐标虚拟桌面
VS2 = (-1920, 0, 1920, 1080)
rx, ry = common.clamp_to_virtual_screen(-2500, 100, W, H, VS2, margin=M)
good = (rx == -2144 and ry == 100)
ok = ok and good
lines.append('双屏负坐标左出界      -> (%d,%d) 期望 (-2184,100) %s' % (rx, ry, 'OK' if good else 'FAIL'))

# get_virtual_screen 真机调用（Windows 上应返回非 None）
vs = common.get_virtual_screen()
lines.append('get_virtual_screen() -> %r %s' % (vs, 'OK' if vs else 'FAIL(None)'))
ok = ok and (vs is not None)

lines.append('OVERALL %s' % ('PASS' if ok else 'FAIL'))
with io.open(os.path.join(ROOT, '_clamp_test.txt'), 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')
print('done')
