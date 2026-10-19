# -*- coding: utf-8 -*-
"""test_corpus_jsonc.py — 用户手改语料时的三条真实踩坑路径（0.3.38）。

背景：右键「编辑语料」打开的 `~/.dsh/pets/airi-pet-corpus.jsonc` 是**给普通
用户手改**的，文件头 `_FILE_HEADER` 还明文写着「本文件允许注释和多余的尾
逗号」。下面三条都是"照着文件头说的做，结果整份语料失效/桌宠起不来"。

  A. **尾随逗号后面跟注释** → 整份语料失效（12 场景 + 3 点击池全回落内置）
     实测（修复前）：加一条台词 + 行尾 `// 新加的` → scenes 12 → 0。
  B. **语料存成非 UTF-8**（记事本另存为 ANSI / Unicode）→ 桌宠**起不来**
     corpus_status() 无兜底，而它在 standalone.py 启动段是**顶层裸调用**
     （在 main() 之前），一抛进程就没了。
  C. **热重载读到坏文件** → 把上一版完好语料**清空**（editor 保存是
     截断后原地写，30 秒周期完全可能落在中间）

用法：Python312 跑，结果写 live2d_probe/corpus_jsonc_result.txt
"""
import io
import os
import sys
import tempfile

PROBE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PROBE)
BACKEND = os.path.join(ROOT, 'python_backend')

# --- 隔离：必须在 import corpus 之前（CORPUS_PATH 是模块加载时算的）----------
TMP = tempfile.mkdtemp(prefix='airi_corpus_jsonc_')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
sys.path.insert(0, BACKEND)

import corpus  # noqa: E402

OUT = os.path.join(PROBE, 'corpus_jsonc_result.txt')
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


def cold(path):
    """冷启动式读取：清掉缓存，模拟进程刚起来第一次读。"""
    corpus.CORPUS_PATH = path
    corpus._cache.update(key=None, scenes={}, clicks={}, error=None,
                         dropped=0)
    return corpus._user_corpus()


# =========================================================== A. 尾逗号 + 注释
out('=== A. 加一条台词后写注释（文件头承诺「允许注释和多余的尾逗号」）===')

BASE = os.path.join(TMP, 'base.jsonc')
corpus.export_default_corpus(BASE, overwrite=True)
RAW = io.open(BASE, encoding='utf-8').read()

# 导出文件里每个场景的最后一行**没有**尾随逗号。用户要加一条自己的台词，
# 必须先给它补上逗号 —— 这是必然的编辑动作，不是刁钻边界。
import re  # noqa: E402

_m = re.search(r'^([ \t]*)"开机完毕[^\n]*\n', RAW, re.M)
check('A0 探针自身：定位到可编辑的那一行', _m is not None)
INDENT = _m.group(1) if _m else '      '
OLD = _m.group(0) if _m else ''


def build(mode):
    if mode is None:
        return RAW
    head = OLD.rstrip('\n') + ',\n'
    if mode == 'NO_COMMENT':
        return RAW.replace(OLD, head + INDENT + '"我新加的问候"\n')
    if mode == 'LINE_COMMENT':
        return RAW.replace(OLD, head + INDENT + '"我新加的问候",  // 新加的\n')
    if mode == 'BLOCK_COMMENT':
        return RAW.replace(OLD, head + INDENT + '"我新加的问候",  /* 新加的 */\n')
    if mode == 'AFTER_BLOCK':
        return RAW.replace('     ],\n', '     ],  // 文件类场景结束\n', 1)
    return RAW


CASES = [
    ('基线（什么都不改）', None, True),
    ('加台词，不写注释', 'NO_COMMENT', True),
    ('加台词 + 行尾 // 注释', 'LINE_COMMENT', True),
    ('加台词 + /* 块注释 */', 'BLOCK_COMMENT', True),
    ('场景块后写 ],  // 注释', 'AFTER_BLOCK', False),
]

# AFTER_BLOCK 那条改的不是台词数组，所以"新台词"本来就不该出现；
# 它要验的是"整份语料没被作废"。
for label, mode, want_new_line in CASES:
    p = os.path.join(TMP, 'a_%s.jsonc' % (mode or 'base'))
    io.open(p, 'w', encoding='utf-8').write(build(mode))
    scenes, clicks, err = cold(p)
    lines = corpus.get_lines('greeting')
    got_new = any('我新加的问候' in x for x in lines)
    ok_scenes = len(scenes) == 12 and len(clicks) == 3 and err is None
    tag = label
    if want_new_line:
        check('%s' % tag, ok_scenes and got_new if mode else ok_scenes,
              'scenes=%d clicks=%d 新台词生效=%s err=%s'
              % (len(scenes), len(clicks), got_new, (err or 'None')[:48]))
    else:
        check('%s' % tag, ok_scenes,
              'scenes=%d clicks=%d err=%s'
              % (len(scenes), len(clicks), (err or 'None')[:48]))

