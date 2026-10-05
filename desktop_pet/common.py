"""
common.py — Airi 桌宠共用工具

被 standalone.py / main.py 引用，提供：
  - get_screen_size():          主屏幕分辨率
  - WindowAPI:                  暴露给 ui.html (window.pywebview.api) 的窗口控制
  - find_character_image():     查找自定义立绘 assets/character.png
  - load_html(port, char_img):  读取 ui.html 并注入 {{PORT}} / {{CHARACTER_IMAGE}}
                                / {{LIVE2D_ENABLED}}
  - start_asset_server(html):   起本地静态服务器，返回可交给 pywebview 的 http URL

为什么不再写临时 HTML 文件（v0.3 起）
-------------------------------------
旧做法是把 ui.html 写到 %TEMP% 再用 file:// 打开。Live2D 一上来就撞在两堵墙上：

1. **`file://` 下 fetch/XHR 一律被 CORS 拦**，model3.json 及其引用的几十个文件
   一个都取不到 —— 不是"麻烦"，是**根本加载不了**。
2. WebView2 对 `file://` 下的 WebGL 贴图本来就容易出问题。

同源 HTTP 一次解决两件事。所以窗口改为指向 http://127.0.0.1:<asset_port>/，
静态资源由本模块的服务器提供（跟 SSE 服务器分开，SSE 那边完全不用动）。
"""

import ctypes
import os
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 屏幕
# ---------------------------------------------------------------------------

def clamp_to_virtual_screen(x, y, win_w, win_h, vs, margin=60):
    """把窗口左上角钳制进虚拟桌面，保证至少 margin px 的窗口留在屏内。

    防止把桌宠一路拖出屏幕后找不回来（只能重启）。vs=(vx0, vy0, vx1, vy1)
    是虚拟桌面的物理边界（多显示器拼接后的总矩形）。
    """
    vx0, vy0, vx1, vy1 = vs
    lo_x = vx0 - win_w + margin
    hi_x = vx1 - margin
    lo_y = vy0 - win_h + margin
    hi_y = vy1 - margin
    return (min(max(x, lo_x), hi_x), min(max(y, lo_y), hi_y))


def get_virtual_screen():
    """虚拟桌面边界 (vx0, vy0, vx1, vy1)，多显示器拼接的总矩形。失败返回 None。"""
    try:
        u = ctypes.windll.user32
        vx0, vy0 = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        cw, ch = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if cw <= 0 or ch <= 0:
            return None
        return (vx0, vy0, vx0 + cw, vy0 + ch)
    except Exception:
        return None


def get_screen_size():
    """返回主屏幕分辨率 (width, height)。"""
    if sys.platform == 'win32':
        try:
            # 进程需要 DPI aware 才能拿到物理像素，否则 GetSystemMetrics
            # 返回缩放后的逻辑分辨率，导致窗口定位偏移
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass
            user32 = ctypes.windll.user32
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:
            pass
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        size = (root.winfo_screenwidth(), root.winfo_screenheight())
        root.destroy()
        return size
    except Exception:
        return (1920, 1080)


def get_dpi_scale(hwnd=None):
    """返回物理像素/逻辑像素的比例（150% 缩放 -> 1.5，量不到按 1.0）。

    为什么必须有它（v0.3.15 单屏不可见的根因）：
    pywebview 的 create_window(x=,y=) / move() 把坐标当**逻辑像素**，
    内部按窗口所在屏的 DPI 乘回物理（winforms.py: Location = Point(x*scale)）；
    而 get_screen_size()/get_virtual_screen() 在 DPI-aware 进程里拿到的是
    **物理像素**。两套坐标系直接混用，坐标会被放大 scale 倍 —— 150% 缩放
    + 2560 主屏时 x=2220 被放到物理 3330，窗口整个落到屏幕右侧之外
    （双屏拼接时落进第二块屏，所以双屏一直"看起来正常"）。
    """
    if sys.platform != 'win32':
        return 1.0
    try:
        # GetDpiForSystem 在 DPI-unaware 进程里恒返回 96（scale 恒 1.0，
        # 量了个寂寞）。本函数必须自包含、与调用顺序无关 —— 先确保 awareness。
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass  # 已设置过会返回错误码，正常
        u = ctypes.windll.user32
        dpi = 0
        if hwnd:
            try:
                dpi = u.GetDpiForWindow(int(hwnd))
            except Exception:
                dpi = 0
        if not dpi:
            try:
                dpi = u.GetDpiForSystem()
            except Exception:
                dpi = 0
        if dpi > 0:
            scale = dpi / 96.0
        else:
            # 老系统兜底：GetScaleFactorForDevice 返回百分比（100/125/150...）
            try:
                scale = max(1, ctypes.windll.shcore.GetScaleFactorForDevice(0)) / 100.0
            except Exception:
                scale = 1.0
        return scale if scale >= 1.0 else 1.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# 窗口控制（pywebview js_api）
# ---------------------------------------------------------------------------

