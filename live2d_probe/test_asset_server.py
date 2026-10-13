# -*- coding: utf-8 -*-
"""test_asset_server.py — 本地静态资源服务器（common._AssetHandler）的边界（v0.3.31）

被测：common.start_asset_server() 暴露出来的 HTTP 面（页面本体 + /vendor/ + /pet-assets/）

两个已修的缺陷：

  C. **HEAD 绕过白名单**。处理器只覆写了 do_GET，于是 HEAD 落到
     SimpleHTTPRequestHandler.do_HEAD → send_head() → translate_path()，
     而它依赖的根基 `self.directory` **从来没有被指定过** = 进程 CWD。
     扩展是这样拉起桌宠的（src/extension.ts:launchStandalonePet）：
         spawn(python, [standalone.py], { cwd: petDir, stdio:'ignore' })
     —— cwd 恰好就是 desktop_pet/，于是：
         HEAD /common.py      → 200  Content-Length=48284     （修复前实测）
         HEAD /standalone.py  → 200  Content-Length=49027
     整个白名单被绕过，源码树的存在性与字节数全部暴露。
     页面**从来不发 HEAD**（页面本体与模型都用 GET），这条路纯属攻击面，
     所以修法不是"补一份 HEAD 白名单"，而是让 HEAD 与 GET 共用同一份路由。

  D. **畸形路径把请求打崩**。_safe_join 里 Path.resolve() 遇到内嵌 NUL
     抛的是 ValueError（不是"越界"那种 ValueError(relative_to)），而它不在
     任何 try 里 → 一个 `GET /vendor/%00` 就让请求**直接断连**
     （客户端 RemoteDisconnected），宿主控制台吐一整段 traceback。
     畸形路径必须与越界走同一条路（403），不能炸穿请求。

关键：本测试**必须把 cwd 切到 desktop_pet/**（见 above），否则缺陷 C 在
修复前也"看起来是好的" —— 那是这台机器上最容易骗过自己的一个条件。

用法：python test_asset_server.py
      → live2d_probe/asset_server_result.txt
      返回码 0 全通过 / 1 有失败
"""
import http.client
import io
import os
import sys
import time

PROBE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PROBE)
PET = os.path.join(ROOT, 'desktop_pet')
sys.path.insert(0, PET)

OUT = os.path.join(PROBE, 'asset_server_result.txt')

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
    out('  [%s] %-64s %s' % ('PASS' if ok else 'FAIL', name,
                             (':: ' + str(detail)) if detail else ''))


# ⚠️ 复现"扩展拉起桌宠时的 cwd" —— 缺陷 C 只在这个条件下现形
os.chdir(PET)

from common import load_html, start_asset_server   # noqa: E402
import live2d_assets as la                          # noqa: E402

HTML = load_html(19876)
HTML_BYTES = HTML.encode('utf-8')

_url, PORT = start_asset_server(HTML)
if not PORT:
    _lines.append('FATAL: asset server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines))
    sys.exit(1)


def req(method, path):
    """返回 (status, headers_dict, body_bytes)；连接异常返回 ('EXC:typ', {}, b'')。"""
    try:
        c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=5)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        hdr = {k.lower(): v for k, v in r.getheaders()}
        st = r.status
        c.close()
        return st, hdr, body
    except Exception as exc:                      # noqa: BLE001
        return 'EXC:%s' % type(exc).__name__, {}, b''


VENDOR_JS = la.VENDOR_JS
MODEL_JSON = la.MODEL_ENTRY
VENDOR_SIZE = VENDOR_JS.stat().st_size
MODEL_SIZE = MODEL_JSON.stat().st_size

out('=' * 82)
out('环境')
out('=' * 82)
out('  cwd                = %s' % os.getcwd())
out('  资产服务器 port     = %s' % PORT)
out('  vendor js          = %d bytes' % VENDOR_SIZE)
out('  model3.json        = %d bytes' % MODEL_SIZE)
out('')

# ---------------------------------------------------------------------------
out('=' * 82)
out('C. HEAD 必须走白名单 —— 不得回出 CWD 里的任何文件')
out('=' * 82)

