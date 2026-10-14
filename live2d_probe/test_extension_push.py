# -*- coding: utf-8 -*-
"""test_extension_push.py — 「推送没送到却被当成送到了」的回归（v0.3.33）

扩展侧有两个共用的判断依据，都是 `checkStandaloneAlive()` 的**缓存**
（HEALTH_CHECK_INTERVAL_MS = 15s）：

  1. `scanActiveFile()` / `pushPendingDiagnostics()` 用缓存决定"要不要回滚"；
  2. `launchStandalonePet()` 用缓存决定"要不要启动"。

桌宠刚死的那段时间里缓存仍然是 true，于是：
  · 消息投进黑洞，而状态被提交 —— 0.3.19 定的"脏标记/状态不可吞"事实上失效。
    用户表现：桌宠重启后第一条 bug 播报消失；file_scan 那条要等满一个
    repeatSec（60s）才会再提醒。
  · 用户点状态栏想重启桌宠，只看到"已经在运行了"，然后什么都没发生。

修法：判据换成**唯一的事实**——HTTP 有没有真的收到 2xx
（postJsonToPet 返回 Promise<boolean>），并在失败时立刻作废健康缓存。

本测试：
  A. src/extension.ts 的静态契约（TS 集成测试要真 VS Code，无头环境跑不了）
  B. **编译产物同步**（VSIX 里带的是 out/，改了 src 忘了 tsc 就是"修了却没生效"）
  C. HTTP 投递契约的行为级验证 —— TS 现在的全部依据就是它

用法：python test_extension_push.py
      → live2d_probe/extension_push_result.txt
      返回码 0 全通过 / 1 有失败
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
_PET = os.path.join(_PROJ, 'desktop_pet')

# ⚠️ 必须在 import standalone 之前（v0.3.27 的教训）
os.environ['AIRI_PET_LOG'] = os.path.join(tempfile.gettempdir(),
                                          'airi_test_extension_push.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(tempfile.gettempdir(),
                                               'airi_test_extension_push_settings.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(tempfile.gettempdir(),
                                             'airi_test_extension_push_corpus.jsonc')

sys.path.insert(0, _PET)
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))

OUT = os.path.join(_HERE, 'extension_push_result.txt')
SRC_TS = os.path.join(_PROJ, 'src', 'extension.ts')
OUT_JS = os.path.join(_PROJ, 'out', 'extension.js')

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


# ===========================================================================
out('=' * 82)
out('A. src/extension.ts —— "送没送到"必须以真实投递结果为准')
out('=' * 82)

TS = io.open(SRC_TS, encoding='utf-8').read()

check('postJsonToPet 返回 Promise<boolean>（不再 void 忽略响应）',
      'function postJsonToPet(path: string, message: unknown, '
      'timeoutMs = 500): Promise<boolean>' in TS)
check('  2xx 才算送达', 'const code = res.statusCode ?? 0;' in TS
      and 'resolve(code >= 200 && code < 300);' in TS)
check('  失败时作废健康缓存（standaloneHealthy = false）',
      TS.count('standaloneHealthy = false;') >= 2,
      '出现 %d 处' % TS.count('standaloneHealthy = false;'))
check('pushToStandalone 透传投递结果',
      'function pushToStandalone(message: unknown): Promise<boolean>' in TS)

check('scanActiveFile 不再用带缓存的探测做前置判断',
      'if (await checkStandaloneAlive()) { pushToStandalone(message); return; }'
      not in TS)
check('  改为 await 投递结果再决定回滚',
      'const delivered = await pushToStandalone(message);' in TS
      and 'if (delivered) { return; }' in TS)

check('pushPendingDiagnostics 不再以缓存探测为门槛',
      'if (!(await checkStandaloneAlive())) { return false; }' not in TS)
check('  去重签名在**确认送达后**才提交（否则失败后永不补发）',
      'if (delivered) { lastPushSignature = signature; }' in TS
      and 'lastPushSignature = signature;\n\n\tpushToStandalone' not in TS)

check('launchStandalonePet 启动前强制重新探测',
      'lastHealthCheckAt = 0;\n\tif (await checkStandaloneAlive()) ' in TS,
      '否则桌宠刚死时点状态栏只会得到"已经在运行了"')


# ===========================================================================
out('')
out('=' * 82)
out('B. 编译产物同步 —— VSIX 里带的是 out/，不是 src/')
out('=' * 82)

if os.path.isfile(OUT_JS):
    JS = io.open(OUT_JS, encoding='utf-8').read()
    check('out/extension.js 里已经有投递结果判断（src 改完确实编译过）',
          'delivered' in JS, '否则打包出去的还是旧逻辑')
    check('out/extension.js 里 pushToStandalone 是异步的',
          'function pushToStandalone(message) {' in JS
          and 'return postJsonToPet(' in JS)
else:
    check('out/extension.js 存在', False, '未编译')


# ===========================================================================
out('')
out('=' * 82)
out('C. HTTP 投递契约（TS 现在的全部依据）—— 起真服务器验证')
out('=' * 82)

import standalone  # noqa: E402

PORT = 19897
ready = threading.Event()
threading.Thread(target=standalone.start_server, args=(PORT, ready),
                 daemon=True).start()
if not ready.wait(timeout=5):
    out('FATAL: 服务器没起来')
    sys.exit(1)

BASE = 'http://127.0.0.1:%d' % PORT


def post(path, payload, timeout=2.0, port=None):
    """返回 (是否送达, 状态码/异常名)。模拟 TS 里的 postJsonToPet。"""
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    url = 'http://127.0.0.1:%d%s' % (port or PORT, path)
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception as e:
        return False, type(e).__name__


ok, code = post('/push', {'type': 'diagnostics',
                          'payload': {'count': 1, 'items': []}})
check('POST /push 到活着的服务器 -> 送达（2xx）', ok, 'code=%s' % code)

# 畸形 body 也必须"被接受"：TS 侧 delivered=true 的含义是"进了队列"，
# 不能因为内容异常就让扩展以为桌宠死了
ok, code = post('/push', {'type': 'diagnostics', 'payload': {'count': 'x'}})
check('POST /push 畸形 body 仍返回 2xx（不会被误判成未送达）', ok,
      'code=%s' % code)

ok, code = post('/event', {'type': 'desktopReady'})
check('POST /event 回执也返回 2xx', ok, 'code=%s' % code)

# 找一个**确定没在监听**的端口，模拟"桌宠刚死"
dead = PORT + 1
s = socket.socket()
try:
    s.bind(('127.0.0.1', dead))
    s.close()
except OSError:
    dead = PORT + 2
import time as _time

_t0 = _time.time()
ok, why = post('/push', {'type': 'diagnostics', 'payload': {'count': 1}},
               port=dead)
_elapsed = _time.time() - _t0
check('POST /push 到已死的端口 -> 未送达（拿不到 2xx）', not ok, 'why=%s' % why)
check('  且失败在超时窗内返回（不会拖住 30s 一拍的时钟）', _elapsed < 2.5,
      'why=%s 耗时=%.2fs（上限 2.0s 超时）' % (why, _elapsed))
out('     （本机连死端口给出的是 %s 而不是 ConnectionRefused —— 无所谓，'
     '判据是"拿不到 2xx"）' % why)


# ===========================================================================
out('')
out('=' * 82)
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'FAILED', _fail))
out('=' * 82)
out('注：A/B 段是静态契约（扩展的 TS 集成测试要真 VS Code 宿主，无头环境')
out('    跑不了）；C 段是行为级 —— 验证 TS 赖以判断"送达与否"的 HTTP 契约。')
sys.exit(0 if _fail == 0 else 1)
