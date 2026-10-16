# -*- coding: utf-8 -*-
"""test_server_noise.py — 「客户端提前断开」的噪声必须真被吞掉（0.3.35）

背景：`handle_error` 被写在了 **AiriHandler**（请求处理器）里，但
socketserver 的 `handle_error` 是 **BaseServer**（服务器）的方法 ——
BaseRequestHandler 上根本没有这个名字。实测（三次连上 /events 再断开）：

    ThreadingHTTPServer 覆盖了 handle_error : False
    服务器实际用的是 : BaseServer.handle_error
    AiriHandler.handle_error 被调用次数 : 0        <- 死代码

于是"关窗时 SSE 断连不要打整段 traceback"这条保护从来没生效：每次关窗，
stderr 上都是一整段 ConnectionResetError traceback，看起来像崩了。

修法：把方法挪到 ThreadingHTTPServer 上。

⚠️ 本测试的关键不是"代码里有没有这个方法"（静态检查骗得过去），而是
**断开时 stderr 到底干不干净**；并且用 spy 反证"异常确实发生了、是被
handle_error 吞掉的" —— 否则"没有 traceback"可能只是因为压根没抛异常。
"""
import contextlib
import io
import os
import socket
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PROJ, 'desktop_pet'))
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))

_TMP = tempfile.gettempdir()
os.environ['AIRI_PET_LOG'] = os.path.join(_TMP, 'airi_test_noise.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(_TMP, 'airi_test_noise_set.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(_TMP, 'airi_test_noise_corpus.jsonc')

OUT = os.path.join(_HERE, 'server_noise_result.txt')
_lines = []
_fail = 0


def out(s=''):
    _lines.append(str(s))


def check(name, ok, detail=''):
    global _fail
    out('[%s] %s%s' % ('PASS' if ok else 'FAIL', name,
                       ('  ' + detail) if detail else ''))
    if not ok:
        _fail += 1


import standalone  # noqa: E402

PORT = 19884
_ready = threading.Event()
threading.Thread(target=standalone.start_server, args=(PORT, _ready), daemon=True).start()
if not _ready.wait(timeout=5):
    out('FATAL: server did not start')
    io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
    sys.exit(1)

# ---------- 静态：方法挂在哪一层 ----------
out('=== A. 归属（handle_error 必须在**服务器**类上）===')
check('A1 ThreadingHTTPServer 覆盖了 handle_error',
      'handle_error' in standalone.ThreadingHTTPServer.__dict__,
      '实际用 %s' % standalone.ThreadingHTTPServer.handle_error.__qualname__)
check('A2 AiriHandler 里不再留一份（留了就是死代码）',
      'handle_error' not in standalone.AiriHandler.__dict__)

# ---------- 行为 1（确定性）：直接喂异常给 handle_error ----------
# 走真实 socket 制造断连是**竞态**的（服务端可能抢在客户端 close 之前把
# ':ok' 写完，那一拍就不抛异常）—— 实测同一份代码两次跑结果不同，把它当
# 断言会变成一条时红时绿的测试。这里改成直接构造异常，行为完全确定：
# ⚠️ 修好之前 ThreadingHTTPServer 没有自己的 handle_error，拿到的就是
#    BaseServer 的版本 → 断连异常照样打 traceback → 下面 B1 会红。
out('')
out('=== B. 行为（确定性）：断开类异常静默，意外异常照旧 ===')

_srv = standalone.ThreadingHTTPServer(('127.0.0.1', 0), standalone.AiriHandler)
try:
    for exc_name, exc, want_silent in (
            ('ConnectionResetError', ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接'), True),
            ('BrokenPipeError', BrokenPipeError(32, 'Broken pipe'), True),
            ('ValueError（真意外，不该被吞）', ValueError('boom'), False),
    ):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                raise exc
            except Exception:
                _srv.handle_error(None, ('127.0.0.1', 1234))
        got = buf.getvalue()
        printed = 'Traceback' in got or 'Exception occurred' in got
        check('B %s -> %s' % (exc_name, '静默' if want_silent else '照旧报错'),
              printed != want_silent,
              'stderr %d 字节' % len(got))
finally:
    _srv.server_close()

# ---------- 行为 2（端到端，撞到才算）：真实断连 ----------
out('')
out('=== C. 端到端：真的关掉 SSE 连接，看 stderr（撞上竞态才取证）===')

seen = []
_orig_handler_error = standalone.ThreadingHTTPServer.handle_error


def _spy(self, request, client_address):
    # 反证用：记录"确实抛了什么异常"。撞不到就说明这一拍服务端先写完了，
    # 无从取证 —— 记为 inconclusive，不以它定成败（B 段才是确定性判据）。
    seen.append(type(sys.exc_info()[1]).__name__)
    return _orig_handler_error(self, request, client_address)


standalone.ThreadingHTTPServer.handle_error = _spy

buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    for _ in range(3):
        try:
            s = socket.create_connection(('127.0.0.1', PORT), timeout=5)
            s.sendall(b'GET /events HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
            s.recv(16)
            s.close()
        except Exception as exc:
            out('   (探针侧异常: %r)' % (exc,))
    time.sleep(1.5)
noise = buf.getvalue()

if seen:
    check('C1 真实断连时 stderr 干净', 'Traceback' not in noise,
          '异常=%r stderr %d 字节' % (seen, len(noise)))
else:
    out('[SKIP] C1 本拍服务端先写完了，没撞出断连异常（以 B 段为准）')

# ---------- 护栏不能吞掉真正的意外 ----------
out('')
out('=== D. 源码契约 ===')
src = io.open(os.path.join(_PROJ, 'desktop_pet', 'standalone.py'),
              encoding='utf-8').read()
check('D1 仍然放行非断连类异常（保留 super().handle_error）',
      'super().handle_error(request, client_address)' in src)

# ---------- 同一条护栏的第二份拷贝：资产服务器 ----------
# 教训（0.3.20 就写过一次）：**同类问题要穷举**。standalone.py 修完顺手看一眼
# common.py，那里还有一份一模一样的 handle_error，同样挂在处理器上。
out('')
out('=== E. 资产服务器（common.py）必须是同一条护栏 ===')
import common  # noqa: E402

check('E1 common._AssetServer 覆盖了 handle_error',
      'handle_error' in getattr(common, '_AssetServer', object).__dict__,
      '实际用 %s' % getattr(common._AssetServer.handle_error, '__qualname__', '?'))
check('E2 _make_handler 产出的处理器不再自带 handle_error',
      'handle_error' not in common._make_handler('<html>').__dict__)
check('E3 start_asset_server 用的是 _AssetServer',
      "_AssetServer(('127.0.0.1', port)" in io.open(
          os.path.join(_PROJ, 'desktop_pet', 'common.py'), encoding='utf-8').read())

_asrv = common._AssetServer(('127.0.0.1', 0), common._make_handler('<html>'))
try:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        try:
            raise ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接')
        except Exception:
            _asrv.handle_error(None, ('127.0.0.1', 1234))
    check('E4 资产服务器：断连异常静默', buf.getvalue() == '',
          'stderr %d 字节' % len(buf.getvalue()))
finally:
    _asrv.server_close()

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if _fail == 0 else 1)
