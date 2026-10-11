# -*- coding: utf-8 -*-
"""ui.html 全部 <script> 块的语法检查（node --check），绕开 PowerShell 管道噪声。

为什么必须从**探针文件**提取：原始 ui.html 的 {{PORT}} / {{LIVE2D_ENABLED}} 等
占位符在替换之前不是合法 JS，直接抽出来 --check 必然整块报错 —— 那是假阳性，
不是真 bug。

用法：python check_ui_js.py      （结果写 live2d_probe/_jscheck_out.txt）
返回码：0 全部通过 / 1 有语法错 / 2 环境或前置条件不满足

0.3.26 修了两处（都是"工具自己坏了、但看起来像测试失败"的类型）：
  1. **探针缺失时自动生成**。以前上来就 io.open(desktop_pet/_probe_a.html)，
     文件不在直接 FileNotFoundError。而 _probe_a.html 是 gen_ui_probe.py 的
     产物、被 gitignore，新克隆的仓库里根本没有 —— 于是这个工具对任何人
     都是 rc=1，而它本该是"页面什么都不显示"时的第一诊断手段。
  2. **node 路径不再写死**（原来硬编码 S:\\nodejs\\node.exe）→ 改用
     node_finder.py 的候选+扫描+PATH 查找，与 jscheck.py 同一套，
     托管 node 换代不会再把工具跑废。

脚本块与生成的探针都不进仓库（live2d_probe/.gitignore 的 `_*`、
根 .gitignore 的 `desktop_pet/_probe_*.html`）。
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                       # node_finder.py 就在本目录
from node_finder import find_node  # noqa: E402

PET_ROOT = HERE.parent
PROBE_HTML = PET_ROOT / 'desktop_pet' / '_probe_a.html'
GEN_PROBE = HERE / 'gen_ui_probe.py'
OUT = HERE / '_jscheck_out.txt'

lines = []


def finish(code):
    OUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))
    sys.exit(code)


NODE = find_node()
lines.append('node = %s' % (NODE or 'NOT FOUND'))
if not NODE:
    lines.append('失败：找不到 node —— 候选路径都不存在，PATH 里也没有 node.exe。'
                 '装一个 node，或把路径加到 live2d_probe/node_finder.py 的候选列表。')
    finish(2)

# ---- 探针：不在就自己生成，别再让人去手动跑第二个脚本 ----
if not PROBE_HTML.is_file():
    lines.append('探针不存在（%s），自动调用 gen_ui_probe.py 生成' % PROBE_HTML.name)
    r = subprocess.run([sys.executable, str(GEN_PROBE)], capture_output=True,
                       text=True, cwd=str(HERE))
    lines.append('  gen_ui_probe.py exit=%d' % r.returncode)
    if r.stdout.strip():
        lines.append('  stdout: ' + r.stdout.strip()[:500])
    if r.stderr.strip():
        lines.append('  stderr: ' + r.stderr.strip()[:1000])
    if not PROBE_HTML.is_file():
        lines.append('失败：gen_ui_probe.py 跑完仍然没有 %s。' % PROBE_HTML)
        lines.append('  手动执行 `python gen_ui_probe.py` 看完整报错；')
        lines.append('  常见原因是 desktop_pet/ui.html 里 </body> 被改掉了。')
        finish(2)

html = PROBE_HTML.read_text(encoding='utf-8')

left = sorted(set(re.findall(r'\{\{[A-Z_]+\}\}', html)))
lines.append('未替换的占位符: %s' % (left or '无'))
if left:
    lines.append('失败：探针里还有占位符，说明 gen_ui_probe.py 的替换规则'
                 '和 common.load_html() 脱节了 —— 此时 --check 的报错是假阳性。')
    finish(2)

blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
if not blocks:
    lines.append('失败：探针里一段 <script> 都没抽到，检查 ui.html 是否被整体替换。')
    finish(2)

tmp = Path(tempfile.mkdtemp(prefix='airi_jscheck_'))
bad = 0
for i, src in enumerate(blocks):
    if not src.strip():
        lines.append('script#%d 空块，跳过' % i)
        continue
    path = tmp / ('_script_%d.js' % i)
    path.write_text(src, encoding='utf-8')
    r = subprocess.run([NODE, '--check', str(path)], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=60, text=True)
    err = (r.stderr or '').strip()
    ok = (r.returncode == 0)
    bad += 0 if ok else 1
    lines.append('script#%d (%d 字符) exit=%d %s'
                 % (i, len(src), r.returncode, 'OK' if ok else 'SYNTAX ERROR'))
    if err:
        lines.append(err[:1200])

lines.append('语法检查 ' + ('全部通过' if bad == 0 else '失败 %d 段' % bad))
finish(0 if bad == 0 else 1)
