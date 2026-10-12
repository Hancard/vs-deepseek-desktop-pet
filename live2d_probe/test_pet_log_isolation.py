# -*- coding: utf-8 -*-
"""test_pet_log_isolation.py — 测试不许污染开发者排障日志（v0.3.27）

背景（本轮审计实锤，含定量证据）：
  几个端到端测试是 `import standalone` 之后直接调 `start_server()` 起
  **进程内**服务器的。standalone 的 AiriHandler 在 /event 收到非 JSON body 时
  会走模块级 log()，而 log() 的落点就是开发者那份排障日志
      desktop_pet/.airi-pet.log
  于是 test_push_hardening.py 里那条**故意投毒**的非 UTF-8 /event 用例，
  每跑一次就往真实日志追加一行
      [airi-standalone] /event parse failed: JSONDecodeError(...)

  定量证据：
      before: parse-failed 3 行 / 2720 字节
      跑 test_push_hardening.py
      after : parse-failed 4 行 / 2832 字节      (+112 = 恰好一行)
      逐一测量其余四个测试：全部 +0 字节 → 唯一污染源就是它。

  危害：产品排障日志里出现"产品自己从未发出过的错误"，而这条日志的头部还写着
  "每次启动重写、不会无限增长"。0.3.26 的审计就是被这条假线索带偏，把它当成
  "产品里一条来路不明的 parse 失败"记进了 CHANGELOG 悬而未决。

修复（v0.3.27）：
  * standalone.LOG_PATH = os.environ.get('AIRI_PET_LOG') or <默认路径>
  * 五个端到端测试都在 `import standalone` **之前**把 AIRI_PET_LOG 指到 %TEMP%

本测试断言（4 条）：
  1. 不设 AIRI_PET_LOG 时，LOG_PATH 仍指向 desktop_pet/.airi-pet.log —— 默认行为不许变
  2. 设了 AIRI_PET_LOG 时，LOG_PATH 跟着走 —— 钩子确实生效
  3. 隔离下投毒 /event：替身日志收到 parse failed 行，真实日志纹丝不动
     （第 3 条前半句是关键——它证明"没写进真实日志"不是因为压根没触发）
  4. 真跑一遍 test_push_hardening.py：真实日志的字节数与 parse-failed 计数都不变
     —— 这条是真正的回归闸门，谁把测试里那行 AIRI_PET_LOG 删了它就红

用法：python test_pet_log_isolation.py
"""
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PET_ROOT = os.path.join(os.path.dirname(HERE), 'desktop_pet')
REAL_LOG = os.path.join(PET_ROOT, '.airi-pet.log')

OUT = os.path.join(HERE, 'pet_log_isolation_result.txt')
_lines = []
fail = 0


def out(s=''):
    _lines.append(str(s))


def check(name, ok, detail=''):
    global fail
    out('%s %s%s' % ('[PASS]' if ok else '[FAIL]', name,
                     (' :: ' + str(detail)) if detail else ''))
    if not ok:
        fail += 1


def snapshot(path):
    """返回 (是否存在, 字节数, 'parse failed' 出现次数)。"""
    if not os.path.exists(path):
        return (False, 0, 0)
    size = os.path.getsize(path)
    with io.open(path, 'rb') as fh:
        blob = fh.read()
    n = blob.decode('utf-8', 'replace').count('parse failed')
    return (True, size, n)


def run_child(code, env_extra=None, timeout=60):
    """在子进程里跑一段脚本，返回 (returncode, stdout)。"""
    env = os.environ.copy()
    env.pop('AIRI_PET_LOG', None)          # 先清掉，由 env_extra 决定
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, '-c', code], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


out('=' * 74)
out('测试日志隔离（AIRI_PET_LOG）')
out('=' * 74)
out('real log : %s' % REAL_LOG)
out('exists   : %s' % os.path.exists(REAL_LOG))
out('')

# ---------------------------------------------------------------------------
# 1 / 2. LOG_PATH 的解析：默认值 + 覆盖值
# ---------------------------------------------------------------------------
_ASK = ('import sys; sys.path.insert(0, %r); import standalone; '
        'print(standalone.LOG_PATH)') % PET_ROOT

