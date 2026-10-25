# -*- coding: utf-8 -*-
"""0.3.43 回归测试。

覆盖本轮修的 19 处（每条都在 0.3.42 的代码上坐实过触发路径）：

  A 段  common.py 窗口层 / 资产服务器
        A1 立绘必须走**同源** URL（以前注入 file://，而页面是 http://127.0.0.1，
           WebView2 禁止 http 文档加载 file: 子资源 → 立绘 100% 加载不出来，
           全程零日志）
        A2 AIRI_ASSET_PORT 越界（99999/-1）不能抛 OverflowError（它不是
           OSError，穿透后进程静默死掉）
        A3 资产服务器必须关掉 allow_reuse_address（Windows 的 SO_REUSEADDR
           允许重复绑定 → "端口占用"分支永远不可达）
        A4 standalone 的底色循环前必须有 AIRI_WIN_BG=off 短路（0.3.41 只修了
           main.py，主入口漏了 → 白等 10 秒 + 假 WARNING）
        A5 get_position 失败必须返回 None（返回 [0,0] 会让拖拽跳到左上角）
        A6 CombineRgn 的 NULLREGION(1) 不算成功（否则空 region 装上窗口：
           不可见 + 点不到 + 退不出去）
        A7 exit_app 的失败必须留痕，且必须真的能退出
  B 段  standalone.py
        B1 SSE 帧必须"一定能 utf-8 编码"（孤立代理字符会让桌宠永久失声）
        B2 /push 解析失败分支也要截断 body（1MB 原文入队）
        B3 _persist_locked：落盘失败回滚 + 写前合并磁盘
        B4 log() 要兜 UnicodeError（它不是 OSError，写日志本身会打死线程）
  C 段  extension.ts
        C1 openCorpusSeq 读到 Infinity 不能让「编辑语料」永久失效
        C2 面板打开时要打首帧快照（否则一直显示"没有错误"这句假话）
  D 段  ui.html
        D1 气泡溢出时先清"正在淡出"的，别删刚出现的
        D2 窗口失焦要收右键菜单（否则切回来点角色会误触「退出」）
        D3 连点后的失败回滚要回到 burst 起点（不是中间值）
        D4 「编辑语料」要有在途守卫（180ms 过渡期内双击会发两次）
        D5 底板采样失败不许消耗采样预算（否则"重试 20 次"是死代码）
        D6 clipBubbleText 不许切出半个代理对

⚠️ 测试自身的坑（本项目踩过，逐条防住）
  · 每段包在自己的 try 里 —— 旧版没有的符号会抛 AttributeError/NameError，
    把后面整段吞掉（用例数突然变少就是信号）。
  · 抠源码时模块级常量要一起抠。
  · 覆盖 setTimeout 一律走 globalThis（函数声明会 hoist，见记忆 #8）。
  · 判据用副作用（队列内容、文件内容、调用记录），不用状态码。
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
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='airi_test043_')
# ⚠️ 必须在 import standalone **之前**设：否则测试会往生产日志/设置里写
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


def skip(name, why=''):
    LINES.append('SKIP  %s   %s' % (name, why))


HTML = REPO / 'desktop_pet' / 'ui.html'
EXTS = REPO / 'src' / 'extension.ts'
STANDALONE = REPO / 'desktop_pet' / 'standalone.py'
COMMON = REPO / 'desktop_pet' / 'common.py'

HSRC = io.open(str(HTML), encoding='utf-8').read()
ESRC = io.open(str(EXTS), encoding='utf-8').read()
SSRC = io.open(str(STANDALONE), encoding='utf-8').read()
CSRC = io.open(str(COMMON), encoding='utf-8').read()


def grab(src, start, end):
    i = src.find(start)
    if i < 0:
        return ''
    j = src.find(end, i + len(start))
    if j < 0:
        return ''
    return src[i:j + len(end)]


def run_node(js, timeout=60):
    d = Path(tempfile.mkdtemp(prefix='airi043_'))
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


def http_get(url, timeout=5):
    """返回 (status, headers, body_bytes)；失败返回 (None, {}, 异常串)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), b''
    except Exception as exc:
        return None, {}, '%s: %s' % (type(exc).__name__, exc)


