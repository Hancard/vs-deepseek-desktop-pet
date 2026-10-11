# -*- coding: utf-8 -*-
"""live2d_probe 共用的 node 查找器。

为什么单独立一个文件：jscheck.py 和 check_ui_js.py 都要调 node 做语法检查，
以前各自把路径写死。托管 node 换代（22.22.2-3 → 22.22.2-6）之后路径失效，
工具直接 FileNotFoundError —— 而这两个正好是"页面什么都不显示"时的
**第一诊断手段**，它们静默失效等于把排查入口焊死了。

查找顺序：
  1. 托管 node（扫 versions/ 下所有版本目录，倒序 → 自动适应换代）
  2. 系统 node（S:\\nodejs\\node.exe）
  3. PATH 里的 node
都找不到返回 None，调用方负责明确报错（不要用裸 'node' 去 subprocess —— 
Windows 上 FileNotFoundError 的 traceback 在批量跑测试时很容易被忽略）。
"""
import shutil
from pathlib import Path

# 托管 node 根目录（版本号会变，所以下面扫目录而不是写死版本）
MANAGED_ROOT = Path(r'C:\Users\Mr.hancard\.workbuddy\binaries\node\versions')
# 系统 node 兜底
SYSTEM_NODE = r'S:\nodejs\node.exe'


def managed_node_candidates(root=MANAGED_ROOT):
    """托管目录下所有版本的 node.exe，按版本目录名倒序（新的优先）。"""
    out = []
    try:
        for d in sorted(Path(root).iterdir(), reverse=True):
            exe = d / 'node.exe'
            if exe.is_file():
                out.append(str(exe))
    except OSError:
        pass
    return out


def find_node():
    """返回可用的 node.exe 绝对路径；都找不到返回 None。"""
    for c in managed_node_candidates() + [SYSTEM_NODE]:
        if Path(c).is_file():
            return c
    return shutil.which('node')