# 反证：确认"注释 + 尾逗号"这个组合确实走到了 strip_jsonc 的逗号分支
check('A9 反证：修复前这条路径真的会失败（注释被剥掉后留下非法尾逗号）',
      corpus.strip_jsonc('{"a":["b", // c\n]}').count('"b",') == 0,
      'strip 结果=%r' % corpus.strip_jsonc('{"a":["b", // c\n]}'))

# 字符串里的 // 不能被当注释（老行为不能回归）
s = '{"a":["// TODO 还没写",],}'
check('A10 字符串里的 // 仍然不是注释',
      '"// TODO 还没写"' in corpus.strip_jsonc(s),
      'strip 结果=%r' % corpus.strip_jsonc(s))


# =========================================================== B. 非 UTF-8
out('')
out('=== B. 语料存成非 UTF-8（记事本另存为 ANSI / Unicode）===')

for label, enc in (('UTF-16LE（记事本"Unicode"）', 'utf-16-le'),
                   ('GBK（记事本"ANSI"）', 'gbk')):
    p = os.path.join(TMP, 'b_%s.jsonc' % enc.replace('-', '_'))
    io.open(p, 'wb').write(RAW.encode(enc, errors='replace'))

    # ⚠️ 每条用例都要能独立跑完：异常路径下 r 可能根本没被赋值，后面
    # 再引用它会抛 NameError，把**后面几条用例整段吞掉**（第一版就栽在这：
    # UTF-16LE 一失败，GBK 那半段压根没跑，看起来像"GBK 没事"，其实没测）。
    r = None
    threw = None
    scenes_n = -1
    try:
        cold(p)
        r = corpus.corpus_status()
        scenes_n = r['scenes_from_user'] if r else -1
    except Exception as exc:
        threw = '%s: %s' % (type(exc).__name__, exc)
    check('B [%s] corpus_status 不抛异常' % label, threw is None,
          threw or 'OK')

    # 冷启动（缓存空）应当如实报 0 并留 error，不是"看着像读成功了"
    err_txt = (r or {}).get('error')
    check('B [%s] 冷启动如实回落（scenes=0 且 error 有值）' % label,
          scenes_n == 0 and bool(err_txt),
          'scenes=%d error=%s' % (scenes_n, (err_txt or 'None')[:44]))
    # 取词不能崩
    try:
        corpus.get_lines('greeting')
        corpus.get_click_lines('head')
        ok_lines = True
    except Exception as exc:
        ok_lines = False
    check('B [%s] get_lines / get_click_lines 不抛' % label, ok_lines,
          '' if ok_lines else '抛了')

# 反证：确认 UnicodeDecodeError 确实不是 OSError（否则这条修得像过度设计）
check('B9 反证：UnicodeDecodeError 不是 OSError 的子类',
      not issubclass(UnicodeDecodeError, OSError),
      'issubclass=%s' % issubclass(UnicodeDecodeError, OSError))

# 静态契约：两处兜底都得在
SRC = io.open(os.path.join(BACKEND, 'corpus.py'), encoding='utf-8').read()
check('B10 _read_corpus_file 捕获 UnicodeDecodeError',
      'except (OSError, UnicodeDecodeError)' in SRC)
check('B11 corpus_status 自带兜底（排障函数自己不能崩）',
      re.search(r'def corpus_status.*?except Exception', SRC, re.S) is not None)


# =========================================================== C. 热重载保留
out('')
out('=== C. 热重载读到坏文件时保留上一版完好语料 ===')

p = os.path.join(TMP, 'c.jsonc')
io.open(p, 'w', encoding='utf-8').write(RAW)
scenes, clicks, err = cold(p)
check('C1 先读到一份好语料', len(scenes) == 12 and err is None,
      'scenes=%d err=%s' % (len(scenes), err))

# 模拟编辑器"截断后原地写"的中间态（或网盘仅联机占位）
io.open(p, 'wb').write(b'')
st = os.stat(p)
corpus._cache.update(key=(st.st_mtime_ns - 1, st.st_size))   # 强制重读
scenes2, clicks2, err2 = corpus._user_corpus()
check('C2 读到坏文件后仍然保住上一版（不是被清空）',
      len(scenes2) == 12,
      'scenes=%d (修复前这里会是 0)' % len(scenes2))
check('C3 仍然如实留下 error 供排障', bool(err2), 'error=%s' % (err2 or 'None')[:50])
check('C4 缓存里的 scenes 也没被抹掉',
      len(corpus._cache['scenes']) == 12,
      'cache scenes=%d' % len(corpus._cache['scenes']))

# 文件修好后必须能恢复（不能被"保留旧值"卡住）
io.open(p, 'w', encoding='utf-8').write(
    RAW.replace(OLD, OLD.rstrip('\n') + ',\n' + INDENT + '"改好了"\n'))
scenes3, _c3, err3 = corpus._user_corpus()
check('C5 文件修好后能再次读进来（保留旧值不会卡住更新）',
      err3 is None and any('改好了' in x for x in scenes3.get('greeting', [])),
      'err=%s' % (err3 or 'None'))


out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
sys.exit(0 if _fail == 0 else 1)