class WindowAPI:
    """ui.html 通过 window.pywebview.api 调用：拖拽移动 / 读取位置 / 隐藏。"""

    def __init__(self):
        self._window = None
        self._click_handler = None     # fn(zone) -> (text, emotion)，由宿主注入
        self._region_enabled = True    # 异形窗口（真透明）开关
        self._region_last = None       # 上次成功的矩形数，仅用于诊断
        self._region_log = None        # 宿主注入的日志函数（standalone 写进 .airi-pet.log）
        self._region_state = None      # 上次 (ok, why)，用于只在状态变化时打日志
        self._region_calls = 0
        self._input_log = None         # 点击/拖拽留痕，见 set_input_logger()
        self._click_calls = 0
        self._move_calls = 0
        self._last_heartbeat = 0.0     # ui.html 每 2s 拍一次的 JS 存活时间戳（monotonic）
        self._hb_warn_at = 0.0         # 上次心跳滞后告警的时刻（限频用）

    def pet_heartbeat(self):
        """ui.html 每 2 秒调一次：记录 JS 主线程的存活时间戳。

        纯诊断用，不返回任何窗口状态。第十轮教训：页面定时器一旦被节流/冻结
        （全透明异形窗口容易被 Chromium 的可见性启发式误判），region 就停在
        过期形状、气泡 8s 自动消失也停摆 —— 以前这条链路是**完全静默**的，
        用户只能看到"气泡被切了一截却永远不恢复"。
        """
        self._last_heartbeat = time.monotonic()
        return True

    def js_heartbeat_lag(self):
        """距上次 JS 心跳的秒数；从没收到过心跳返回 None。"""
        if not self._last_heartbeat:
            return None
        return time.monotonic() - self._last_heartbeat

    def set_input_logger(self, fn):
        """宿主注入日志函数：fn(str) —— 给「点击互动 / 拖拽」这条链路留痕。

        为什么必须留痕（第六轮教训）：用户报"点击没反应、也拖不动"时，**整条链路
        一句日志都没有** —— 前端 mousedown 有没有触发、桥有没有把调用送过来、
        pet_click 有没有真的被调到，全都看不见，只能猜。
        这条链路跨 JS -> pywebview 桥 -> Python 三层，和 region 那条一样必须每层留痕。

        注意 move_window 是**每帧**被调的，所以只记第一次 + 每 60 次一条，
        否则日志会被刷爆（刷爆的日志等于没有日志）。
        """
        self._input_log = fn

    def _log_input(self, msg):
        if self._input_log is None:
            return
        try:
            self._input_log(msg)
        except Exception:
            pass

    def set_region_logger(self, fn):
        """宿主注入日志函数：fn(str)。

        为什么非要这个 —— 异形窗口以前是**静默降级**的：前端失败 3 次就永久
        放弃，宿主侧一句日志都没有。用户看到的就是"窗口还是个方块"，
        而日志里只到 "Desktop pet started"，完全无从判断卡在哪一环。
        这条链路跨 JS / 桥 / ctypes 三层，必须每一层都留痕。
        """
        self._region_log = fn

    def _log_region(self, msg):
        if self._region_log is None:
            return
        try:
            self._region_log(msg)
        except Exception:
            pass

    def set_window(self, window):
        self._window = window

    def ping(self):
        """桥自检：前端启动时调一次，能返回就说明 JS->pywebview->Python 通了。

        第六轮教训：这条链路一断，点击/拖拽/region 三条链**同时**失明，
        而日志里一行痕迹都没有 —— 因为留痕的代码全在"链路正常才会被调"
        的方法里。ping 是唯一一个不依赖任何交互就会被打的方法。
        """
        self._log_input('ping -> ok')
        return {'ok': True, 'pid': os.getpid()}

    def get_position(self):
        if self._window is None:
            self._log_input('get_position -> no window')
            return [0, 0]
        pos = [self._window.x or 0, self._window.y or 0]
        self._log_input('get_position -> %s' % pos)
        return pos

    def move_window(self, x, y):
        if self._window is None:
            self._move_calls += 1
            self._log_input('move_window x%d -> no window' % self._move_calls)
            return
        self._move_calls += 1
        if self._move_calls == 1 or self._move_calls % 60 == 0:
            self._log_input('move_window #%d -> (%s,%s)' % (self._move_calls, x, y))
        try:
            # 拖拽钳制：至少留 60px 在虚拟桌面内，防止把桌宠拖出屏幕找不回来。
            # 钳制失败（非 Windows/度量异常）就按原坐标移 —— 移动永远不能挂。
            #
            # 坐标系必须统一（v0.3.15）：x/y 与 win_w/win_h 来自 pywebview，
            # 是**逻辑像素**；get_virtual_screen() 在 DPI-aware 进程里给的是
            # **物理像素**。150% 缩放下直接拿物理边界钳逻辑坐标，右/下边界
            # 的钳制永远够不到（逻辑值 < 物理边界）→ 等于没有钳制，桌宠
            # 照样能被拖出屏幕。先把边界除以 scale 转成逻辑再钳。
            vs = get_virtual_screen()
            if vs is not None:
                win_w = int(getattr(self._window, 'width', 0) or 300)
                win_h = int(getattr(self._window, 'height', 0) or 450)
                hwnd = None
                try:
                    hwnd = self._window.native.Handle
                except Exception:
                    hwnd = None
                scale = get_dpi_scale(hwnd)
                if scale != 1.0:
                    vs = tuple(v / scale for v in vs)
                x, y = clamp_to_virtual_screen(int(x), int(y), win_w, win_h, vs)
            self._window.move(int(x), int(y))
        except Exception as exc:
            self._log_input('move_window FAILED %r' % (exc,))

    def exit_app(self):
        """销毁窗口并退出进程（webview.start 返回后主线程结束，daemon 线程随之退出）"""
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 点击互动
    # ------------------------------------------------------------------

    def set_click_handler(self, fn):
        """宿主注入取词回调：fn(zone) -> (text, emotion)。

        台词语料放在宿主（standalone.py）而不是这里 —— common 只负责
        "前端点了角色 -> 问宿主要一句话 -> 回给前端"，不掺业务内容。
        """
        self._click_handler = fn

    def pet_click(self, zone):
        """ui.html 命中角色后调用，返回 {text, emotion}。

        前端只在**真的点到角色像素上**时才调这里（用 canvas alpha 判定），
        所以这里不必再管命中，只管取词。

        ⚠️ 因此：**日志里没有 pet_click，就说明前端认为你没点到角色**
        （alphaAt() < 24 就静默 return），而不是台词库坏了 —— 这两件事
        以前完全分不开，只能猜。
        """
        self._click_calls += 1
        try:
            if self._click_handler is not None:
                text, emotion = self._click_handler(str(zone or 'body'))
                self._log_input('pet_click #%d zone=%s -> %r'
                                % (self._click_calls, zone, (text or '')[:18]))
                return {'text': text or '', 'emotion': emotion or 'idle'}
            self._log_input('pet_click #%d zone=%s -> NO HANDLER'
                            % (self._click_calls, zone))
        except Exception as exc:
            self._log_input('pet_click #%d FAILED %r' % (self._click_calls, exc))
            print(f'[airi-common] click handler failed: {exc!r}')
        return {'text': '', 'emotion': 'idle'}

    # ------------------------------------------------------------------
    # 异形窗口 —— 这是 Windows 上让桌宠"真透明"的唯一可行路线
    # ------------------------------------------------------------------

    def set_region_enabled(self, flag):
        self._region_enabled = bool(flag)

    def set_window_region(self, payload):
        """把窗口裁成 ui.html 上报的形状 —— 只影响**鼠标命中**，不负责画面透明。

        排查链的结论（实测数据见 live2d_probe/live_fix3.txt、live_fix4.txt）：
          1. 画面上的透明**不是这里做的**。pywebview 把 WebView2 那层设成
             DefaultBackgroundColor=Transparent，页面又是 background:transparent，
             所以角色之外本来就是透明的；用户看到的那块实心方块是
             **DWM 的 Mica 背景材质**，由 pywebview 的 update_title_bar_theme()
             在系统深色模式下装上。它不在 GDI 表面里，所以 region 裁不到它
             —— 这正是"region 设了却看不见效果"的原因。
             关掉 Mica 画面就是真透明（屏幕实测 0.00% → 90.49%），
             见 common.disable_window_backdrop()。
          2. region 在这里只负责**点穿**：窗口形状之外不接受鼠标消息，
             点角色以外的位置会落到下层窗口（比如 VS Code），
             不会让桌宠占着的那块地方变成点击黑洞。
             实测：客户区 (20,20)（形状之外）WindowFromPoint 命中的是别的窗口；
             (140,220)（形状之内）命中的是桌宠自己。

        payload = {'vw': 窗口 CSS 宽, 'vh': 窗口 CSS 高,
                   'rects': [[left, top, right, bottom], ...]}（页面 CSS 像素）
        """
        if not self._region_enabled:
            return {'ok': False, 'why': 'disabled'}
        if self._window is None:
            return {'ok': False, 'why': 'no window'}
        self._region_calls += 1
        try:
            p = payload or {}
            rects = p.get('rects') or []
            vw = float(p.get('vw') or 0) or 1.0
            vh = float(p.get('vh') or 0) or 1.0
            src = str(p.get('src') or '?')
            res = _apply_window_region(self._window, rects, vw, vh)
            res['src'] = src
            res['in_rects'] = len(rects)
            res['vw'] = vw
            res['vh'] = vh
            if res.get('ok'):
                self._region_last = res.get('rects')
            # 只在状态变化时打日志 + 失败时每 20 次补一条，保证日志不刷屏但绝不静默
            # 心跳滞后 >3s 说明 JS 定时器正在被节流/冻结 —— 这时推上来的形状
            # 很可能是过期的，而且之后不会再有更新来纠正它，必须留痕。
            lag = self.js_heartbeat_lag()
            if lag is not None and lag > 3.0 and time.monotonic() - self._hb_warn_at > 30:
                self._hb_warn_at = time.monotonic()
                self._log_region('WARNING: JS 心跳滞后 %.1fs —— 页面定时器可能被节流/冻结，'
                                 '窗口形状可能停在过期状态' % lag)
            state = (bool(res.get('ok')), res.get('why'))
            if state != self._region_state or (
                    not res.get('ok') and self._region_calls % 20 == 0):
                self._region_state = state
                meta = res.get('meta') or {}
                self._log_region(
                    'region #%d src=%s in=%d out=%s ok=%s why=%s '
                    'hwnd=%s client=%s scale=%s vw=%s vh=%s'
                    % (self._region_calls, src, len(rects), res.get('rects'),
                       res.get('ok'), res.get('why'),
                       meta.get('hwnd'), meta.get('client'), meta.get('scale'),
                       int(vw), int(vh)))
            return res
        except Exception as exc:
            import traceback as _tb
            self._log_region('region #%d EXCEPTION %r\n%s'
                             % (self._region_calls, exc, _tb.format_exc()))
            return {'ok': False, 'why': repr(exc)}


