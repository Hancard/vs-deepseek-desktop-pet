"""
main.py - Airi 桌面宠物入口（pywebview 窗口）

通过 SSE 连接到 Airi 桌宠服务器（standalone.py，v0.2 起扩展内不再内置
HTTP 服务器），接收消息并显示为可拖动的透明无边框桌面窗口。

【运行方式】
  python main.py --port <PORT>

  PORT: Airi 桌宠服务器端口（standalone.py，默认 19876）

【依赖】
  pip install pywebview
"""

import sys
import argparse

from common import (get_screen_size, get_dpi_scale, WindowAPI, load_html, start_asset_server,
                    disable_window_backdrop, fix_window_background, win_bg_mode)
import live2d_assets


def main():
    parser = argparse.ArgumentParser(description='deepseek Desktop Pet')
    parser.add_argument('--port', type=int, required=True,
                        help='VS Code extension HTTP server port')
    args = parser.parse_args()
    port = args.port

    try:
        import webview
    except ImportError:
        print("[airi-desktop] pywebview not installed. Run: pip install pywebview")
        sys.exit(1)

    l2d_ok, l2d_detail = live2d_assets.check_assets()
    for ln in l2d_detail:
        print(f'[airi-live2d] {ln}')
    print(f'[airi-live2d] live2d {"ENABLED" if l2d_ok else "disabled (image fallback)"}')

    # 真透明分两件事：画面靠关掉 DWM 的 Mica 背景（系统深色模式下 pywebview 会装），
    # 鼠标点穿靠 SetWindowRgn。详见 common.py 里的长注释。
    sil = live2d_assets.silhouette_enabled()
    print(f'[airi-live2d] silhouette {"ON" if sil else "OFF"}')
    card = live2d_assets.card_mode()
    print(f'[airi-live2d] character card {card}')

    html = load_html(port, live2d=l2d_ok, silhouette=sil, card=card)
    page_url, _asset_port = start_asset_server(html)
    if page_url is None:
        print('[airi-desktop] ERROR: cannot start asset server, exit.')
        sys.exit(1)

    screen_w, screen_h = get_screen_size()

    win_w, win_h = 300, 450
    # 物理像素 -> 逻辑像素换算（窗口物理尺寸是 win_w*scale，详见 standalone.py）
    _scale = get_dpi_scale()
    x = int((screen_w - win_w * _scale - 40) / _scale)
    y = int((screen_h - win_h * _scale - 120) / _scale)

    api = WindowAPI()
    # 点击互动只在 standalone.py（当前入口）接线；这里是旧入口，保持最小改动
    api.set_region_enabled(sil)
    # 但"点击/拖拽到底走到哪一层"必须留痕：这里没接 click handler，
    # 留痕能让"点了没反应"一眼看出是前端没命中还是本入口没接线。
    api.set_input_logger(lambda m: print(f'[airi-input] {m}', flush=True))

    window = webview.create_window(
        title='deepseek',
        url=page_url,
        width=win_w,
        height=win_h,
        x=x,
        y=y,
        frameless=True,
        transparent=True,
        on_top=True,
        resizable=False,
        easy_drag=False,
        js_api=api,
    )

    api.set_window(window)

    def _after_window_ready():
        """窗口建出来后：关 Mica 背景 + 处理宿主 Form 的浅灰底。

        后者不处理的话，窗口 region 的 pad 环里会露出一圈 #F0F0F0 —— 就是
        气泡/名牌周围那几块"白色方块"。见 common.fix_window_background()。
        """
        import time
        deadline = time.time() + 20
        while time.time() < deadline:
            ok, detail = disable_window_backdrop(window)
            if ok:
                print(f'[airi-dwm] Mica backdrop disabled ({detail})')
                break
            time.sleep(0.25)
        else:
            print('[airi-dwm] WARNING: 没能关掉 Mica backdrop')

        bg_deadline = time.time() + 10
        while time.time() < bg_deadline:
            ok, detail = fix_window_background(window)
            if ok:
                print(f'[airi-winbg] Form 底色处理完毕 [{win_bg_mode()}] {detail}')
                return
            time.sleep(0.25)
        print(f'[airi-winbg] WARNING: 没能处理 Form 底色（{detail}）')

    webview.start(_after_window_ready, debug=False)


if __name__ == '__main__':
    main()
