# -*- coding: utf-8 -*-
"""test_pet_startup_guard.py — 启动链路与窗口桥接的兜底（0.3.38）。

这一批的共同点：**一抛异常，桌宠就整个起不来或整个不能用，而用户侧零提示**
（扩展以 stdio:'ignore' 拉起，traceback 走 stderr 被直接丢掉）。

  A. find_character_image：必须返回**绝对路径**（下游 as_uri() 只吃绝对），
     且要认 README 里承诺的 .jpg
  B. load_html：相对路径立绘 / 读不到 ui.html 都不能抛
  C. get_position：桥方法里唯一没兜底的，且 `x or 0` 把坐标 0 当假值
  D. region 合并：CombineRgn 返回值不能丢（丢了会把空 region 设上去），
     rad 要钳制，异常白名单要覆盖 OverflowError / ArgumentError

用法：Python312 跑，结果写 live2d_probe/startup_guard_result.txt
"""
import io
import os
import re
import sys
import tempfile

PROBE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PROBE)
PET = os.path.join(ROOT, 'desktop_pet')
BACKEND = os.path.join(ROOT, 'python_backend')

TMP = tempfile.mkdtemp(prefix='airi_startup_guard_')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')

sys.path.insert(0, PET)
sys.path.insert(0, BACKEND)

import common  # noqa: E402

OUT = os.path.join(PROBE, 'startup_guard_result.txt')
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


SRC = io.open(os.path.join(PET, 'common.py'), encoding='utf-8').read()
UI = io.open(os.path.join(PET, 'ui.html'), encoding='utf-8').read()


def as_uri_exc(rel):
    """Path(相对路径).as_uri() 会抛什么？不抛返回 None。"""
    try:
        from pathlib import Path
        Path(rel).as_uri()
        return None
    except Exception as exc:
        return type(exc).__name__


def int_of_inf():
    """int(round(float('inf'))) 会抛什么？"""
    try:
        int(round(float('inf')))
        return None
    except Exception as exc:
        return type(exc).__name__

# ============================================ A. 立绘路径
out('=== A. find_character_image：绝对路径 + README 承诺的格式 ===')

_saved_env = os.environ.get('AIRI_CHARACTER_IMAGE')
_img_dir = os.path.join(TMP, 'img')
os.makedirs(_img_dir, exist_ok=True)
for nm in ('character.png', 'character.jpg'):
    io.open(os.path.join(_img_dir, nm), 'wb').write(b'\x89PNG\r\n\x1a\n')

# 相对路径：README 就是这么教用户写的（assets/README.md）
_cwd = os.getcwd()
try:
    os.chdir(_img_dir)
    for nm, label in (('character.png', 'PNG'), ('character.jpg', 'JPG')):
        os.environ['AIRI_CHARACTER_IMAGE'] = nm
        try:
            got = common.find_character_image()
            raised = None
        except Exception as exc:
            got, raised = None, '%s: %s' % (type(exc).__name__, exc)
        check('A [%s] 相对路径不抛异常' % label, raised is None, raised or 'OK')
        check('A [%s] 返回的是绝对路径（下游 as_uri 只吃绝对）' % label,
              bool(got) and os.path.isabs(got), 'got=%r' % (got,))
finally:
    os.chdir(_cwd)
    if _saved_env is None:
        os.environ.pop('AIRI_CHARACTER_IMAGE', None)
    else:
        os.environ['AIRI_CHARACTER_IMAGE'] = _saved_env

check('A9 反证：as_uri() 对相对路径确实会抛（否则这条修得像过度设计）',
      as_uri_exc('character.png') == 'ValueError',
      '实测抛 %s' % as_uri_exc('character.png'))
check('A10 候选名里包含 README 承诺的 .jpg / .jpeg',
      "'character.jpg'" in SRC and "'character.jpeg'" in SRC)


# ============================================ B. load_html
out('')
out('=== B. load_html：不能因为立绘/文件问题把整个进程带崩 ===')

# 相对路径立绘 → 降级（退回 CSS 角色），而不是 ValueError 穿透
try:
    h = common.load_html(19876, 'assets/character.png', live2d=False)
    raised, ok_html = None, bool(h)
except Exception as exc:
    h, raised, ok_html = None, '%s: %s' % (type(exc).__name__, exc), False
check('B1 相对路径立绘不抛异常', raised is None, raised or 'OK')
check('B2 相对路径立绘时页面照常产出（只是退回 CSS 角色）', ok_html,
      '%s 字节' % (len(h) if h else 0))