# --- 窗口 region 的底层实现 ------------------------------------------------

def _get_form(window):
    """取 pywebview 窗口背后的 WinForms BrowserForm 实例。

    pywebview 没有公开 HWND，只能从它的 winforms 后端翻 BrowserForm 实例
    （winforms.py 里 `BrowserView.instances[window.uid] = browser`）。
    取不到返回 None，调用方一律静默降级 —— 这些都是锦上添花，
    绝不该因为它们让桌宠起不来。
    """
    try:
        from webview.platforms import winforms as _wf
        instances = getattr(_wf.BrowserView, 'instances', None)
        if not instances:
            return None
        uid = getattr(window, 'uid', None)
        form = instances.get(uid) if uid is not None else None
        if form is None:
            form = list(instances.values())[-1]
        return form
    except Exception:
        return None


def _get_hwnd(window):
    """取 pywebview 窗口背后的 Win32 HWND（取不到返回 0）。"""
    form = _get_form(window)
    if form is None:
        return 0
    try:
        return int(form.Handle.ToInt64())
    except Exception:
        return 0


# --- DWM 背景材质（Mica）----------------------------------------------------
#
# 这是「窗口根本不透明」的**真正根因**，别再往别处找。
#
# pywebview 的 BrowserForm.update_title_bar_theme()（winforms.py:333）在
# **系统深色模式**下会执行：
#     DwmSetWindowAttribute(hwnd, 38, 2)   # 38=DWMWA_SYSTEMBACKDROP_TYPE
#                                          # 2=DWMSBT_MAINWINDOW，也就是 Mica
# 浅色模式下它设的是 1(DWMSBT_NONE)，不装。
#
# Mica 由 DWM 自己绘制，**不在窗口的 GDI 重定向表面里**。这一条一举解释了
# 之前全部三个"见了鬼"的现象（实测见 live2d_probe/live_fix3.txt）：
#   * SetWindowRgn 返回成功、GetWindowRgn 也能读回 138x220，屏幕上一像素不裁；
#   * SetLayeredWindowAttributes(LWA_COLORKEY, #202020) 返回成功，挖不掉底色；
#   * 只有整窗 LWA_ALPHA 有效 —— 因为它作用在最终合成结果上。
# 屏幕实测（live2d_probe/live_fix4.txt，可反向复现）：
#     backdrop=2  → 窗口区域与桌面一致的像素 0.00%（整块不透明）
#     改成 NONE   → 立刻 90.49%（角色周围真透出桌面）
#     改回 2      → 又回到 0.00%
#
# 这还顺带解释了用户两张截图为什么颜色不同：那块底色跟着系统主题在
# #F0F0F0（浅）/ #202020（深）之间变，而 SystemColors.Control 恰好也是这两个值。

DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_NONE = 1


def _set_backdrop_none(hwnd):
    """把窗口的 DWM 背景材质设成 None（关掉 Mica）。成功返回 True。"""
    if not hwnd:
        return False
    try:
        v = ctypes.c_int(DWMSBT_NONE)
        rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_uint(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(v), ctypes.c_uint(ctypes.sizeof(v)))
        return rc == 0
    except Exception:
        return False


def get_backdrop_type(hwnd):
    """读回 DWMWA_SYSTEMBACKDROP_TYPE，仅用于诊断；读不到返回 -1。"""
    try:
        v = ctypes.c_int(-1)
        rc = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(int(hwnd)),
            ctypes.c_uint(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(v), ctypes.c_uint(ctypes.sizeof(v)))
        return v.value if rc == 0 else -1
    except Exception:
        return -1


def disable_window_backdrop(window):
    """关掉 DWM 的 Mica 背景，并防止它被重新装上。返回 (ok, detail)。

    为什么还要"防止重新装上"：切换系统主题会触发
    SystemEvents.UserPreferenceChanged -> update_title_bar_theme()，
    深色模式下 Mica 会被**重新装回去**，窗口立刻又变回一块实心方块。
    所以这里把该实例方法包一层，之后每次调用完都再按一次。
    """
    if sys.platform != 'win32':
        return False, 'not win32'
    form = _get_form(window)
    if form is None:
        return False, 'no BrowserForm instance yet'
    hwnd = _get_hwnd(window)
    if not hwnd:
        return False, 'no hwnd'

    before = get_backdrop_type(hwnd)
    ok = _set_backdrop_none(hwnd)
    after = get_backdrop_type(hwnd)

    guarded = bool(getattr(form, '_airi_backdrop_guarded', False))
    if not guarded:
        try:
            original = form.update_title_bar_theme

            def _guarded():
                try:
                    original()
                finally:
                    _set_backdrop_none(_get_hwnd(window))

            form.update_title_bar_theme = _guarded
            form._airi_backdrop_guarded = True
            guarded = True
        except Exception:
            pass

    detail = ('hwnd=%s backdrop=%s->%s set=%s guarded=%s'
              % (hex(hwnd), before, after, ok, guarded))
    return (ok and after == DWMSBT_NONE), detail


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