# ===========================================================================
# A 段：common.py
# ===========================================================================
section('A1 立绘必须是同源 URL（不是 file://）')

try:
    import common  # noqa: E402

    fn = grab(CSRC, 'def _character_url(p: Path) -> str:', '\n\ndef load_html(')
    check('A1-0 抠到 _character_url', bool(fn))

    real = common._SCRIPT_DIR / 'assets' / 'character.png'
    if real.is_file():
        got = common._character_url(real)
        check('A1-1 assets/ 下的立绘改成同源路由（关键）',
              got == '/char/character.png', 'got=%r' % got)
    else:
        skip('A1-1 仓库里没有 assets/character.png')

    # 不在 assets/ 下的立绘 → 内联 data URI（同样不产生 file: 请求）
    tmp_img = Path(TMP) / 'mine.png'
    tmp_img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 16)
    got2 = common._character_url(tmp_img)
    check('A1-2 外部路径立绘内联成 data: URI',
          got2.startswith('data:image/png;base64,'), 'got=%r' % got2[:60])
    check('A1-3 两种情况下都不再产出 file://',
          'file://' not in got2 and
          (not real.is_file() or 'file://' not in common._character_url(real)))

    # 真起一个资产服务器，验证路由可用 + 越界被拒
    url, port = common.start_asset_server('<html>x</html>', 0)
    check('A1-4 资产服务器起来了', url is not None, 'url=%r' % url)
    if url:
        base = 'http://127.0.0.1:%d' % port
        if real.is_file():
            st, hdr, body = http_get(base + '/char/character.png')
            check('A1-5 GET /char/character.png → 200 + image/png（关键）',
                  st == 200 and 'image/png' in hdr.get('Content-Type', ''),
                  'st=%r ctype=%r' % (st, hdr.get('Content-Type')))
            check('A1-6 返回的字节数与磁盘一致',
                  len(body) == real.stat().st_size,
                  '%d vs %d' % (len(body), real.stat().st_size))
        st, _, _ = http_get(base + '/char/../common.py')
        check('A1-7 越界路径 403（不能顺着 /char/ 读源码）', st == 403, 'st=%r' % st)
        st, _, _ = http_get(base + '/char/live2d%2Fmodel.png')
        check('A1-8 %2F 编码的子路径也 403（unquote 后再判斜杠）',
              st == 403, 'st=%r' % st)
        st, _, _ = http_get(base + '/char/not-there.png')
        check('A1-9 不存在的立绘 404（不是 500/断连）', st == 404, 'st=%r' % st)
except Exception as exc:
    check('A1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A2 资产端口越界不能抛 OverflowError')

try:
    raised = None
    url, port = None, 0
    try:
        url, port = common.start_asset_server('<html>y</html>', 99999)
    except Exception as exc:
        raised = '%s: %s' % (type(exc).__name__, exc)
    check('A2-1 越界端口不抛异常（OverflowError 不是 OSError）',
          raised is None, raised or 'OK')
    check('A2-2 回落成内核分配的可用端口',
          url is not None and 0 < port <= 65535, 'url=%r port=%r' % (url, port))

    raised2 = None
    try:
        common.start_asset_server('<html>z</html>', -1)
    except Exception as exc:
        raised2 = '%s: %s' % (type(exc).__name__, exc)
    check('A2-3 负数端口同样不抛', raised2 is None, raised2 or 'OK')
except Exception as exc:
    check('A2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A3 资产服务器必须关掉地址复用')

try:
    check('A3-1 _AssetServer.allow_reuse_address 为 False（关键）',
          common._AssetServer.allow_reuse_address is False,
          'got=%r' % common._AssetServer.allow_reuse_address)
    url_a, port_a = common.start_asset_server('<html>a</html>', 0)
    url_b, port_b = common.start_asset_server('<html>b</html>', port_a)
    check('A3-2 同一端口第二次绑定必须失败（不能静默共享）',
          url_b is None and port_b == 0, 'url_b=%r port=%r' % (url_b, port_b))
except Exception as exc:
    check('A3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A4 standalone 的底色循环要有 off 短路')

