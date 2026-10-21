"""
standalone.py — deepseek 桌面宠物独立启动器

一键启动：python standalone.py
不依赖 VS Code 扩展，内置 SSE 服务器 + pywebview 窗口。

可选：VS Code 扩展可向 http://127.0.0.1:<port>/push POST 消息来推送诊断事件
（端口默认 19876，可用环境变量 AIRI_STANDALONE_PORT 覆盖）。
"""

import sys
import os
import json
import random
import shutil
import socket
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

from common import (get_screen_size, get_dpi_scale, WindowAPI, find_character_image,
                    load_html, start_asset_server, disable_window_backdrop,
                    fix_window_background, win_bg_mode)
import live2d_assets

# 导入 Python 后端（回复生成器）
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.join(os.path.dirname(_script_dir), 'python_backend')
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)
try:
    from response_generator import generate_response
    _backend_available = True
except Exception as e:
    _backend_available = False
    print(f'[airi-standalone] WARNING: Python backend not available: {e!r}')

# 台词库（语料）。单独 try：它是**用户可编辑**的，而且点击台词也归它管
# （原来的 CLICK_LINES 已迁进 corpus.py）—— 不能因为 response_generator
# 那条链出问题就连"点一下有反应"都没了。
try:
    import corpus
except Exception as e:
    corpus = None
    print(f'[airi-standalone] WARNING: corpus module not available: {e!r}')

# ---------------------------------------------------------------------------
# 启动诊断 —— 同时打控制台和落盘
# ---------------------------------------------------------------------------
# 为什么必须落盘：VS Code 扩展是这样起桌宠的（src/extension.ts:launchStandalonePet）
#
#     spawn(python, [standalone.py], { cwd: petDir, detached: true,
#                                      stdio: 'ignore', windowsHide: true })
#
# **stdio:'ignore' 意味着这里 print 的任何东西都被丢掉。** 于是「Live2D 悄悄降级成
# 立绘」这类问题在用户侧零痕迹可查 —— 只能猜。所以关键诊断同时写一份到
# desktop_pet/.airi-pet.log（已 gitignore）。
#
# 每次启动会截断重写，只保留本次的日志，不会无限增长。
#
# ⚠️ 落点可用 AIRI_PET_LOG 覆盖，**测试必须覆盖它**（v0.3.27）。
# 原因：测试是 `import standalone` 之后直接 `start_server()` 起**进程内**服务器的，
# 于是测试自己的请求也会走这里的 log()，写进开发者的这份排障日志里 ——
# 而且没有任何标记能区分"这条是活的桌宠写的"还是"这条是我跑测试打进去的"。
# 实锤：test_push_hardening.py 那条故意投毒的非 UTF-8 `/event` 用例，
# 每跑一次就往真实日志追加一行
#     [airi-standalone] /event parse failed: JSONDecodeError(...)
# 上一轮（0.3.26）把它当成了"产品里一条来路不明的 parse 失败"记进 CHANGELOG
# 悬而未决 —— 就是被这个假线索带偏的。测试请设 AIRI_PET_LOG 到 %TEMP%，
# 见 live2d_probe/test_pet_log_isolation.py。
LOG_PATH = os.environ.get('AIRI_PET_LOG') or os.path.join(_script_dir, '.airi-pet.log')


# ---------------------------------------------------------------------------
# 桌宠设置（右键菜单可改，v0.3.28）
# ---------------------------------------------------------------------------
# 「检测周期」= 扩展侧那个桌宠响应时钟（petTick）的间隔：每 N 秒，桌宠最多主动
# 说一条（当前文件 bug 变化 / 工作区诊断变化）。范围 1~60 秒，默认 30 秒。
#
# 为什么设置存放在桌宠这一侧，而不是 VS Code 的 settings.json：
#   菜单在桌宠页面里，而真正跑时钟的是扩展 —— 两边之间本来**没有任何
#   「桌宠 → 扩展」的反向通道**（扩展只在 /push 上一路往桌宠送）。这里拿
#   「桌宠落盘一个 JSON、扩展 fs.watchFile 监听它」当通道，好处是：
#     * 只有桌宠一个写入方，不存在两边互相覆盖
#     * 天然持久化（重启 VS Code / 重开桌宠都还在）
#     * 扩展不需要轮询 HTTP（1 秒周期就意味着一秒一次本机请求）
#   代价是两边要约定同一个路径 —— 用 ~/.dsh/pets/（Live2D core 缓存也在那儿，
#   见 live2d_assets.py:ensure_core），不是为这个功能新发明的目录。
#
# ⚠️ 落点可用 AIRI_PET_SETTINGS 覆盖，**测试必须覆盖它**（与 AIRI_PET_LOG 同理）：
#   测试若直接 POST /settings，改掉的是用户真实的检测周期。
SETTINGS_PATH = (os.environ.get('AIRI_PET_SETTINGS')
                 or os.path.join(os.path.expanduser('~'), '.dsh', 'pets',
                                 'airi-pet-settings.json'))

DETECT_MIN_SEC = 1        # 用户指定：最短 1 秒
DETECT_MAX_SEC = 60       # 用户指定：最长 60 秒
DETECT_DEFAULT_SEC = 30   # 与扩展 petTick 的默认值保持一致

DEFAULT_PORT = 19876      # 与扩展侧 extension.ts 的默认值必须一致
PORT_MIN = 1
PORT_MAX = 65535


