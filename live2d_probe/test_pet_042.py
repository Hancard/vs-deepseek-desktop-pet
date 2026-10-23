# -*- coding: utf-8 -*-
"""0.3.42 回归测试。

覆盖本轮修的 14 处（每条都在 0.3.41 的代码上坐实过触发路径）：

  A 段  ui.html 角色/拖拽
        A1 气泡先到时写的内联 animation，必须在 Live2D 就绪后撤掉（否则
           内联打穿 .live2d-active 且永不恢复）
        A2 setEmotion 的 onload/onerror 必须认领自己那次切换（慢图后到不许
           覆盖新情绪）
        A3 initLive2D 的 catch 必须清掉半初始化状态（否则模型+静态立绘叠一起）
        A4 get_position 的异步回填必须认领自己那次按下（否则拖拽起点被覆盖）
        A5 命中判定的兜底元素必须判断"图有没有真的加载出来"
  B 段  common.py 窗口层
        B1 disable_window_backdrop 在本系统读不到该 DWM 属性时要判成功
           （否则 20 秒空转 + 假 WARNING + 底色修复被推迟 20 秒）
        B2 move_window 的钳制失败不能连 move 一起吞掉
  C 段  extension.ts
        C1 readDetectIntervalMs 对 null/""/[] 必须返回 undefined（不是 1 秒）
        C2 面板（webview）的 items 也要裁剪，兑现注释里的承诺
        C3 推送签名要带总数（否则第 21 条之后的修复被静默吞掉）
  D 段  standalone.py
        D1 /push 的 chatMessage 分支也要归一化文本
        D2 pywebview 缺失时不能再"保持进程活着"（否则扩展谎报已启动）
        D3 落盘失败要把内存值还原（磁盘还有扩展这个消费者）
        D4 SSE 空闲心跳：既探测断连，也防中间设备掐断长连接

⚠️ 测试自身的坑（本项目踩过一轮，这里逐条防住）
  · 每段包在自己的 try 里 —— 访问旧版没有的符号会抛 AttributeError，
    把后面整段吞掉，看起来像"后面没事"其实压根没跑。用例数突然变少就是信号。
  · 抠源码时**模块级常量要一起抠**：只抠函数会 ReferenceError（记忆 #2）。
  · 覆盖 setTimeout 时用 var 赋值、且放在被测代码**之前**（函数声明会 hoist，
    见记忆 #8）。
  · 判据用副作用（move 调用记录、设置文件内容）而不是状态码。
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
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='airi_test042_')
# ⚠️ 必须在 import standalone **之前**设好：否则测试会往生产日志/设置里写
os.environ['AIRI_PET_LOG'] = os.path.join(TMP, 'pet.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')

REPO = Path(r'S:\My event\projects\vscode-anime-assistent')
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'

ROOT = str(REPO)
sys.path.insert(0, os.path.join(ROOT, 'desktop_pet'))
sys.path.insert(0, os.path.join(ROOT, 'python_backend'))

PASS = 0
FAIL = 0
LINES = []


def check(name, ok, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        LINES.append('PASS  %s' % name)
    else:
        FAIL += 1
        LINES.append('FAIL  %s   %s' % (name, detail))


def section(t):
    LINES.append('')
    LINES.append('--- %s ---' % t)


HTML = REPO / 'desktop_pet' / 'ui.html'
EXTS = REPO / 'src' / 'extension.ts'
STANDALONE = REPO / 'desktop_pet' / 'standalone.py'
COMMON = REPO / 'desktop_pet' / 'common.py'

HSRC = io.open(str(HTML), encoding='utf-8').read()
ESRC = io.open(str(EXTS), encoding='utf-8').read()
SSRC = io.open(str(STANDALONE), encoding='utf-8').read()


def grab(src, start, end):
    """从 start 起取到第一次出现 end（含 end）。取不到返回 ''。"""
    i = src.find(start)
    if i < 0:
        return ''
    j = src.find(end, i + len(start))
    if j < 0:
        return ''
    return src[i:j + len(end)]


def run_node(js, timeout=60):
    d = Path(tempfile.mkdtemp(prefix='airi042_'))
    f = d / 't.js'
    f.write_text(js, encoding='utf-8')
    try:
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout, r.stderr, r.returncode
    except Exception as exc:
        return '', '%s: %s' % (type(exc).__name__, exc), -1


def kv_of(out):
    d = {}
    for line in (out or '').strip().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip()
    return d


# ===========================================================================
# A 段：ui.html
# ===========================================================================
section('A1 内联 animation 打穿 live2d-active')

try:
    # 判据：加上 live2d-active 之后**紧接着**就要把内联 animation 撤掉。
    # 旧版只加了 class，而 showBubble 里那三行内联是在这之前写的 —— 内联
    # 优先级高于样式表，class 管不住它，且守卫此后为真、再也不会重写，
    # 于是 bubble 先到（常态）时这份内联永久留下。
    # ⚠️ 区间要取到 live2dStage 那行为止：中间夹了一大段注释，按行数取会漏。
    # （第一版按"之后 6 行"取，结果取到的全是注释，断言假失败。）
    i = HSRC.find("charBox.classList.add('live2d-active');")
    j = HSRC.find("live2dStage.classList.add('visible');", i)
    between = HSRC[i:j] if (i >= 0 and j > i) else ''
    check('A1-1 存在加 live2d-active 的位置', i >= 0 and j > i)
    check('A1-2 加上 class 之后、接管角色位之前撤掉内联 animation（关键）',
          "removeProperty('animation')" in between or
          "style.animation = ''" in between,
          'between=%r' % between[-200:])
    check('A1-3 全文件仍只有一处写内联 animation（就是 showBubble 那处）',
          len(re.findall(r"charBox\.style\.animation\s*=", HSRC)) == 2,
          'count=%d' % len(re.findall(r"charBox\.style\.animation\s*=", HSRC)))
except Exception as exc:
    check('A1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A2 setEmotion 情绪立绘的异步竞态')

try:
    fn = grab(HSRC, '    function setEmotion(emotion) {', '\n    }\n')
    check('A2-0 抠到 setEmotion', bool(fn) and 'testImg.onload' in fn)
    js = r"""