# --- 宿主 Form 的底色（第五轮找到的"块状白"真正来源）------------------------
#
# pywebview 的 winforms.py:286-292 在 transparent=True 时只做了两件事：
#     self.SetStyle(WinForms.ControlStyles.SupportsTransparentBackColor, True)
#     self.browser.DefaultBackgroundColor = Color.Transparent
# **它没有给 Form.BackColor 赋值** —— 走透明分支时那句
# `self.BackColor = ColorTranslator.FromHtml(window.background_color)` 在 else 里，
# 于是 Form 保持 WinForms 的默认底色 SystemColors.Control（浅色主题 = #F0F0F0）。
#
# WebView2 那层确实是透明的，但"透明"露出来的是**这张浅灰底**。
# 平时看不到，是因为窗口被 region 裁成了 DOM 元素的形状 —— region 之外整个
# 窗口被剪掉，显出来的是桌面。而 region 的矩形是 DOM 盒**加了 pad** 的
# （见 ui.html collectUiRects），那一圈 pad 环里页面什么都没画 ⇒ 浅灰底露出来。
#
# 用户截图里的三块"白"因此全部对得上（第五轮实测，见 live2d_probe/README.md）：
#     气泡  : DOM 盒 212x36.6 + pad5 -> 222x46.6   截图实测 222x46   ✓
#     Airi  : DOM 盒  33x15.6 + pad3 ->  39x21.6   截图实测  40x20   ✓
#     online: DOM 盒  37x14   + pad3 ->  43x20     截图实测  44x20   ✓
#
# 修法（v0.3.4 修订）：把 Form 底色压成**近黑**，pad 环收窄到 1px ——
# 细黑边视觉上是一条投影线，不再是块状白。
#
# ⚠️ 第六轮的教训：颜色键（挖洞）路线在这台机器上**不可用**，三条证据：
#   1. 隔离实验（live2d_probe/winbg_fix6.txt）：给 Form 赋 .NET 的
#      TransparencyKey 属性，GetLayeredWindowAttributes 读回
#      flags=LWA_ALPHA alpha=0 —— 不是 LWA_COLORKEY！alpha=0 的分层窗口
#      = 完全不可见 + 对鼠标完全穿透（"点击没反应、也拖不动"）。
#   2. 绕开 .NET 用 ctypes 直接 SetLayeredWindowAttributes(LWA_COLORKEY)：
#      调用返回成功、flags 也对，但**键色读回恒为 #000000**（五种写法全试过，
#      见 live2d_probe/colorkey_test2.txt）。键色存不进去 = 挖洞行为不可控。
#   3. 颜色键的鼠标命中判定作用在窗口的 GDI 表面上，而 WebView2 的画面走
#      DirectComposition —— 不在 GDI 表面里。键控之后鼠标判定很可能认为
#      "整个窗口都是键色"→ 整窗穿透，表现成"看得见但点不着"。
#      （第五轮的 A/B 只验证了画面，没验证输入，漏掉了这一层。）
#
# AIRI_WIN_BG:
#   dark  默认。只把 Form 底色压黑，不碰分层 —— 输入路径与 v0.3.2 完全一致
#   key   opt-in 实验：底色=键色=#010203 + ctypes 打颜色键（在本机不可控，勿依赖）
#   off   完全不动（A/B 用）
FORM_BG_KEY = (1, 2, 3)
WIN_BG_MODES = ('key', 'dark', 'off')

_WS_EX_LAYERED = 0x00080000
_GWL_EXSTYLE = -20
_LWA_COLORKEY = 0x2
_LWA_ALPHA = 0x1

# 这三个调用必须带全 argtypes —— 第六轮实测：不带的时候
# SetLayeredWindowAttributes 打出去的键色会变成 #000000（读回证实），
# 黑键会把模型画里所有纯黑像素都挖成洞。声明齐了读回才是 #030201。
_user32 = ctypes.WinDLL('user32')
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long
_user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_ubyte, ctypes.c_uint32]
_user32.SetLayeredWindowAttributes.restype = ctypes.c_int
_user32.GetLayeredWindowAttributes.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_ubyte),
                                               ctypes.POINTER(ctypes.c_uint32)]
_user32.GetLayeredWindowAttributes.restype = ctypes.c_int
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_uint]
_user32.SetWindowPos.restype = ctypes.c_int


def win_bg_mode():
    v = os.environ.get('AIRI_WIN_BG', '').strip().lower()
    # 默认 dark：颜色键路线在本机不可控（见上面的长注释），宁可细黑边也别
    # 冒"整窗不可见/穿透"的险。
    return v if v in WIN_BG_MODES else 'dark'


def _apply_colorkey(hwnd, rgb):
    """绕开 .NET，直接给顶层窗口打 LWA_COLORKEY。返回 (ok, detail)。

    detail 里带**读回的**分层状态 —— SetLayeredWindowAttributes 返回 1
    只代表调用成功，不代表状态就是我们要的（第六轮的 .NET 属性就是这么骗人的）。
    """
    try:
        key = rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)
        ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if not (ex & _WS_EX_LAYERED):
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex | _WS_EX_LAYERED)
            # 样式变更必须用 SetWindowPos(SWP_FRAMECHANGED) 强制生效，
            # 否则紧接着的 SetLayeredWindowAttributes 会"返回成功但状态没落"
            # （第六轮实测：键色读回 #000000，正确值应是 #030201）。
            SWP_NOSIZE = 0x1; SWP_NOMOVE = 0x2; SWP_NOZORDER = 0x4
            SWP_NOACTIVATE = 0x10; SWP_FRAMECHANGED = 0x20
            _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                 SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                                 | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        ok = _user32.SetLayeredWindowAttributes(hwnd, key, 0, _LWA_COLORKEY)
        # 读回验证
        k = ctypes.c_uint32(0)
        alpha = ctypes.c_ubyte(0)
        flags = ctypes.c_uint32(0)
        read_ok = _user32.GetLayeredWindowAttributes(
            hwnd, ctypes.byref(k), ctypes.byref(alpha), ctypes.byref(flags))
        keyed = bool(read_ok) and bool(flags.value & _LWA_COLORKEY)
        alpha_on = bool(read_ok) and bool(flags.value & _LWA_ALPHA)
        key_ok = keyed and (k.value & 0xFFFFFF) == key
        detail = ('set=%s read=%s flags=%d key=#%06X alpha=%d %s'
                  % (bool(ok), bool(read_ok), flags.value, k.value & 0xFFFFFF,
                     alpha.value,
                     'OK' if (key_ok and not alpha_on) else
                     '!! 键色不对' if keyed else
                     '!! ALPHA 也开着，整窗会被 alpha 混合' if alpha_on else
                     '!! 颜色键没生效'))
        return (key_ok and not alpha_on), detail
    except Exception as e:
        return (False, 'apply failed: %r' % (e,))


