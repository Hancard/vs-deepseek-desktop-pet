# -*- coding: utf-8 -*-
"""measure_click_zones.py — 量出角色在 canvas 里的真实轮廓，给 zoneAt() 定边界

为什么需要它：zoneAt() 现在是**拍脑袋的固定比例**（<0.45 head / <0.85 body /
else desk，2026-09-22 定的）。而模型没声明 HitAreas、没有 Layout，预览图又是
不透明的海报图 —— 没有任何离线数据能告诉我们"桌子到底画在第几行"。
无头浏览器在本机也不可用，所以只能开一个**真的** pywebview 窗口，等 Live2D
就绪后把 canvas 的 alpha 通道读回来，画成字符画 + 行向不透明度曲线。

产物：live2d_probe/zone_profile.txt（字符画 + 每行占比），肉眼就能读出
"头在哪几行、身体在哪几行、桌子在哪几行"。

用法（必须用带 pywebview 的解释器，系统 Python 3.12）：
    python measure_click_zones.py
"""
import json
import os
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PROJ, 'desktop_pet'))
sys.path.insert(0, os.path.join(_PROJ, 'python_backend'))

_TMP = tempfile.gettempdir()
os.environ['AIRI_PET_LOG'] = os.path.join(_TMP, 'airi_zoneprobe.log')
os.environ['AIRI_PET_SETTINGS'] = os.path.join(_TMP, 'airi_zoneprobe_set.json')
os.environ['AIRI_PET_CORPUS'] = os.path.join(_TMP, 'airi_zoneprobe_corpus.jsonc')

OUT = os.path.join(_HERE, 'zone_profile.txt')
_lines = []


def out(s=''):
    _lines.append(str(s))
    try:
        print(s)
    except UnicodeEncodeError:
        print(str(s).encode('ascii', 'replace').decode('ascii'))
    flush()


def flush():
    try:
        with open(OUT, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(_lines) + '\n')
    except OSError:
        pass


import common  # noqa: E402
import live2d_assets  # noqa: E402

# 与 standalone.py 完全同一条路径构造页面（否则量到的不是用户看到的那个画面）
_l2d_ok, _detail = live2d_assets.check_assets()
_char_img = common.find_character_image()
_html = common.load_html(19876, _char_img, live2d=_l2d_ok,
                         silhouette=live2d_assets.silhouette_enabled(),
                         card=live2d_assets.card_mode(), detect_sec=30)
out('live2d 素材可用: %s' % _l2d_ok)
for _ln in _detail:
    out('  %s' % _ln)

page_url, _port = common.start_asset_server(_html)
if not page_url:
    out('FATAL: 资产服务器没起来')
    flush()
    sys.exit(1)
out('页面: %s' % page_url)

DUMP_JS = r"""
(function () {
  var a = window.__airi && window.__airi.live2d;
  if (!a || !a.ready || !a.app || !a.app.canvas) return JSON.stringify({ err: 'not-ready' });
  var c = a.app.canvas;
  var r = c.getBoundingClientRect();
  var t = document.createElement('canvas');
  t.width = c.width; t.height = c.height;
  var ctx = t.getContext('2d');
  ctx.clearRect(0, 0, t.width, t.height);
  ctx.drawImage(c, 0, 0);
  var d;
  try { d = ctx.getImageData(0, 0, t.width, t.height).data; }
  catch (e) { return JSON.stringify({ err: 'getImageData: ' + e }); }

  // 行向不透明度（getImageData 行 0 = 画布最上面）
  var N = 40, rows = [];
  for (var b = 0; b < N; b++) {
    var y0 = Math.floor(b * t.height / N), y1 = Math.floor((b + 1) * t.height / N);
    var on = 0, tot = 0;
    for (var y = y0; y < y1; y++) {
      for (var x = 0; x < t.width; x += 2) { tot++; if (d[(y * t.width + x) * 4 + 3] > 24) on++; }
    }
    rows.push(Math.round(1000 * on / Math.max(tot, 1)) / 10);
  }

  // 字符画：60 列 x 40 行，'#' = 这个位置有角色像素
  var CW = 60, CH = 40, art = [];
  for (var ry = 0; ry < CH; ry++) {
    var line = '';
    for (var rx = 0; rx < CW; rx++) {
      var px0 = Math.floor(rx * t.width / CW), px1 = Math.floor((rx + 1) * t.width / CW);
      var py0 = Math.floor(ry * t.height / CH), py1 = Math.floor((ry + 1) * t.height / CH);
      var hit = 0;
      for (var yy = py0; yy < py1; yy++) {
        for (var xx = px0; xx < px1; xx++) { if (d[(yy * t.width + xx) * 4 + 3] > 24) { hit = 1; break; } }
        if (hit) break;
      }
      line += hit ? '#' : '.';
    }
    art.push(line);
  }

  // 角色实际占的行范围（canvas 归一化 0=顶 1=底）
  var top = -1, bot = -1;
  for (var y2 = 0; y2 < t.height; y2++) {
    var any = false;
    for (var x2 = 0; x2 < t.width; x2 += 2) { if (d[(y2 * t.width + x2) * 4 + 3] > 24) { any = true; break; } }
    if (any) { if (top < 0) top = y2; bot = y2; }
  }

  return JSON.stringify({
    bufW: t.width, bufH: t.height,
    rectW: Math.round(r.width), rectH: Math.round(r.height),
    rectTop: Math.round(r.top),
    rows: rows, art: art,
    charTop: Math.round(1000 * top / t.height) / 1000,
    charBottom: Math.round(1000 * bot / t.height) / 1000
  });
})()
"""