var IMAGES = [];
function Image() { this.onload = null; this.onerror = null; this._src = ''; }
Object.defineProperty(Image.prototype, 'src', {
  get: function() { return this._src; },
  set: function(v) { this._src = v; IMAGES.push(this); }
});
var CHAR_IMG = '/pet-assets/character.png';
function mkClassList() {
  var s = {};
  return {
    add: function(c) { s[c] = true; },
    remove: function(c) { s[c] = false; },
    contains: function(c) { return !!s[c]; }
  };
}
var charImg = { src: '', style: { display: '' } };
var cssChar = { classList: mkClassList() };
var charBox = { classList: mkClassList(), style: {},
                setAttribute: function() {}, offsetHeight: 1 };
var live2d = { ready: false };
var currentEmotion = 'idle';
var emotionTimer = null;
function applyLive2DEmotion(e) {}
// ⚠️ 用 var 赋值、且放在被测代码之前（函数声明会 hoist，见文件头注释）
var setTimeout = function(fn, ms) { return 0; };
var clearTimeout = function(id) {};
__FN__
var out = [];
setEmotion('greeting');          // 第 1 条消息：这张图慢
setEmotion('happy');             // 第 2 条消息：这张图快，先到
// 快的先完成
IMAGES[1].onload();
out.push('AFTER_FAST=' + charImg.src);
// 慢的**后到** —— 旧代码在这里无条件覆盖
IMAGES[0].onload();
out.push('AFTER_SLOW=' + charImg.src);
out.push('CUR=' + currentEmotion);
console.log(out.join('\n'));
""".replace('__FN__', fn)
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('A2-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:200]))
    check('A2-2 快的那张先生效', 'happy' in kv.get('AFTER_FAST', ''),
          kv.get('AFTER_FAST'))
    # 关键：慢图后到不许把画面拽回上一张
    check('A2-3 慢图后到不许覆盖新情绪（关键）',
          'happy' in kv.get('AFTER_SLOW', ''),
          'AFTER_SLOW=%r CUR=%r' % (kv.get('AFTER_SLOW'), kv.get('CUR')))
except Exception as exc:
    check('A2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A3 initLive2D 的 catch 要清半初始化')

try:
    body = grab(HSRC, '      } catch (err) {\n        live2d.failed = true;',
                '\n      }\n')
    check('A3-0 抠到 catch 分支', bool(body))
    check('A3-1 catch 里把 ready 复位', 'live2d.ready = false' in body, body[-200:])
    check('A3-2 catch 里摘掉 live2d-active',
          "charBox.classList.remove('live2d-active')" in body)
    check('A3-3 catch 里撤掉 live2dStage 的 visible',
          "live2dStage.classList.remove('visible')" in body)
    check('A3-4 catch 里清掉模型引用（model/app 都要清）',
          'live2d.model = null' in body and 'live2d.app = null' in body)
    check('A3-5 catch 里也要撤内联 animation',
          "removeProperty('animation')" in body)
except Exception as exc:
    check('A3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A4 get_position 回填必须认领那次按下')

try:
    # ⚠️ 两行要**分别**抓：dragSeq 的声明前面还有两行注释，用"下一行含
    # dragSeq"的可选组只会匹配失败，于是声明没被抠进来 → ++dragSeq 抛
    # ReferenceError 被 pointerdown 里的 catch(err){} 吞掉，用例看起来像
    # "回填没发生"，其实是测试自己没把变量抠全（第一版就栽在这里）。
    d1 = re.search(r'    var dragging = false[^\n]*\n', HSRC)
    d2 = re.search(r'    var dragSeq = [^\n]*\n', HSRC)
    decls = (d1.group(0) if d1 else '') + (d2.group(0) if d2 else '')
    check('A4-0 抠到拖拽状态声明', d1 is not None)
    check('A4-0a 抠到 dragSeq 声明（旧版没有 → 回填无守卫）', d2 is not None)
    pd = grab(HSRC, "    petArea.addEventListener('pointerdown', function(e) {",
              '\n    });\n')
    check('A4-0b 抠到 pointerdown', bool(pd) and 'get_position' in pd)
    js = r"""
