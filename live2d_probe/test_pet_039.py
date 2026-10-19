# -*- coding: utf-8 -*-
"""回归：0.3.39 的四类修复

**A/B —— 跨源请求"声明多发少"能永久占死 handler 线程**（standalone.py）
    `_cors_guard` 排空 body 用的是 `while remaining > 0: self.rfile.read(...)`，
    既没夹上限也没设读超时。0.3.37 在这里写的注释只论证了"负数进不去循环"，
    漏了第二种挂死：**声明 999999 字节却一个字节都不发** —— 那个 read 在
    无超时的 socket 上安静地等到 EOF，线程被占住、403 永远写不出去。
    浏览器做不到（body 一定发全），raw socket / curl / 探测脚本做得到。
    实测（修复前）：连发 3 条并保持连接 → 进程线程数 2→5，客户端 3 秒读
    超时全部 TimeoutError（收到的根本不是 403）。

**C —— 端口没有范围校验**：AIRI_STANDALONE_PORT=0 让内核随机分配端口，
    绑定成功 → 启动守卫不触发 → 窗口照常弹出，但页面里注入的是
    http://127.0.0.1:0。扩展侧 0.3.37 已夹 [1,65535]，这边一直没夹。

**D —— /push 在请求线程里同步跑 LLM**：generate_response 是一次真实网络
    往返，而扩展 postJsonToPet 的超时是 500ms。慢于它 → 扩展 destroy 掉
    请求、判定 delivered=false → fileScanState 回滚、lastPushSignature 不
    提交 → 下一拍重推同一条（桌宠每周期复读同一句）。
    实测（修复前）：注入 sleep(3) 的生成函数，/push 耗时 3.00s。

**E —— 二次启动会清空正在运行的那个实例的日志**：_init_log() 以 'w' 打开
    日志，而"已有实例在跑"要等 main() 里绑定失败才看得出来 —— 第二个进程
    先把排障记录清空、再写一行 already running 退出。

**F —— 前端三处「意图与实现不匹配」**（ui.html，用 node 跑**真实源码**）：
    ① 非 Live2D 时 alphaAt 恒 0 → 点角色毫无反应（zoneAt 的 'body' 兜底到不了）
    ② errorAlert 的表情兜底是死代码（emotion 已兜成 'idle'，|| 'angry' 永不生效）
    ③ showBubble 的内联 animation 打穿 .live2d-active{animation:none}
    ④ silAlphaMode='off' 时命中判定却走那条刚被判不可信的 '2d'

判据一律用副作用（线程数、真实响应、耗时、队列长度、node 里真实调用次数）。
"""

import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PET = os.path.join(REPO, 'desktop_pet')
TMP = os.path.join(os.environ.get('TEMP', '/tmp'), 'airi_test_039')