import webview  # noqa: E402

WIN_W, WIN_H = 300, 480          # 与 standalone.py 一致
window = webview.create_window('zone probe', url=page_url, width=WIN_W, height=WIN_H,
                               x=60, y=60, frameless=True, transparent=True,
                               on_top=True, resizable=False, easy_drag=False)


def worker():
    try:
        # 等 Live2D 就绪：轮询 window.__airi.live2d.ready
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                st = window.evaluate_js(
                    "(function(){var a=window.__airi&&window.__airi.live2d;"
                    "return String(!!(a&&a.ready));})()")
            except Exception:
                st = 'err'
            if st == 'true':
                ready = True
                break
        out('Live2D 就绪: %s' % ready)
        if not ready:
            out('没等到 live2d.ready（模型可能加载失败），无数据')
            flush()
            return
        time.sleep(1.5)   # 让首帧画完
        raw = window.evaluate_js(DUMP_JS)
        try:
            data = json.loads(raw)
        except Exception as exc:
            out('解析失败: %r  raw=%r' % (exc, str(raw)[:300]))
            flush()
            return
        if data.get('err'):
            out('页面返回错误: %s' % data['err'])
            flush()
            return

        out('canvas 缓冲区 %dx%d, 显示尺寸 %dx%d, rect.top=%d'
            % (data['bufW'], data['bufH'], data['rectW'], data['rectH'], data['rectTop']))
        out('角色纵向范围: ry %.3f ~ %.3f' % (data['charTop'], data['charBottom']))
        out('')
        out('=== 字符画（行 0 = 画布最上边，# = 有角色像素）===')
        art = data['art']
        for i, line in enumerate(art):
            ry0 = i / float(len(art))
            out('%5.2f |%s|' % (ry0, line))
        out('')
        out('=== 行向不透明度（40 段）===')
        for i, pct in enumerate(data['rows']):
            ry0 = i / float(len(data['rows']))
            out('  ry %.3f-%.3f  %5.1f%% %s'
                % (ry0, ry0 + 1.0 / len(data['rows']), pct, '#' * int(pct / 5)))
        out('')
        out('当前 zoneAt: ry<0.45 = head, ry<0.85 = body, 其余 = desk')
        flush()
    except Exception:
        import traceback
        out('!! 出错:')
        out(traceback.format_exc())
        flush()
    finally:
        try:
            window.destroy()
        except Exception:
            pass


def hard_kill():
    time.sleep(90)
    out('!! 硬超时')
    flush()
    os._exit(9)


threading.Thread(target=hard_kill, daemon=True).start()
# ⚠️ 用 webview.start(worker) 让 pywebview 自己在 GUI 起来后开线程跑 worker。
#    自己 threading 起来再调 evaluate_js 会**死锁**（实测：90 秒硬超时前
#    worker 一行输出都没写出来）—— 跨线程 evaluate_js 需要走 pywebview 的
#    那条调度路径。
webview.start(worker, debug=False)
flush()