# 这几条的白名单之外文件**确实存在**于 cwd（desktop_pet/）里 ——
# 所以"404"只能来自白名单，不可能来自"文件不存在"。
for name in ('common.py', 'standalone.py', 'watcher.py', 'live2d_assets.py',
             'main.py'):
    st, hdr, body = req('HEAD', '/' + name)
    check('HEAD /%-22s -> 404（白名单外）' % name, st == 404,
          'status=%s CL=%s' % (st, hdr.get('content-length')))
    time.sleep(0.02)

for name, path in (('父目录 README', '/../README.md'),
                   ('编码穿越', '/%2e%2e/README.md'),
                   ('凭空', '/definitely-not-here.txt')):
    st, _h, _b = req('HEAD', path)
    check('HEAD %-18s -> 404' % path, st == 404, 'status=%s' % st)
    time.sleep(0.02)

# 白名单内的三条路必须仍然可用，且 Content-Length 必须**等于实际字节数**
st, hdr, body = req('HEAD', '/ui.html')
check('HEAD /ui.html -> 200 且 CL == 注入后的页面字节数',
      st == 200 and hdr.get('content-length') == str(len(HTML_BYTES))
      and body == b'',
      'status=%s CL=%s expect=%d body=%d'
      % (st, hdr.get('content-length'), len(HTML_BYTES), len(body)))
check('  —— 且 HEAD **不带 body**（这是 HEAD 的定义）', body == b'',
      'body=%d' % len(body))

st, hdr, body = req('HEAD', '/')
check('HEAD / -> 200 且与 /ui.html 一致',
      st == 200 and hdr.get('content-length') == str(len(HTML_BYTES)),
      'status=%s CL=%s' % (st, hdr.get('content-length')))

st, hdr, body = req('HEAD', '/favicon.ico')
check('HEAD /favicon.ico -> 204 且无 body',
      st == 204 and body == b'', 'status=%s body=%d' % (st, len(body)))

st, hdr, body = req('HEAD', '/vendor/live2d-vendor.js')
check('HEAD /vendor/live2d-vendor.js -> 200 且 CL == 磁盘上的字节数',
      st == 200 and hdr.get('content-length') == str(VENDOR_SIZE)
      and body == b'',
      'status=%s CL=%s expect=%d body=%d'
      % (st, hdr.get('content-length'), VENDOR_SIZE, len(body)))

st, hdr, body = req('HEAD', '/pet-assets/ds-whale-girl/c_0120.model3.json')
check('HEAD /pet-assets/.../c_0120.model3.json -> 200 且 CL 正确',
      st == 200 and hdr.get('content-length') == str(MODEL_SIZE)
      and body == b'',
      'status=%s CL=%s expect=%d body=%d'
      % (st, hdr.get('content-length'), MODEL_SIZE, len(body)))

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('D. 畸形路径必须 403，不得断连')
out('=' * 82)

# 路径里带内嵌 NUL：**只有落在白名单前缀下**的才走 _safe_join → 403。
# `/%00` 不属于任何前缀（连 /vendor/ /pet-assets/ 都不匹配），天然落到末尾
# 404 —— 它压根没机会调用 resolve()，所以旧代码在这条上也不崩。预期必须
# 按"实际归哪条路"写，否则得到的是自造的矛盾而不是回归。
malformed_403 = ['/vendor/%00', '/vendor/live2d-vendor.js%00',
                 '/pet-assets/%00', '/pet-assets/ds-whale-girl/%00']
malformed_404 = ['/%00', '/ui.html%00']

for path in malformed_403:
    st, _h, _b = req('GET', path)
    check('GET  %-38s -> 403' % path, st == 403, 'status=%s' % st)
    time.sleep(0.02)
for path in malformed_404:
    st, _h, _b = req('GET', path)
    check('GET  %-38s -> 404（不在任何白名单前缀下）' % path, st == 404,
          'status=%s' % st)
    time.sleep(0.02)

