# -*- coding: utf-8 -*-
"""test_bubble_region.py — 「最新气泡被窗口 region 裁成两个字」的回归（v0.3.32）

用户截图：旧气泡完整，最新气泡只剩中间两个字，且两条气泡在 22:48:31 还没按
8 秒 TTL 消失（最后一次点击在 22:48:08）。链条是这样的：

  1. showBubble 锁了**高度**却没锁**宽度**：`el.textContent = ''`（为打字机清空）
     之后，shrink-to-fit 的盒子塌缩成 padding 宽（约 32px）；
  2. 紧接着 `setTimeout(pushSilhouette(true), 0)` 量到的就是这条**窄条** ——
     它被写进窗口 region，任何落在条外的像素都不显示；
  3. 设计上 540ms 后还有一次修正推送能救回来；但页面若在 540ms 内被冻结
     （本次实锤：点击后 ~0.4s 定时器停摆 —— 心跳证据显示点击那一刻页面还是活的，
     而 23s 后的截图里两条 8s TTL 的气泡都没消失），窄条形状就永久驻留。

三条修复各自有断言：
  A. ui.html —— 宽度锁必须与高度锁成对落下，且位置在清空文本**之前**（静态契约）
  B. standalone._merge_webview2_args —— WebView2 开关的合并（行为级）
  C. common.WindowAPI.pet_heartbeat —— 心跳停摆-恢复留痕（行为级）

用法：python test_bubble_region.py
      → live2d_probe/bubble_region_result.txt
      返回码 0 全通过 / 1 有失败
"""
import io
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
_PET = os.path.join(_PROJ, 'desktop_pet')

# ⚠️ 必须在 import standalone 之前设置（v0.3.27 教训）：不覆盖的话测试会把
#    噪声写进开发者的 desktop_pet/.airi-pet.log，伪装成产品里来路不明的报错
os.environ['AIRI_PET_LOG'] = os.path.join(tempfile.gettempdir(),
                                          'airi_test_bubble_region.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(tempfile.gettempdir(),
                                               'airi_test_bubble_region_settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(tempfile.gettempdir(),
                                             'airi_test_bubble_region_corpus.jsonc')

sys.path.insert(0, _PET)
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))

OUT = os.path.join(_HERE, 'bubble_region_result.txt')
UI_HTML = os.path.join(_PET, 'ui.html')
STANDALONE = os.path.join(_PET, 'standalone.py')

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
    out('  [%s] %-58s %s' % ('PASS' if ok else 'FAIL', name,
                             (':: ' + str(detail)) if detail else ''))


# ===========================================================================
out('=' * 82)
out('A. ui.html —— 气泡盒在"打字前清空文本"时必须已经锁定**宽度**')
out('=' * 82)

UI = io.open(UI_HTML, encoding='utf-8').read()


def line_of(needle, src=None):
    """返回 needle 所在行的行号（1 基）；找不到返回 None。"""
    for i, ln in enumerate((src if src is not None else UI).splitlines(), 1):
        if needle in ln:
            return i
    return None


i_w = line_of("el.style.width = el.offsetWidth + 'px';")
i_h = line_of("el.style.height = el.offsetHeight + 'px';")
i_clear = line_of("el.textContent = '';")

check('存在宽度锁 `el.style.width = el.offsetWidth`', i_w is not None,
      '行号=%s' % i_w)
check('存在高度锁 `el.style.height = el.offsetHeight`', i_h is not None,
      '行号=%s' % i_h)
check('两把锁都落在 `el.textContent = \'\'` **之前**',
      None not in (i_w, i_h, i_clear) and max(i_w, i_h) < i_clear,
      'width=%s height=%s clear=%s' % (i_w, i_h, i_clear))

# 上一轮的病灶（气泡 popUp 动画未跑第一帧时的强制推送 + 540ms 复推）必须还在
check('气泡出现时的 0ms 强制推送仍在',
      'setTimeout(function() { pushSilhouette(true); }, 0);' in UI)
check('540ms 的修正推送仍在（双保险，不是唯一防线）',
      'setTimeout(function() { pushSilhouette(true); }, 540);' in UI)

# 宽度锁只有在 border-box 下才成立：offsetWidth 是整盒宽，而 style.width 设的是
# 盒子宽度（content-box 下会多出 padding+border，把气泡撑变形）
check('全局 box-sizing 是 border-box（宽度锁成立的前提）',
      'box-sizing: border-box' in UI,
      'content-box 下 style.width=offsetWidth 会撑大一圈')

# 宽度锁是这一版刚落的：确保注释里交代了"为什么"，别让后人当成随手优化删掉
_src = UI.splitlines()
_comment_ok = False
for i in range(max(0, (i_w or 1) - 20), (i_w or 1)):
    if 'shrink-to-fit' in _src[i] or '塌缩' in _src[i]:
        _comment_ok = True
