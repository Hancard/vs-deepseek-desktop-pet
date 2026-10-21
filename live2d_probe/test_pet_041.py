# -*- coding: utf-8 -*-
"""
0.3.41 回归测试

覆盖本轮修的 10 处：
  A. watcher 目录过滤只应作用在监听根之下的相对路径上（严重：整份监听静默失效）
  B. watcher check_c：文件不存在不再产出伪诊断；显式给 encoding
  C. watcher 检查阶段并发（C/C++ 子进程不再串行 200 秒）
  D. ui.html SSE 进入 CLOSED 后必须自己重建（否则永久断流）
  E. ui.html 检测周期：保存失败要回滚+提示；响应乱序不能覆盖新值
  F. extension.ts：启动并发守卫 / 回滚引用比较 / items 截断 / 端口插值
  G. ui.html 底板取样失败计数不再振荡
  H. ui.html reportSil 有 .catch；edit-corpus 失败有反馈
  I. main.py AIRI_WIN_BG=off 不再白等 10 秒
  J. extension.test.ts 断言的是被测模块的真实集合

⚠️ 每段都必须能**独立跑完**：某段里访问旧版本没有的符号会抛 AttributeError，
   把后面所有段整段吞掉，输出看起来像"后面没事"其实压根没跑（0.3.39 踩过）。
   用例数突然变少就是信号。
"""
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(r'S:\My event\projects\vscode-anime-assistent')
PY = r'C:/Users/Mr.hancard/AppData/Local/Programs/Python/Python312/python.exe'
NODE = r'C:/Users/Mr.hancard/.workbuddy/binaries/node/versions/22.22.2-6/node.exe'

PASS = FAIL = 0
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
    LINES.append('--- %s ---' % t)


WATCHER = REPO / 'desktop_pet' / 'watcher.py'
HTML = REPO / 'desktop_pet' / 'ui.html'
EXTS = REPO / 'src' / 'extension.ts'
MAINPY = REPO / 'desktop_pet' / 'main.py'
TESTTS = REPO / 'src' / 'test' / 'extension.test.ts'

WSRC = io.open(str(WATCHER), encoding='utf-8').read()
HSRC = io.open(str(HTML), encoding='utf-8').read()
ESRC = io.open(str(EXTS), encoding='utf-8').read()
MSRC = io.open(str(MAINPY), encoding='utf-8').read()
TSRC = io.open(str(TESTTS), encoding='utf-8').read()


# ===================================================================
# A. watcher 目录过滤
# ===================================================================
section('A 目录过滤')
try:
    sys.path.insert(0, str(REPO / 'desktop_pet'))
    import watcher  # noqa: E402

    has_skip = hasattr(watcher, 'should_skip')
    check('A1 watcher 暴露 should_skip(rel_parts)', has_skip)
    if has_skip:
        check('A2 相对段里有 .git 要跳过',
              watcher.should_skip(('.git', 'a.py')) is True)
        check('A3 相对段里有 node_modules 要跳过',
              watcher.should_skip(('src', 'node_modules', 'a.py')) is True)
        check('A4 普通相对路径不跳过',
              watcher.should_skip(('src', 'a.py')) is False)
        check('A5 单个 . 开头的段要跳过',
              watcher.should_skip(('.cache',)) is True)

    # 源码级：扫描循环必须传 relative_to(watch_path).parts，不能传 f.parts
    loop = re.search(r'for f in watch_path\.rglob', WSRC)
    check('A6 扫描循环存在', loop is not None)
    check('A7 过滤用的是 relative_to(watch_path).parts',
          'should_skip(f.relative_to(watch_path).parts)' in WSRC)
    check('A8 不再拿绝对路径的 f.parts 做过滤',
          not re.search(r'should_skip\(\s*f\.parts\s*\)', WSRC)
          and 'for part in f.parts' not in WSRC)

    # 端到端：监听根落在 . 开头的祖先目录下，仍要扫得到
    if has_skip:
        tmp = Path(tempfile.mkdtemp(prefix='.t041_'))
        proj = tmp / 'proj'
        proj.mkdir()
        (proj / 'a.py').write_text('x=1\n', encoding='utf-8')
        (proj / 'b.py').write_text('y=2\n', encoding='utf-8')
        sub = proj / 'src'
        sub.mkdir()
        (sub / 'c.py').write_text('z=3\n', encoding='utf-8')
        # 复刻产品代码里的扫描逻辑（判据必须来自 should_skip 本身）
        wp = proj.resolve()
        found = [f for e in watcher.SUPPORTED
                 for f in wp.rglob('*' + e)
                 if not watcher.should_skip(f.relative_to(wp).parts)]
        check('A9 点开头的祖先目录下仍能扫到 3 个文件',
              len(found) == 3, 'got=%d' % len(found))
        check('A10 监听根确实有点开头的祖先',
              any(x.startswith('.') for x in wp.parts))