def resolve_port():
    """解析监听端口，坏输入 / 越界一律回落 DEFAULT_PORT（0.3.39）。

    ⚠️ 为什么要夹范围：端口 **0** 是合法整数，绑 0 = 让内核随机分配一个
    端口，绑定**成功** → server_ready 被 set → 启动守卫不触发 → 窗口照常
    弹出。但 load_html 往页面里注入的是 `http://127.0.0.1:0`，页面里的
    SSE / fetch 全部打到一个不存在的端口；扩展却仍往 19876 推。桌宠站得
    住、不说话、菜单读不到设置，而日志里一条异常都没有（实测：绑 0 拿到
    2106）。扩展侧 0.3.37 已经夹过 [1,65535]，这边一直没夹 —— 两头必须
    同一口径，否则"环境变量被设了个怪值"时表现完全对不上。
    """
    try:
        port = int(os.environ.get('AIRI_STANDALONE_PORT', '') or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    if not (PORT_MIN <= port <= PORT_MAX):
        log('[airi-standalone] AIRI_STANDALONE_PORT=%r out of range '
            '[%d..%d], fallback to %d'
            % (os.environ.get('AIRI_STANDALONE_PORT'), PORT_MIN, PORT_MAX,
               DEFAULT_PORT))
        return DEFAULT_PORT
    return port


def clamp_detect_sec(value):
    """把任意输入收敛成 [1, 60] 整数秒；坏输入回落默认值。

    · 小数四舍五入取整 —— 与前端 setDetectSec 里的 Math.round 对齐。两边对
      同一个值给出不同结果，迟早变成"界面显示 4 秒、实际跑 3 秒"这种鬼问题。
    · ±Infinity / NaN / 字符串垃圾一律算"不可用" → 回落默认值。注意 Python 的
      json **默认就接受**字面量 Infinity / NaN，所以这些值真的能走到这里；
      而 round(float('inf')) 抛的是 OverflowError —— 只兜
      (TypeError, ValueError) 会让这个请求 500 断连（与 0.3.17 / 0.3.25 那几轮
      "畸形 body 炸穿链路"是同一类）。
    """
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return DETECT_DEFAULT_SEC
    return max(DETECT_MIN_SEC, min(DETECT_MAX_SEC, n))


def _as_seq(value):
    """把任意输入收敛成非负整数序号（「编辑语料」请求的计次）。

    ⚠️ 必须连 OverflowError 一起兜住：Python 的 json 默认接受字面量 Infinity，
    `int(float('inf'))` 抛的就是它 —— 与 clamp_detect_sec 同源的老坑。
    """
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


def load_settings():
    """读设置文件；缺失/损坏/字段类型不对一律回落默认值 ——
    一个坏掉的设置文件不该有能力让桌宠起不来。"""
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {'detectIntervalSec': clamp_detect_sec(data.get('detectIntervalSec')),
                    'openCorpusSeq': _as_seq(data.get('openCorpusSeq'))}
    except Exception:
        pass
    return {'detectIntervalSec': DETECT_DEFAULT_SEC, 'openCorpusSeq': 0}


def save_settings(new):
    """原子写：先写同目录临时文件再 os.replace。

    扩展在 watch 这个文件，直接覆写会留给它一个"读到半截 JSON"的窗口。
    @returns 是否落盘成功（失败不抛：设置只在内存里生效也比崩掉强）"""
    tmp = SETTINGS_PATH + '.tmp'
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(new, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


settings = load_settings()
settings_lock = threading.Lock()


def settings_payload():
    """给前端和扩展的统一视图：当前值 + 合法范围。

    范围随 payload 一起发，前端就不用自己写死 1 和 60（写死了早晚和这里不一致）。"""
    return {'detectIntervalSec': settings['detectIntervalSec'],
            'min': DETECT_MIN_SEC,
            'max': DETECT_MAX_SEC,
            'openCorpusSeq': settings.get('openCorpusSeq', 0)}


# ---------------------------------------------------------------------------
# 「编辑语料」的打开流程（v0.3.29）
# ---------------------------------------------------------------------------
# 三级回落，优先级从高到低：
#   1. **扩展用 VS Code 打开** —— 复用 0.3.28 建好的那条通道：这里把
#      openCorpusSeq 加一写进设置文件，扩展 fs.watchFile 看到序号变了就
#      openTextDocument 打开 .jsonc（VS Code 原生按"带注释的 JSON"处理），
#      然后 POST /event {type:'corpusOpened'} 回执。
#   2. **code 命令** —— 扩展没在跑（用户直接 start.bat 起的桌宠）时兜底。
#   3. **系统默认程序** —— 前两条都不通时 os.startfile。`.jsonc` 没关联的话
#      Windows 会弹一次"选择打开方式"，这是可接受的降级。
#
# 为什么要在第 1 步"等回执"而不是直接顺序执行：扩展在跑的时候不该再去拉起
# 一个 VS Code（code 命令会新开窗口/抢焦点）。回执就是"有人接单了"的信号；
# 超时没等到才往下走。回执**不是** HTTP 轮询 —— 就是本文件里一个 Event。
CORPUS_OPEN_ACK_TIMEOUT = 2.5   # 秒。扩展轮询设置文件 500ms 一拍，够宽裕了

_corpus_open_lock = threading.Lock()
#: seq -> {'event': Event, 'ok': bool, 'method': str|None}
_corpus_open_wait = {}


def _as_dict(value):
    """把外部 JSON 里的 payload 收敛成 dict。

    /event 的各个分支只按 dict 消费（`.get`），而 payload 是**外部写的**：
    null / 数字 / 字符串 / 数组都能通过 json.loads。以前写的是
    `msg.get('payload') or {}` —— 那只兜得住 falsy 值，`payload=5` 这种
    真值非 dict 会在 `.get` 上抛 AttributeError，再被外层 except 记成
    "/event parse failed"，把一条良性的畸形输入伪装成解析错误
    （v0.3.31；/push 侧早就有 `isinstance(p, dict)` 守卫，见那里的注释——
    这是同一个坑的第二个入口）。
    """
    return value if isinstance(value, dict) else {}


def _corpus_open_ack(seq, ok, method):
    """扩展回执入口（/event 的 corpusOpened 分支调）。@returns 是否有人等着它"""
    try:
        seq = int(seq)
    except (TypeError, ValueError, OverflowError):
        # ⚠️ OverflowError 必须一起兜：Python 的 json **默认接受**字面量
        #    Infinity，`int(float('inf'))` 抛的正是它 —— 与 _as_seq /
        #    clamp_detect_sec 同源的老坑，这里是第三个用 int() 的地方
        #    （v0.3.31 补）。
        return False
    with _corpus_open_lock:
        slot = _corpus_open_wait.get(seq)
        if slot is None:
            return False
        slot['ok'] = bool(ok)
        slot['method'] = str(method or '')
        slot['event'].set()
        return True


def _launch_code_cli(path):
    """用 `code` 命令行打开（2 级回落）。@returns 方法名或 None"""
    exe = (shutil.which('code') or shutil.which('code.cmd')
           or shutil.which('code-insiders') or shutil.which('code-insiders.cmd'))
    if not exe:
        return None
    # ⚠️ `code` 在 Windows 上是 .cmd 批处理，CreateProcess 不能直接执行它，
    #    必须经 cmd.exe —— 所以这里只能 shell=True。
    #
    # ⚠️⚠️ exe 的路径**必须带引号**（v0.3.30 修）：本机
    #    shutil.which('code') 返回的是 `S:\Microsoft VS Code\bin\code.CMD`
    #    —— **含空格**。旧写法只给参数加引号、exe 裸拼，交给 cmd.exe 后它
    #    拿 `S:\Microsoft` 当命令名 → 命令找不到；而 Popen 本身不抛错
    #    （stderr 早已丢进 DEVNULL），于是这里**谎报成功**：日志写
    #    "opened via code CLI"、气泡说"台词库打开了"，实际什么都没打开；
    #    并且因为返回了非 None，第三级 os.startfile 永远轮不到 ——
    #    三级回落静默退化成两级。
    #    用 list2cmdline 而不是手写 '"%s" "%s"'：它会连参数内部的引号一起处理。
    cmd = subprocess.list2cmdline([exe, path])
    try:
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x00000008 | 0x08000000  # DETACHED|NO_WINDOW
        subprocess.Popen(cmd, shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, **kwargs)
        return 'code-cli'
    except OSError:
        return None


def _open_corpus_fallback(path):
    """2 级 / 3 级回落。@returns 方法名或 None（全挂）"""
    method = _launch_code_cli(path)
    if method:
        return method
    if os.name == 'nt' and hasattr(os, 'startfile'):
        try:
            os.startfile(path)          # noqa: S606 - 打开用户自己的语料文件
            return 'system-default'
        except OSError:
            return None
    return None


def _corpus_release(seq, slot):
    """摘掉自己的等待槽。

    只摘"还是我这一个"的时候：并发/迟到回执的情况下别误摘别人的槽
    （序号在一个进程内单调递增，正常不会撞，但显式判一下更安全）。"""
    with _corpus_open_lock:
        if _corpus_open_wait.get(seq) is slot:
            _corpus_open_wait.pop(seq, None)


def _open_corpus_worker(seq, slot=None):
    """后台线程：确保文件存在 → 等扩展回执 → 没等到就逐级回落 → 说一句结果。

    slot 允许由 /settings 处理器**预先登记好**再传进来（顺序理由见那里：
    登记必须早于"落盘发布"）。传 None 时本函数自己登记 —— 直接调用它的
    测试/脚本走这条路。
    """
    path = None
    try:
        if corpus is None:
            raise RuntimeError('corpus module not available')
        path = corpus.ensure_corpus_file()
    except Exception as exc:
        log('[airi-corpus] ensure failed: %r' % (exc,))
        # ⚠️ 槽可能是调用方预先登记好的 —— 这条路径提前返回，必须自己摘干净，
        #    否则它会一直挂在 _corpus_open_wait 里（测试的 drain() 也会一直等）
        if slot is not None:
            _corpus_release(seq, slot)
        push_message('errorAlert', '唔…台词库文件没生成出来，我看下日志再说。', 'angry')
        return

    if slot is None:
        slot = {'event': threading.Event(), 'ok': False, 'method': None}
        with _corpus_open_lock:
            _corpus_open_wait[seq] = slot
    try:
        got = slot['event'].wait(CORPUS_OPEN_ACK_TIMEOUT)
    finally:
        _corpus_release(seq, slot)

    if got and slot['ok']:
        log('[airi-corpus] opened in VS Code (seq=%d, by=%s)'
            % (seq, slot['method'] or 'extension'))
        push_message('chatMessage',
                     '台词库给你在 VS Code 里打开了，改完保存就生效，不用重启我。',
                     'greeting')
        return
    if got:
        # 扩展接了单但打开失败（文件被占/权限…）—— 继续走回落，并留痕
        log('[airi-corpus] extension could not open it (seq=%d) -> fallback' % seq)

    method = _open_corpus_fallback(path)
    if method == 'code-cli':
        log('[airi-corpus] opened via code CLI (seq=%d)' % seq)
        push_message('chatMessage',
                     '台词库打开了（用 code 命令）。改完保存就生效。', 'greeting')
    elif method == 'system-default':
        log('[airi-corpus] opened via system default app (seq=%d)' % seq)
        push_message('chatMessage',
                     '台词库放到你默认的编辑器里了。改完保存就生效。', 'greeting')
    else:
        log('[airi-corpus] could not open corpus file (seq=%d, path=%s)' % (seq, path))
        push_message('errorAlert',
                     '唔，我打不开这个文件，你手动去这儿找它：%s' % path, 'angry')



def log(msg=''):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def _init_log():
    try:
        with open(LOG_PATH, 'w', encoding='utf-8') as fh:
            fh.write('deepseek 桌宠启动日志 —— 每次启动重写\n')
            fh.write(f'python  : {sys.executable}\n')
            fh.write(f'cwd     : {os.getcwd()}\n')
            fh.write(f'script  : {os.path.abspath(__file__)}\n')
            fh.write('-' * 60 + '\n')
    except OSError:
        pass


# 多线程 HTTP 服务器
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """吞掉「客户端提前断开」的噪声。

        桌宠关窗时 SSE 长连接会被 reset，socketserver 默认会把
        ConnectionResetError/BrokenPipeError 打成整段 traceback，看起来像崩溃。
        真正意外的错误照旧抛出去。

        ⚠️ 这个方法**必须挂在服务器类上**：socketserver 的 handle_error 是
        BaseServer 的方法，请求处理器（BaseRequestHandler）上根本没有它。
        0.3.35 之前它被写在了 AiriHandler 里 —— 实测连开三次 SSE 再断开，
        计数器一次都没被拨动（死代码），traceback 一行没少（详见
        test_server_noise.py）。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

# ---------------------------------------------------------------------------
# 跨源护栏（0.3.35）
#
# 桌宠的 HTTP 服务需要 CORS 才能工作：ui.html 是从**资产服务器**
# （127.0.0.1:<动态端口>）加载的，而它要请求的是**主服务**
# （127.0.0.1:<PORT>）—— 端口不同就是跨源，不带 CORS 头前端一个请求都发不出去
# （0.3.25 踩过：相对 URL 打到资产服务器 404）。
#
# 但 CORS 头写成 '*' 等于**把桌宠开放给整个互联网**：实测（0.3.35）
#   POST /push   Origin: https://evil.example.com -> 200，桌宠照着念任意文案
#   GET  /settings                                -> 200，设置被读走
#   OPTIONS 预检                                   -> 204 一路放行
# 也就是说用户随手打开的一个网页就能：① 冒充桌宠说话（"你的构建失败了，
# 点这里修复"这类钓鱼文案）② 开 /events 偷听 —— SSE 流里有 file_scan 的
# 文件名、代码行和诊断内容。
#
# 修法：只在 Origin 是**回环地址**时放行。非浏览器客户端（VS Code 扩展 /
# curl / python）根本不发 Origin，照旧放行 —— 本机进程本来就在信任边界内，
# 护栏针对的是"网页"。
# ---------------------------------------------------------------------------

# 回环地址的几种写法；IPv6 的 hostname 解析后不带方括号，两种都收
_LOOPBACK_HOSTS = frozenset(('127.0.0.1', 'localhost', '::1'))


def _origin_allowed(origin):
    """这个 Origin 是不是来自本机的页面？

    返回 True 的三种情况：
      - 没有 Origin 头（非浏览器客户端：扩展 / curl / python 脚本）
      - Origin 是回环地址（桌宠自己的页面：资产服务器动态端口）
      - 以上都不是 → False
    """
    if not origin:
        return True
    if origin == 'null':
        # file:// 页面和 sandbox iframe 都会发 'null'。桌宠页面是
        # http://127.0.0.1:<port>，用不到它；而沙箱 iframe 是攻击者
        # 随手就能造出来的（<iframe sandbox>），放行等于把护栏拆了。
        return False
    try:
        host = (urlsplit(origin).hostname or '').lower()
    except Exception:
        return False
    return host in _LOOPBACK_HOSTS

# ---------------------------------------------------------------------------
# SSE 消息队列（线程安全，有上限防内存泄漏）
# 每条消息带递增序号：队列裁剪后客户端仍按序号取新消息，不会错位
# ---------------------------------------------------------------------------
MAX_QUEUE_SIZE = 200
SSE_REPLAY_COUNT = 20   # 新连接重播最近的消息数（含启动问候）

# 请求体上限与读超时：挡住"畸形 Content-Length 把 handler 线程永久挂死"
# （详见 AiriHandler._read_body 的注释）。桌宠的消息最大也就几 KB，1 MB 是
# 留了几个数量级的余量；5 秒足够本机任何正常请求把 body 发完。
MAX_BODY_BYTES = 1 << 20
BODY_READ_TIMEOUT_SEC = 5.0

sse_queue = []          # [(seq, json_str)]
_seq = 0
queue_lock = threading.Lock()

def push_message(msg_type: str, text: str, emotion: str = "idle"):
    """向 SSE 客户端推送消息。超过上限时丢弃最旧的消息。"""
    global _seq
    with queue_lock:
        _seq += 1
        sse_queue.append((_seq, json.dumps(
            {"type": msg_type, "payload": {"text": text, "emotion": emotion}},
            ensure_ascii=False
        )))
        if len(sse_queue) > MAX_QUEUE_SIZE:
            del sse_queue[:len(sse_queue) - MAX_QUEUE_SIZE]
    _warn_if_js_stalled()


_hb_warn_state = {'at': 0.0}

def _warn_if_js_stalled():
    """推送用户可见消息时顺带检查 JS 心跳（第十轮诊断）。

    SSE 事件是网络驱动，不受页面定时器节流影响 —— 消息一定送达；但页面若
    定时器停摆（节流/冻结），气泡虽然会显示，region 更新和 8s 自动消失
    全都不会执行。在日志里留下这个状态，用户报"气泡被切/不消失"时一眼定位。
    """
    api = _api_ref
    if api is None:
        return
    try:
        lag = api.js_heartbeat_lag()
    except Exception:
        return
    if lag is not None and lag > 3.0 and time.monotonic() - _hb_warn_state['at'] > 30:
        _hb_warn_state['at'] = time.monotonic()
        log(f'[airi-js] WARNING: 推送消息时 JS 心跳滞后 {lag:.1f}s '
            '—— 页面定时器可能被节流/冻结（region 与气泡自动消失将停摆）')


_api_ref = None   # main() 里创建 WindowAPI 后回填，供 push_message 检查心跳


# ---------------------------------------------------------------------------
# 点击互动 —— 点角色身上不同位置，说不同的话
# ---------------------------------------------------------------------------
# 分区由 ui.html 的 zoneAt() 给出：canvas 上半是 head、中段是 body、底部是 desk。
# 每池多放几条，点着才不像复读机。
# 情绪词必须和 ui.html 的 EMOTION_EXPRESSION 对得上
# （idle / greeting / angry / happy / surprised），否则表情切不过去。
#
# ⚠️ v0.3.29 起台词**不再写在这里**：点击台词和诊断台词一起归 corpus.py 管
#    （右键菜单「编辑语料」能改的就是它）。这里保留一个只读别名，纯粹是为了
#    让探针（live2d_probe/verify_click_region.py）还能 `standalone.CLICK_LINES`
#    地做结构/情绪检查 —— 真值只有一份，就在 corpus.py。
CLICK_LINES = corpus.CLICK_LINES if corpus is not None else {}

#: 兜底台词：corpus 模块不可用（安装残缺）时至少还"点得动"，不至于点了没反应
_CLICK_FALLBACK = (('唔……', 'idle'),)

_last_pick = {}   # zone -> 上次选中的下标（避免连点两句一样，2026-09-23 日志实锤 #9/#10 复读）

def _pick_click_line(zone):
    """WindowAPI 的取词回调：返回 (台词, 情绪)。

    用量小而固定的本地语料，不走 response_generator —— 那个是给"诊断事件"
    生成回复的，点一下角色要的是**立刻**有反应，不该去碰网络/DEEPSEEK 那条路。
    取词走 corpus.get_click_lines()：**用户语料优先、缺失/写坏回落内置**。
    """
    pool = corpus.get_click_lines(zone) if corpus is not None else None
    if not pool:
        pool = _CLICK_FALLBACK
    last = _last_pick.get(zone, -1)
    if len(pool) > 1:
        # 池里多于一句时，只从"不是上一句"的里面挑 —— 连点不复读
        idx = random.choice([i for i in range(len(pool)) if i != last])
    else:
        idx = 0
    _last_pick[zone] = idx
    return pool[idx]

def _builtin_reply(error_count, trigger='diagnostics', file='', items=None, reason='changed'):
    """后端（response_generator）不可用时的内置兜底台词。

    diagnostics 与 trigger/all_clear 两条推送分支共用 —— 后者此前漏了兜底，
    all_clear 在无后端时被静默吞掉（用户修好了 bug 却毫无反应）。

    file_scan（当前文件 bug 播报）同样要有兜底：无后端时也能说出
    文件名、数量和前几条 bug 的行号内容。reason='remind' 表示 bug 数量
    没变化的周期提醒，换一套说法避免与首播复读同一句。
    """
    if trigger == 'file_scan':
        fname = file or '当前文件'
        if error_count > 0:
            detail = ''
            if isinstance(items, list):
                parts = []
                for it in items[:3]:
                    if isinstance(it, dict):
                        parts.append('第%s行：%s' % (
                            it.get('line', '?'), str(it.get('message', ''))[:40]))
                detail = '；'.join(parts)
            if reason == 'remind':
                text = '%s 里那 %d 个 bug 还在呢，我可都记着账的。%s' % (
                    fname, error_count, (' ' + detail) if detail else '')
            else:
                text = '%s 里还躺着 %d 个 bug，别装没看见！%s' % (
                    fname, error_count, (' ' + detail) if detail else '')
            push_message('errorAlert', text, 'angry')
        else:
            push_message('chatMessage',
                         '%s 干净了，零 bug。哼，勉强表扬你一下。' % fname,
                         'happy')
        return
    if error_count > 0:
        push_message('errorAlert', f"喂！{error_count} 个错误！给我认真点检查！", 'angry')
    else:
        push_message('chatMessage', '哼，全部修好了…算你厉害。', 'happy')


# ---------------------------------------------------------------------------
# 后台生成的调度状态（0.3.40）
#
# 0.3.39 把生成挪出请求线程时只解决了"扩展 500ms 超时"，却把「/push 一定
# 回 200」变成了「这条播报一定算送达」—— 而生成是在 200 之后才跑的：
# 它失败、返回空、或者返回得比下一条还晚，扩展那边已经把状态提交完了，
# 没有任何人会再来补一次。于是 0.3.39 换来的是三个新症状（都实测过）：
#
#   · 丢单：生成抛异常 / 返回 None / 返回空文本 → 一条气泡都不出（实测
#     三种情况 sse_queue 增量都是 0，而 /push 回了 200）
#   · 重复：0.2 秒内连发两次同内容的 /push → 两条线程各说一遍
#     （实测 sse_queue 增量 = 2）
#   · 乱序：先 file_scan「还有 3 个 bug」后 all_clear「干净了」，前一条慢
#     → 气泡先说"干净了"再说"还有 3 个 bug"，桌宠自己推翻自己
#     （实测 order = ['干净了', '还有3个bug']）
#
# 所以这里补三件事：失败回落到内置台词（保证"收了就一定说得出话"）、
# 同签名在途去重、单调序号丢弃过期结果。
# ---------------------------------------------------------------------------
_gen_lock = threading.Lock()
_gen_seq = 0          # 已受理的生成序号（单调递增）
_gen_done_seq = 0     # 已完成并播报的最大序号；比它小的结果一律作废
_gen_inflight = set() # 在途生成的签名

# 在途上限：后端挂住时（无 timeout 的 LLM 往返）不能无限堆积线程
MAX_PENDING_GENERATIONS = 4


def _gen_signature(ctx):
    """把一次生成请求压成去重用的签名。

    只取"说什么"相关的字段：同一批 bug 的重复提醒会被并成一条。
    """
    try:
        return json.dumps({
            'trigger': ctx.get('trigger'),
            'file': ctx.get('file'),
            'error_count': ctx.get('error_count'),
            'reason': ctx.get('reason'),
            'sample': ctx.get('sample_errors'),
        }, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(ctx)


def _generate_async(ctx, tag):
    """在**后台线程**里跑 generate_response（0.3.39）。

    ⚠️ 为什么不能在 HTTP 请求线程里同步生成：generate_response 会走一次真实
    的 DeepSeek 往返。而扩展侧 postJsonToPet 的超时是 **500ms**
    （extension.ts）——请求线程一旦慢于它，扩展就 `req.destroy()`、判定
    delivered=false，于是：

      · fileScanState 回滚成上一个状态、lastPushAt **倒退**（60s 提醒冷却被绕过）
      · diagPushPending / lastPushSignature 不提交
      · 下一拍重新判定 → 又是同一批错误 → **桌宠每个周期把同一句话重说一遍**

    实测：注入一个 sleep(3) 的生成函数，/push 耗时 3.00s —— 扩展在 500ms
    就已经放弃了，而桌宠这边其实**已经把话说出去了**（push_message 在响应
    之前执行）。结果是"说了 + 状态没提交"的最坏组合：复读机。

    改到后台线程后：/push 立刻回 200（扩展按时提交状态），桌宠 1~2 秒后
    说话 —— 本来就是异步气泡，晚一点到这才是符合直觉的时序。

    ⚠️ 但 200 就意味着扩展**不会再补发**，所以这条链路上任何"没说出来"都
    是永久丢失。三条不变量见上方 _gen_lock 处的说明：
    失败必须回落到内置台词、同签名在途要丢弃、过期结果要作废。
    """
    global _gen_seq, _gen_done_seq

    if not isinstance(ctx, dict):
        return
    sig = _gen_signature(ctx)

    with _gen_lock:
        if sig in _gen_inflight:
            # 同一件事还有一条没生成完：这一条直接丢掉，否则会说两遍
            log('[airi-standalone] %s duplicate generation dropped' % tag)
            return
        if len(_gen_inflight) >= MAX_PENDING_GENERATIONS:
            # 后端（LLM）挂住时不能无限起线程，宁可丢新保旧
            log('[airi-standalone] %s dropped: %d generations still pending'
                % (tag, len(_gen_inflight)))
            return
        _gen_seq += 1
        my_seq = _gen_seq
        _gen_inflight.add(sig)

    def _fallback(exc=None):
        """生成拿不到结果时的兜底：宁可说一句内置台词，也不能什么都不说。

        扩展用 stdio:'ignore' 起进程、日志是唯一通道，而用户看的是气泡 ——
        "静默丢单"在这条链路上等于"这件事从没发生过"。
        """
        if exc is not None:
            log('[airi-standalone] %s generate failed: %r -> fallback' % (tag, exc))
        else:
            log('[airi-standalone] %s generated nothing -> fallback' % tag)
        try:
            ec = ctx.get('error_count', 0)
            try:
                ec = int(ec or 0)
            except (TypeError, ValueError):
                ec = 0
            _builtin_reply(ec, str(ctx.get('trigger', 'diagnostics') or 'diagnostics'),
                           ctx.get('file') or '', ctx.get('sample_errors'),
                           str(ctx.get('reason', 'changed') or 'changed'))
        except Exception as fb_exc:
            log('[airi-standalone] %s fallback failed: %r' % (tag, fb_exc))

    def _run():
        global _gen_done_seq
        try:
            try:
                r = generate_response(ctx)
            except Exception as exc:
                _fallback(exc)
                return
            if not isinstance(r, dict):
                # ⚠️ None 是"故意不说"的契约：heartbeat 这类触发本来就期望
                # 静默（response_generator 里 `elif trigger == "heartbeat":
                # return None`）。兜底绝不能把"决定不说"改成"说一句内置台词"，
                # 否则桌宠会在每次心跳时自言自语。
                if r is None and str(ctx.get('trigger') or '') == 'heartbeat':
                    return
                _fallback()
                return
            payload = r.get('payload')
            if not isinstance(payload, dict):
                _fallback()
                return
            text = payload.get('text', '')
            if not isinstance(text, str) or not text.strip():
                _fallback()
                return
            emotion = payload.get('emotion', 'idle')
            mtype = r.get('type') or 'chatMessage'
        finally:
            with _gen_lock:
                _gen_inflight.discard(sig)

        with _gen_lock:
            # 已经有更新的生成播报过 → 这条是过期的，丢掉（否则桌宠会
            # 先说"干净了"再改口"还有 3 个 bug"）
            if my_seq < _gen_done_seq:
                log('[airi-standalone] %s stale result dropped (seq=%d < %d)'
                    % (tag, my_seq, _gen_done_seq))
                return
            _gen_done_seq = my_seq
        try:
            push_message(mtype, text, emotion)
        except Exception as exc:
            log('[airi-standalone] %s push failed: %r' % (tag, exc))

    threading.Thread(target=_run, daemon=True,
                     name='airi-gen-%s-%d' % (tag, my_seq)).start()


# ---------------------------------------------------------------------------
# HTTP 服务器
# ---------------------------------------------------------------------------
class AiriHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    # ---- 跨源护栏（见文件上方的说明）----
    def _cors_guard(self):
        """非本机来源直接 403，且**不执行**任何动作。True = 可以继续处理。"""
        origin = self.headers.get('Origin')
        if _origin_allowed(origin):
            return True
        # 留痕：这是"有人想从网页操纵桌宠"的唯一证据，静默拒绝会无从查起
        log('[airi-http] blocked cross-origin %s %s (Origin=%r)'
            % (self.command, self.path, origin))
        # 先把请求体读完再回 403：HTTP/1.0 下服务端随即关连接，若接收缓冲区
        # 里还剩没读的字节，Windows 会发 RST 把刚写出去的响应一起冲掉 ——
        # 浏览器看到的就是"连接被重置"而不是一个干净的 403。
        # （排空自己也有挂死风险，_drain_body() 内部夹了上限与读超时。）
        self._drain_body()
        self.send_response(403)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"error":"cross-origin blocked"}')
        return False

    def _cors_header(self):
        """只给允许的来源回 CORS 头。

        原来是写死的 '*'（等于对任何网页开放）。现在改回显请求里的 Origin ——
        前提是它已经过 _cors_guard；没有 Origin 头的（扩展 / curl）根本不看
        CORS，不需要这个头。
        """
        origin = self.headers.get('Origin')
        if origin and _origin_allowed(origin):
            self.send_header('Access-Control-Allow-Origin', origin)

    def _read_body(self):
        """读请求体。**返回 None 表示已经回过响应了，调用方直接 return。**

        ⚠️ 为什么不能直接 `self.rfile.read(int(Content-Length))`：

        ① 负数 —— `read(-1)` 是 **readall** 语义，会一直读到 EOF。而客户端
           发完请求头是在**等响应**的，不会主动关连接 ⇒ 这个 handler 线程
           永久挂死。0.3.37 实测：连发 5 次 `Content-Length: -1` 并保持连接，
           进程线程数从 2 涨到 7，之后再也没退过。
        ② 虚大 —— 声明 1000 字节只发 20，`read(1000)` 阻塞等收满 ⇒ 同样挂死。

        两者**都不抛异常**（socket 没设读超时，就是安静地等着），所以
        handle_error 不会被调用、日志一行都没有 —— 是能静默累积的线程 +
        socket 泄漏。修法两道：先夹范围（负数/超限 400），再加读超时（挡
        "声明合法但少发"）。

        ⚠️ 四处 Content-Length 里的第 4 处（_cors_guard 里那个）走
        `_drain_body()` —— 它同样要夹上限 + 加读超时，理由见那里的注释。
        0.3.37 这里原本写的是"while remaining > 0 的循环负数天然进不去，
        本来就是安全的"：那只论证了①（负数），漏了②（**声明合法但少发**）
        —— 跨源请求声明 Content-Length: 999999 却一个字节都不发，那个循环
        在无超时 socket 上安静地等到 EOF，handler 线程被占住、403 永远发
        不出去。0.3.39 实测：连发 3 条并保持连接，线程数 2→5，客户端
        3 秒读超时全部 TimeoutError。
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            log('[airi-http] rejected body: Content-Length=%d (allowed 0..%d)'
                % (length, MAX_BODY_BYTES))
            self.send_response(400)
            self.end_headers()
            return None
        if length == 0:
            return ''
        # 挡"声明合法但少发"：整体预算到点就 400 收工，不让线程挂下去
        try:
            self.connection.settimeout(BODY_READ_TIMEOUT_SEC)
        except OSError:
            pass
        try:
            raw = self.rfile.read(length)
        except (socket.timeout, TimeoutError, OSError) as exc:
            log('[airi-http] body read failed (%s): %r'
                % (type(exc).__name__, exc))
            self.send_response(400)
            self.end_headers()
            return None
        finally:
            # SSE 长连接不能带着读超时跑，读完必须恢复成阻塞
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
        return raw.decode('utf-8', errors='replace')

    def _drain_body(self):
        """把请求体**排空**（不关心内容），给随后的 403 铺路。

        排空是必要的：HTTP/1.0 下服务端随即关连接，接收缓冲区里若还剩没读
        的字节，Windows 会发 RST 把刚写出去的响应一起冲掉 —— 浏览器看到
        的就是"连接被重置"而不是一个干净的 403。

        ⚠️ 但排空本身也是一次 read，同样必须夹上限 + 加读超时（0.3.39）：
        跨域请求的头是**攻击者/失控脚本**发的，声明 999999 字节却一字节
        不发是完全做得到的（浏览器做不到，raw socket / curl 做得到）。
        原来的 `while remaining > 0: self.rfile.read(...)` 两条都没有 ——
        于是一个跨源请求就能永久占住一个 handler 线程，而且那句 403 永远
        写不出去。实测：连发 3 条并保持连接 → 进程线程数 2→5，客户端
        3 秒读超时全部超时（收到的不是 403 是 TimeoutError）。
        """
        try:
            remaining = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            return
        if remaining <= 0:
            return
        remaining = min(remaining, MAX_BODY_BYTES)
        try:
            self.connection.settimeout(BODY_READ_TIMEOUT_SEC)
        except OSError:
            pass
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (socket.timeout, TimeoutError, OSError):
            pass
        finally:
            # 读完必须恢复成阻塞（SSE 长连接不能带着读超时跑）
            try:
                self.connection.settimeout(None)
            except OSError:
                pass

    def do_GET(self):
        if not self._cors_guard():
            return
        if self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self._cors_header()
            self.end_headers()
            self.wfile.write(b':ok\n\n')
            # 断线续传：EventSource 自动重连时会带上最后收到的 id（Last-Event-ID 头）。
            # 此前每条事件不带 id，重连后整批重播最近 SSE_REPLAY_COUNT(20) 条 ——
            # 一次网络抖动，桌宠就把旧对话当新气泡全弹一遍（复读机）。
            # 现在每条事件带 id: <seq>，重连只补发缺口；全新连接（无该头）维持原重播行为。
            try:
                after_seq = int(self.headers.get('Last-Event-ID') or 0)
            except (TypeError, ValueError):
                after_seq = 0
            # ⚠️ 必须把上界钳到当前 _seq，否则这条连接会被**永久饿死**：
            # _seq 是进程级单调计数且**重启归零**，客户端带一个比当前 _seq
            # 更大的 id 时，下面的 replay 必然为空，而 last_seq 就停在那个
            # 巨大值上 —— 此后 _seq 得一路涨到 after_seq+1 才可能有消息被
            # 选出，其间这条连接一条都收不到，而且因为循环外层是
            # `except Exception: pass`，日志里一行痕迹都没有。
            # 0.3.37 实测：同样推 3 条消息，全新连接收到 14 条，带
            # `Last-Event-ID: 99999999` 的那条收到 **0 条**。
            # 现实触发：持有上一次进程遗留 id 的客户端（旧页面残留、排障脚本）
            # 重连后就静默断流。
            with queue_lock:
                after_seq = max(0, min(after_seq, _seq))
            last_seq = after_seq   # 轮询基线从续传点起步：replay 为空时若停在 0，
                                   # 下面的轮询循环会把整个队列再发一遍（实测踩过）
            try:
                # 新连接先重播最近几条（让刚打开的窗口能看到问候语）；
                # 带 Last-Event-ID 的重连只重播 seq 大于它的部分
                with queue_lock:
                    if after_seq > 0:
                        replay = [(s, d) for (s, d) in sse_queue if s > after_seq]
                        replay = replay[-SSE_REPLAY_COUNT:]
                    else:
                        replay = sse_queue[-SSE_REPLAY_COUNT:]
                    if replay:
                        last_seq = replay[-1][0]
                for _seq_no, d in replay:
                    self.wfile.write(f'id: {_seq_no}\ndata: {d}\n\n'.encode('utf-8'))
                self.wfile.flush()
                while True:
                    time.sleep(0.3)
                    with queue_lock:
                        new_msgs = [(s, d) for (s, d) in sse_queue if s > last_seq]
                        if new_msgs:
                            last_seq = new_msgs[-1][0]
                    for _seq_no, d in new_msgs:
                        try:
                            self.wfile.write(f'id: {_seq_no}\ndata: {d}\n\n'.encode('utf-8'))
                            self.wfile.flush()
                        except Exception:
                            return
            except Exception:
                pass
        elif self.path == '/ping':
            self._json_response({'status': 'ok', 'server': 'airi-standalone'})
        elif self.path == '/settings':
            # 只读：右键菜单打开时同步当前值；也方便从外部（脚本/排障）查
            self._json_response(settings_payload())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._cors_guard():
            return
        if self.path in ('/push', '/diagnostics'):
            body = self._read_body()
            if body is None:
                return
            try:
                msg = json.loads(body)
            except Exception:
                push_message('chatMessage', body, 'idle')
                self._json_response({'status': 'pushed'})
                return
            if not isinstance(msg, dict):
                # body 是合法 JSON 但不是对象（数组/标量/null）：后面的
                # 'payload' in msg / msg.get 会 TypeError/AttributeError 炸断
                # 连接。与解析失败同路径——按原始文本进气泡，超长截断。
                push_message('chatMessage', body[:500], 'idle')
                self._json_response({'status': 'pushed'})
                return
            # payload 不可信：null / 数字 / 布尔 / 列表都会让
            # 'text' in payload 抛 TypeError（None 与 int 不可迭代），
            # 而这一句此前在 try 之外 —— 直接 500 断连（实测
            # payload=null / 123 / ["text"] 三种都断）。必须先确认是 dict。
            p = msg.get('payload')
            if isinstance(p, dict) and 'text' in p:
                t = msg.get('type', 'chatMessage')
                tx = p['text']
                # 外部 POST 的内容不可信：text 传数字/对象时，前端 typeText 的
                # charAt 会直接抛 TypeError（气泡空白 + jsError 黑匣子报警）。
                # 非字符串一律序列化成可读文本，绝不让畸形 payload 炸穿链路。
                if not isinstance(tx, str):
                    tx = json.dumps(tx, ensure_ascii=False) if (
                        isinstance(tx, (dict, list))) else str(tx)
                em = p.get('emotion', 'idle')
                push_message(t, tx, em)
            elif msg.get('type') == 'diagnostics' and isinstance(p, dict):
                # items 传 null 会在这里炸出 None[:5] TypeError → 500 断连；
                # count 传字符串会让 _builtin_reply 的 > 0 比较抛 TypeError。
                # 诊断来源不止自家扩展一个，字段类型必须兜底。
                try:
                    error_count = int(p.get('count', 0) or 0)
                except (TypeError, ValueError):
                    error_count = 0
                items = p.get('items')
                ctx = {
                    'trigger': p.get('trigger', 'diagnostics'),
                    'error_count': error_count,
                    'language': p.get('language', 'unknown'),
                    'sample_errors': items[:5] if isinstance(items, list) else [],
                }
                if _backend_available:
                    # 见 _generate_async 的注释：生成必须在请求线程之外，
                    # 否则扩展 500ms 超时 → 状态回滚 → 每拍复读同一条。
                    _generate_async(ctx, 'diagnostics')
                else:
                    _builtin_reply(ctx['error_count'], ctx.get('trigger', 'diagnostics'),
                                   '', ctx.get('sample_errors'))
            elif 'trigger' in msg or 'error_count' in msg:
                if _backend_available:
                    _generate_async(msg, 'trigger')
                else:
                    try:
                        _ec = int(msg.get('error_count', 0) or 0)
                    except (TypeError, ValueError):
                        _ec = 0
                    _builtin_reply(_ec, msg.get('trigger', 'diagnostics'),
                                   msg.get('file'), msg.get('sample_errors'),
                                   str(msg.get('reason', 'changed') or 'changed'))
            else:
                push_message('chatMessage', msg.get('message', body), 'idle')
            self._json_response({'status': 'pushed'})
        elif self.path == '/event':
            body = self._read_body()
            if body is None:
                return
            # decode 已在 _read_body 里用 errors='replace' 做过（与 /push 一致）：
            # 非 UTF-8 的 body 会让 decode 抛 UnicodeDecodeError，而它原先在
            # try 之外 —— 请求直接断连，日志里连一条 /event parse failed 都留不下。
            try:
                msg = json.loads(body)
                # 顶层不是对象时 msg.get 会 AttributeError → 被下面的 except
                # 记成 "parse failed"（v0.3.31）。/event 只收对象，直接忽略。
                kind = msg.get('type') if isinstance(msg, dict) else None
                if kind is None:
                    log('[airi-standalone] /event ignored non-object body: %s'
                        % type(msg).__name__)
                elif kind == 'desktopReady':
                    # 这行以前是 print —— 扩展用 stdio:'ignore' 起进程，print 全被丢掉，
                    # 于是"页面到底连上没有"在 .airi-pet.log 里毫无痕迹。必须走 log()。
                    log('[airi-standalone] Desktop pet connected')
                elif kind == 'silhouetteDiag':
                    # 前端上报的轮廓诊断：桥/尺寸/alpha 读数是否可信，全靠这条
                    # （这一条只 dump，不 .get，所以 payload 是什么形状都收得下）
                    p = msg.get('payload') or {}
                    log('[airi-sil] %s' % json.dumps(p, ensure_ascii=False))
                elif kind == 'jsError':
                    # JS 黑匣子：前端任何未捕获错误都会落到这里。
                    # 没有它，"点击/拖拽/region 全部失明"时分不清是页面崩了
                    # 还是窗口层断了 —— 现在页面崩了会直接写出错误原文。
                    p = msg.get('payload') or {}
                    log('[airi-js] ERROR %s' % json.dumps(p, ensure_ascii=False))
                elif kind == 'bridgePing':
                    p = _as_dict(msg.get('payload'))
                    if p.get('ok'):
                        log('[airi-js] bridge ping OK (at=%s pid=%s)'
                            % (p.get('at'), p.get('pid')))
                    else:
                        log('[airi-js] bridge ping FAIL %s'
                            % json.dumps(p, ensure_ascii=False))
                elif kind == 'corpusOpened':
                    # 扩展对「编辑语料」的回执（v0.3.29）。它不是页面事件，
                    # 是扩展告诉我们"这次打开我接单了 / 我失败了" ——
                    # 收到就唤醒 _open_corpus_worker，让它别再走 code 命令回落
                    # （否则会在扩展已经打开的情况下再拉起一个 VS Code）。
                    p = _as_dict(msg.get('payload'))
                    seq = p.get('seq')
                    handled = _corpus_open_ack(seq, p.get('ok'), p.get('method'))
                    if not handled:
                        # 迟到的回执（超时后回落已经接管）—— 只留痕，不重复说话
                        log('[airi-corpus] late ack for seq=%s (ok=%s)'
                            % (seq, p.get('ok')))
            except Exception as exc:
                log(f'[airi-standalone] /event parse failed: {exc!r}')
            self._json_response({'status': 'ok'})
        elif self.path == '/settings':
            # 写设置（右键菜单「检测周期」）。只改这一个键，值一律夹到
            # [DETECT_MIN_SEC, DETECT_MAX_SEC]；坏输入不猜、直接 400。
            body = self._read_body()
            if body is None:
                return
            try:
                msg = json.loads(body)
            except Exception:
                msg = None
            if not isinstance(msg, dict):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self._cors_header()
                self.end_headers()
                self.wfile.write(b'{"error":"bad request"}')
                return
            if msg.get('openCorpus'):
                # 「编辑语料」：只把序号加一写进设置文件 —— 扩展 watch 到它
                # 变了就会打开语料文件并回执。真正的打开动作在后台线程里做，
                # 因为要等回执（最多 CORPUS_OPEN_ACK_TIMEOUT 秒），
                # 不能在 HTTP 线程里阻塞前端。
                #
                # ⚠️ 顺序（v0.3.30 修）：**先登记等待槽，再落盘**。
                #    落盘 = 把这个请求"发布"给扩展，而扩展的 fs.watchFile 是
                #    500ms 轮询，它可能在下一拍立刻回执。旧写法先落盘、由后台
                #    线程稍后再登记，于是存在这个窗口：回执先到 →
                #    _corpus_open_ack 找不到槽 → 当成"迟到回执"丢掉 →
                #    worker 白等 2.5s 后走 code 回落 → **同一个文件被打开两次**
                #    （VS Code 里两个标签页/两个窗口）。登记必须早于发布。
                slot = {'event': threading.Event(), 'ok': False, 'method': None}
                with settings_lock:
                    seq = _as_seq(settings.get('openCorpusSeq')) + 1
                    settings['openCorpusSeq'] = seq
                    with _corpus_open_lock:
                        _corpus_open_wait[seq] = slot
                    saved = save_settings(settings)
                log('[airi-corpus] open requested (seq=%d, saved=%s)' % (seq, saved))
                threading.Thread(target=_open_corpus_worker, args=(seq, slot),
                                 daemon=True).start()
                self._json_response({'ok': True, 'openCorpusSeq': seq,
                                     'saved': saved})
                return
            if 'detectIntervalSec' not in msg:
                # 这条路径没有"把文本当消息说话"的语义（区别于 /push），
                # 拿不准就当坏请求，别顺手改掉用户设好的值。
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self._cors_header()
                self.end_headers()
                self.wfile.write(b'{"error":"missing detectIntervalSec"}')
                return
            with settings_lock:
                prev = settings['detectIntervalSec']
                cur = clamp_detect_sec(msg.get('detectIntervalSec'))
                settings['detectIntervalSec'] = cur
                saved = save_settings(settings)
            if cur != prev or not saved:
                # 改周期是"会影响观感"的操作，日志里留一条，省得排查
                # "为什么它突然变得这么爱说话"时无从下手。
                log('[airi-settings] detect interval %ds -> %ds (saved=%s)'
                    % (prev, cur, saved))
            self._json_response(dict(settings_payload(), saved=saved))
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        # 预检也要过护栏：放行了预检，浏览器才会把真正的 POST 发出去。
        # 不给非法来源回 ACAO，浏览器自己就会把后续请求挡掉（双保险）。
        if not _origin_allowed(self.headers.get('Origin')):
            log('[airi-http] blocked cross-origin preflight %s (Origin=%r)'
                % (self.path, self.headers.get('Origin')))
            # 与 _cors_guard 同一个理由：先排空再回，否则 RST 会把 403 冲掉
            self._drain_body()
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._cors_header()
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _json_response(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self._cors_header()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

def start_server(port: int, ready: threading.Event):
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), AiriHandler)
    except Exception as e:
        print(f'[airi-standalone] HTTP server ERROR: {e}', flush=True)
        return
    print(f'[airi-standalone] HTTP server on http://127.0.0.1:{port}', flush=True)
    ready.set()
    server.serve_forever()

def _ping_ok(port: int) -> bool:
    """探测端口上是否已有 Airi 服务器在运行（/ping 返回 200）"""
    try:
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/ping', timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False

def _merge_webview2_args(prev: str) -> str:
    """把桌宠必需的 WebView2 开关**并进**已有的 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS。

    ⚠️ 为什么不能用 os.environ.setdefault（0.3.32 修）：扩展拉起桌宠时，子进程
    继承 VS Code 宿主进程的完整环境 —— 只要宿主链上任何一环设过这个变量，
    setdefault 就会**整组静默放弃**我们的开关（不报错、日志无痕），四条禁节流
    形同虚设。合并策略：宿主的开关全保留，我们的补在后面（重复对 Chromium 无害）。

    ⚠️ `--disable-features` 是唯一有"后写覆盖先写"风险的开关（它的值是逗号表），
    必须把两边的 feature 取并集、合成**一个** flag，否则会把别人的 feature 顶掉。
    """
    _REQUIRED = ('--disable-background-timer-throttling',
                 '--disable-renderer-backgrounding',
                 '--disable-backgrounding-occluded-windows')
    _FEATURES = ('IntensiveWakeUpThrottling', 'CalculateNativeWinOcclusion')

    feats = set(_FEATURES)
    kept = []
    for tok in (prev or '').split():
        if tok.startswith('--disable-features='):
            feats.update(f for f in tok[len('--disable-features='):].split(',') if f)
        else:
            kept.append(tok)
    for arg in _REQUIRED:
        if arg not in kept:
            kept.append(arg)
    kept.append('--disable-features=' + ','.join(sorted(feats)))
    return ' '.join(kept)

def main(port=None):
    global _api_ref
    if port is None:
        port = resolve_port()
    PORT = port
    server_ready = threading.Event()
    server_thread = threading.Thread(target=start_server, args=(PORT, server_ready), daemon=True)
    server_thread.start()
    if not server_ready.wait(timeout=5.0):
        # 绑定失败：区分「已有 Airi 实例」和「端口被无关程序占用」，
        # 两种情况都不能继续开窗（否则会弹出一个连不上自己服务器的僵尸窗口）
        if _ping_ok(PORT):
            log(f'[airi-standalone] Airi is already running on port {PORT}, exit.')
            sys.exit(0)
        log(f'[airi-standalone] Port {PORT} is occupied by another program, exit.')
        sys.exit(1)
    char_img = find_character_image()
    if char_img:
        log(f'[airi-standalone] Using character image: {char_img}')

    # Live2D 素材可用就上 Live2D；不可用就安静地退回图片 / CSS 角色。
    # 任何一步失败都不该让桌宠起不来 —— 素材缺失、拿不到 Cubism Core 都属可恢复。
    l2d_ok, l2d_detail = live2d_assets.check_assets()
    for _ln in l2d_detail:
        log(f'[airi-live2d] {_ln}')
    log(f'[airi-live2d] live2d {"ENABLED" if l2d_ok else "disabled (image fallback)"}')

    # 「真透明」是两件独立的事，别再混在一起（详见 common.py 里的长注释）：
    #   1. 画面透出桌面 —— 靠关掉 DWM 的 Mica 背景材质。pywebview 在**系统深色
    #      模式**下会装 Mica，它由 DWM 绘制、不在窗口的 GDI 表面里，最终盖成
    #      一整块 #202020（浅色模式下不装，所以那时是 #F0F0F0 的 Form 底色）。
    #      这一条才是用户说的"并不是透明的"。见 disable_window_backdrop()。
    #   2. 点穿 —— 靠 SetWindowRgn 把窗口形状裁成"角色轮廓 + 气泡 + 名字"，
    #      形状之外不接受鼠标消息，点击落到下层窗口（比如 VS Code）。
    #      region 裁不到 DirectComposition 合成的 WebView2 内容，所以它
    #      **不改变画面**，只改变鼠标命中。
    sil = live2d_assets.silhouette_enabled()
    log(f'[airi-live2d] silhouette {"ON" if sil else "OFF"}')
    card = live2d_assets.card_mode()
    log(f'[airi-live2d] character card {card}')

    html = load_html(PORT, char_img, live2d=l2d_ok, silhouette=sil, card=card,
                     detect_sec=settings['detectIntervalSec'])
    if not html:
        # load_html 返回 None = ui.html 读不出来。以前这里会直接抛异常，
        # traceback 进 stderr 被扩展丢掉，用户只看到"点了没反应"（0.3.38）。
        log('[airi-standalone] ERROR: cannot load ui.html, exit.')
        sys.exit(1)
    page_url, asset_port = start_asset_server(html)
    if page_url is None:
        log('[airi-standalone] ERROR: cannot start asset server, exit.')
        sys.exit(1)
    log(f'[airi-standalone] UI served on {page_url}')
    try:
        import webview
    except ImportError:
        log('[airi-standalone] ERROR: pywebview not installed. Run: pip install pywebview')
        log(f'[airi-standalone] HTTP server still running on port {PORT}')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        sys.exit(1)
    # WebView2 的后台节流会把"不可见/被遮挡"页面的定时器压到极低频甚至冻结。
    # 桌宠是全透明异形窗口，最容易被 Chromium 的可见性启发式误判成后台页 ——
    # 第十轮实锤：定时器停摆后 region 冻结在气泡动画中途量出的过期形状上，
    # 最新气泡的顶边被永久裁掉一截，8s 自动消失也停摆。这些开关禁掉整类节流
    # （WebView2 加载器在创建环境时读这个环境变量）。
    # 0.3.32：① 改用**合并**而非 setdefault（见 _merge_webview2_args 的注释）；
    # ② 补 CalculateNativeWinOcclusion —— 桌宠是分层（region 裁剪）+ 置顶窗口，
    #    正是 Windows 原生遮挡计算最容易误判成"被完全盖住"的形状；前三个开关
    #    都还在的情况下页面依然在最后一次点击后 ~0.4s 被冻结（心跳证据：点击
    #    那一刻心跳还是新的，23s 后截图时两条 8s TTL 的气泡都没消失），
    #    剩下的嫌疑就落在这条遮挡启发式上。
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = _merge_webview2_args(
        os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', ''))
    screen_w, screen_h = get_screen_size()
    win_w, win_h = 300, 480
    # get_screen_size() 是物理像素，pywebview 的 create_window(x=,y=) 按
    # 逻辑像素消费（内部乘 DPI scale 转物理）—— 直接传会把坐标放大 scale
    # 倍：150% 缩放 + 2560 主屏时 x=2220 被放到物理 3330，单屏下窗口整个
    # 在屏幕右侧之外；双屏拼接落进第二块屏，所以这个 bug 一直没暴露。
    # 窗口的物理尺寸是 win_w*scale（300x480 逻辑 -> 450x720 物理），
    # 右下角对齐要按物理尺寸算，再除回 scale 转成逻辑像素（v0.3.15）。
    _scale = get_dpi_scale()
    x = int((screen_w - win_w * _scale - 40) / _scale)
    y = int((screen_h - win_h * _scale - 120) / _scale)
    api = WindowAPI()
    _api_ref = api                 # push_message 借此检查 JS 心跳
    api.set_click_handler(_pick_click_line)   # 点角色 -> 取一句台词
    api.set_region_enabled(sil)               # 异形窗口开关（前端也会自己判断一次）
    # 异形窗口必须留痕：这条链路跨 JS -> pywebview 桥 -> ctypes -> Win32 四层，
    # 任何一层断了以前都是静默的（用户只看到"窗口还是个方块"）。
    api.set_region_logger(lambda m: log(f'[airi-region] {m}'))
    # 点击/拖拽同样必须留痕：用户报"点了没反应"时，日志里至少要能看出
    # 是前端没命中（没有 pet_click）还是桥/后端断了。
    api.set_input_logger(lambda m: log(f'[airi-input] {m}'))
    window = webview.create_window(title='deepseek', url=page_url, width=win_w, height=win_h, x=x, y=y, frameless=True, transparent=True, on_top=True, resizable=False, easy_drag=False, js_api=api)
    api.set_window(window)
    if _backend_available:
        from response_generator import pick_corpus
        push_message('chatMessage', pick_corpus('greeting'), 'greeting')
    else:
        push_message('chatMessage', '哼，我上线了。有错误我会骂你的，给我认真写！', 'greeting')
    def _after_window_ready():
        """窗口真正建出来之后才能做的两件事。

        1. 关掉 DWM 的 Mica 背景 —— 不关的话整窗是一块实心色（第三轮）
        2. 压掉/挖掉**宿主 Form 的浅灰底** —— 默认是 WinForms 的
           SystemColors.Control(#F0F0F0)，pywebview 走 transparent 分支时
           忘了给它赋值。它会从窗口 region 的 pad 环里露出来，就是用户报的
           "气泡/名牌周围的块状白"（第五轮）。见 common.fix_window_background()

        Form 是 webview.start() 内部才创建的，所以这里轮询等它出现（最多 20s）。
        这两样都只影响"窗口好不好看"，不是"能不能用" —— 超时只告警、不抛异常。
        """
        deadline = time.time() + 20
        detail = ''
        while time.time() < deadline:
            ok, detail = disable_window_backdrop(window)
            if ok:
                log(f'[airi-dwm] Mica backdrop disabled ({detail})')
                break
            time.sleep(0.25)
        else:
            log(f'[airi-dwm] WARNING: 没能关掉 Mica backdrop（{detail}）')

        bg_deadline = time.time() + 10
        while time.time() < bg_deadline:
            ok, detail = fix_window_background(window)
            if ok:
                log(f'[airi-winbg] Form 底色处理完毕 [{win_bg_mode()}] {detail}')
                return
            time.sleep(0.25)
        log(f'[airi-winbg] WARNING: 没能处理 Form 底色（{detail}）—— '
            '气泡/名牌周围可能还会有一圈浅色方块')

    log('[airi-standalone] Desktop pet starting...')
    try:
        webview.start(_after_window_ready, debug=False)
    except Exception:
        # 窗口起不来是最容易被 stdio:'ignore' 吃掉的一类失败 —— 必须留痕
        import traceback
        log('[airi-standalone] ERROR: webview.start() failed:')
        for _tln in traceback.format_exc().splitlines():
            log('    ' + _tln)
        raise

if __name__ == '__main__':
    # ⚠️ 端口守卫必须在 _init_log() **之前**（0.3.39）：_init_log 是以 'w'
    # 打开日志的（"每次启动重写，不会无限增长"）。而"已有实例在跑"这件事
    # 要等 main() 里 start_server 绑定失败才看得出来 —— 于是第二个进程
    # 先把正在运行的那个实例的全部排障记录清空，再写一行 "already running"
    # 退出。用户报障时打开日志只剩一句"已退出"，真正的现场已经被抹掉了。
    # 而且它此后继续 append 到这个被截断的文件里，两个进程的记录混在一起。
    # 真实例：点两次状态栏按钮 / 扩展重复拉起 / 双击 launch.bat。
    _port = resolve_port()
    if _ping_ok(_port):
        # 注意：这里**故意不截断日志**，也不往里写 —— 那份日志属于还在跑
        # 的那个进程。
        sys.exit(0)
    _init_log()
    log('[airi-standalone] --- boot ---')
    # 把"这一轮用的是哪个检测周期"写进日志头：用户改了周期之后，排查
    # "为什么桌宠变得这么爱说话"只需要看这一行。
    log('[airi-settings] detect interval = %ds (min=%d max=%d, file=%s)'
        % (settings['detectIntervalSec'], DETECT_MIN_SEC, DETECT_MAX_SEC,
           SETTINGS_PATH))
    if corpus is not None:
        # 语料模块的内部日志（读盘失败/丢弃了多少条）接到这份排障日志上。
        # 用户改了语料说"怎么没生效"时，答案就在这几行里。
        corpus.set_log_hook(log)
        # 启动就确保文件存在（存在则什么都不做）。放在这里而不是"等用户点
        # 菜单才生成"：这样用户直接去 ~/.dsh/pets/ 就能找到它改；
        # 用户把文件删了想"恢复出厂"，下次启动也会自动长回来。
        corpus.ensure_corpus_file()
        _cs = corpus.corpus_status()
        log('[airi-corpus] file=%s exists=%s scenes(user)=%d clicks(user)=%d '
            'dropped=%d%s'
            % (_cs['path'], _cs['exists'], _cs['scenes_from_user'],
               _cs['clicks_from_user'], _cs['dropped'],
               (' error=%s' % _cs['error']) if _cs['error'] else ''))
    main(_port)
