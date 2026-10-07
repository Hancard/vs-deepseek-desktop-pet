"""
standalone.py — Airi 桌面宠物独立启动器

一键启动：python standalone.py
不依赖 VS Code 扩展，内置 SSE 服务器 + pywebview 窗口。

可选：VS Code 扩展可向 http://127.0.0.1:<port>/push POST 消息来推送诊断事件
（端口默认 19876，可用环境变量 AIRI_STANDALONE_PORT 覆盖）。
"""

import sys
import os
import json
import random
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

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
LOG_PATH = os.path.join(_script_dir, '.airi-pet.log')


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
            fh.write('Airi 桌宠启动日志 —— 每次启动重写\n')
            fh.write(f'python  : {sys.executable}\n')
            fh.write(f'cwd     : {os.getcwd()}\n')
            fh.write(f'script  : {os.path.abspath(__file__)}\n')
            fh.write('-' * 60 + '\n')
    except OSError:
        pass


# 多线程 HTTP 服务器
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ---------------------------------------------------------------------------
# SSE 消息队列（线程安全，有上限防内存泄漏）
# 每条消息带递增序号：队列裁剪后客户端仍按序号取新消息，不会错位
# ---------------------------------------------------------------------------
MAX_QUEUE_SIZE = 200
SSE_REPLAY_COUNT = 20   # 新连接重播最近的消息数（含启动问候）
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
CLICK_LINES = {
    'head': (
        ('唔…别摸头啦，会长不高的。', 'surprised'),
        ('头发会乱的，你赔得起吗。', 'angry'),
        ('……再摸一下也行。就一下。', 'happy'),
        ('摸头杀对我没用哦，快去写代码。', 'greeting'),
        ('…今天格外温柔？才、才没有因为你摸就开心！', 'happy'),
        ('头很贵的，摸一次收你一行 bug。', 'angry'),
        ('呜…突然伸手过来，吓我一跳！', 'surprised'),
    ),
    'body': (
        ('干嘛？我没偷懒，我在思考。', 'surprised'),
        ('你这个 bug 还没改完呢，别戳我。', 'angry'),
        ('戳我一次，我就少看你一行代码。', 'happy'),
        ('需要我帮你看报错吗？我不嫌你菜。', 'greeting'),
        ('别戳了别戳了，痒！', 'surprised'),
        ('有事说事，没事就回去看报错。', 'idle'),
        ('哼，想搭讪就直接说嘛。', 'happy'),
    ),
    'desk': (
        ('桌子是我的地盘，手拿开。', 'angry'),
        ('我在画图呢，别碰。', 'surprised'),
        ('……这张桌子陪我改过通宵的。', 'happy'),
        ('要不要一起喝点什么？你请。', 'greeting'),
        ('桌面整洁是你的事，代码整洁是我的事。', 'idle'),
        ('在我桌上放咖啡？你是故意的吧！', 'angry'),
        ('这张桌子的第一主人是我，你排第二。', 'greeting'),
    ),
}


_last_pick = {}   # zone -> 上次选中的下标（避免连点两句一样，2026-09-23 日志实锤 #9/#10 复读）

