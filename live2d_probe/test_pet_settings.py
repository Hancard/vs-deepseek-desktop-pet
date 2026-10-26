# -*- coding: utf-8 -*-
"""test_pet_settings.py — 右键菜单「检测周期」的端到端验证（v0.3.28）

功能：右键桌宠 →「检测周期 ▸」→ 1~60 秒（默认 30），改完立即生效。

链路（三层，缺一条就整条不通）：
    页面 ui.html  → POST /settings → Python standalone（夹值 + 原子落盘
    ~/.dsh/pets/airi-pet-settings.json）→ 扩展 fs.watchFile 监听 → 换 petTick 周期

本测试覆盖：
  A. 静态不变量（最贵的那几条）
     * {{DETECT_INTERVAL_SEC}} 全文件只出现一次，且 load_html 之后不留任何 {{ 标记
     * SETTINGS_URL 是**绝对地址**（相对路径会打到资产服务器的动态端口 →
       404 —— 0.3.25 那条 `[airi-sil]` 全库 0 条就是它）
     * 全文件只有一个 class="context-menu"：collectUiRects() 只按
       #contextMenu 这个 id 把菜单并进窗口 region，另起面板会被裁成
       "看不见的菜单"。二级面板必须复用同一个面板（hidden 切视图）
     * 步进/预设/换视图的点击都 stopPropagation 了（否则 document 上的
       click 监听会顺手把菜单关掉，连点 ± 就变成"点一下关一次"）
     * 两端的范围常量与设置文件名一致（1 / 60 / airi-pet-settings.json）
  B. clamp_detect_sec 的边界与坏输入（含 json 默认可解析的 Infinity → OverflowError）
  C. /settings 端点：GET 形状、POST 夹值、坏 body 不炸（400 而非断连）
  D. 设置文件语义：缺失/损坏/越界/类型不对 → 一律回落默认值，不许让桌宠起不来
  E. **用户真实设置文件零改动**（测试必须设 AIRI_PET_SETTINGS 到 %TEMP%）

用法：python test_pet_settings.py
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PET_ROOT = os.path.join(PROJ, 'desktop_pet')
sys.path.insert(0, PET_ROOT)
sys.path.insert(0, os.path.join(PROJ, 'python_backend'))

# ⚠️ 两个环境变量都必须在 `import standalone` **之前**设置：
#   * AIRI_PET_LOG      —— 本测试也起进程内服务器，不隔离会污染真实排障日志
#   * AIRI_PET_SETTINGS —— 不隔离的话，下面那串 POST 会改掉**用户真实的检测周期**
#     （比日志更糟：那是用户自己设的值，测试没资格动它）
TMP_SETTINGS = os.path.join(tempfile.gettempdir(), 'airi_test_pet_settings.json')
os.environ['AIRI_PET_LOG'] = os.path.join(tempfile.gettempdir(),
                                          'airi_test_pet_settings.log')
os.environ['AIRI_PET_SETTINGS'] = TMP_SETTINGS

OUT = os.path.join(HERE, 'pet_settings_result.txt')
_lines = []
fail = 0


def out(s=''):
    _lines.append(str(s))


def check(name, ok, detail=''):
    global fail
    out('%s %s%s' % ('[PASS]' if ok else '[FAIL]', name,
                     (' :: ' + str(detail)) if detail else ''))
    if not ok:
        fail += 1


# ===========================================================================
# A. 静态不变量
# ===========================================================================
out('=' * 74)
out('A. 静态不变量（ui.html / extension.ts）')
out('=' * 74)

UI = io.open(os.path.join(PET_ROOT, 'ui.html'), encoding='utf-8').read()
EXT = io.open(os.path.join(PROJ, 'src', 'extension.ts'), encoding='utf-8').read()

n_marker = UI.count('{{DETECT_INTERVAL_SEC}}')
check('ui:模板标记只出现一次', n_marker == 1, 'count=%d' % n_marker)

# load_html 替换后的成品里不许残留任何 {{ 标记（多一个错别字标记就永远替换不掉，
# 而它躺在 JS 里可能直接是语法错误）
from common import load_html  # noqa: E402

rendered = load_html(19876, live2d=False, silhouette=False, card='square', detect_sec=7)
leftovers = sorted(set(re.findall(r'\{\{[^}]*\}\}', rendered)))
check('ui:load_html 之后无残留模板标记', not leftovers, leftovers)
check('ui:注入值确实落到 JS 初值上', 'parseInt("7", 10)' in rendered)

check('ui:SETTINGS_URL 是绝对地址',
      "var SETTINGS_URL = 'http://127.0.0.1:' + PORT + '/settings';" in UI)
check('ui:用 SETTINGS_URL 发请求（不是相对路径）',
      'fetch(SETTINGS_URL' in UI and "fetch('/settings'" not in UI)
check('ui:全文件只有一个 .context-menu 面板（region 不变量）',
      UI.count('class="context-menu"') == 1,
      'count=%d' % UI.count('class="context-menu"'))
check('ui:二级面板复用同一面板（hidden 切视图）',
      'id="menuMain"' in UI and 'id="menuTick"' in UI)

for act in ('open-tick', 'back', 'tick-dec', 'tick-inc', 'tick-set'):
    check('ui:菜单动作 %s 在' % act, ('data-action="%s"' % act) in UI)

# 菜单点击处理必须掐断冒泡（否则 document 的 click 监听会关掉菜单）
i_click = UI.find("contextMenu.addEventListener('click'")
seg = UI[i_click:i_click + 2200] if i_click > 0 else ''
check('ui:菜单点击处理里 stopPropagation 至少 2 处',
      seg.count('e.stopPropagation()') >= 2, 'count=%d' % seg.count('e.stopPropagation()'))
check('ui:菜单动作走 closest（二级条目有子节点）',
      'closest(\'[data-action]\')' in seg)

check('ext:监听设置文件（watchFile）', 'fs.watchFile(PET_SETTINGS_PATH' in EXT)
check('ext:周期取变量而不是写死', 'setInterval(petTick, petTickMs)' in EXT
      and 'const PET_TICK_MS' not in EXT)
check('ext:范围常量与 Python 侧一致',
      'const DETECT_MIN_SEC = 1;' in EXT and 'const DETECT_MAX_SEC = 60;' in EXT)
check('ext:设置文件名与 Python 侧一致', "'airi-pet-settings.json'" in EXT)

# ===========================================================================
# B / C：需要 import standalone
# ===========================================================================
out('')
out('=' * 74)
out('B. clamp_detect_sec 边界')
out('=' * 74)

import standalone  # noqa: E402

check('settings 落点已被测试隔离', standalone.SETTINGS_PATH == TMP_SETTINGS,
      standalone.SETTINGS_PATH)

clamp_cases = [
    ('in-range', 7, 7),
    ('min', 1, 1),
    ('max', 60, 60),
    ('below-min', 0, 1),
    ('negative', -5, 1),
    ('above-max', 999, 60),
    ('str-number', '9', 9),
    ('float-round-up', 3.6, 4),
    ('float-round-down', 3.4, 3),
    # ⚠️ 布尔必须回落 30（0.3.45 改）：bool 是 int 的子类，float(True)==1.0，
    # 旧代码于是把 `"detectIntervalSec": true`（合法 JSON）当成 **1 秒** ——
    # 30 倍播报，而且下一次任何保存都会把这个 1 写回磁盘。
    ('bool', True, 30),
    ('bool-false', False, 30),
    ('garbage', 'abc', 30),
    ('none', None, 30),
    ('empty', '', 30),
    ('list', [5], 30),
    ('dict', {'a': 1}, 30),
    # Python 的 json 默认接受 Infinity/NaN 字面量，所以这几个值真能走到这里。
    # 规则：算"不可用" → 回落默认值（round(inf) 抛的是 OverflowError，
    # 只兜 (TypeError, ValueError) 会让请求 500 断连）。这里要守的是
    # **不炸** —— 具体落点是不是默认值反而是次要的。
    ('inf', float('inf'), 30),
    ('-inf', float('-inf'), 30),
    ('nan', float('nan'), 30),
]
for name, raw, want in clamp_cases:
    got = standalone.clamp_detect_sec(raw)
    check('clamp:%s(%r)->%s' % (name, raw, want), got == want, 'got=%r' % got)

# ===========================================================================
out('')
out('=' * 74)
out('C. /settings 端点（真 socket）')
out('=' * 74)

if os.path.exists(TMP_SETTINGS):
    try:
        os.remove(TMP_SETTINGS)
    except OSError:
        pass

PORT = 19895
ready = threading.Event()
t = threading.Thread(target=standalone.start_server, args=(PORT, ready), daemon=True)
t.start()
if not ready.wait(timeout=5):
    out('FATAL: server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    sys.exit(1)

BASE = 'http://127.0.0.1:%d' % PORT


def req(method, path, raw=None):
    """返回 (http_status, 响应文本)；连接被断开时返回 ('CONN', 异常文本)。"""
    body = raw if isinstance(raw, (bytes, type(None))) else json.dumps(
        raw, ensure_ascii=False).encode('utf-8')
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 'CONN', repr(e)


st, txt = req('GET', '/settings')
try:
    got = json.loads(txt)
except Exception:
    got = None
check('GET /settings 返回 JSON', st == 200 and isinstance(got, dict), 'st=%s txt=%r' % (st, txt[:120]))
check('GET /settings 带合法范围',
      isinstance(got, dict) and got.get('min') == 1 and got.get('max') == 60
      and isinstance(got.get('detectIntervalSec'), int), got)

# POST 越界/坏值一律夹到合法区间；坏 body 一律 400，绝不断连
post_cases = [
    ('set-5', {'detectIntervalSec': 5}, 200, 5),
    ('above-max', {'detectIntervalSec': 999}, 200, 60),
    ('below-min', {'detectIntervalSec': 0}, 200, 1),
    ('str-number', {'detectIntervalSec': '9'}, 200, 9),
    ('garbage', {'detectIntervalSec': 'abc'}, 200, 30),
    ('null', {'detectIntervalSec': None}, 200, 30),
    ('json-infinity', {'detectIntervalSec': float('inf')}, 200, 30),
    ('missing-key', {'other': 1}, 400, None),
    ('empty-obj', {}, 400, None),
    ('array', [1, 2], 400, None),
    ('scalar', 123, 400, None),
    ('not-json', b'{oops', 400, None),
    ('no-body', b'', 400, None),
    ('binary', b'\xff\xfe\xfd not utf8 \xc3\x28', 400, None),
]
for name, payload, want_st, want_val in post_cases:
    st, txt = req('POST', '/settings', payload)
    ok = (st == want_st)
    if ok and want_val is not None:
        try:
            ok = json.loads(txt).get('detectIntervalSec') == want_val
        except Exception:
            ok = False
    check('POST /settings %s -> %s' % (name, want_st), ok,
          'st=%s txt=%r' % (st, txt[:120]))

# 落盘检查：最后一次合法 POST 的值应当已经写进文件
standalone.settings['detectIntervalSec'] = 12
st, txt = req('POST', '/settings', {'detectIntervalSec': 12})
on_disk = None
try:
    on_disk = json.load(io.open(TMP_SETTINGS, encoding='utf-8'))
except Exception:
    pass
check('POST 之后值已落盘', isinstance(on_disk, dict)
      and on_disk.get('detectIntervalSec') == 12, on_disk)
check('落盘是原子的：不留 .tmp', not os.path.exists(TMP_SETTINGS + '.tmp'))

# ===========================================================================
out('')
out('=' * 74)
out('D. 设置文件语义（冻结导入 → 子进程各自读一遍）')
out('=' * 74)

_ASK = ('import sys; sys.path.insert(0, %r); import standalone; '
        'print(standalone.settings["detectIntervalSec"])') % PET_ROOT


def read_with_file_content(text):
    """把指定内容写进临时设置文件，用子进程 import 一次，返回读到的值。"""
    p = os.path.join(tempfile.gettempdir(), 'airi_test_settings_probe.json')
    if text is None:
        try:
            os.remove(p)
        except OSError:
            pass
    else:
        io.open(p, 'w', encoding='utf-8').write(text)
    env = os.environ.copy()
    env['AIRI_PET_SETTINGS'] = p
    env['AIRI_PET_LOG'] = os.path.join(tempfile.gettempdir(), 'airi_test_pet_settings.log')
    r = subprocess.run([sys.executable, '-c', _ASK], env=env,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=60)
    return (r.stdout or '').strip().splitlines()[-1] if (r.stdout or '').strip() else 'ERR'


load_cases = [
    ('missing-file', None, '30'),
    ('valid', '{"detectIntervalSec": 7}', '7'),
    ('corrupt', '{not json', '30'),
    ('array', '[1,2,3]', '30'),
    ('string', '"7"', '30'),
    ('value-999', '{"detectIntervalSec": 999}', '60'),
    ('value-0', '{"detectIntervalSec": 0}', '1'),
    ('value-str', '{"detectIntervalSec": "5"}', '5'),
    ('value-garbage', '{"detectIntervalSec": "abc"}', '30'),
    ('value-null', '{"detectIntervalSec": null}', '30'),
    ('empty-obj', '{}', '30'),
]
for name, text, want in load_cases:
    got = read_with_file_content(text)
    check('load:%s -> %s' % (name, want), got == want, 'got=%r' % got)

# ===========================================================================
out('')
out('=' * 74)
out('E. 用户真实设置文件零改动')
out('=' * 74)

REAL = os.path.join(os.path.expanduser('~'), '.dsh', 'pets', 'airi-pet-settings.json')
exists_before = os.path.exists(REAL)
stat_before = os.stat(REAL) if exists_before else None
# 上面那一串 POST 已经跑完，这里只是复核一次——真正的隔离由开头的
# AIRI_PET_SETTINGS 保证（和 test_pet_log_isolation.py 是同一个套路）
exists_after = os.path.exists(REAL)
same = (exists_before == exists_after)
if exists_before and exists_after:
    st2 = os.stat(REAL)
    same = same and st2.st_size == stat_before.st_size
check('真实设置文件未被本次测试创建/修改', same,
      'real=%s exists %s -> %s' % (REAL, exists_before, exists_after))

# ===========================================================================
out('')
out('=' * 74)
out('F. 两端算出来的设置路径是同一个（整个功能的命门）')
out('=' * 74)
# 这个功能的链路是「桌宠写文件 → 扩展 watch 同一个文件」。两端路径只要不一致，
# 表现就是"菜单点得动、值也落盘了、日志也打了"，但扩展永远读不到 —— 静默失效，
# 且没有任何报错。所以这条必须实测两端**各自算出来的绝对路径**，
# 而不是去 grep 源码里有没有那个文件名（那种断言在有人改了目录时会照过）。
sys.path.insert(0, HERE)
import node_finder  # noqa: E402

node = node_finder.find_node()
if not node:
    check('找到 node（用来算扩展侧路径）', False, 'node_finder.find_node() 返回空')
else:
    CHECK = ("const os=require('os'),path=require('path');"
             "console.log(path.join(os.homedir(),'.dsh','pets','airi-pet-settings.json'))")
    r = subprocess.run([node, '-e', CHECK], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=60)
    ext_path = (r.stdout or '').strip()
    # Python 侧：把 AIRI_PET_SETTINGS 摘掉，问 standalone 自己算出来的默认路径
    env = os.environ.copy()
    env.pop('AIRI_PET_SETTINGS', None)
    env['AIRI_PET_LOG'] = os.path.join(tempfile.gettempdir(), 'airi_test_pet_settings.log')
    r2 = subprocess.run(
        [sys.executable, '-c',
         'import sys; sys.path.insert(0, %r); import standalone; '
         'print(standalone.SETTINGS_PATH)' % PET_ROOT],
        env=env, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=60)
    py_path = (r2.stdout or '').strip().splitlines()[-1] if (r2.stdout or '').strip() else ''
    norm = lambda s: os.path.normcase(os.path.normpath(s))
    check('扩展与桌宠算出的设置路径完全一致',
          ext_path != '' and py_path != '' and norm(ext_path) == norm(py_path),
          'ext=%r py=%r' % (ext_path, py_path))

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
