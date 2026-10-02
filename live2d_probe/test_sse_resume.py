# -*- coding: utf-8 -*-
"""SSE 断线续传的真 socket 验证（2026-09-24 新增 id/Last-Event-ID 机制）。

场景：
  1. 全新连接（无 Last-Event-ID）-> 重播最近 SSE_REPLAY_COUNT 条，每条带 id:
  2. 带第一条 id 重连          -> 只补发缺口（msg-2/msg-3），msg-1 不重复
  3. 带最后一条 id 重连        -> 什么都不补发

真 socket 读原始字节，验证的是真实 do_GET 而不是复刻链。
"""
import io
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

PROBE = os.path.dirname(os.path.abspath(__file__))
PET_ROOT = os.path.join(os.path.dirname(PROBE), 'desktop_pet')
sys.path.insert(0, PET_ROOT)
sys.path.insert(0, PROBE)

import standalone  # noqa: E402

lines = []
ok = True

srv = ThreadingHTTPServer(('127.0.0.1', 0), standalone.AiriHandler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
lines.append('server on 127.0.0.1:%d' % PORT)


def read_sse(extra_header=None, seconds=2.0):
    s = socket.create_connection(('127.0.0.1', PORT), timeout=3)
    req = ('GET /events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
           'Accept: text/event-stream\r\n' % PORT)
    if extra_header:
        req += extra_header + '\r\n'
    req += '\r\n'
    s.sendall(req.encode('ascii'))
    s.settimeout(0.5)
    buf = b''
    end = time.time() + seconds
    while time.time() < end:
        try:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            pass
    s.close()
    return buf.decode('utf-8', 'replace')


def data_lines(body):
    return re.findall(r'^data: (.*)$', body, re.M)


def check(name, cond, detail=''):
    global ok
    ok = ok and cond
    lines.append('%-34s %s %s' % (name, 'OK' if cond else 'FAIL', detail))


try:
    for i in (1, 2, 3):
        standalone.push_message('chatMessage', 'msg-%d' % i, 'idle')

    # 场景 1：全新连接
    body1 = read_sse()
    d1 = data_lines(body1)
    ids1 = re.findall(r'^id: (\d+)$', body1, re.M)
    texts1 = [json.loads(x)['payload']['text'] for x in d1]
    check('fresh: 3 msgs replayed', texts1 == ['msg-1', 'msg-2', 'msg-3'], str(texts1))
    check('fresh: every event has id', len(ids1) == 3 and len(set(ids1)) == 3, str(ids1))

    # 场景 2：带第一条 id 重连 -> 只补 msg-2/msg-3
    body2 = read_sse('Last-Event-ID: %s' % ids1[0])
    t2 = [json.loads(x)['payload']['text'] for x in data_lines(body2)]
    check('resume from #1: gap only', t2 == ['msg-2', 'msg-3'], str(t2))

    # 场景 3：带最后一条 id 重连 -> 无补发
    body3 = read_sse('Last-Event-ID: %s' % ids1[-1])
    check('resume from last: nothing', data_lines(body3) == [],
          repr(data_lines(body3)))

    # 场景 4：畸形 Last-Event-ID 不炸（按全新连接处理）
    body4 = read_sse('Last-Event-ID: garbage')
    t4 = [json.loads(x)['payload']['text'] for x in data_lines(body4)]
    check('garbage Last-Event-ID safe', t4 == ['msg-1', 'msg-2', 'msg-3'], str(t4))
finally:
    srv.shutdown()

with io.open(os.path.join(PROBE, '_sse_test.txt'), 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\nRESULT: ' + ('PASS' if ok else 'FAIL') + '\n')
print('done')