def fix_window_background(window):
    """把宿主 Form 的浅灰底压掉/挖掉，返回 (ok, detail)。

    不动的后果：窗口 region 里凡是页面没画的像素都是 #F0F0F0 的浅灰，
    表现为气泡/名牌/名字周围一圈"块状白"。详见上面那段长注释。

    ⚠️⚠️ 第六轮最大的教训（比颜色键本身更重要）：
    本函数跑在 webview.start() 的**后台线程**里。WinForms 控件不是线程
    安全的 —— v0.3.3 在这里直接给 form.BackColor / form.TransparencyKey
    赋值，其中 TransparencyKey 内部会 UpdateStyles() -> RecreateHandle()，
    跨线程重建窗口句柄直接把 GUI 线程搞挂：pywebview 之后的 JS 桥注入
    （pywebviewready）永远不会完成 => 点击/拖拽/region 三条链**同时失明**
    （用户报的回归就是这么来的；黑匣子证据：bridge ping FAIL "api 未就绪"
    且 pywebviewready 永不触发，见 live2d_probe/_live6_off.log）。
    所以一切 .NET 属性访问必须 BeginInvoke 编组到 GUI 线程执行。
    """
    mode = win_bg_mode()
    if mode == 'off':
        return (False, 'AIRI_WIN_BG=off（不动底色）')
    form = _get_form(window)
    if form is None:
        return (False, 'no form')

    box = {}
    try:
        from System.Windows.Forms import MethodInvoker

        def _work():
            box['before'] = str(form.BackColor)
            from System.Drawing import Color
            rgb = FORM_BG_KEY if mode == 'key' else (0, 0, 0)
            form.BackColor = Color.FromArgb(rgb[0], rgb[1], rgb[2])
            box['now'] = str(form.BackColor)
            box['rgb'] = rgb
            try:
                box['hwnd'] = int(form.Handle.ToInt64())
            except Exception:
                box['hwnd'] = 0

        form.BeginInvoke(MethodInvoker(_work))
        deadline = time.time() + 3
        while 'now' not in box and time.time() < deadline:
            time.sleep(0.05)
    except Exception as e:
        return (False, 'marshal failed: %r' % (e,))
    if 'now' not in box:
        return (False, 'GUI 线程 3s 内没执行底色设置（BeginInvoke 没被处理）')

    rgb = box.get('rgb', (0, 0, 0))
    hole = 'no'
    detail2 = ''
    ok = True
    if mode == 'key':
        # 注意：不碰 form.TransparencyKey（.NET 属性，会把窗口打成
        # LWA_ALPHA alpha=0，见上面的长注释）。纯 ctypes 的 Win32 调用
        # 是线程安全的，可以留在后台线程。
        hwnd = box.get('hwnd') or 0
        if hwnd:
            hole_ok, detail2 = _apply_colorkey(hwnd, rgb)
            hole = 'yes' if hole_ok else 'FAILED'
            ok = hole_ok
        else:
            hole = 'FAILED no-hwnd'
            ok = False
    return (ok, 'backdrop=%s -> %s hole=%s key=#%02X%02X%02X %s'
            % (box.get('before'), box.get('now'), hole,
               rgb[0], rgb[1], rgb[2], detail2))