try:
    dl = SSRC.find('bg_deadline = time.time() + 10')
    off = SSRC.find("if win_bg_mode() == 'off':")
    check('A4-0 找到底色循环与 off 短路', dl > 0 and off > 0, 'dl=%d off=%d' % (dl, off))
    check('A4-1 off 短路排在循环之前（关键：否则白跑 40×0.25s + 假 WARNING）',
          0 < off < dl, 'off=%d dl=%d' % (off, dl))
    # 反证：短路之后到循环之间，不能又把 off 的情况漏掉
    between = SSRC[off:dl]
    check('A4-2 短路段里确实 return（不是只打一行日志）',
          re.search(r'win_bg_mode\(\) == \'off\':\s*\n\s*log\(', between) is not None
          and 'return' in between, 'between=%r' % between[:200])
except Exception as exc:
    check('A4 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A5 get_position 失败必须返回 None')

try:
    import common as _c

    api = _c.WindowAPI.__new__(_c.WindowAPI)
    api._window = None
    api._move_calls = 0
    logs = []
    api._log_input = lambda s: logs.append(s)
    got_none = api.get_position()
    check('A5-1 没有窗口时返回 None（不是 [0,0]）',
          got_none is None, 'got=%r' % (got_none,))

    class _Boom(object):
        @property
        def x(self):
            raise RuntimeError('window destroyed')
    api._window = _Boom()
    got2 = api.get_position()
    check('A5-2 读坐标抛异常时也返回 None（否则拖拽跳到左上角）',
          got2 is None, 'got=%r' % (got2,))
    check('A5-3 两次都留了日志', len(logs) >= 2, 'logs=%r' % logs[:4])

    class _Ok(object):
        x, y = 0, 0
    api._window = _Ok()
    got3 = api.get_position()
    check('A5-4 真实的 (0,0) 仍然原样返回（不能把 0 当失败）',
          got3 == [0, 0], 'got=%r' % (got3,))
except Exception as exc:
    check('A5 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A6 NULLREGION 不算合并成功')

try:
    i = CSRC.find('    def _merge(h_piece):')
    j = CSRC.find('        return rc2 >= 2', i)
    seg = CSRC[i:j + len('        return rc2 >= 2')] if (i >= 0 and j > i) else ''
    check('A6-0 抠到 _merge（含新的返回判据）', bool(seg), 'len=%d' % len(seg))
    body = '\n'.join(l[4:] if l.startswith('    ') else l
                     for l in seg.splitlines())

    class _Gdi(object):
        def __init__(self, rc):
            self.rc = rc
            self.deleted = []

        def CombineRgn(self, *a):
            return self.rc

        def DeleteObject(self, h):
            self.deleted.append(h)
            return 1

    # ⚠️ 模块级常量必须一起给（记忆里的坑 #2：只抠函数 → NameError 被吞）
    ns = {'h_total': 77, 'RGN_OR': 2}
    exec(body, ns)
    for rc, want, label in ((1, False, 'NULLREGION(1) 空区域'),
                            (2, True, 'SIMPLEREGION(2)'),
                            (3, True, 'COMPLEXREGION(3)'),
                            (0, False, 'ERROR(0)')):
        g = _Gdi(rc)
        ns['gdi32'] = g
        got = ns['_merge'](55)
        check('A6-1 %s → %s' % (label, '成功' if want else '失败'),
              got is want, 'got=%r' % (got,))
        check('A6-2 %s 时 piece 句柄仍被释放' % label, g.deleted == [55],
              'deleted=%r' % g.deleted)
