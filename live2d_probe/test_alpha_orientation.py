# -*- coding: utf-8 -*-
"""test_alpha_orientation.py — alpha 采样行序约定回归（2026-10-11）

背景（0.3.26 修的 bug）：
    ui.html 有两条读 alpha 的路径 ——
      'gl'  gl.readPixels  第 0 行 = 画布**最下边**（OpenGL 约定）
      '2d'  getImageData   第 0 行 = 图片**最上边**（栅格约定）
    而下游 scanAlpha() / alphaAt() 只认一种约定：行 0 = 底边
    （alphaAt 的注释："WebGL 的 y 轴朝上，DOM 的朝下，这里要翻"）。
    两条路径以前都没有做行序归一，于是**只有 '2d' 回退路径**会把轮廓矩形
    和点击命中整体上下镜像 —— 而 '2d' 恰恰是 'gl' 读数不可信时才会走的
    那条（WebView2 windowed hosting 下就是如此），所以在本机从未暴露。

本测试从**真实的 ui.html** 里抽出 rowsBottomUp() 源码，交给 node 执行：
  1. 行为：自顶向下的像素行序被翻成自底向上
  2. 接线：readAlphaGrid 的 '2d' 分支确实把 buf 过了这个函数
  3. 约定：两个消费方（scanAlpha / alphaAt）用的都是"行 0 = 底边"的算式
三条必须同时成立，镜像 bug 才不可能回来。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI = HERE.parent / 'desktop_pet' / 'ui.html'
OUT = HERE / 'alpha_orientation_result.txt'

NODE_CANDIDATES = [
    r'C:\Users\Mr.hancard\.workbuddy\binaries\node\versions\22.22.2-6\node.exe',
    r'C:\Users\Mr.hancard\.workbuddy\binaries\node\versions\22.22.2-4\node.exe',
    r'S:\nodejs\node.exe',
]

_lines = []
fail = 0


def out(s=''):
    _lines.append(str(s))


def find_node():
    for c in NODE_CANDIDATES:
        if os.path.isfile(c):
            return c
    return shutil.which('node')


src = io.open(str(UI), 'r', encoding='utf-8').read()

# ---------- 1. 抽出真实函数源码 ----------
m = re.search(r'(function rowsBottomUp\(data, w, h\) \{.*?\n    \})', src, re.S)
if not m:
    out('[FAIL] 从 ui.html 里抽不到 rowsBottomUp() —— 函数被改名/删除了？')
    fail += 1
    func_src = None
else:
    func_src = m.group(1)
    out('[PASS] 抽到 rowsBottomUp() 源码 (%d 字符)' % len(func_src))

# ---------- 2. 交给 node 跑行为 ----------
node = find_node()
if func_src and node:
    harness = """
%s

function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

var out = [];
// 2a. 4 行 1 像素宽：第 0 行（最上边）不透明，其余透明
var top = new Uint8ClampedArray([255,0,0,255,  0,0,0,0,  0,0,0,0,  0,0,0,0]);
var r = rowsBottomUp(top, 1, 4);
var alphaOf = function (buf, row, w) { return buf[row * w * 4 + 3]; };
out.push(['opaque-row-moves-to-bottom',
          alphaOf(r, 3, 1) === 255 && alphaOf(r, 0, 1) === 0,
          'in=[' + [0,1,2,3].map(function(i){return alphaOf(top,i,1);}).join(',') +
          '] out=[' + [0,1,2,3].map(function(i){return alphaOf(r,i,1);}).join(',') + ']']);

// 2b. 行序必须严格倒过来（多行、每行可区分）
var rows = [];
for (var i = 0; i < 5; i++) { rows.push(i + 1, i + 1, i + 1, 255); }
var td = new Uint8ClampedArray(rows);
var got = rowsBottomUp(td, 1, 5);
var gotRows = [];
for (var y = 0; y < 5; y++) { gotRows.push(got[y * 4]); }
out.push(['order-reversed', eq(gotRows, [5,4,3,2,1]),
          'out=[' + gotRows.join(',') + ']']);

// 2c. 尺寸不变（不能丢行/多行）
var wide = new Uint8ClampedArray(3 * 2 * 4);
var rw = rowsBottomUp(wide, 3, 2);
out.push(['length-preserved', rw.length === wide.length, 'len=' + rw.length]);

for (var k = 0; k < out.length; k++) {
  console.log((out[k][1] ? 'PASS' : 'FAIL') + ' node:' + out[k][0] + ' ' + out[k][2]);
}
""" % func_src

    tmp = Path(tempfile.mkdtemp(prefix='airi_alpha_'))
    js = tmp / '_orient_test.js'
    io.open(str(js), 'w', encoding='utf-8').write(harness)
    p = subprocess.run([node, '--check', str(js)], capture_output=True, text=True)
    if p.returncode != 0:
        out('[FAIL] node --check 语法错: %s' % p.stderr.strip()[:300])
        fail += 1
    else:
        p = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=60)
        for ln in p.stdout.splitlines():
            out('[%s]' % ln if ln.startswith('FAIL') else '[%s]' % ln)
            if ln.startswith('FAIL'):
                fail += 1
elif not node:
    out('[FAIL] 找不到 node，无法做行为验证（候选路径都不存在，PATH 里也没有）')
    fail += 1

# ---------- 3. 接线 + 消费者约定（静态） ----------
if re.search(r"rowsBottomUp\(img\.data, w, h\)", src):
    out('[PASS] wiring:readAlphaGrid-2d-normalized')
else:
    out('[FAIL] wiring:readAlphaGrid 的 2d 分支没有把 buf 过 rowsBottomUp —— 镜像 bug 会复发')
    fail += 1

# scanAlpha: 行 y 映射到 CSS 的 (h-1-y)*sy —— 自底向上
if re.search(r'top = Math\.round\(rect\.top \+ \(h - 1 - y\) \* sy\)', src):
    out('[PASS] consumer:scanAlpha-bottom-up')
else:
    out('[FAIL] consumer:scanAlpha 的行映射不再是 (h-1-y) —— 与行序约定脱钩了')
    fail += 1

# alphaAt: clientY 用 (1 - ry) 翻 y —— 自底向上
if re.search(r'\(1 - \(clientY - rect\.top\) / rect\.height\) \* grid\.h', src):
    out('[PASS] consumer:alphaAt-bottom-up')
else:
    out('[FAIL] consumer:alphaAt 的 y 映射不再是 (1 - ry) —— 与行序约定脱钩了')
    fail += 1

# gl 路径不该被翻（readPixels 本来就是自底向上）
if re.search(r"return \{ buf: rowsBottomUp\(buf", src):
    out('[FAIL] gl 路径也被翻了 —— readPixels 本来就是自底向上，翻了就反了')
    fail += 1
else:
    out('[PASS] gl-path-untouched')

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(str(OUT), 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