except Exception as exc:
    check('A 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===================================================================
# B. check_c
# ===================================================================
section('B check_c')
try:
    errs = watcher.check_c(os.path.join(tempfile.gettempdir(), '_no_such_file_041.c'))
    check('B1 文件不存在时返回 0 条（不再产出 No such file 伪诊断）',
          len(errs) == 0, 'got=%d %r' % (len(errs), errs[:1]))
    check('B2 subprocess.run 显式给了 encoding',
          "encoding='utf-8'" in WSRC and "errors='replace'" in WSRC)
    check('B3 check_c 里有 isfile 前置判断',
          'os.path.isfile(filepath)' in WSRC)
    check('B4 解码失败不再被当成诊断内容',
          'codec' not in ' '.join(str(e.get('message', '')) for e in errs))
except Exception as exc:
    check('B 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===================================================================
# C. 检查阶段并发
# ===================================================================
section('C 并发检查')
try:
    check('C1 引入 ThreadPoolExecutor',
          'ThreadPoolExecutor' in WSRC and
          'from concurrent.futures import ThreadPoolExecutor' in WSRC)
    check('C2 有 _check_one 可并发单元', '_check_one' in WSRC)
    check('C3 每轮仍有限流上限', 'MAX_CHECKS_PER_SCAN' in WSRC)

    # 实测并发：把 CHECKERS 换成 sleep 0.3 的假检查器，8 个文件
    # 串行 = 2.4s，并发 4 = ~0.6s
    saved = dict(watcher.CHECKERS)
    try:
        def slow(key):
            time.sleep(0.3)
            return []

        for e in watcher.SUPPORTED:
            watcher.CHECKERS[e] = slow
        files = []
        for i in range(8):
            fd, fp = tempfile.mkstemp(suffix='.py', prefix='t041_')
            os.close(fd)
            files.append(fp)
        t0 = time.time()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda kv: watcher._check_one(*kv),
                          [(f, 1.0) for f in files]))
        el = time.time() - t0
        check('C4 8 个慢检查并发 4 应在 1.5s 内完成（串行要 2.4s）',
              el < 1.5, 'elapsed=%.2f' % el)
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
    finally:
        watcher.CHECKERS.clear()
        watcher.CHECKERS.update(saved)