var HANDLERS = {};
var petArea = { addEventListener: function(t, f) { HANDLERS[t] = f; },
                setPointerCapture: function() {} };
var dragging = false, moved = false, downX = 0, downY = 0;
__DECLS__
var window = {
  screenX: 100, screenY: 200,
  pywebview: { api: { get_position: function() { return PENDING; } } }
};
var PENDING = null;
var RESOLVERS = [];
function mkPending() {
  var p = { then: function(ok, no) { RESOLVERS.push(ok); return p; } };
  return p;
}
__PD__
function down(px) {
  PENDING = mkPending();
  HANDLERS['pointerdown']({ button: 0, clientX: 0, clientY: 0,
    screenX: px, screenY: px, pointerId: 1, preventDefault: function() {} });
}
var out = [];
down(0);            // 第一次按下（起点 100,200）
down(0);            // 松手后很快再按
// 此刻才让**第一次**那个还在飞行的 promise 落地，带回上一次的起点
RESOLVERS[0]([777, 888]);
out.push('RESOLVERS=' + RESOLVERS.length);
out.push('WINSTART=' + winStartX + ',' + winStartY);
console.log(out.join('\n'));
""".replace('__DECLS__', decls) \
   .replace('__PD__', pd)
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('A4-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    # 关键：第二次按下的起点不能被第一次的回填盖掉
    check('A4-2 迟到的回填不许覆盖新的拖拽起点（关键）',
          kv.get('WINSTART') == '100,200', 'WINSTART=%r' % kv.get('WINSTART'))
except Exception as exc:
    check('A4 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A5 命中判定的兜底元素要判断图有没有加载出来')

try:
    fb = grab(HSRC, '    function fallbackCharacterEl() {', '\n    }\n')
    check('A5-0 抠到 fallbackCharacterEl', bool(fb))
    js = r"""
function mkClassList(vis) {
  var s = {}; if (vis) s['visible'] = true;
  return { add: function(c) { s[c] = true; }, remove: function(c) { s[c] = false; },
           contains: function(c) { return !!s[c]; } };
}
var charImg = { src: '/pet-assets/character.png', style: { display: 'block' },
                complete: false, naturalWidth: 0 };
