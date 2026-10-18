# -*- coding: utf-8 -*-
"""test_ext_port_guard.py — 扩展侧桌宠端口的校验（v0.3.37）

**bug**：`const STANDALONE_PORT = Number(process.env.AIRI_STANDALONE_PORT) || 19876;`
只兜了 NaN/0，**没有做 0~65535 范围校验**。而 `http.get` 对越界端口是**同步抛**
`ERR_SOCKET_BAD_PORT` —— 实测 node：

    99999 -> 同步抛出 ERR_SOCKET_BAD_PORT
    -1    -> 同步抛出 ERR_SOCKET_BAD_PORT
    70000 -> 同步抛出 ERR_SOCKET_BAD_PORT

（不是 error 事件，是同步抛。）它发生在 `checkStandaloneAlive()` 的 Promise
executor 里，而那个 executor **没有 try/catch** → Promise reject → 两处
`await checkStandaloneAlive()` 全部抛出去：
  · `launchStandalonePet` 里那处在它**唯一那个 try 之外** → 桌宠永远拉不起来，
    用户点了「启动桌宠」什么都不会发生，连一条报错都没有；
  · `refreshPetStatus` 那处 → 状态栏文本永不更新，调用点是 `void refreshPetStatus()`
    → unhandled rejection。
`AIRI_STANDALONE_PORT` 是 .env.example 明示支持的官方配置项，手写错完全可能。

对照：`postJsonToPet` 早就有 `try { ... } catch { resolve(false); }`，同样的
http 调用却是两套标准 —— 这是漏掉的那半边。

**修法**：端口夹紧到 [1, 65535]；`checkStandaloneAlive` 的 http.get 包 try/catch。

A 段**行为级**：把端口计算那段抽出来在 node 里真跑，逐个人工构造的环境变量值；
并做**反证**（旧写法确实会放行 -1 / 99999，证明这条校验不是多余的）。
B 段：坐实 http.get 同步抛（bug 的前提）。
C 段：编译产物必须同步（改 TS 必须 tsc，否则 VSIX 里还是旧的）。

用法：python test_ext_port_guard.py → live2d_probe/ext_port_result.txt
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from node_finder import find_node  # noqa: E402

TS = os.path.join(_PROJ, 'src', 'extension.ts')
JS = os.path.join(_PROJ, 'out', 'extension.js')
OUT = os.path.join(_HERE, 'ext_port_result.txt')

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
    out('  [%s] %-54s %s' % ('PASS' if ok else 'FAIL', name,
                             (':: ' + str(detail)) if detail else ''))


SRC = io.open(TS, encoding='utf-8').read()
try:
    OUTJS = io.open(JS, encoding='utf-8').read()
except OSError:
    OUTJS = ''


def run_node(js, timeout=60):
    fd, tmp = tempfile.mkstemp(suffix='.js', dir=os.environ.get('TEMP', '/tmp'))
    os.close(fd)
    io.open(tmp, 'w', encoding='utf-8').write(js)
    try:
        p = subprocess.run([find_node(), tmp], capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode('utf-8', 'replace'), \
            p.stderr.decode('utf-8', 'replace')
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ===========================================================================
out('=' * 82)
out('A. 端口夹紧 —— 行为级（抽出来在 node 里真跑）')

m = re.search(r'const STANDALONE_PORT = .*?\}\)\(\);', SRC, re.S)
check('A0 能抽出 STANDALONE_PORT 的定义', m is not None)
decl = m.group(0) if m else ''

# 把 "const X = <expr>;" 改写成可重复调用的函数，这样能逐个喂环境变量值
port_fn = ''
if decl:
    port_fn = decl.replace('const STANDALONE_PORT = ',
                           'function __computePort() { return ', 1) + ' }'

# 反证：修复前的写法（同文件历史版本）
old_fn = ('function __oldPort() { return '
          'Number(process.env.AIRI_STANDALONE_PORT) || 19876; }')

JS_A = port_fn + '\n' + old_fn + r'''
var cases = [
  [undefined, 19876, '没设环境变量'],
  ['',        19876, '空串'],
  ['abc',     19876, '非数字'],
  ['19877',   19877, '合法的自定义端口'],
  ['0',       19876, '0（|| 兜底挡得住，但 0 也不是合法监听端口）'],
  ['-1',      19876, '负数'],
  ['65535',   65535, '上界内最大值'],
  ['65536',   19876, '刚好越界'],
  ['99999',   19876, '远超上界'],
  ['70000',   19876, '越界（实测能触发 ERR_SOCKET_BAD_PORT）'],
  ['1.5',     19876, '小数'],
  ['19876abc', 19876, '数字混字符串（Number -> NaN）'],
  [' 19877 ', 19877, '前后空格（Number 容忍）']
];
var bad = [], oldBad = [];
for (var i = 0; i < cases.length; i++) {
  var v = cases[i][0], want = cases[i][1], label = cases[i][2];
  if (v === undefined) { delete process.env.AIRI_STANDALONE_PORT; }
  else { process.env.AIRI_STANDALONE_PORT = v; }
  var got = __computePort();
  var old = __oldPort();
  if (got !== want) { bad.push({ v: String(v), want: want, got: got, label: label }); }
  // 旧写法：只关心它会不会把越界值放行出去（那就是 bug）
  if (typeof old === 'number' && (old < 1 || old > 65535)) {
    oldBad.push({ v: String(v), old: old, label: label });
  }
}
console.log(JSON.stringify({ bad: bad, oldBad: oldBad }));
'''

rc, so, se = run_node(JS_A)
R = {}
if rc == 0 and so.strip():
    try:
        R = json.loads(so.strip().splitlines()[-1])
    except Exception as exc:
        out('  (解析失败 %r) %r' % (exc, so[:200]))
else:
    out('  (node rc=%d) %s' % (rc, se[:300]))

check('A1 node 真的跑起来了', bool(R), '输出 %r' % so[:120])
check('A2 13 个人工构造的端口值全部落到预期（含 -1 / 99999 / 65536）',
      R.get('bad') == [], '不符 %r' % (R.get('bad'),))
check('A3 【反证】旧写法确实会放行越界端口（证明这条校验不是多余的）',
      len(R.get('oldBad') or []) >= 3,
      '旧写法放行的越界值 %r' % (R.get('oldBad'),))
check('A4 反证里包含 -1（负数端口）',
      any(str(x.get('v')) == '-1' for x in (R.get('oldBad') or [])),
      '%r' % (R.get('oldBad'),))
check('A5 反证里包含 99999（超上界）',
      any(str(x.get('v')) == '99999' for x in (R.get('oldBad') or [])),
      '%r' % (R.get('oldBad'),))
check('A6 合法的自定义端口仍然生效（没把功能夹死）',
      not any(str(x.get('v')) == '19877' for x in (R.get('bad') or [])),
      '%r' % (R.get('bad'),))

# ===========================================================================
out('')
out('=' * 82)
out('B. 坐实 bug 前提：http.get 对越界端口是**同步抛**')

JS_B = r'''
var http = require('http');
var res = [];
for (var i = 0; i < [99999, -1, 70000].length; i++) {
  var port = [99999, -1, 70000][i];
  var mode = 'none';
  try {
    var req = http.get({ hostname: '127.0.0.1', port: port, path: '/ping', timeout: 300 },
      function () {});
    req.on('error', function (e) { });
    mode = 'no-throw';
  } catch (e) {
    mode = 'sync-throw:' + e.code;
  }
  res.push({ port: port, mode: mode });
}
console.log(JSON.stringify(res));
'''
rc2, so2, se2 = run_node(JS_B)
RB = []
if rc2 == 0 and so2.strip():
    try:
        RB = json.loads(so2.strip().splitlines()[-1])
    except Exception:
        pass
check('B1 三个越界端口全部**同步抛** ERR_SOCKET_BAD_PORT（不是 error 事件）',
      bool(RB) and all(str(x.get('mode', '')).startswith('sync-throw')
                       and 'ERR_SOCKET_BAD_PORT' in str(x.get('mode'))
                       for x in RB),
      '%r' % (RB,))

# ===========================================================================
out('')
out('=' * 82)
out('C. 静态契约 + 编译产物同步')

check('C1 源码里有端口范围校验',
      re.search(r'Number\.isInteger\(n\)\s*&&\s*n\s*>\s*0\s*&&\s*n\s*<\s*65536',
                SRC) is not None)
check('C2 checkStandaloneAlive 的 http.get 包在 try 里',
      re.search(r'let req[^;]*;\s*try\s*\{\s*req = http\.get', SRC) is not None)
check('C3 catch 里 resolve(false) 后 return（不让 req.on 挂到 undefined 上）',
      re.search(r'catch\s*\{\s*resolve\(false\);\s*return;', SRC) is not None)
check('C4 编译产物 out/extension.js 里也有端口校验（改 TS 必须 tsc）',
      'n > 0 && n < 65536' in OUTJS,
      'out/extension.js %d 字节' % len(OUTJS))
check('C5 编译产物里也有 try/catch 兜底',
      'resolve(false);' in OUTJS and 'return;' in OUTJS)
check('C6 没有残留旧的裸写法',
      'const STANDALONE_PORT = Number(' not in SRC)

out('')
out('RESULT: %s (%d failures)' % ('ALL PASS' if _fail == 0 else 'HAS FAILURE',
                                  _fail))
out('=' * 82)
sys.exit(0 if _fail == 0 else 1)