for path in malformed_403:
    st, _h, _b = req('HEAD', path)
    check('HEAD %-38s -> 403（HEAD 同一条路）' % path, st == 403,
          'status=%s' % st)
    time.sleep(0.02)
for path in malformed_404:
    st, _h, _b = req('HEAD', path)
    check('HEAD %-38s -> 404' % path, st == 404, 'status=%s' % st)
    time.sleep(0.02)

# 越界（用 unquote 之后真的跑出 base 的路径）
for path in ('/vendor/../../common.py', '/pet-assets/../../../package.json',
             '/vendor/..%2f..%2fcommon.py'):
    st, _h, _b = req('GET', path)
    check('GET  %-38s -> 403（越界）' % path, st == 403, 'status=%s' % st)
    time.sleep(0.02)

# 每条请求都必须拿得到 HTTP 状态（拿不到 = 断连 = 这条链路会炸）
_disconnects = 0
for path in malformed_403 + malformed_404:
    for m in ('GET', 'HEAD'):
        st, _h, _b = req(m, path)
        if not isinstance(st, int):
            _disconnects += 1
            out('    !! %s %s -> %r' % (m, path, st))
check('畸形路径一条都没有断连（全部有 HTTP 应答）',
      _disconnects == 0, 'disconnects=%d' % _disconnects)

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('白名单内仍然可用（别为了堵路把路堵死）')
out('=' * 82)

st, hdr, body = req('GET', '/ui.html')
check('GET /ui.html -> 200 且内容与注入的页面逐字节相同',
      st == 200 and body == HTML_BYTES,
      'status=%s body=%d' % (st, len(body)))

st, hdr, body = req('GET', '/vendor/live2d-vendor.js')
check('GET /vendor/live2d-vendor.js -> 200 且内容与磁盘文件一致',
      st == 200 and len(body) == VENDOR_SIZE
      and body == VENDOR_JS.read_bytes(),
      'status=%s body=%d expect=%d' % (st, len(body), VENDOR_SIZE))
check('  —— 且带 no-store（别让 WebView2 缓存旧模型）',
      hdr.get('cache-control') == 'no-store', hdr.get('cache-control'))

st, hdr, body = req('GET', '/pet-assets/ds-whale-girl/c_0120.model3.json')
check('GET /pet-assets/.../c_0120.model3.json -> 200 且是合法 JSON',
      st == 200 and len(body) == MODEL_SIZE, 'status=%s body=%d' % (st, len(body)))

st, _h, _b = req('GET', '/common.py')
check('GET /common.py -> 404（对照：GET 侧一直是好的）', st == 404, st)

# ---------------------------------------------------------------------------
out('')
out('=' * 82)
out('静态不变量')
out('=' * 82)

_src = io.open(os.path.join(PET, 'common.py'), encoding='utf-8').read()

check('do_GET 与 do_HEAD 共用 _dispatch（否则 HEAD 会落回 translate_path）',
      'def _dispatch(self, path: str, body=True):' in _src
      and 'return self._dispatch(urllib.parse.urlparse(self.path).path, True)' in _src
      and 'return self._dispatch(urllib.parse.urlparse(self.path).path, False)' in _src)
check('do_HEAD 显式覆写（不能只靠继承）',
      'def do_HEAD(self):' in _src)
check('_safe_join 把 ValueError/OSError 一起兜住（内嵌 NUL 抛的正是它们）',
      'except (ValueError, OSError):' in _src)
check('_safe_join 仍然做 relative_to 越界检查',
      'target.relative_to(base.resolve())' in _src)
check('_respond 是唯一应答出口（Content-Length 不会两处各写一遍）',
      'def _respond(self, code: int, ctype: str, length: int,' in _src)
check('204/304 不带 Content-Length（按 HTTP 定义没有 body）',
      'if code not in (204, 304):' in _src)
check('白名单前缀只有 /vendor/ 与 /pet-assets/ 两条',
      "for prefix, base in (('/vendor/', VENDOR_DIR)," in _src
      and "('/pet-assets/', MODEL_ROOT)):" in _src)

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if _fail == 0 else 1)