var cssChar = { classList: mkClassList(true) };
__FB__
var out = [];
charImg.complete = true; charImg.naturalWidth = 120;
out.push('LOADED=' + (fallbackCharacterEl() === charImg));
// 图还在加载（或 URL 挂起）时：外接框是 0x0，不能拿它当命中框
charImg.complete = false; charImg.naturalWidth = 0;
var el = fallbackCharacterEl();
out.push('LOADING_IS_CSS=' + (el === cssChar));
out.push('LOADING_IS_IMG=' + (el === charImg));
console.log(out.join('\n'));
""".replace('__FB__', fb)
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('A5-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:200]))
    check('A5-2 图加载好时用图片立绘', kv.get('LOADED') == 'true',
          kv.get('LOADED'))
    check('A5-3 图还在加载时落到 CSS 角色（关键）',
          kv.get('LOADING_IS_CSS') == 'true',
          'CSS=%r IMG=%r' % (kv.get('LOADING_IS_CSS'), kv.get('LOADING_IS_IMG')))

    # showImageCharacter：CSS 角色要等图片真的画出来才撤
    si = grab(HSRC, '    function showImageCharacter() {', '\n    }\n')
    check('A5-4 抠到 showImageCharacter', bool(si))
    # 旧版最后一行是 cssChar.classList.remove('visible') —— 在设 src 之前就撤了
    lines = [l.strip() for l in si.splitlines() if l.strip()]
    check('A5-5 不再在设 src 之前就撤掉 CSS 角色',
          lines[-1] != "cssChar.classList.remove('visible');",
          'last=%r' % (lines[-1] if lines else None))
    check('A5-6 CSS 角色改为 onload 之后再撤',
          'charImg.onload' in si and "cssChar.classList.remove('visible')" in si)
except Exception as exc:
    check('A5 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# B 段：common.py
# ===========================================================================
section('B1 backdrop 属性读不到时要判成功')

try:
    import common

    # 模拟"本系统不支持 DWMWA_SYSTEMBACKDROP_TYPE"：Get/Set 都失败
    common.get_backdrop_type = lambda hwnd: -1
    common._set_backdrop_none = lambda hwnd: False

    class _Form(object):
        pass

    common._get_form = lambda w: _Form()
    common._get_hwnd = lambda w: 0x1234

    ok, detail = common.disable_window_backdrop(object())
    check('B1-1 读不到该属性时判成功（否则 20 秒空转 + 假 WARNING）',
          ok is True, 'ok=%r detail=%r' % (ok, detail))

    # 对照：属性**读得到**、但没按下去 —— 这才是真失败
    common.get_backdrop_type = lambda hwnd: 2      # DWMSBT_MAINWINDOW = Mica
    common._set_backdrop_none = lambda hwnd: False
    ok2, detail2 = common.disable_window_backdrop(object())
    check('B1-2 属性可读但没设成 NONE 仍是失败（判据不能一刀切）',
          ok2 is False, 'ok=%r' % ok2)

    # 对照：本来就是 NONE
    common.get_backdrop_type = lambda hwnd: 1
    common._set_backdrop_none = lambda hwnd: True
    ok3, _d3 = common.disable_window_backdrop(object())
    check('B1-3 正常关掉 Mica 仍然判成功', ok3 is True, 'ok=%r' % ok3)
except Exception as exc:
    check('B1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B2 move_window 钳制失败不许吞掉移动')

try:
    import common

    class _FakeWindow(object):
        """模拟窗口已 destroy：width/height 取不到（pywebview 返回 None →
        int(None or 300) 之外的解包直接 TypeError）。"""

        def __init__(self):
            self.calls = []

        @property
        def width(self):
            raise RuntimeError('BrowserView.instances gone')

        @property
        def height(self):
            raise RuntimeError('BrowserView.instances gone')

        @property
        def native(self):
            raise RuntimeError('no native')

        def move(self, x, y):
            self.calls.append((x, y))

    common.get_virtual_screen = lambda: (0, 0, 1920, 1080)
    common.get_dpi_scale = lambda hwnd=None: 1.0

    win = _FakeWindow()
    api = common.WindowAPI()
    api.set_window(win)
    api.move_window(50, 60)
    check('B2-1 钳制抛异常时窗口**照样**移动（关键）',
          win.calls == [(50, 60)], 'calls=%r' % (win.calls,))
except Exception as exc:
    check('B2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# C 段：extension.ts
# ===========================================================================
section('C1 readDetectIntervalMs 对 falsy 值')


def ts_to_js(s):
    """把抠出来的 TS 函数体转成能跑的 JS（最小改写）。

    ⚠️ 转换失败必须暴露：调用方要断言结果里仍然含有关键标识符，
    否则转换把代码削没了会变成"恒真通过"。
    """
    s = re.sub(r'\)\s*:\s*[^{]*\{', ') {', s, count=1)      # 返回类型注解
    s = re.sub(r'\bas\s+(?:\{[^{}]*\}|\w+)', '', s)          # as T / as {...}
    s = re.sub(r'\bas\s+unknown', '', s)
    s = s.replace('const ', 'var ').replace('let ', 'var ')
    return s


try:
    fn = grab(ESRC, 'function readDetectIntervalMs(): number | undefined {',
              '\n}\n')
    consts = '\n'.join(re.findall(r'const DETECT_\w+ = [^;]+;', ESRC))
    pet_path_line = '\n'.join(re.findall(r'const PET_SETTINGS_PATH = [^;]+;',
                                         ESRC))
    check('C1-0 抠到函数', bool(fn))
    check('C1-0b 抠到模块级常量（只抠函数会 ReferenceError）',
          'DETECT_MIN_SEC' in consts and 'DETECT_MAX_SEC' in consts,
          'consts=%r' % consts[:120])
    js = r"""