# 测试隔离：必须在 import standalone 之前设（0.3.27 的教训）
os.makedirs(TMP, exist_ok=True)
LOG_PATH = os.path.join(TMP, 'run.log')
os.environ['AIRI_PET_LOG'] = LOG_PATH
os.environ['AIRI_PET_SETTINGS'] = os.path.join(TMP, 'settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(TMP, 'corpus.jsonc')
os.environ.pop('AIRI_STANDALONE_PORT', None)

sys.path.insert(0, PET)
sys.path.insert(0, HERE)
import standalone  # noqa: E402
from node_finder import find_node  # noqa: E402

PORT = 19894
LINES = []
_fail = 0


def out(s):
    print(s)
    LINES.append(s)


def check(label, ok, detail=''):
    global _fail
    if not ok:
        _fail += 1
    out('  [%s] %s%s' % ('PASS' if ok else 'FAIL', label,
                         ('  -- ' + detail) if detail else ''))


def flush():
    with io.open(os.path.join(HERE, 'pet_039_result.txt'), 'w',
                 encoding='utf-8') as f:
        f.write('\n'.join(LINES) + '\n')


SRC = io.open(os.path.join(PET, 'standalone.py'), encoding='utf-8').read()
HTML = io.open(os.path.join(PET, 'ui.html'), encoding='utf-8').read()

# =============================================================== A. 静态契约
out('=== A. 静态契约：排空 body 不能没有护栏 ===')
check('A1 有 _drain_body()', 'def _drain_body(self):' in SRC)
check('A2 _cors_guard 走 _drain_body', 'self._drain_body()' in SRC)
_i_opt = SRC.find('def do_OPTIONS')
_seg_opt = SRC[_i_opt:SRC.find('def ', _i_opt + 10)]
check('A3 do_OPTIONS 的 403 也走 _drain_body（否则 RST 冲掉 403）',
      '_drain_body()' in _seg_opt)
_i_drain = SRC.find('def _drain_body')
_seg_drain = SRC[_i_drain:SRC.find('def ', _i_drain + 10)]
check('A4 _drain_body 夹了 MAX_BODY_BYTES 上限',
      'MAX_BODY_BYTES' in _seg_drain)
check('A5 _drain_body 设了读超时',
      'settimeout(BODY_READ_TIMEOUT_SEC)' in _seg_drain)
check('A6 _drain_body 读完把超时恢复成阻塞', 'settimeout(None)' in _seg_drain)
# 穷举：除了 _read_body 与 _drain_body 内部各一处，不该再有别的裸 rfile.read
# （注释/文档串里提到的 `self.rfile.read(...)` 不算 —— 只数真正的赋值调用）
_naked = re.findall(r'=\s*self\.rfile\.read\(', SRC)
check('A7 真正的 rfile.read 只剩两处（_read_body / _drain_body 各一）',
      len(_naked) == 2, 'count=%d' % len(_naked))

out('')
out('=== A2. 端口与生成链路的静态契约 ===')
check('A8 有 resolve_port()', 'def resolve_port():' in SRC)
check('A9 resolve_port 夹了 [PORT_MIN, PORT_MAX]',
      'PORT_MIN <= port <= PORT_MAX' in SRC)
check('A10 main() 用 resolve_port() 而不是自己 int()',
      'port = resolve_port()' in SRC)
# `r = generate_response(` 只允许留在 _generate_async 内部（那是后台线程），
# do_POST 里一处都不能有
_i_ga = SRC.find('def _generate_async')
_seg_ga = SRC[_i_ga:SRC.find('\ndef ', _i_ga + 10)]
_i_dop = SRC.find('def do_POST')
_seg_dop = SRC[_i_dop:SRC.find('\ndef ', _i_dop + 10)]
check('A11 do_POST 里不再同步调 generate_response',
      'generate_response(' not in _seg_dop.replace('_generate_async(', ''),
      'do_POST 里仍有 generate_response')
check('A11b 生成调用确实搬进了 _generate_async 的后台线程体',
      'generate_response(' in _seg_ga)
check('A12 有 _generate_async()', 'def _generate_async(' in SRC)
check('A13 _generate_async 起的是后台线程',
      'threading.Thread(target=_run' in SRC)
_i_main = SRC.find("if __name__ == '__main__':")
_seg_main = SRC[_i_main:]
# ⚠️ 不能用 str.find 找 `_init_log()`：__main__ 的注释里就写着这个名字，
#    find 命中的是注释（0.3.39 实测：ping@397 init@44，看起来像"顺序没改"，
#    实际是注释在前）。只认**真正的调用行**。
_m_ping = re.search(r'^\s+if _ping_ok\(', _seg_main, re.M)
_m_init = re.search(r'^\s+_init_log\(\)', _seg_main, re.M)
check('A14 __main__ 里有 _ping_ok 守卫调用', _m_ping is not None)
check('A14b __main__ 里有 _init_log() 调用', _m_init is not None)
if _m_ping and _m_init:
    check('A14c 守卫（_ping_ok）在截断（_init_log）**之前**',
          _m_ping.start() < _m_init.start(),
          'ping@%d init@%d' % (_m_ping.start(), _m_init.start()))

# =============================================================== B. 实测挂死
out('')
out('=== B. 实测：跨源「声明多发少」能不能占死线程 ===')
ready = threading.Event()
threading.Thread(target=standalone.start_server, args=(PORT, ready),
                 daemon=True).start()
if not ready.wait(timeout=5.0):
    out('  [SKIP] 服务器没起来，B/C/D 段无法执行')
    flush()
    sys.exit(1)

base = threading.active_count()
socks = []
for _ in range(3):
    s = socket.create_connection(('127.0.0.1', PORT), timeout=10)
    socks.append(s)
    s.sendall(b'POST /push HTTP/1.1\r\n'
              b'Host: 127.0.0.1\r\n'
              b'Origin: http://evil.example.com\r\n'
              b'Content-Length: 999999\r\n'
              b'Connection: keep-alive\r\n'
              b'\r\n')
out('  发了 3 个"声明 999999 字节但一字节都不发"的跨源请求（连接保持）')
time.sleep(standalone.BODY_READ_TIMEOUT_SEC + 1.5)
peak = threading.active_count()
out('  等 %.1fs（读超时 %.1fs + 余量）后线程数 = %d（基线 %d）'
    % (standalone.BODY_READ_TIMEOUT_SEC + 1.5,
       standalone.BODY_READ_TIMEOUT_SEC, peak, base))
check('B1 读超时到了之后 handler 线程全部退出（不再永久占住）',
      peak <= base + 1, 'peak=%d base=%d' % (peak, base))

got403 = 0
for s in socks:
    s.settimeout(3.0)
    try:
        data = s.recv(64)
    except Exception as exc:
        data = b'<%s>' % type(exc).__name__.encode()
    if b'403' in data:
        got403 += 1
    try:
        s.close()
    except Exception:
        pass
out('  三条连接各自收到的响应中带 403 的条数 = %d / 3' % got403)
check('B2 干净的 403 真的发出去了（不是连接被重置/超时）', got403 == 3)

# =============================================================== C. 端口
out('')
out('=== C. resolve_port() 的取值范围 ===')
cases = [('0', 19876), ('65536', 19876), ('-1', 19876), ('99999999', 19876),
         ('abc', 19876), ('', 19876), ('20000', 20000), ('65535', 65535),
         ('1', 1)]
# ⚠️ 不能直接用 standalone.resolve_port —— 旧版本没有这个函数，一上来就
#    AttributeError 把 C/D/F 三段整段吞掉（0.3.39 验证判断力时第一次就
#    是这样：只看到 A/B 的 16 条 FAIL，以为 C/D/F "没事"，其实压根没跑）。
_rp = getattr(standalone, 'resolve_port', None)
if _rp is None:
    check('C standalone 暴露了 resolve_port()', False, '没有这个函数')
else:
    for raw, want in cases:
        os.environ['AIRI_STANDALONE_PORT'] = raw
        got = _rp()
        check('C AIRI_STANDALONE_PORT=%r -> %d' % (raw, want), got == want,
              'got=%d' % got)
    os.environ.pop('AIRI_STANDALONE_PORT', None)
    check('C 未设环境变量 -> 19876', _rp() == 19876)

# =============================================================== D. /push
out('')
out('=== D. /push 不能再被 LLM 往返拖住 ===')
_calls = []


def _slow_generate(ctx):
    _calls.append(time.time())
    time.sleep(2.0)
    return {'type': 'chatMessage',
            'payload': {'text': '(slow) 哼，又是你。', 'emotion': 'idle'}}


standalone.generate_response = _slow_generate
standalone._backend_available = True

before = len(standalone.sse_queue)
s = socket.create_connection(('127.0.0.1', PORT), timeout=20)
body = json.dumps({'type': 'diagnostics',
                   'payload': {'trigger': 'diagnostics', 'count': 2,
                               'language': 'python'}}).encode('utf-8')
s.sendall(b'POST /push HTTP/1.1\r\nHost: 127.0.0.1\r\n'
          b'Content-Type: application/json\r\n'
          b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
s.settimeout(20)
resp = b''
t0 = time.time()
try:
    while b'\r\n\r\n' not in resp:
        c = s.recv(4096)
        if not c:
            break
        resp += c
except Exception as exc:
    resp += ('<%s>' % type(exc).__name__).encode()
elapsed = time.time() - t0
s.close()
out('  /push 返回耗时 = %.3fs（扩展侧超时 500ms）' % elapsed)
check('D1 /push 在 500ms 内就返回', elapsed < 0.5, '%.3fs' % elapsed)
check('D2 返回的是 200', b'200' in resp[:20], repr(resp[:20]))

time.sleep(2.6)   # 等后台线程生成完
after = len(standalone.sse_queue)
out('  sse_queue %d -> %d，generate_response 被调用 %d 次'
    % (before, after, len(_calls)))
check('D3 生成确实发生了（不是被整个跳过）', len(_calls) == 1)
check('D4 生成结果进了队列（后台线程 push 到了）', after > before)
_txt = ' '.join(x[1] for x in standalone.sse_queue[-1:])
check('D5 队列里是那条慢生成的文本', '(slow)' in _txt, _txt[:80])

# =============================================================== F. 前端
out('')
out('=== F. 前端（node 里跑 ui.html 的真实函数源码） ===')
NODE = find_node()
if not NODE:
    out('  [SKIP] 找不到 node，F 段无法执行')
else:
    def grab(name, stub=None):
        """抠出函数源码；找不到就返回 stub（旧版本没有这个函数时用它占位，
        让后续用例能照常跑完并**红出来**，而不是整个脚本崩掉 —— 0.3.39
        验证判断力时第一次就是这么把 F 段整段吞掉的）。"""
        key = 'function %s(' % name
        i = HTML.find(key)
        if i < 0:
            return stub
        j = HTML.index('{', i)
        depth = 0
        for k in range(j, len(HTML)):
            if HTML[k] == '{':
                depth += 1
            elif HTML[k] == '}':
                depth -= 1
                if depth == 0:
                    return HTML[i:k + 1]
        return stub

    def grab_var(name, stub=''):
        for ln in HTML.splitlines():
            s = ln.strip()
            if s.startswith('var %s' % name) and '=' in s:
                return s
        return stub

    # ⚠️ ZONE_HEAD_MAX / ZONE_DESK_MIN 必须一起抠出来：zoneFromRatio 用它俩，
    #    少了就是 ReferenceError —— 而 handlePetClick 把 pet_click 那句包在
    #    try{}catch{} 里，异常被吞，表现成"CALLS=0"（看着像不命中，其实是
    #    分区函数根本没跑完）。0.3.39 写这段时就这么假 PASS 过一次。
    js = r"""
%(head)s
%(desk)s
var CALLS = [];
var live2d = { ready: false, failed: true, model: null, app: null };
var silAlphaMode = 'gl';
var clickBusy = false;
var window = { pywebview: { api: { pet_click: function(zone, ry) {
    CALLS.push({ zone: zone, ry: ry });
    return Promise.resolve({ text: '(stub)', emotion: 'idle' });
} } } };
var readAlphaGrid = function() { return null; };
function showBubble(t, e) {}
function setEmotion(e) {}
function rect(l, t, w, h) {
  return { left: l, top: t, right: l + w, bottom: t + h, width: w, height: h };
}
var charImg = { style: { display: 'block' }, src: 'file:///x.png',
                getBoundingClientRect: function() { return rect(0,0,300,480); } };
var cssChar = { classList: { contains: function() { return false; } } };
var charBox = { classList: { contains: function() { return false; } }, style: {} };

%(fb)s
%(alpha)s
%(ratio)s
%(zone)s
%(click)s

var out = [];
// ⚠️ handlePetClick 有 600ms 的防连点锁（clickBusy），而下面几条用例是
//    **同步**连着跑的 —— setTimeout 根本没机会触发。不手动复位的话，
//    第一条之后的用例全部因为 clickBusy 直接 return，看起来像"不命中"，
//    其实是**压根没跑**（0.3.39 写这段时就这么假 PASS 过一次）。
function reset() { CALLS.length = 0; clickBusy = false; }

// --- F1: 非 Live2D（图片立绘），点角色正中 ---
reset();
handlePetClick({ clientX: 150, clientY: 240 });
out.push('F1_CALLS=' + CALLS.length);
out.push('F1_ZONE=' + (CALLS.length ? CALLS[0].zone : 'none'));

// --- F2: 角色外接框之外（300x480 的框外）不该命中 ---
reset();
handlePetClick({ clientX: 900, clientY: 900 });
out.push('F2_CALLS=' + CALLS.length);

// --- F3: silAlphaMode='off' 时，canvas 内任意点都算命中 ---
reset();
live2d.ready = true;
live2d.app = { canvas: { getBoundingClientRect: function() {
    return rect(0, 0, 300, 480); } } };
silAlphaMode = 'off';
// 先单独确认分区函数自己能跑完（别让 try/catch 把 ReferenceError 吞成 0）
out.push('F3_ZONE=' + (function() {
    try { return zoneAt(10); } catch (e) { return 'ERR:' + e.message; }
})());
handlePetClick({ clientX: 10, clientY: 10 });
out.push('F3_CALLS=' + CALLS.length);
out.push('F3_ALPHA=' + alphaAt(10, 10));

// --- F4: silAlphaMode='gl' 但采样拿不到（readAlphaGrid 返回 null）不该命中 ---
reset();
silAlphaMode = 'gl';
handlePetClick({ clientX: 150, clientY: 240 });
out.push('F4_CALLS=' + CALLS.length);

// --- F4b: 采样给一个"整块都是角色"的可信 grid，gl 模式下应命中 ---
reset();
readAlphaGrid = function() {
  return { w: 4, h: 4, buf: (function() {
      var b = new Uint8Array(4 * 4 * 4);
      for (var i = 3; i < b.length; i += 4) b[i] = 255;
      return b; })() };
};
handlePetClick({ clientX: 150, clientY: 240 });
out.push('F4b_CALLS=' + CALLS.length);
readAlphaGrid = function() { return null; };

console.log(out.join('\n'));
""" % {'head': grab_var('ZONE_HEAD_MAX', 'var ZONE_HEAD_MAX = 0.45;'),
       'desk': grab_var('ZONE_DESK_MIN', 'var ZONE_DESK_MIN = 0.62;'),
       'fb': grab('fallbackCharacterEl',
                  'function fallbackCharacterEl() { return null; }'),
       'alpha': grab('alphaAt', 'function alphaAt() { return 0; }'),
       'ratio': grab('zoneFromRatio',
                     "function zoneFromRatio() { return 'body'; }"),
       'zone': grab('zoneAt', "function zoneAt() { return 'body'; }"),
       'click': grab('handlePetClick', 'function handlePetClick() {}')}

    check('F0 抠到的前端源码是完整的（5 个函数 + 2 个分区常量都在）',
          'function fallbackCharacterEl(' in HTML
          and 'function alphaAt(' in HTML
          and 'function handlePetClick(' in HTML
          and 'var ZONE_HEAD_MAX' in HTML
          and 'var ZONE_DESK_MIN' in HTML)

    tmp_js = os.path.join(TMP, '_click.js')
    io.open(tmp_js, 'w', encoding='utf-8').write(js)
    r = subprocess.run([NODE, tmp_js], capture_output=True, text=True,
                       timeout=60)
    kv = {}
    for ln in (r.stdout or '').splitlines():
        if '=' in ln:
            k, v = ln.split('=', 1)
            kv[k.strip()] = v.strip()
    if r.stderr.strip():
        out('  node stderr: ' + r.stderr.strip()[:400])
    out('  node 输出: %s' % kv)
    check('F1 非 Live2D 时点角色正中 → pet_click 真的被调用',
          kv.get('F1_CALLS') == '1', 'calls=%s' % kv.get('F1_CALLS'))
    check('F1b 分区落在 body（ry=0.5）', kv.get('F1_ZONE') == 'body',
          'zone=%s' % kv.get('F1_ZONE'))
    check('F2 点在角色外接框之外 → 不触发', kv.get('F2_CALLS') == '0',
          'calls=%s' % kv.get('F2_CALLS'))
    check('F3 silAlphaMode=off 时 canvas 内算命中（与矩形窗口一致）',
          kv.get('F3_CALLS') == '1', 'calls=%s alpha=%s zone=%s'
          % (kv.get('F3_CALLS'), kv.get('F3_ALPHA'), kv.get('F3_ZONE')))
    check('F3b 分区函数自己没抛异常（防止 try/catch 把错误吞成"不命中"）',
          not (kv.get('F3_ZONE') or '').startswith('ERR'),
          'zone=%s' % kv.get('F3_ZONE'))
    check('F4 采样不可用（grid=null）时不命中', kv.get('F4_CALLS') == '0',
          'calls=%s' % kv.get('F4_CALLS'))
    check('F4b 采样可信且该点 alpha=255 时命中（没有把功能改坏）',
          kv.get('F4b_CALLS') == '1', 'calls=%s' % kv.get('F4b_CALLS'))

    # --- F5: errorAlert 的表情兜底（跑 ui.html 里的真实表达式）---
    m_expr = re.search(r"var emotion = (.+?);\n", HTML)
    m_case = re.search(r"case 'errorAlert':.*?setEmotion\((.+?)\);", HTML,
                       re.S)
    if not m_expr or not m_case:
        out('  [SKIP] 没抠到 emotion 表达式 / errorAlert 分支')
    else:
        js2 = ("var msg = { type: 'errorAlert', payload: { text: 'boom' } };\n"
               "var emotion = %s;\n"
               "console.log(String(%s));\n"
               % (m_expr.group(1), m_case.group(1)))
        tmp2 = os.path.join(TMP, '_emo.js')
        io.open(tmp2, 'w', encoding='utf-8').write(js2)
        r2 = subprocess.run([NODE, tmp2], capture_output=True, text=True,
                            timeout=60)
        val = (r2.stdout or '').strip()
        out('  ui.html 真实表达式：var emotion = %s  /  setEmotion(%s)'
            % (m_expr.group(1), m_case.group(1)))
        check('F5 不带 emotion 的 errorAlert → angry（不是 idle）',
              val == 'angry', 'got=%r' % val)
        if r2.stderr.strip():
            out('  node stderr: ' + r2.stderr.strip()[:300])

    # --- F6/F7: showBubble 的内联 animation ---
    i_anim = HTML.find("charBox.style.animation = 'idleSway")
    seg = HTML[max(0, i_anim - 400):i_anim] if i_anim > 0 else ''
    check('F6 内联 animation 仍在（非 Live2D 的轻推没被删掉）', i_anim > 0)
    check('F7 但它被 live2d-active 判断包住了（不再打穿 animation:none）',
          'live2d-active' in seg, seg[-120:].replace('\n', ' | '))

# =============================================================== 收尾
out('')
out('结果：%s（失败 %d 条）' % ('全部通过' if _fail == 0 else '有失败', _fail))
flush()
sys.exit(1 if _fail else 0)