def _apply_window_region(window, rects, vw, vh):
    """按前端上报的形状重建窗口 region。

    第七轮起 region 从「矩形 + pad」升级成「与页面绘制同形状」，消灭黑边：
      * [l, t, r, b]              纯矩形（Live2D 轮廓扫描的行程矩形还在用）
      * {l,t,r,b, rad}            圆角矩形（气泡 14 / 名牌板 10 / 角色卡 16）
      * {poly: [[x,y], ...]}      多边形（气泡尾巴的三角形）

    两组换算缺一不可：
      * 页面 CSS 像素 -> 窗口客户区**物理**像素（1.25 / 1.5 缩放很常见，
        用 GetClientRect 的实际尺寸除以前端上报的视口尺寸，比自己猜 DPI 稳）
      * region 坐标系原点就是客户区左上角；frameless 窗口没有非客户区，不用再减
    """
    meta = {}
    if sys.platform != 'win32':
        return {'ok': False, 'why': 'not win32', 'meta': meta}
    if not rects:
        # 空列表 = 整个窗口都不可见。宁可不裁，也绝不做出一个
        # "看不见又关不掉"的窗口。
        return {'ok': False, 'why': 'empty rects (refused)', 'meta': meta}

    hwnd = _get_hwnd(window)
    meta['hwnd'] = hex(hwnd) if hwnd else '0'
    if not hwnd:
        return {'ok': False, 'why': 'no hwnd', 'meta': meta}

    # 保险：region 只管鼠标命中，画面透明靠的是 Mica 关着。
    # 万一主题切换把它装回来了，这里顺手再按一次（单次 syscall，很便宜）。
    _set_backdrop_none(hwnd)

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
    for fn, name in ((gdi32.CreateRectRgn, 'CreateRectRgn'),
                     (gdi32.CreateRoundRectRgn, 'CreateRoundRectRgn'),
                     (gdi32.CreatePolygonRgn, 'CreatePolygonRgn')):
        fn.restype = ctypes.c_void_p
    # ⚠️ CreateRectRgn / CreateRoundRectRgn / CreatePolygonRgn / CombineRgn
    # 全是 **gdi32.dll** 的导出，只有 SetWindowRgn / GetClientRect 在 user32。
    # 第八轮教训：写成 user32.CreateRectRgn 不会在 py_compile 报错，而是每次
    # 运行时抛 AttributeError("function 'CreateRectRgn' not found")，
    # region 一条都应用不上（probe 日志 region #155 EXCEPTion 即此）。
    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_long] * 6
    gdi32.CreatePolygonRgn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CombineRgn.restype = ctypes.c_int
    gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_int]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    RGN_OR = 2        # NULLREGION=1 RGN_OR=2 XOR=3 ...
    WINDING = 2       # ALTERNATE=1 WINDING=2

    rc = _RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return {'ok': False, 'why': 'GetClientRect failed', 'meta': meta}
    cw, ch = rc.right - rc.left, rc.bottom - rc.top
    meta['client'] = '%dx%d' % (cw, ch)
    if cw <= 1 or ch <= 1:
        return {'ok': False, 'why': 'client rect empty', 'meta': meta}

    sx, sy = cw / vw, ch / vh
    meta['scale'] = '%.4f,%.4f' % (sx, sy)

    def _clip_x(v):
        return max(0, min(cw, v))

    def _clip_y(v):
        return max(0, min(ch, v))

    h_total = gdi32.CreateRectRgn(0, 0, 0, 0)   # 空区域，逐块 RGN_OR 并进去
    if not h_total:
        return {'ok': False, 'why': 'CreateRectRgn failed', 'meta': meta}
    kept = 0
    minx, miny, maxx, maxy = cw, ch, 0, 0

    def _merge(h_piece):
        # 合并成功后 piece 归 h_total 所有，必须删掉 piece 句柄防泄漏
        rc2 = gdi32.CombineRgn(h_total, h_total, h_piece, RGN_OR)
        gdi32.DeleteObject(h_piece)
        return rc2

    try:
        for entry in rects:
            h_piece = None
            try:
                if isinstance(entry, dict):
                    if entry.get('poly'):
                        pts = []
                        for p in entry['poly']:
                            px = _clip_x(int(round(float(p[0]) * sx)))
                            py = _clip_y(int(round(float(p[1]) * sy)))
                            pts.append(_POINT(px, py))
                        if len(pts) < 3:
                            continue
                        arr = (_POINT * len(pts))(*pts)
                        h_piece = gdi32.CreatePolygonRgn(
                            arr, len(pts), WINDING)
                        xs = [p.x for p in pts]
                        ys = [p.y for p in pts]
                        l, t, r, b = min(xs), min(ys), max(xs), max(ys)
                    else:
                        l = _clip_x(int(round(float(entry['l']) * sx)))
                        t = _clip_y(int(round(float(entry['t']) * sy)))
                        r = _clip_x(int(round(float(entry['r']) * sx)))
                        b = _clip_y(int(round(float(entry['b']) * sy)))
                        rad = float(entry.get('rad', 0) or 0)
                        if r <= l or b <= t:
                            continue
                        if rad > 0.5:
                            ew = max(2, int(round(rad * sx * 2)))
                            eh = max(2, int(round(rad * sy * 2)))
                            h_piece = gdi32.CreateRoundRectRgn(
                                l, t, r, b, ew, eh)
                        else:
                            h_piece = gdi32.CreateRectRgn(l, t, r, b)
                else:
                    l = _clip_x(int(round(float(entry[0]) * sx)))
                    t = _clip_y(int(round(float(entry[1]) * sy)))
                    r = _clip_x(int(round(float(entry[2]) * sx)))
                    b = _clip_y(int(round(float(entry[3]) * sy)))
                    if r <= l or b <= t:
                        continue
                    h_piece = gdi32.CreateRectRgn(l, t, r, b)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if not h_piece:
                continue
            minx = min(minx, l); miny = min(miny, t)
            maxx = max(maxx, r); maxy = max(maxy, b)
            _merge(h_piece)
            kept += 1

        meta['kept'] = kept
        if not kept:
            return {'ok': False, 'why': 'all rects clipped away', 'meta': meta}

        # SetWindowRgn 成功后 region 归系统所有，不能再 DeleteObject
        if not user32.SetWindowRgn(hwnd, h_total, True):
            gdi32.DeleteObject(h_total)
            return {'ok': False, 'why': 'SetWindowRgn failed', 'meta': meta}
        meta['bbox'] = '%d,%d,%d,%d' % (minx, miny, maxx, maxy)
        return {'ok': True, 'rects': kept, 'meta': meta}
    except Exception:
        # 兜底：句柄还没交给系统，得自己删
        try:
            gdi32.DeleteObject(h_total)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# 立绘与 HTML
# ---------------------------------------------------------------------------

def find_character_image():
    """查找自定义立绘，找不到返回 None（ui.html 会退回 CSS 角色）。

    优先级：AIRI_CHARACTER_IMAGE 环境变量 > assets/character.* > 根目录 character.png
    """
    env_path = os.environ.get('AIRI_CHARACTER_IMAGE', '').strip()
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return str(p)
        print(f'[airi-common] WARNING: AIRI_CHARACTER_IMAGE not found: {env_path}')
    for name in ('character.png', 'character.gif', 'character.webp'):
        p = _SCRIPT_DIR / 'assets' / name
        if p.is_file():
            return str(p)
    p = _SCRIPT_DIR / 'character.png'
    return str(p) if p.is_file() else None