var fs = { readFileSync: function(p, e) { return FILE; } };
var FILE = '{}';
var PET_SETTINGS_PATH = 'x';
__CONSTS__
__FN__
var out = [];
var cases = [null, '', [], 0, false, undefined, 5, '5', 999, 'abc', 'NaN', {}];
for (var i = 0; i < cases.length; i++) {
  var v = cases[i];
  FILE = JSON.stringify({ detectIntervalSec: v });
  var r = readDetectIntervalMs();
  out.push(JSON.stringify(v) + '->' + (r === undefined ? 'undef' : r));
}
console.log(out.join('\n'));
""".replace('__CONSTS__', consts).replace('__FN__', ts_to_js(fn))
    check('C1-0c TS→JS 转换没把函数削没',
          'detectIntervalSec' in ts_to_js(fn) and 'Math.max' in ts_to_js(fn))
    out, err, rc = run_node(js)
    check('C1-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    got = {}
    for line in (out or '').strip().splitlines():
        if '->' in line:
            k, v = line.rsplit('->', 1)
            got[k] = v
    for bad in ('null', '""', '[]', '0', 'false', 'undefined', '{}'):
        check('C1-2 detectIntervalSec=%s 必须返回 undefined' % bad,
              got.get(bad) == 'undef', 'got=%r' % got.get(bad))
    check('C1-3 合法 5 秒照常生效', got.get('5') == '5000', 'got=%r' % got.get('5'))
    check('C1-4 字符串 "5" 照常生效', got.get('"5"') == '5000',
          'got=%r' % got.get('"5"'))
    check('C1-5 超范围 999 夹到 60 秒', got.get('999') == '60000',
          'got=%r' % got.get('999'))
except Exception as exc:
    check('C1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('C2 面板 items 也要裁剪')

try:
    hd = grab(ESRC, 'function handleDiagnosticsChanged(): void {', '\n}\n')
    check('C2-0 抠到 handleDiagnosticsChanged', bool(hd))
    # 注释里写的是"连同 webview 一起用裁剪后的文本"，实现必须真的裁
    check('C2-1 发给面板的 items 走了 clipMessage（关键）',
          'clipMessage' in hd, 'block=%r' % hd[-260:])
except Exception as exc:
    check('C2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('C3 推送签名要带总数')

try:
    pp = grab(ESRC, 'async function pushPendingDiagnostics(): Promise<boolean> {',
              '\n}\n')
    check('C3-0 抠到 pushPendingDiagnostics', bool(pp))
    sig = re.search(r'const signature = JSON\.stringify\(\[(.*?)\]\);', pp, re.S)
    check('C3-1 找到签名构造', sig is not None)
    body = sig.group(1) if sig else ''
    check('C3-2 签名里带上总数（否则第 21 条之后的修复被吞）',
          'workspaceErrors.length' in body, 'body=%r' % body)
except Exception as exc:
    check('C3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# D 段：standalone.py
# ===========================================================================
section('D1 /push 的 chatMessage 分支也要归一化')

try:
    import standalone

    has = hasattr(standalone, '_as_text')
    check('D1-0 暴露 _as_text', has)
    if has:
        check('D1-1 dict 序列化成 JSON',
              standalone._as_text({'a': 1}) == '{"a": 1}',
              repr(standalone._as_text({'a': 1})))
        check('D1-2 list 序列化成 JSON', standalone._as_text([1, 2]) == '[1, 2]',
              repr(standalone._as_text([1, 2])))
        check('D1-3 数字转字符串', standalone._as_text(12) == '12')
        check('D1-4 None 转空串（不许念出 None）', standalone._as_text(None) == '')
        check('D1-5 字符串原样返回', standalone._as_text('hi') == 'hi')

    # 源码级：else 分支必须用它（不是只改了 payload 分支）。
    # ⚠️ 别用"任意 push_message('chatMessage', ...)"的正则去捞：do_POST 里
    # 还有一处在 json 解析失败时把 body 原样推下去，捞到它就成了假阳性。
    check('D1-6 chatMessage 分支改走 _as_text（关键）',
          "_as_text(msg.get('message', body))" in SSRC)
except Exception as exc:
    check('D1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D2 pywebview 缺失时不能再保持进程活着')

try:
    m = re.search(r'    except ImportError:\n((?:.*\n)*?)        sys\.exit', SSRC)
    block = m.group(1) if m else ''
    check('D2-0 找到 ImportError 分支', bool(m), 'block=%r' % block[-200:])
    # ⚠️ 判之前要把注释行剥掉：修这条时写的注释里就出现了 `while True`
    # 这个字面量，不剥的话断言被自己的注释误伤（G1 那次是同一类）。
    code = '\n'.join(l for l in block.splitlines()
                     if not l.strip().startswith('#'))
    check('D2-1 分支里不再有 while True（否则扩展谎报已启动 + 僵尸进程）',
          'while True' not in code, 'code=%r' % code[-200:])
except Exception as exc:
    check('D2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D3/D4 端到端：设置回滚 与 SSE 心跳')

BASE = None
try:
    import standalone

    for _p in range(21700, 21800):
        _ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(_p, _ready),
                         daemon=True).start()
        if _ready.wait(3):
            BASE = _p
            break
    check('D0 测试服务器已起', BASE is not None, 'port=%r' % BASE)
except Exception as exc:
    check('D 段服务器未起来', False, '%s: %s' % (type(exc).__name__, exc))

if BASE is not None:
    try:
        # --- D3 落盘失败要把内存值还原 ---
        real_save = standalone.save_settings

        def fake_save(_s):
            return False

        standalone.save_settings = fake_save
        try:
            before = json.loads(urllib.request.urlopen(
                'http://127.0.0.1:%d/settings' % BASE, timeout=3).read())
            data = json.dumps({'detectIntervalSec': 5}).encode()
            req = urllib.request.Request(
                'http://127.0.0.1:%d/settings' % BASE, data=data, method='POST',
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=3) as r:
                body = json.loads(r.read())
            after = json.loads(urllib.request.urlopen(
                'http://127.0.0.1:%d/settings' % BASE, timeout=3).read())
            check('D3-1 落盘失败时响应里 saved=false', body.get('saved') is False,
                  'body=%r' % body)
            check('D3-2 落盘失败后内存值还原（否则和扩展读的磁盘值分叉）',
                  after.get('detectIntervalSec') == before.get('detectIntervalSec'),
                  'before=%r after=%r' % (
                      before.get('detectIntervalSec'),
                      after.get('detectIntervalSec')))
        finally:
            standalone.save_settings = real_save

        # 对照：能落盘时值要真的变
        n = json.dumps({'detectIntervalSec': 7}).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:%d/settings' % BASE, data=n, method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=3) as r:
            b2 = json.loads(r.read())
        check('D3-3 能落盘时值照常生效（判据不是一刀切）',
              b2.get('detectIntervalSec') == 7, 'body=%r' % b2)
    except Exception as exc:
        check('D3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))

    try:
        # --- D4 SSE 空闲心跳 ---
        check('D4-0 存在心跳间隔常量',
              hasattr(standalone, 'SSE_HEARTBEAT_TICKS'),
              'SSE_HEARTBEAT_TICKS missing')
        real_ticks = standalone.SSE_HEARTBEAT_TICKS
        standalone.SSE_HEARTBEAT_TICKS = 2      # 0.3s * 2 = 0.6s，别真等 15 秒
        s = None
        try:
            s = __import__('socket').create_connection(('127.0.0.1', BASE), 3)
            s.sendall(('GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
                      .encode())
            s.settimeout(6)
            buf = b''
            deadline = time.time() + 5
            while time.time() < deadline and b':hb' not in buf:
                try:
                    chunk = s.recv(4096)
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
            check('D4-1 空闲时收到 SSE 心跳注释行（关键：断连探测 + 防中间设备）',
                  b':hb' in buf, 'buf=%r' % buf[:200])
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            standalone.SSE_HEARTBEAT_TICKS = real_ticks
    except Exception as exc:
        check('D4 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


LINES.append('')
LINES.append('PASS=%d FAIL=%d' % (PASS, FAIL))
out = '\n'.join(LINES)
print(out)
io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '_pet042_result.txt'), 'w',
        encoding='utf-8').write(out + '\n')
sys.exit(1 if FAIL else 0)
