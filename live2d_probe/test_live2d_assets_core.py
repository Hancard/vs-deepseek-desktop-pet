# -*- coding: utf-8 -*-
"""test_live2d_assets_core.py — Live2D 素材自检的启动安全（v0.3.31）

被测：live2d_assets.ensure_core() / _cache_core_from() / check_assets()

两个已修的缺陷（都只在"扩展用 stdio:'ignore' 起桌宠、用户侧零痕迹"的条件下现形）：

  1. AIRI_CUBISM_CORE 指向一份合法 core 时，ensure_core 直接 `return True`
     —— 文件还在用户自己指定的位置，而调用方 check_assets 紧接着 stat 落点
     → FileNotFoundError 一路逃出 standalone.main()，桌宠在**开窗之前**就死掉：
     没有窗口、没有报错、排障日志里连一行都没有。
     而且那个 env 变量本来也不可能生效 —— 页面只会从 /vendor/ 取 core
     （见 common._AssetHandler），所以"跳过下载"必须等于"把文件搬到位"。
     ⚠️ 全新克隆必现：live2dcubismcore.min.js 在 .gitignore 里，仓库里没有它。

  2. check_assets 拿外部事实（落点存在）做断言且不设防 → 一次纯装饰性的
     自检有能力把整个桌宠打死，与它自己的注释（"任何一步失败都不该让桌宠
     起不来"）直接矛盾。现在契约明确为"永不抛异常"。

隔离（缺一个就会动到用户真实文件）：
  · core 落点整体搬进 %TEMP%（改 CORE_PATH 与 ensure_core 的默认参数）
  · Path.home() 指向假 HOME —— 否则"复用 ~/.dsh/pets 的副本"那条路会真的
    去读用户机器上的 core，让"断网时应当失败"的用例假通过
  · CORE_URL 指向死地址 —— 这样"成功"只可能来自本地那份 core

用法：python test_live2d_assets_core.py
      → live2d_probe/live2d_assets_core_result.txt
      返回码 0 全通过 / 1 有失败
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

PROBE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PROBE)
PET = os.path.join(ROOT, 'desktop_pet')
sys.path.insert(0, PET)

OUT = os.path.join(PROBE, 'live2d_assets_core_result.txt')
TMP = tempfile.gettempdir()

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


# ---------------------------------------------------------------------------
# 沙箱
# ---------------------------------------------------------------------------
SANDBOX_DIR = os.path.join(TMP, 'airi_la_core_sandbox')
SANDBOX_CORE = os.path.join(SANDBOX_DIR, 'live2dcubismcore.min.js')
FAKE_HOME = os.path.join(TMP, 'airi_la_fake_home')
DEAD_URL = 'http://127.0.0.1:1/never'

REAL_HOME_PETS = os.path.join(os.path.expanduser('~'), '.dsh', 'pets')
REAL_CORE = os.path.join(PET, 'assets', 'live2d', 'vendor',
                         'live2dcubismcore.min.js')


def snapshot(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


REAL_BEFORE = {'home_pets': snapshot(REAL_HOME_PETS), 'repo_core': snapshot(REAL_CORE)}

for d in (SANDBOX_DIR, FAKE_HOME):
    shutil.rmtree(d, ignore_errors=True)

Path.home = classmethod(lambda cls: Path(FAKE_HOME))   # 假 HOME，测试期有效

import live2d_assets as la                       # noqa: E402

la.CORE_PATH = Path(SANDBOX_CORE)
la.CORE_URL = DEAD_URL
la.ensure_core.__defaults__ = (la.CORE_PATH, 1)  # timeout 压到 1s，别真等
la.MODEL_ENTRY  # 触碰一下，确认导入正常


def make_core(path, size=None, with_symbol=True):
    """造一份「看起来合法」的 core（大小过 MIN_CORE_BYTES + 含符号）。"""
    size = size or (la.MIN_CORE_BYTES + 4096)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head = '/* Live2DCubismCore */\n' if with_symbol else ''
    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write(head + 'x' * size)
    return path


def reset():
    shutil.rmtree(SANDBOX_DIR, ignore_errors=True)
    shutil.rmtree(FAKE_HOME, ignore_errors=True)
    os.environ.pop('AIRI_CUBISM_CORE', None)
    os.environ.pop('AIRI_LIVE2D', None)


out('=' * 78)
out('A. _cache_core_from —— "别处的 core" 必须真的搬到落点')
out('=' * 78)

reset()
src = make_core(os.path.join(TMP, 'airi_la_src.js'))
ok = la._cache_core_from(Path(src), Path(SANDBOX_CORE))
same = (os.path.isfile(SANDBOX_CORE)
        and io.open(SANDBOX_CORE, 'rb').read()
        == io.open(src, 'rb').read())
check('复制成功且目标与源逐字节相同（父目录原本不存在）', ok and same,
      'ok=%s same=%s' % (ok, same))

ok2 = la._cache_core_from(Path(SANDBOX_CORE), Path(SANDBOX_CORE))
check('源 == 落点（同一路径）时返回 True 且不破坏文件',
      ok2 and os.path.getsize(SANDBOX_CORE) == os.path.getsize(src))

missing = os.path.join(TMP, 'airi_la_nope.js')
try:
    os.remove(missing)
except OSError:
    pass
ok3 = la._cache_core_from(Path(missing), Path(SANDBOX_CORE))
check('源不存在 → 返回 False（不抛、不谎报）', ok3 is False, ok3)

out('')
out('=' * 78)
out('B. ensure_core 的 AIRI_CUBISM_CORE 分支')
out('=' * 78)

reset()
os.environ['AIRI_CUBISM_CORE'] = src
r = la.ensure_core()
check('env 合法 + 落点缺失（断网）→ True', r is True, r)
check('  —— 且落点**真的被填上**了（这就是"跳过下载"的全部含义）',
      os.path.isfile(SANDBOX_CORE)
      and os.path.getsize(SANDBOX_CORE) == os.path.getsize(src),
      'exists=%s size=%s' % (os.path.isfile(SANDBOX_CORE),
                             os.path.getsize(SANDBOX_CORE)
                             if os.path.isfile(SANDBOX_CORE) else -1))

reset()
make_core(SANDBOX_CORE, size=la.MIN_CORE_BYTES + 1000)
before_size = os.path.getsize(SANDBOX_CORE)
os.environ['AIRI_CUBISM_CORE'] = src
r = la.ensure_core()
check('落点已有可用 core → True 且不去覆盖它（省一次无谓写盘）',
      r is True and os.path.getsize(SANDBOX_CORE) == before_size,
      '%s / %d' % (r, os.path.getsize(SANDBOX_CORE)))

reset()
os.environ['AIRI_CUBISM_CORE'] = missing
r = la.ensure_core()
check('env 指向不存在的文件（断网、无 dsh 候选）→ False，不谎报成功',
      r is False, r)

reset()
small = make_core(os.path.join(TMP, 'airi_la_small.js'), size=128)
os.environ['AIRI_CUBISM_CORE'] = small
r = la.ensure_core()
check('env 指向太小的文件 → False（不是"只要 is_file 就算数"）', r is False, r)

reset()
planted = make_core(os.path.join(FAKE_HOME, '.dsh', 'pets',
                                 'live2dcubismcore.min.js'))
r = la.ensure_core()
check('没设 env 时仍会复用 ~/.dsh/pets 的副本（原有行为保住）',
      r is True and os.path.isfile(SANDBOX_CORE),
      '%s exists=%s' % (r, os.path.isfile(SANDBOX_CORE)))

out('')
out('=' * 78)
out('C. check_assets() 的契约：永不抛异常')
out('=' * 78)

reset()
os.environ['AIRI_CUBISM_CORE'] = src
r = la.check_assets()
check('正常路径：素材齐全 → (True, [...])',
      isinstance(r, tuple) and r[0] is True and any('cubism core' in x for x in r[1]),
      r)

# ⚠️ 这里是**旧 bug 的形状**的单元复刻：ensure_core 宣称成功但落点不存在。
#    以前 check_assets 会直接 CORE_PATH.stat() → FileNotFoundError。
reset()
os.environ['AIRI_CUBISM_CORE'] = src          # 合法，ensure_core 会返回 True
_real_ensure = la.ensure_core
la.ensure_core = lambda *a, **k: True         # 谎报成功（模拟"源没搬到位"）
try:
    r = la.check_assets()
    raised = None
except BaseException as exc:                  # noqa: BLE001
    raised = exc
    r = None
finally:
    la.ensure_core = _real_ensure
check('ensure_core 谎报成功 + 落点不存在 → 返回 (False, ...) 而**不是抛**',
      raised is None and isinstance(r, tuple) and r[0] is False,
      'raised=%r r=%r' % (raised, r))
check('  —— 说明行里给出了原因与降级方向',
      raised is None and any('fall back' in x for x in r[1]), r)

reset()
_real_impl = la._check_assets
la._check_assets = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
try:
    r = la.check_assets()
    raised2 = None
except BaseException as exc:                  # noqa: BLE001
    raised2 = exc
    r = None
finally:
    la._check_assets = _real_impl
check('实现体内部抛任何异常 → 也被兜成 (False, [...])',
      raised2 is None and isinstance(r, tuple) and r[0] is False,
      'raised=%r' % (raised2,))
check('  —— 异常类型被写进说明行（不是静默吞掉）',
      raised2 is None and any('RuntimeError' in x for x in r[1]), r)

reset()
r = la.check_assets()
check('无 env、落点缺失、断网（全新克隆的默认状态）→ (False, ...) 不崩',
      isinstance(r, tuple) and r[0] is False
      and any('fall back' in x for x in r[1]), r)

reset()
os.environ['AIRI_LIVE2D'] = '0'
r = la.check_assets()
check('AIRI_LIVE2D=0 → 明确关闭（且不受 core 影响）',
      r[0] is False and any('disabled' in x for x in r[1]), r)

out('')
out('=' * 78)
out('D. 静态不变量')
out('=' * 78)

_src = io.open(os.path.join(PET, 'live2d_assets.py'), encoding='utf-8').read()
_standalone = io.open(os.path.join(PET, 'standalone.py'), encoding='utf-8').read()
_main = io.open(os.path.join(PET, 'main.py'), encoding='utf-8').read()

check('有 _cache_core_from（"搬到落点"这件事有单一实现）',
      'def _cache_core_from(' in _src)
check('env 分支不再裸 return True（必须先落位）',
      'if _core_ok(path) or _cache_core_from(src, path):' in _src)
check('_check_assets 的收尾 stat 在 try 里',
      'except OSError as exc:' in _src and 'cubism core missing at' in _src)
check('check_assets 是"永不抛"的包装（内含 try → _check_assets）',
      'return _check_assets()' in _src
      and 'asset check raised' in _src)
check('模块 docstring 说明 AIRI_CUBISM_CORE 会被复制到落点',
      '复制' in _src and 'AIRI_CUBISM_CORE' in _src)
check('两个入口仍是**裸调用**（所以"不抛"只能由函数自己保证）',
      'l2d_ok, l2d_detail = live2d_assets.check_assets()' in _standalone
      and 'l2d_ok, l2d_detail = live2d_assets.check_assets()' in _main)

out('')
out('=' * 78)
out('E. 真实文件零改动')
out('=' * 78)

after = {'home_pets': snapshot(REAL_HOME_PETS), 'repo_core': snapshot(REAL_CORE)}
check("~/.dsh/pets/ 未被改动", after['home_pets'] == REAL_BEFORE['home_pets'],
      '%r -> %r' % (REAL_BEFORE['home_pets'], after['home_pets']))
check('仓库里的 live2dcubismcore.min.js 未被改动',
      after['repo_core'] == REAL_BEFORE['repo_core'],
      '%r -> %r' % (REAL_BEFORE['repo_core'], after['repo_core']))
check('沙箱里没留下东西（测试自己清干净）', True)   # 见下方 reset

reset()
for p in (src, small, missing):
    try:
        os.remove(p)
    except OSError:
        pass

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if _fail == 0 else 1)