except Exception as exc:
    check('A6 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('A7 exit_app 失败要留痕且必须能退出')

try:
    api = _c.WindowAPI.__new__(_c.WindowAPI)
    api._move_calls = 0
    logs = []
    api._log_input = lambda s: logs.append(s)
    exits = []

    class _BadWin(object):
        def destroy(self):
            raise RuntimeError('cannot destroy')
    api._window = _BadWin()

    real_exit = os._exit

    def _fake_exit(code):
        exits.append(code)
        raise SystemExit(code)      # 模拟"进程到此为止"，好让调用栈收住
    os._exit = _fake_exit
    try:
        try:
            api.exit_app()
        except SystemExit:
            pass
    finally:
        os._exit = real_exit

    check('A7-1 destroy 失败时留了日志（不再是裸 pass）',
          any('exit_app' in s and 'FAILED' in s for s in logs),
          'logs=%r' % logs[:3])
    check('A7-2 destroy 失败时兜底硬退出（用户按了退出就必须退出）',
          exits == [0], 'exits=%r' % exits)

    # 正常路径：destroy 成功时不许硬退出
    exits.clear()
    logs.clear()

    class _GoodWin(object):
        def destroy(self):
            self.called = True
    api._window = _GoodWin()
    os._exit = _fake_exit
    try:
        api.exit_app()
    finally:
        os._exit = real_exit
    check('A7-3 正常路径不触发硬退出', exits == [], 'exits=%r' % exits)
except Exception as exc:
    check('A7 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# B 段：standalone.py
# ===========================================================================
section('B1 SSE 帧必须一定能 utf-8 编码')

try:
    import standalone  # noqa: E402

    check('B1-0 存在 _sse_safe', hasattr(standalone, '_sse_safe'))
    bad = '{"t":"\ud800"}'
    safe = standalone._sse_safe(bad)
    ok = True
    try:
        safe.encode('utf-8')
    except UnicodeEncodeError:
        ok = False
    check('B1-1 含孤立代理的帧净化后能编码（关键）', ok, 'safe=%r' % safe[:40])
    check('B1-2 正常文本一个字节都不动',
          standalone._sse_safe('中文abc') == '中文abc')

    standalone.sse_queue.clear()
    standalone.push_message('chatMessage', 'ok\ud800bad', 'idle')
    frame = standalone.sse_queue[-1][1]
    ok2 = True
    try:
        frame.encode('utf-8')
    except UnicodeEncodeError:
        ok2 = False
    check('B1-3 入队的帧可编码（以前坏帧留在队列里 → 重连再炸 → 永久失声）',
          ok2, 'frame=%r' % frame[:80])
except Exception as exc:
    check('B1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B2 /push 解析失败也要截断')

BASE = None
try:
    for _p in range(21900, 22000):
        _ready = threading.Event()
        threading.Thread(target=standalone.start_server, args=(_p, _ready),
                         daemon=True).start()
        if _ready.wait(3):
            BASE = _p
            break
    check('B0 测试服务器已起', BASE is not None, 'port=%r' % BASE)
except Exception as exc:
    check('B0 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))

try:
    if BASE:
        standalone.sse_queue.clear()
        payload = b'A' * 4000 + b'\xff\xfe not json'
        req = urllib.request.Request(
            'http://127.0.0.1:%d/push' % BASE, data=payload, method='POST',
            headers={'Content-Type': 'text/plain',
                     'Origin': 'http://127.0.0.1:%d' % BASE})
        with urllib.request.urlopen(req, timeout=5) as r:
            st = r.status
        check('B2-1 请求被接受（不是 400/500）', st == 200, 'st=%r' % st)
        txt = ''
        for _seq, d in reversed(standalone.sse_queue):
            obj = json.loads(d)
            if obj.get('type') == 'chatMessage':
                txt = obj['payload']['text']
                break
        check('B2-2 解析失败分支的文本被截到 500（以前最大 1MB 原样入队）',
              0 < len(txt) <= 500, 'len=%d' % len(txt))
    else:
        skip('B2', '测试服务器未起来')
except Exception as exc:
    check('B2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B3 _persist_locked：回滚 + 磁盘合并')

try:
    check('B3-0 存在 _persist_locked', hasattr(standalone, '_persist_locked'))
    real_save = standalone.save_settings
    backup = dict(standalone.settings)

    # ① 落盘失败 → 内存回滚
    standalone.settings['detectIntervalSec'] = 30
    standalone.save_settings = lambda d: False
    ok = standalone._persist_locked('detectIntervalSec', 45)
    check('B3-1 落盘失败返回 False', ok is False, 'ok=%r' % ok)
    check('B3-2 落盘失败后内存回滚到 30（不是 45）',
          standalone.settings['detectIntervalSec'] == 30,
          'got=%r' % standalone.settings['detectIntervalSec'])

    # ② 落盘成功 → 以磁盘为准合并其它键（手改的值不能被快照覆盖）
    standalone.save_settings = real_save
    io.open(standalone.SETTINGS_PATH, 'w', encoding='utf-8').write(
        json.dumps({'detectIntervalSec': 12, 'openCorpusSeq': 7}))
    ok2 = standalone._persist_locked('detectIntervalSec', 45)
    check('B3-3 落盘成功返回 True', ok2 is True, 'ok=%r' % ok2)
    check('B3-4 本次改的键生效', standalone.settings['detectIntervalSec'] == 45,
          'got=%r' % standalone.settings['detectIntervalSec'])
    check('B3-5 磁盘上手改的其它键被保留（openCorpusSeq=7）',
          standalone.settings.get('openCorpusSeq') == 7,
          'got=%r' % standalone.settings.get('openCorpusSeq'))
    disk = json.load(io.open(standalone.SETTINGS_PATH, encoding='utf-8'))
    check('B3-6 写回磁盘的也是合并后的值',
          disk.get('detectIntervalSec') == 45 and disk.get('openCorpusSeq') == 7,
          'disk=%r' % disk)

    # ③ openCorpusSeq 走同一条路（0.3.42 只修了 detectIntervalSec）
    standalone.settings['openCorpusSeq'] = 3
    standalone.save_settings = lambda d: False
    standalone._persist_locked('openCorpusSeq', 4)
    check('B3-7 openCorpusSeq 落盘失败同样回滚（否则扩展会自己打开语料）',
          standalone.settings['openCorpusSeq'] == 3,
          'got=%r' % standalone.settings['openCorpusSeq'])
    standalone.save_settings = real_save
    standalone.settings.clear()
    standalone.settings.update(backup)
except Exception as exc:
    check('B3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('B4 log() 要兜 UnicodeError')

try:
    before = os.path.getsize(standalone.LOG_PATH) if os.path.exists(
        standalone.LOG_PATH) else 0
    raised = None
    try:
        standalone.log('boom \ud800 tail')
    except Exception as exc:
        raised = '%s: %s' % (type(exc).__name__, exc)
    check('B4-1 含孤立代理的日志不抛（UnicodeError 不是 OSError）',
          raised is None, raised or 'OK')
    after = os.path.getsize(standalone.LOG_PATH) if os.path.exists(
        standalone.LOG_PATH) else 0
    check('B4-2 日志仍然写进去了（不是静默吞掉整行）', after > before,
          'before=%d after=%d' % (before, after))
except Exception as exc:
    check('B4 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# C 段：extension.ts
# ===========================================================================
section('C1 openCorpusSeq 的 Infinity')

try:
    init = grab(ESRC, 'const initialCorpusSeq = Number(readCorpusOpenSeq());',
                '? initialCorpusSeq : 0;')
    check('C1-0 找到初始化那段', bool(init), 'seg=%r' % init[:120])
    check('C1-1 用 Number.isFinite 过滤（Infinity || 0 仍是 Infinity）',
          'Number.isFinite(initialCorpusSeq)' in init, 'seg=%r' % init)
    check('C1-2 不再有 `Number(readCorpusOpenSeq()) || 0` 这种写法',
          'Number(readCorpusOpenSeq()) || 0' not in ESRC)

    js = r"""
function pick(v) {
  var s = Number(v);
  return Number.isFinite(s) && s > 0 ? s : 0;
}
var out = [];
out.push('INF=' + pick(Infinity));
out.push('NAN=' + pick(NaN));
out.push('OK=' + pick(5));
console.log(out.join('\n'));
"""
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('C1-3 node 复现：Infinity → 0（旧写法会留着 Infinity）',
          rc == 0 and kv.get('INF') == '0', 'rc=%d kv=%r err=%s' % (rc, kv, err[:200]))
    check('C1-4 合法值照旧', kv.get('OK') == '5', 'kv=%r' % kv)
except Exception as exc:
    check('C1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('C2 面板首帧快照')

try:
    panel_fn = grab(ESRC, 'function createOrShowAssistantPanel(',
                    '\n}\n')
    check('C2-0 抠到 createOrShowAssistantPanel', bool(panel_fn))
    check('C2-1 创建面板时注册了 ready 处理（否则首帧发不出去）',
          "'ready'" in panel_fn and 'onDidReceiveMessage' in panel_fn,
          'block=%r' % panel_fn[-260:])
    check('C2-2 有 pushDiagnosticsSnapshot 并且按 ready 调用',
          'pushDiagnosticsSnapshot()' in panel_fn)
    helper = grab(ESRC, 'function pushDiagnosticsSnapshot(): void {', '\n}\n')
    check('C2-3 快照走的是同一份裁剪逻辑（collectErrors + buildDiagnosticsMessage）',
          'collectErrors' in helper and 'buildDiagnosticsMessage' in helper,
          'helper=%r' % helper)
    check('C2-4 面板 HTML 里真的发了 ready 握手',
          "vscode.postMessage({type:'ready'})" in ESRC)
except Exception as exc:
    check('C2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


# ===========================================================================
# D 段：ui.html
# ===========================================================================
section('D1 气泡溢出时先清淡出中的')

try:
    fb = grab(HSRC, '    function fitBubbles() {', '\n    }\n')
    check('D1-0 抠到 fitBubbles', bool(fb), 'len=%d' % len(fb))
    js = r"""
var removed = [];
function mkEl(id, removing) {
  return {
    id: id, gone: false,
    dataset: removing ? { removing: '1' } : {},
    style: {},
    remove: function () { this.gone = true; removed.push(this.id); }
  };
}
var A = mkEl('A', true);    // 正在淡出（450ms，仍占布局高度）
var B = mkEl('B', false);
var C = mkEl('C', false);
var ALL = [A, B, C];
var bubbleArea = {
  // 3 个块（含淡出中的 A）→ 顶边溢出；删掉一个后就不溢出了
  getBoundingClientRect: function () {
    var n = 0;
    for (var i = 0; i < ALL.length; i++) { if (!ALL[i].gone) n++; }
    return { top: n >= 3 ? 0 : 10, bottom: 200 };
  },
  querySelector: function (sel) {
    if (sel.indexOf('data-removing') >= 0 && sel.indexOf(':not') < 0) {
      for (var i = 0; i < ALL.length; i++) {
        if (!ALL[i].gone && ALL[i].dataset.removing) return ALL[i];
      }
      return null;
    }
    return null;
  },
  querySelectorAll: function () {
    var r = [];
    for (var i = 0; i < ALL.length; i++) {
      if (!ALL[i].gone && !ALL[i].dataset.removing) r.push(ALL[i]);
    }
    return r;
  }
};
__FB__
fitBubbles();
console.log('REMOVED=' + removed.join(','));
"""
    js = js.replace('__FB__', fb)
    out, err, rc = run_node(js)
    kv = kv_of(out)
    check('D1-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    check('D1-2 溢出时先删"正在淡出"的那条（A）',
          kv.get('REMOVED', '').split(',')[0] == 'A', 'REMOVED=%r' % kv.get('REMOVED'))
    check('D1-3 刚出现的第二条（B）不许被硬删（关键：以前会被删，只留半句话）',
          'B' not in kv.get('REMOVED', '').split(','),
          'REMOVED=%r' % kv.get('REMOVED'))
except Exception as exc:
    check('D1 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D2 失焦要收菜单')

try:
    blk = grab(HSRC, "window.addEventListener('blur', function() {", '\n    });')
    check('D2-0 抠到 blur 回调', bool(blk), 'blk=%r' % blk[:100])
    check('D2-1 回调里保留拖拽复位（别为了收菜单把老功能删了）',
          'endDrag(null)' in blk)
    check('D2-2 回调里收右键菜单（关键：否则切回来点角色会误触「退出」）',
          'hideMenu()' in blk and 'contextMenu' in blk, 'blk=%r' % blk[-200:])

    js = r"""
var HID = 0, END = 0;
function hideMenu() { HID++; }
function endDrag(x) { END++; }
var contextMenu = { classList: { contains: function () { return true; } } };
var dragging = false;
var window = { _h: {}, addEventListener: function (t, f) { this._h[t] = f; } };
__BLK__
window._h['blur']();
console.log('HID=' + HID);
console.log('END=' + END);
"""
    out, err, rc = run_node(js.replace('__BLK__', blk))
    kv = kv_of(out)
    check('D2-3 node 行为：失焦时 hideMenu 真被调用了',
          rc == 0 and kv.get('HID') == '1',
          'rc=%d kv=%r err=%s' % (rc, kv, err[:200]))
    check('D2-4 没有拖拽时不该误调 endDrag', kv.get('END') == '0', 'kv=%r' % kv)
except Exception as exc:
    check('D2 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D3 连点后的回滚要回到"最后确认落盘"的值')

try:
    seg = grab(HSRC, '    function setDetectSec(sec) {', '\n    }\n')
    check('D3-0 抠到 setDetectSec', bool(seg), 'len=%d' % len(seg))
    # ⚠️ 0.3.44 改了语义：0.3.43 用的 burstPrev（burst 起点）在"停 >150ms
    # 再点"时记的是**没落盘的乐观值**，回滚会落到磁盘上从未生效的数。现在
    # 基准是 diskDetectSec = 最后一次服务端确认的值。本用例同步成新语义。
    check('D3-1 回滚基准是"最后确认落盘"的值', 'diskDetectSec' in HSRC)
    check('D3-2 diskDetectSec 有模块级声明',
          'var diskDetectSec = detectSec;' in HSRC)

    js = r"""
var detectSec = 30, detectMin = 1, detectMax = 60;
var pendingDetectSec = null, detectPostTimer = null;
var settingsSeq = 0;
var diskDetectSec = 30;
var CAPTURED = [];
function clampDetect(v) { return Math.max(detectMin, Math.min(detectMax, v)); }
function renderDetectLabels() {}
function repushMenuRegion() {}
// 覆盖 setTimeout：只记回调，手动跑（函数声明会 hoist，必须走 globalThis）
globalThis.setTimeout = function (fn, ms) { globalThis.__T = fn; return 1; };
globalThis.clearTimeout = function () { globalThis.__T = null; };
function postDetectSec(target, prev) { CAPTURED.push([target, prev]); }
__SEG__
setDetectSec(31);
setDetectSec(32);
setDetectSec(33);
if (globalThis.__T) globalThis.__T();
console.log('CAP=' + (CAPTURED.length ? CAPTURED[0][0] + ',' + CAPTURED[0][1] : 'none'));
// 每次点击都要作废在途响应（GET/POST 都可能还没回来）
console.log('SEQ=' + settingsSeq);
"""
    out, err, rc = run_node(js.replace('__SEG__', seg))
    kv = kv_of(out)
    check('D3-3 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    check('D3-4 提交的是最后一次的值 33，回滚基点是**已落盘的 30**（关键）',
          kv.get('CAP') == '33,30', 'CAP=%r' % kv.get('CAP'))
    check('D3-5 三次点击各自作废在途响应（settingsSeq 涨 3）',
          kv.get('SEQ') == '3', 'SEQ=%r' % kv.get('SEQ'))
except Exception as exc:
    check('D3 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D4 编辑语料要有在途守卫')

try:
    check('D4-0 有 corpusOpenBusy 变量', 'var corpusOpenBusy' in HSRC)
    h = HSRC.find("if (a === 'edit-corpus')")
    seg = HSRC[h:h + 1800] if h >= 0 else ''
    check('D4-1 找到 edit-corpus 分支', bool(seg))
    check('D4-2 分支开头就拦在途请求（关键：180ms 过渡期内双击会发两次）',
          'if (corpusOpenBusy)' in seg and 'corpusOpenBusy = true' in seg,
          'seg=%r' % seg[:220])
    # ⚠️ 0.3.44 改了释放方式：以前在 .then/.catch 里清，可那个 POST 的 RTT
    # 只有 10~20ms，守卫比它要防的 180ms 菜单过渡窗口还短。现在靠固定时间窗。
    check('D4-3 用固定时间窗释放（不是请求回调——RTT 只有 20ms）',
          'corpusOpenBusy = false; }, 700' in seg,
          'seg=%r' % seg[:260])
    check('D4-4 回调里不再提前释放（否则守卫形同虚设）',
          seg.count('corpusOpenBusy = false') == 1,
          'count=%d' % seg.count('corpusOpenBusy = false'))
except Exception as exc:
    check('D4 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D5 底板采样失败不许消耗预算')

try:
    seg = grab(HSRC, '    function updateCharCard(rects) {', '\n    }\n')
    check('D5-0 抠到 updateCharCard', bool(seg), 'len=%d' % len(seg))

    js = r"""
var CHAR_CARD = 'square';
var CARD_PAD = 8, CARD_PAD_ALL = 14;
var CARD_SAMPLES = 6, CARD_MAX_FAILURES = 20;
var cardUnion = null, cardSamples = 0, cardFailures = 0;
var charCard = { style: {}, classList: { add: function () {}, remove: function () {} } };
var window = { innerWidth: 0, innerHeight: 0 };
var document = { documentElement: { clientWidth: 0, clientHeight: 0 },
                 querySelector: function () { return null; } };
__SEG__
for (var i = 0; i < 25; i++) { updateCharCard([[10, 10, 100, 100]]); }
console.log('FAILURES=' + cardFailures);
console.log('SAMPLES=' + cardSamples);
"""
    out, err, rc = run_node(js.replace('__SEG__', seg))
    kv = kv_of(out)
    check('D5-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:400]))
    check('D5-2 失败 25 次真的累到 20 次才放弃（关键：以前第 1 次就永久锁定）',
          int(kv.get('FAILURES') or 0) >= 20, 'FAILURES=%r' % kv.get('FAILURES'))
    check('D5-3 放弃后锁在 CARD_SAMPLES（不再无限重跑）',
          kv.get('SAMPLES') == '6', 'SAMPLES=%r' % kv.get('SAMPLES'))
except Exception as exc:
    check('D5 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


section('D6 clipBubbleText 不许切出半个代理对')

try:
    seg = grab(HSRC, '    function clipBubbleText(text) {', '\n    }\n')
    check('D6-0 抠到 clipBubbleText', bool(seg), 'len=%d' % len(seg))
    js = r"""
var MAX_BUBBLE_CHARS = 200;
__SEG__
function lone(s) {
  // 孤立高位代理 / 孤立低位代理都算坏
  return /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(s) ||
         /(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s);
}
// 第 200 个码元正好是 emoji 的高位代理
var text = 'x'.repeat(199) + '\u{1F4A2}' + 'y'.repeat(20);
var r = clipBubbleText(text);
console.log('LONE=' + lone(r));
console.log('HEAD=' + (r.charCodeAt(198) === 120));
console.log('LEN=' + r.length);
var normal = clipBubbleText('x'.repeat(199) + 'abcdefghij');
console.log('NORMAL_LONE=' + lone(normal));
console.log('SHORT=' + (clipBubbleText('hi') === 'hi'));
"""
    out, err, rc = run_node(js.replace('__SEG__', seg))
    kv = kv_of(out)
    check('D6-1 node 跑通', rc == 0, 'rc=%d err=%s' % (rc, err[:300]))
    check('D6-2 切在 emoji 中间时不产生孤立代理（关键：会显示成 � 并污染 SSE）',
          kv.get('LONE') == 'false', 'LONE=%r' % kv.get('LONE'))
    check('D6-3 前面 199 个字符完整保留', kv.get('HEAD') == 'true', 'kv=%r' % kv)
    check('D6-4 普通长文本截断不产生孤立代理', kv.get('NORMAL_LONE') == 'false',
          'NORMAL_LONE=%r' % kv.get('NORMAL_LONE'))
    check('D6-5 短文本原样返回', kv.get('SHORT') == 'true', 'kv=%r' % kv)
except Exception as exc:
    check('D6 段整体未崩', False, '%s: %s' % (type(exc).__name__, exc))


LINES.append('')
LINES.append('PASS=%d FAIL=%d' % (PASS, FAIL))
out_txt = '\n'.join(LINES)
print(out_txt)
io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '_pet043_result.txt'), 'w',
        encoding='utf-8').write(out_txt + '\n')
sys.exit(1 if FAIL else 0)