def _pick_click_line(zone):
    """WindowAPI 的取词回调：返回 (台词, 情绪)。

    用量小而固定的本地语料，不走 response_generator —— 那个是给"诊断事件"
    生成回复的，点一下角色要的是**立刻**有反应，不该去碰网络/DEEPSEEK 那条路。
    """
    pool = CLICK_LINES.get(zone) or CLICK_LINES['body']
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
# HTTP 服务器
# ---------------------------------------------------------------------------
class AiriHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle_error(self, request, client_address):
        """吞掉「客户端提前断开」的噪声。

        桌宠关窗时 SSE 长连接会被 reset，socketserver 默认会把
        ConnectionResetError/BrokenPipeError 打成整段 traceback，看起来像崩溃。
        真正意外的错误照旧抛出去。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def do_GET(self):
        if self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Access-Control-Allow-Origin', '*')
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
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path in ('/push', '/diagnostics'):
            try:
                length = int(self.headers.get('Content-Length', 0))
            except (TypeError, ValueError):
                self.send_response(400)
                self.end_headers()
                return
            body = self.rfile.read(length).decode('utf-8', errors='replace')
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
            if 'payload' in msg and 'text' in msg.get('payload', {}):
                t = msg.get('type', 'chatMessage')
                tx = msg['payload']['text']
                # 外部 POST 的内容不可信：text 传数字/对象时，前端 typeText 的
                # charAt 会直接抛 TypeError（气泡空白 + jsError 黑匣子报警）。
                # 非字符串一律序列化成可读文本，绝不让畸形 payload 炸穿链路。
                if not isinstance(tx, str):
                    tx = json.dumps(tx, ensure_ascii=False) if (
                        isinstance(tx, (dict, list))) else str(tx)
                em = msg['payload'].get('emotion', 'idle')
                push_message(t, tx, em)
            elif msg.get('type') == 'diagnostics' and 'payload' in msg:
                p = msg['payload']
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
                    r = generate_response(ctx)
                    if r:
                        push_message(r.get('type', 'chatMessage'), r.get('payload', {}).get('text', ''), r.get('payload', {}).get('emotion', 'idle'))
                else:
                    _builtin_reply(ctx['error_count'], ctx.get('trigger', 'diagnostics'),
                                   '', ctx.get('sample_errors'))
            elif 'trigger' in msg or 'error_count' in msg:
                if _backend_available:
                    r = generate_response(msg)
                    if r:
                        push_message(r.get('type'), r.get('payload', {}).get('text', ''), r.get('payload', {}).get('emotion', 'idle'))
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
            try:
                length = int(self.headers.get('Content-Length', 0))
            except (TypeError, ValueError):
                self.send_response(400)
                self.end_headers()
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                msg = json.loads(body)
                kind = msg.get('type')
                if kind == 'desktopReady':
                    # 这行以前是 print —— 扩展用 stdio:'ignore' 起进程，print 全被丢掉，
                    # 于是"页面到底连上没有"在 .airi-pet.log 里毫无痕迹。必须走 log()。
                    log('[airi-standalone] Desktop pet connected')
                elif kind == 'silhouetteDiag':
                    # 前端上报的轮廓诊断：桥/尺寸/alpha 读数是否可信，全靠这条
                    p = msg.get('payload') or {}
                    log('[airi-sil] %s' % json.dumps(p, ensure_ascii=False))
                elif kind == 'jsError':
                    # JS 黑匣子：前端任何未捕获错误都会落到这里。
                    # 没有它，"点击/拖拽/region 全部失明"时分不清是页面崩了
                    # 还是窗口层断了 —— 现在页面崩了会直接写出错误原文。
                    p = msg.get('payload') or {}
                    log('[airi-js] ERROR %s' % json.dumps(p, ensure_ascii=False))
                elif kind == 'bridgePing':
                    p = msg.get('payload') or {}
                    if p.get('ok'):
                        log('[airi-js] bridge ping OK (at=%s pid=%s)'
                            % (p.get('at'), p.get('pid')))
                    else:
                        log('[airi-js] bridge ping FAIL %s'
                            % json.dumps(p, ensure_ascii=False))
            except Exception as exc:
                log(f'[airi-standalone] /event parse failed: {exc!r}')
            self._json_response({'status': 'ok'})
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _json_response(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
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

def main():
    global _api_ref
    try:
        PORT = int(os.environ.get('AIRI_STANDALONE_PORT', '') or 19876)
    except ValueError:
        PORT = 19876
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

    html = load_html(PORT, char_img, live2d=l2d_ok, silhouette=sil, card=card)
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
    # （WebView2 加载器在创建环境时读这个环境变量；setdefault 留出覆盖口子）。
    os.environ.setdefault(
        'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
        '--disable-background-timer-throttling '
        '--disable-renderer-backgrounding '
        '--disable-backgrounding-occluded-windows '
        '--disable-features=IntensiveWakeUpThrottling')
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
    window = webview.create_window(title='Airi', url=page_url, width=win_w, height=win_h, x=x, y=y, frameless=True, transparent=True, on_top=True, resizable=False, easy_drag=False, js_api=api)
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
    _init_log()
    log('[airi-standalone] --- boot ---')
    main()