check('B3 该情况下 CHARACTER_IMAGE 被置空（页面不会拿到坏 file://）',
      bool(h) and 'var CHAR_IMG = "";' in h)

# 绝对但畸形的路径（内嵌 NUL）→ 也不能抛
try:
    h2 = common.load_html(19876, 'C:\\a\x00b.png', live2d=False)
    raised2, ok2 = None, bool(h2)
except Exception as exc:
    h2, raised2, ok2 = None, '%s: %s' % (type(exc).__name__, exc), False
check('B4 畸形立绘路径（内嵌 NUL）不抛异常', raised2 is None, raised2 or 'OK')
check('B5 畸形路径时页面照常产出', ok2)

# 正常路径仍然照旧工作
_abs = os.path.join(_img_dir, 'character.png')
h3 = common.load_html(19876, _abs, live2d=False)
check('B6 正常绝对路径立绘仍然注入 file:// URI',
      bool(h3) and 'file:///' in h3 and 'character.png' in h3)

# 静态契约
check('B7 load_html 读 ui.html 失败时返回 None 而不是抛',
      'except (OSError, UnicodeDecodeError)' in SRC and 'return None' in SRC)
check('B8 两个调用点都判空了',
      io.open(os.path.join(PET, 'standalone.py'), encoding='utf-8')
      .read().count('cannot load ui.html') == 1
      and 'cannot load ui.html' in io.open(
          os.path.join(PET, 'main.py'), encoding='utf-8').read())


# ============================================ C. get_position
out('')
out('=== C. get_position：唯一没兜底的桥方法 + `or 0` 把坐标 0 当假值 ===')


class _FakeWin(object):
    def __init__(self, x, y):
        self.x, self.y = x, y


class _BoomWin(object):
    @property
    def x(self):
        raise RuntimeError('window already destroyed')

    @property
    def y(self):
        raise RuntimeError('window already destroyed')


api = common.WindowAPI()

api._window = _FakeWin(0, 0)
got = api.get_position()
check('C1 窗口真的在 (0,0) 时如实返回 [0, 0]（不被当成假值丢掉）',
      got == [0, 0], 'got=%r（修复前 `x or 0` 也是 0，但 JS 侧会丢掉它）' % (got,))

api._window = _FakeWin(-8, 640)
check('C2 负坐标也能如实返回（拖拽钳制允许贴边）',
      api.get_position() == [-8, 640], 'got=%r' % (api.get_position(),))

api._window = _BoomWin()
try:
    r = api.get_position()
    raised = None
except Exception as exc:
    r, raised = None, '%s: %s' % (type(exc).__name__, exc)
check('C3 窗口属性访问抛异常时不穿出桥（JS 侧是空 catch）',
      raised is None, raised or 'got=%r' % (r,))

check('C4 源码里不再是 `x or 0` 这种假值兜底',
      'self._window.x or 0' not in SRC)

# JS 侧必须同步 —— 否则 Python 改了，前端照样把 0 丢掉
check('C5 JS 侧用 typeof/isFinite 判断而不是 `||`',
      "typeof pos[0] === 'number' && isFinite(pos[0])" in UI)
check('C6 JS 侧没有残留的 `pos[0]) || winStartX`',
      'pos[0]) || winStartX' not in UI and 'p[0] || winStartX' not in UI)


# ============================================ D. region 合并健壮性
out('')
out('=== D. region 合并：CombineRgn 返回值不能丢 + rad 要钳制 ===')

check('D1 _merge 返回的是布尔（成功/失败），不再是裸返回值',
      'return rc2 != 0' in SRC)
check('D2 调用点只在合并成功时才 kept += 1',
      'if _merge(h_piece):' in SRC)
check('D3 kept 计数跟着 _merge 的结果走（不再无条件 +1）',
      re.search(r'if _merge\(h_piece\):\s*\n\s*kept \+= 1', SRC) is not None)
check('D4 rad 派生的椭圆宽高被钳到不超过矩形自身',
      'min(r - l, int(round(rad * sx * 2)))' in SRC
      and 'min(b - t, int(round(rad * sy * 2)))' in SRC)
check('D5 异常白名单覆盖 OverflowError（float("inf") → int 会抛它）',
      'OverflowError' in SRC)
check('D6 异常白名单覆盖 ctypes.ArgumentError（数值超 c_long）',
      'ctypes.ArgumentError' in SRC)
check('D7 反证：int(round(inf)) 抛的确实是 OverflowError 而不是 ValueError',
      int_of_inf() == 'OverflowError', '实测抛 %s' % int_of_inf())


out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
sys.exit(0 if _fail == 0 else 1)