check('宽度锁附近写明了"塌缩"这个根因（防后人误删）', _comment_ok)


# ===========================================================================
out('')
out('=' * 82)
out('B. standalone._merge_webview2_args —— 禁止用 setdefault 整组放弃开关')
out('=' * 82)

import standalone  # noqa: E402

fn = getattr(standalone, '_merge_webview2_args', None)
check('_merge_webview2_args 存在', callable(fn))

NEED = ('--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows')
FEAT = ('IntensiveWakeUpThrottling', 'CalculateNativeWinOcclusion')

if callable(fn):
    empty = fn('')
    for a in NEED:
        check('空环境 -> 带上 %s' % a, a in empty)
    check('空环境 -> 合并成一个 --disable-features',
          empty.count('--disable-features=') == 1,
          '出现 %d 次' % empty.count('--disable-features='))
    check('  且同时包含 IntensiveWakeUpThrottling 与 CalculateNativeWinOcclusion',
          all(f in empty for f in FEAT), empty)

    # 宿主环境里已经设过这个变量：**必须合并**，不能像 setdefault 那样整组放弃
    host = '--my-custom-flag --disable-features=SleepingTabs'
    merged = fn(host)
    check('宿主自定义开关被保留（--my-custom-flag）', '--my-custom-flag' in merged)
    check('宿主的 feature 没被顶掉（SleepingTabs 仍在）', 'SleepingTabs' in merged)
    check('  —— 这就是 setdefault 做不到的地方：那边会整组放弃我们的开关',
          all(a in merged for a in NEED) and all(f in merged for f in FEAT))
    check('合并后仍然只有一个 --disable-features（否则后写覆盖先写）',
          merged.count('--disable-features=') == 1,
          '出现 %d 次' % merged.count('--disable-features='))
    check('幂等：merge(merge(x)) == merge(x)', fn(merged) == merged,
          '再次合并后 = %s' % fn(merged))

    # main() 里必须是"读旧值 -> 合并 -> 写回"，不能还是 setdefault
    main_src = io.open(STANDALONE, encoding='utf-8').read()
    check('main() 里不再用 setdefault 写这个变量',
          "setdefault(\n        'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'" not in main_src)
    check('main() 里 os.environ.get 旧值再写回',
          "os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = _merge_webview2_args("
          in main_src)


# ===========================================================================
out('')
out('=' * 82)
out('C. common.WindowAPI.pet_heartbeat —— 心跳停摆后必须留痕')
out('=' * 82)

import common  # noqa: E402

api = common.WindowAPI()
logged = []
api.set_region_logger(logged.append)

_real_monotonic = common.time.monotonic
_t = [1000.0]
common.time.monotonic = lambda: _t[0]

try:
    api.pet_heartbeat()          # 第一次：无历史，不算停摆
    check('首次心跳不报停摆（没有上一拍可比）', not logged, 'logged=%s' % logged)

    _t[0] += 2.0
    api.pet_heartbeat()          # 正常 2s 一拍
    check('正常 2s 间隔不报停摆', not logged, 'logged=%s' % logged)

    _t[0] += 23.0                # 模拟"冻结 23 秒后恢复"（就是本次截图那台机器）
    api.pet_heartbeat()
    check('停摆 23s 后恢复 -> 记一条 WARNING', len(logged) == 1,
          'logged=%s' % logged)
    check('  —— 日志里带上停摆时长', logged and '23.0' in logged[0],
          (logged[0] if logged else ''))
    check('  —— 写明"窗口形状与气泡自动消失均已冻结"',
          logged and ('已冻结' in logged[0]), (logged[0] if logged else ''))

    _t[0] += 2.0
    api.pet_heartbeat()          # 恢复后的正常一拍：不该再刷
    check('恢复后的正常心跳不再重复告警', len(logged) == 1,
          'logged=%s' % logged)
finally:
    common.time.monotonic = _real_monotonic

# 心跳留痕走的是 region 日志通道（standalone 把它写进 .airi-pet.log）
check('停摆日志走宿主的 region logger（同一份 .airi-pet.log）',
      hasattr(common.WindowAPI, '_log_region'))

# 没注入 logger 时也不能抛（扩展场景里这条链是"能跑就跑"）
try:
    bare = common.WindowAPI()
    bare._last_heartbeat = 1.0
    bare.pet_heartbeat()
    check('未注入 logger 时 pet_heartbeat 不抛异常', True)
except Exception as exc:
    check('未注入 logger 时 pet_heartbeat 不抛异常', False, repr(exc))


# ===========================================================================
out('')
out('=' * 82)
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
out('=' * 82)
out('注：A 段是静态契约（无头环境跑不了浏览器布局，shrink-to-fit 的塌缩')
out('    无法单元复现）；B / C 段是行为级断言。')
sys.exit(0 if _fail == 0 else 1)