rc, so = run_child(_ASK)
got = so.strip().splitlines()[-1] if so.strip() else ''
check('default:LOG_PATH=desktop_pet/.airi-pet.log',
      rc == 0 and os.path.normcase(os.path.normpath(got)) == os.path.normcase(os.path.normpath(REAL_LOG)),
      'rc=%s got=%r' % (rc, got))

probe_a = os.path.join(tempfile.gettempdir(), 'airi_iso_probe_a.log')
rc, so = run_child(_ASK, {'AIRI_PET_LOG': probe_a})
got = so.strip().splitlines()[-1] if so.strip() else ''
check('override:LOG_PATH 跟随 AIRI_PET_LOG',
      rc == 0 and os.path.normcase(os.path.normpath(got)) == os.path.normcase(os.path.normpath(probe_a)),
      'rc=%s got=%r' % (rc, got))

# ---------------------------------------------------------------------------
# 3. 隔离下投毒 /event：替身日志收到、真实日志不动
# ---------------------------------------------------------------------------
# 子进程内自建服务器（不调 start_server，避免端口冲突），投一条非 UTF-8 body
_POISON = r'''
import sys, threading, urllib.request
from http.server import ThreadingHTTPServer
sys.path.insert(0, {pet!r})
import standalone
srv = ThreadingHTTPServer(("127.0.0.1", 0), standalone.AiriHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
req = urllib.request.Request(
    "http://127.0.0.1:%d/event" % port,
    data=b"\xff\xfe\xfd not utf8 \xc3\x28",
    headers={{"Content-Type": "application/json"}})
try:
    urllib.request.urlopen(req, timeout=5).read()
except Exception:
    pass
print("LOG_PATH=" + standalone.LOG_PATH)
'''.format(pet=PET_ROOT)

probe_b = os.path.join(tempfile.gettempdir(), 'airi_iso_probe_b.log')
if os.path.exists(probe_b):                 # 清掉旧内容，让本次增长可测
    try:
        os.remove(probe_b)
    except OSError:
        pass
real_before = snapshot(REAL_LOG)
rc, so = run_child(_POISON, {'AIRI_PET_LOG': probe_b})
real_after = snapshot(REAL_LOG)
_, b_size, b_hits = snapshot(probe_b)
check('poison:/event 报错确实触发了（替身日志收到 parse failed）',
      b_hits >= 1, 'rc=%s tmp_hits=%d tmp_size=%d' % (rc, b_hits, b_size))
check('poison:真实日志纹丝不动',
      real_before == real_after,
      'before=%r after=%r' % (real_before, real_after))

# ---------------------------------------------------------------------------
# 4. 真跑污染源 test_push_hardening.py：真实日志必须零增量
# ---------------------------------------------------------------------------
victim = os.path.join(HERE, 'test_push_hardening.py')
tmp_victim = os.path.join(tempfile.gettempdir(), 'airi_test_push_hardening.log')
before = snapshot(REAL_LOG)
before_tmp = os.path.getsize(tmp_victim) if os.path.exists(tmp_victim) else 0
p = subprocess.run([sys.executable, victim], cwd=HERE,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
after = snapshot(REAL_LOG)
after_tmp = os.path.getsize(tmp_victim) if os.path.exists(tmp_victim) else 0
check('victim:test_push_hardening 自身仍 rc=0',
      p.returncode == 0, 'rc=%s' % p.returncode)
check('victim:真实日志字节数/parse-failed 计数均不变',
      before == after, 'before=%r after=%r' % (before, after))
check('victim:它自己的日志写进了 %TEMP%（证明隔离真的接上了）',
      after_tmp > before_tmp,
      '%d -> %d' % (before_tmp, after_tmp))

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if fail == 0 else 'FAILED', fail))
io.open(OUT, 'w', encoding='utf-8').write('\n'.join(_lines) + '\n')
sys.exit(0 if fail == 0 else 1)