except Exception as exc:
    check('C 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===================================================================
# D/E. ui.html（用 node 跑真实函数源码）
# ===================================================================
def grab_block(start_marker, end_marker):
    i = HSRC.find(start_marker)
    if i < 0:
        raise RuntimeError('ui.html 找不到: ' + start_marker[:40])
    j = HSRC.find(end_marker, i)
    if j < 0:
        raise RuntimeError('ui.html 找不到结束: ' + end_marker[:40])
    return HSRC[i:j + len(end_marker)]


def grab_fn(name):
    key = 'function %s(' % name
    i = HSRC.find(key)
    if i < 0:
        raise RuntimeError('ui.html 找不到 ' + key)
    j = HSRC.index('{', i)
    depth = 0
    for k in range(j, len(HSRC)):
        if HSRC[k] == '{':
            depth += 1
        elif HSRC[k] == '}':
            depth -= 1
            if depth == 0:
                return HSRC[i:k + 1]
    raise RuntimeError('括号没配平: ' + name)


def grab_var(name):
    for ln in HSRC.splitlines():
        s = ln.strip()
        if s.startswith('var %s ' % name) or s.startswith('var %s=' % name):
            return s
    raise RuntimeError('ui.html 找不到 var ' + name)


def run_node(js):
    fd, path = tempfile.mkstemp(suffix='.js', prefix='t041_')
    os.close(fd)
    io.open(path, 'w', encoding='utf-8').write(js)
    try:
        r = subprocess.run([NODE, path], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=60)
        return r.stdout, r.stderr, r.returncode
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


section('D SSE 断流重建')
try:
    sse_block = grab_block('var sseInstance = null;',
                           '      return es;\n    }')
    js = r"""
var realSetTimeout = setTimeout;
var TIMERS = [];
function setTimeout(fn, ms) { TIMERS.push({ fn: fn, ms: ms }); return TIMERS.length; }
function clearTimeout(id) {}
function runTimers() {
  var t = TIMES__;
  return t;
}
var NEW_ES = [];
var CLOSED_LIST = [];
function EventSource(url) {
  NEW_ES.push(url);
  this.url = url; this.readyState = 0;
  var self = this;
  CLOSED_LIST.push(this);
}
var SSE_URL = 'http://127.0.0.1:19876/events';
var EVENT_URL = 'http://127.0.0.1:19876/event';
var FETCHES = [];
function fetch(url, opt) { FETCHES.push(url); return { catch: function() { return {}; } }; }
var ONLINE = [];
function setOnline(v) { ONLINE.push(v); }
function reportJS(a, b) {}
__SSE__
var out = [];
connectSSE();
out.push('D1_NEW_AFTER_CONNECT=' + NEW_ES.length);
// 拿到刚建的实例，模拟服务端回了非 event-stream → 浏览器置 CLOSED
var es = sseInstance;
es.readyState = 2;
es.onerror();
out.push('D2_ONLINE_AFTER_ERR=' + ONLINE[ONLINE.length - 1]);
out.push('D3_TIMERS_QUEUED=' + TIMERS.length);
out.push('D4_NEW_BEFORE_TIMER=' + NEW_ES.length);
if (TIMERS.length) { TIMERS[0].fn(); }
out.push('D5_NEW_AFTER_TIMER=' + NEW_ES.length);
out.push('D6_DELAY=' + (TIMERS.length ? TIMERS[0].ms : -1));
// 退避：再失败一次，间隔应该变大
if (TIMERS.length) { TIMERS.shift(); }
var es2 = sseInstance;
es2.readyState = 2; es2.onerror();
if (TIMERS.length) { out.push('D7_DELAY2=' + TIMERS[TIMERS.length - 1].ms); }
// readyState=0（CONNECTING，浏览器自己在重试）时不该插手
var before = TIMERS.length;
var es3 = sseInstance; es3.readyState = 0; es3.onerror();
out.push('D8_NO_RETRY_WHEN_CONNECTING=' + (TIMERS.length === before));
console.log(out.join('\n'));
""".replace('__SSE__', sse_block)
    out, err, rc = run_node(js)
    kv = dict(l.split('=', 1) for l in out.strip().splitlines() if '=' in l)
    check('D node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:200]))
    check('D1 connect 后建了 1 个 EventSource', kv.get('D1_NEW_AFTER_CONNECT') == '1',
          kv.get('D1_NEW_AFTER_CONNECT'))
    check('D2 onerror 时置 offline', kv.get('D2_ONLINE_AFTER_ERR') == 'false',
          kv.get('D2_ONLINE_AFTER_ERR'))
    check('D3 CLOSED 时排了重连定时器', kv.get('D3_TIMERS_QUEUED') == '1',
          kv.get('D3_TIMERS_QUEUED'))
    check('D5 定时器跑完后重建了连接（关键）', kv.get('D5_NEW_AFTER_TIMER') == '2',
          kv.get('D5_NEW_AFTER_TIMER'))
    check('D6 首次重连间隔 1s', kv.get('D6_DELAY') == '1000', kv.get('D6_DELAY'))
    check('D7 再次失败退避到 2s', kv.get('D7_DELAY2') == '2000', kv.get('D7_DELAY2'))
    check('D8 CONNECTING 时不插手（交给浏览器自重连）',
          kv.get('D8_NO_RETRY_WHEN_CONNECTING') == 'true',
          kv.get('D8_NO_RETRY_WHEN_CONNECTING'))
except Exception as exc:
    check('D 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))

section('E 检测周期面板')
try:
    set_block = grab_block('var settingsSeq = 0;',
                           '        }).catch(function() {});\n'
                           '      } catch (err) {}\n    }')
    js = r"""
var realSetTimeout = globalThis.setTimeout;
var TIMERS = [];
function setTimeout(fn, ms) { TIMERS.push({ fn: fn, ms: ms }); return TIMERS.length; }
function clearTimeout(id) { TIMERS.length = 0; }
function runOne() { var t = TIMERS.shift(); if (t) t.fn(); }
var SETTINGS_URL = 'http://127.0.0.1:19876/settings';
var FETCHES = [];
var BUBBLES = [];
function showBubble(t, isErr) { BUBBLES.push(t); }
function renderDetectLabels() {}
function repushMenuRegion() {}
function reportJS() {}
__CLAMP__
var detectSec = 30, detectMin = 1, detectMax = 60;
__SET__
var out = [];
function settle(ms) { return new Promise(function(r) { realSetTimeout(r, ms); }); }

(async function() {
  // --- E1: 保存失败（fetch reject）应回滚 + 提示 ---
  setDetectSec(5);
  out.push('E1_LOCAL=' + detectSec);           // 本地立刻变 5
  runOne();                                     // 跑 150ms debounce → 真发请求
  out.push('E1_FETCHES=' + FETCHES.length);
  FETCHES[FETCHES.length - 1].reject(new Error('boom'));
  await settle(30);
  out.push('E1_ROLLED_BACK=' + detectSec);      // 应回到 30
  out.push('E1_BUBBLES=' + BUBBLES.length);

  // --- E2: 200 但 saved:false，同样要回滚 ---
  BUBBLES.length = 0;
  setDetectSec(10);
  runOne();
  var r2 = FETCHES[FETCHES.length - 1];
  r2.resolve({ json: function() { return Promise.resolve({ detectIntervalSec: 10, saved: false }); } });
  await settle(30);
  out.push('E2_ROLLED_BACK=' + detectSec);
  out.push('E2_BUBBLES=' + BUBBLES.length);

  // --- E3: 乱序 —— 先发 GET(旧值)，再发 POST；GET 后到不能覆盖 ---
  BUBBLES.length = 0;
  detectSec = 30;
  syncDetectSec();                 // 发 GET
  var getRec = FETCHES[FETCHES.length - 1];
  setDetectSec(15);                // 再发 POST（序号更大）
  runOne();
  var postRec = FETCHES[FETCHES.length - 1];
  postRec.resolve({ json: function() { return Promise.resolve({ detectIntervalSec: 15, saved: true }); } });
  await settle(30);
  out.push('E3_AFTER_POST=' + detectSec);      // 15
  // 那个读到旧值的 GET 现在才回来
  getRec.resolve({ json: function() { return Promise.resolve({ detectIntervalSec: 30, saved: true }); } });
  await settle(30);
  out.push('E3_AFTER_STALE_GET=' + detectSec); // 必须还是 15
  out.push('E3_BUBBLES=' + BUBBLES.length);

  // --- E4: 连点「+」要合并成一个请求 ---
  var n0 = FETCHES.length;
  detectSec = 20;
  setDetectSec(21); setDetectSec(22); setDetectSec(23);
  runOne();
  await settle(10);
  out.push('E4_SENT=' + (FETCHES.length - n0));
  out.push('E4_VALUE=' + detectSec);

  console.log(out.join('\n'));
})();
""".replace('__CLAMP__', grab_fn('clampDetect')).replace('__SET__', set_block)
    # fetch 桩需要能手动 resolve/reject
    js = js.replace(
        "var FETCHES = [];\nvar BUBBLES = [];",
        "var FETCHES = [];\nvar BUBBLES = [];\n"
        "function fetch(url, opt) {\n"
        "  var rec = { url: url, opt: opt };\n"
        "  FETCHES.push(rec);\n"
        "  return new Promise(function(res, rej) { rec.resolve = res; rec.reject = rej; });\n"
        "}")
    out, err, rc = run_node(js)
    kv = dict(l.split('=', 1) for l in out.strip().splitlines() if '=' in l)
    check('E node 跑通', rc == 0 and out.strip() != '',
          'rc=%d out=%r err=%s' % (rc, out[:300], err[:400]))
    check('E1a 本地立刻渲染新值', kv.get('E1_LOCAL') == '5', kv.get('E1_LOCAL'))
    check('E1b debounce 后发了 1 个请求', kv.get('E1_FETCHES') == '1',
          kv.get('E1_FETCHES'))
    check('E1c 失败后回滚到 30', kv.get('E1_ROLLED_BACK') == '30',
          kv.get('E1_ROLLED_BACK'))
    check('E1d 失败后有气泡提示', kv.get('E1_BUBBLES') == '1', kv.get('E1_BUBBLES'))
    check('E2a saved:false 也回滚', kv.get('E2_ROLLED_BACK') == '30',
          kv.get('E2_ROLLED_BACK'))
    check('E2b saved:false 也有提示', kv.get('E2_BUBBLES') == '1',
          kv.get('E2_BUBBLES'))
    check('E3a POST 生效为 15', kv.get('E3_AFTER_POST') == '15',
          kv.get('E3_AFTER_POST'))
    check('E3b 迟到的旧 GET 不再覆盖（关键）',
          kv.get('E3_AFTER_STALE_GET') == '15', kv.get('E3_AFTER_STALE_GET'))
    check('E3c 没有误报失败气泡', kv.get('E3_BUBBLES') == '0', kv.get('E3_BUBBLES'))
    check('E4a 连点 3 次只发 1 个请求', kv.get('E4_SENT') == '1', kv.get('E4_SENT'))
    check('E4b 界面停在最后一次的值', kv.get('E4_VALUE') == '23',
          kv.get('E4_VALUE'))
except Exception as exc:
    check('E 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===================================================================
# F. extension.ts
# ===================================================================
section('F extension.ts')
check('F1 启动有并发守卫（launchingPet）',
      'let launchingPet' in ESRC and 'if (launchingPet) { return launchingPet; }' in ESRC)
check('F2 真正的实现挪进了 doLaunchStandalonePet',
      'async function doLaunchStandalonePet' in ESRC)
check('F3 守卫在 finally 里清空', 'launchingPet = undefined' in ESRC)
check('F4 回滚做引用比较（不再无条件覆盖）',
      'if (fileScanState === mine) { fileScanState = prev; }' in ESRC)
check('F5 引用比较前有 const mine',
      'const mine: FileScanState =' in ESRC)
check('F6 items 走了 clipMessage',
      'message: clipMessage(e.message, 200)' in ESRC)
check('F7 签名用裁剪后的 items',
      'items.map((e) => `${e.file}|${e.line}|${e.message}`)' in ESRC)
check('F8 getWebviewHtml 收 port 参数',
      'function getWebviewHtml(port: number)' in ESRC)
check('F9 面板文案插值端口', 'port ${port}' in ESRC)
check('F10 面板 dispose 时从 subscriptions 摘掉自己',
      'context.subscriptions.splice' in ESRC)
check('F11 supportedLanguageIds 已导出',
      'export const supportedLanguageIds' in ESRC)


# ===================================================================
# G/H. ui.html 其余
# ===================================================================
section('G 底板取样')
_code = '\n'.join(ln for ln in HSRC.splitlines()
                  if not ln.strip().startswith('//'))
check('G1 不再把 cardSamples 回退成 CARD_SAMPLES - 1（会永远锁不定）',
      'cardSamples = CARD_SAMPLES - 1' not in _code)
check('G2 有失败次数上限常量', 'CARD_MAX_FAILURES' in HSRC)
check('G3 失败封顶后锁定',
      'if (++cardFailures >= CARD_MAX_FAILURES) cardSamples = CARD_SAMPLES' in HSRC)

section('H 其余前端')
check('H1 reportSil 的 fetch 有 .catch',
      "}).catch(function() { /* 服务器不可达" in HSRC)
check('H2 edit-corpus 失败有气泡反馈',
      "没能打开语料文件" in HSRC)
check('H3 edit-corpus 看 ok === false', "d.ok === false" in HSRC)
check('H4 SSE 消息统一取安全 payload',
      "typeof msg.payload === 'object'" in HSRC)


# ===================================================================
# I/J
# ===================================================================
section('I main.py')
check('I1 AIRI_WIN_BG=off 时提前返回（不再白等 10 秒）',
      "if win_bg_mode() == 'off':" in MSRC)
check('I2 提前返回在 10 秒循环之前',
      0 <= MSRC.find("if win_bg_mode() == 'off':") < MSRC.find('bg_deadline = time.time() + 10'))

section('J 测试文件')
check('J1 从被测模块 import 真实集合',
      "import { supportedLanguageIds } from '../extension'" in TSRC)
check('J2 不再自己抄一份字面量',
      "const supportedLanguages = ['c', 'cpp', 'python'];" not in TSRC)
check('J3 断言的是真实集合', 'supportedLanguageIds.has(' in TSRC)


# ===================================================================
print('\n'.join(LINES))
print('=' * 60)
print('%d passed, %d failed' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