def load_html(port, char_img=None, live2d=False, silhouette=False, card='square'):
    """读取 ui.html，注入端口、立绘 file:// URI 和各个开关，返回最终 HTML 字符串。

    live2d 为 True 时才让页面去加载引擎；素材缺失时保持 False，
    页面就还走原来的图片 / CSS 角色，不会在控制台刷一堆 404。

    silhouette 为 True 时页面会定期把「角色轮廓 + 气泡 + 名字」的形状上报给
    WindowAPI.set_window_region()，由 Python 侧 SetWindowRgn 把窗口裁成异形。
    ⚠️ 它**只管鼠标点穿**，不管画面透明 —— 画面透明是关掉 DWM 的 Mica 背景材质
    （见 disable_window_backdrop()）。

    card 是角色底板模式（off/tight/square/frame/all），见 live2d_assets.card_mode()。
    默认与 card_mode() 保持一致 —— 两处默认值不一样的话，将来谁漏传一次 card，
    就会得到和界面其它地方不同的观感，很难查。
    """
    html = (_SCRIPT_DIR / 'ui.html').read_text(encoding='utf-8')
    img_uri = Path(char_img).as_uri() if char_img else ''
    html = html.replace('{{PORT}}', str(int(port)))
    html = html.replace('{{CHARACTER_IMAGE}}', img_uri)
    html = html.replace('{{LIVE2D_ENABLED}}', 'true' if live2d else 'false')
    html = html.replace('{{SILHOUETTE}}', 'true' if silhouette else 'false')
    html = html.replace('{{CHAR_CARD}}', str(card or 'off'))
    return html


# ---------------------------------------------------------------------------
# 本地静态资源服务器（Live2D 素材 + 页面本体）
# ---------------------------------------------------------------------------

_MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.txt': 'text/plain; charset=utf-8',
    '.md': 'text/plain; charset=utf-8',
    # Cubism 原生网格，无标准 MIME
    '.moc3': 'application/octet-stream',
}


def _safe_join(base: Path, url_path: str):
    """把 URL 路径安全映射到 base 之下；越界（含 `..`、绝对路径）返回 None。"""
    rel = urllib.parse.unquote(url_path)
    rel = rel.replace('\\', '/').lstrip('/')
    if not rel:
        return None
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def _make_handler(html_text: str):
    """闭包出一个绑定了当前 HTML 的处理器类。"""
    from live2d_assets import VENDOR_DIR, MODEL_ROOT

    page = html_text.encode('utf-8')

    class _AssetHandler(SimpleHTTPRequestHandler):
        server_version = 'AiriAsset/1.0'
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            pass          # 桌宠不该往控制台刷请求日志

        def handle_error(self, request, client_address):
            """吞掉「客户端提前断开」这类噪声。

            WebView2 关窗/刷新时，正在传输的请求会被 reset，socketserver 的默认
            handle_error 会把 ConnectionResetError / BrokenPipeError 打成一整段
            traceback —— 看起来像崩溃，其实是正常收尾。真正意外的错误照旧抛出去。
            """
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                BrokenPipeError, TimeoutError)):
                return
            super().handle_error(request, client_address)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path

            if path in ('/', '/index.html', '/ui.html'):
                return self._blob(page, 'text/html; charset=utf-8')

            if path == '/favicon.ico':
                return self._plain(204, b'')

            # 引擎 js 与 core
            if path.startswith('/vendor/'):
                target = _safe_join(VENDOR_DIR, path[len('/vendor/'):])
                if target is None:
                    return self._plain(403, b'forbidden path')
                if not target.is_file():
                    return self._plain(404, b'not found: ' + path.encode('utf-8'))
                return self._file(target)

            # Live2D 模型闭包
            if path.startswith('/pet-assets/'):
                target = _safe_join(MODEL_ROOT, path[len('/pet-assets/'):])
                if target is None:
                    return self._plain(403, b'forbidden path')
                if not target.is_file():
                    return self._plain(404, b'not found: ' + path.encode('utf-8'))
                return self._file(target)

            return self._plain(404, b'not found: ' + path.encode('utf-8'))

        def _file(self, target: Path):
            try:
                blob = target.read_bytes()
            except OSError:
                return self._plain(404, b'unreadable')
            ctype = _MIME.get(target.suffix.lower(), 'application/octet-stream')
            return self._blob(blob, ctype)

        def _blob(self, blob: bytes, ctype: str):
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(blob)))
            # 桌宠每次启动都重读本地文件，别让 WebView2 缓存住旧模型
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(blob)
            except Exception:
                pass

        def _plain(self, code: int, blob: bytes):
            try:
                self.send_response(code)
                # HTTP/1.1 keep-alive 必须有准确的 Content-Length；
                # 204/304 按定义没有 body，不能带
                if code not in (204, 304):
                    self.send_header('Content-Length', str(len(blob)))
                self.end_headers()
                if blob:
                    self.wfile.write(blob)
            except Exception:
                pass

    return _AssetHandler


def start_asset_server(html_text: str, port: int = 0):
    """起一个本地静态服务器，返回 (base_url, actual_port)。失败返回 (None, 0)。

    port=0 表示让系统分配空闲端口；AIRI_ASSET_PORT 可固定端口，便于调试。
    """
    if not port:
        try:
            port = int(os.environ.get('AIRI_ASSET_PORT', '') or 0)
        except ValueError:
            port = 0

    try:
        srv = ThreadingHTTPServer(('127.0.0.1', port), _make_handler(html_text))
    except OSError as exc:
        print(f'[airi-common] asset server failed to bind: {exc!r}')
        return None, 0

    srv.daemon_threads = True
    actual = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{actual}/', actual
