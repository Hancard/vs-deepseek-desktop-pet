"""
============================================================================
watcher.py — 独立文件监听器
============================================================================

监听指定目录下的 .py / .c / .cpp 文件修改，
保存时自动运行语法检查，将错误推送到 Airi 桌宠服务器。

完全不依赖 VS Code 扩展。配合 standalone.py 使用。

【运行方式】
  python watcher.py [--dir 监听目录] [--port 桌宠端口]

【依赖】
  Python 3.9+（标准库即可，无需额外安装）
"""

import sys
import os
import re
import time
import json
import subprocess
import urllib.request
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


# ---------------------------------------------------------------------------
# 语法检查器
# ---------------------------------------------------------------------------

def check_python(filepath: str) -> list:
    """对 Python 文件做语法检查，返回错误列表。

    用进程内 compile() 而非 py_compile 子进程：结果等价（遇到第一个语法
    错误即停，IndentationError/TabError 也是 SyntaxError 子类），但不会在
    被监听的项目里写 __pycache__/*.pyc 副作用，也省去子进程开销。
    错误消息形如 "SyntaxError: '(' was never closed"。
    """
    errors = []
    try:
        import tokenize
        with tokenize.open(filepath) as f:  # 按 PEP263 编码声明/UTF-8 读取
            source = f.read()
    except SyntaxError as e:
        # 文件编码声明错误等（读取阶段即可判定）
        errors.append({
            'file': filepath,
            'message': f'{type(e).__name__}: {e.msg or "unknown error"}',
            'line': e.lineno or 0,
            'languageId': 'python',
            'source': 'python',
        })
        return errors
    except Exception as e:
        # 文件被删除/占用/解码失败等
        errors.append({
            'file': filepath, 'message': str(e), 'line': 0,
            'languageId': 'python', 'source': 'python'
        })
        return errors
    try:
        compile(source, filepath, 'exec')
    except SyntaxError as e:
        errors.append({
            'file': filepath,
            'message': f'{type(e).__name__}: {e.msg or "unknown error"}',
            'line': e.lineno or 0,
            'languageId': 'python',
            'source': 'python',
        })
    except Exception as e:
        # 例如源码含 null byte（ValueError）
        errors.append({
            'file': filepath, 'message': str(e), 'line': 0,
            'languageId': 'python', 'source': 'python'
        })
    return errors


def check_c(filepath: str) -> list:
    """对 C/C++ 文件运行语法检查"""
    # ⚠️ 必须 lower()（0.3.44）：Windows 的文件名大小写不敏感，`Foo.CPP` /
    # `bar.Cxx` 是合法且常见的写法。不 lower 时 is_cpp 恒为 False ——
    # 于是 C++ 文件被交给 **gcc**（C 编译器）并标成 languageId 'c'，
    # 合法的 C++ 语法（iostream / 类 / 模板）会被报成一堆假错误，
    # 而调用方（第 179 行）是按 lower 后的扩展名分发到这里的，两边口径不一致。
    ext = os.path.splitext(filepath)[1].lower()
    is_cpp = ext in ('.cpp', '.cxx', '.cc')
    compiler = 'g++' if is_cpp else 'gcc'
    language_id = 'cpp' if is_cpp else 'c'
    errors = []
    # 文件已被删掉/移走时不要再跑编译器：gcc 会输出
    # "cc1.exe: fatal error: xxx.c: No such file or directory" —— 里面带
    # 'error:'，会被当成一条真错误计入总数，并长期留在 known_errors 里
    # （实测：`gcc -fsyntax-only foo.c` 对不存在的文件 rc=1 且 stderr 含
    #  "fatal error: ... No such file or directory"）。
    if not os.path.isfile(filepath):
        return errors
    try:
        # ⚠️ 必须显式给 encoding（0.3.41）：只写 text=True 时 subprocess 用
        # **系统 ANSI 代码页**解码（本仓库开发机 ACP=936 → cp936）。gcc 的
        # 诊断输出是 UTF-8（中文路径、中文标识符、UTF-8 源码里的字符串都能
        # 触发），cp936 解码失败会抛 UnicodeDecodeError —— 它不是 OSError，
        # 会穿过下面的 except 落到裸 `except Exception`，于是变成一条
        # "'utf-8' codec can't decode ..." 的**伪诊断**推给用户。
        result = subprocess.run(
            [compiler, '-fsyntax-only', filepath],
            capture_output=True, text=True, timeout=10,
            encoding='utf-8', errors='replace'
        )
        stderr = result.stderr.strip()
        if stderr:
            for line in stderr.split('\n'):
                line = line.strip()
                # 只推 error 行：warning 属于提示信息，混进错误计数会导致
                # all_clear 永远不触发（想看警告请用编辑器内诊断）
                if 'error:' in line:
                    errors.append({
                        'file': filepath,
                        'message': line,
                        'line': _extract_line(line),
                        'languageId': language_id,
                        'source': compiler
                    })
    except FileNotFoundError:
        pass  # gcc 未安装
    except Exception as e:
        errors.append({
            'file': filepath, 'message': str(e), 'line': 0,
            'languageId': language_id, 'source': compiler
        })
    return errors


def _extract_line(text: str) -> int:
    """从错误文本中提取行号"""
    # 匹配 "line 42" 或 ":42:"（gcc 格式 file.c:42:5: error: ...）
    m = re.search(r'(?:line\s+|:)(\d+)', text)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# 文件监听器
# ---------------------------------------------------------------------------

SUPPORTED = {'.py', '.c', '.cpp', '.cxx', '.cc'}

CHECKERS = {
    '.py': check_python,
    '.c': check_c, '.cpp': check_c,
    '.cxx': check_c, '.cc': check_c,
}

# 跳过隐藏目录和虚拟环境
SKIP_PARTS = ('node_modules', 'venv', '__pycache__', 'build', 'dist', '.git')


#: 只连本机，**绝不走系统代理**（0.3.46）。
#:
#: ⚠️ urllib 默认读 http_proxy / https_proxy 环境变量，而它对 127.0.0.1
#:    **不一定**绕过：CPython 的 proxy_bypass_registry 只认 `<local>`（无点
#:    主机名）和"前缀/后缀通配"，Windows 上常见的 `127.*` 这种中间通配写法
#:    匹配不到 —— 实测本机（ProxyOverride 里明明写了 `127.*;<local>`）：
#:
#:     proxy_bypass('127.0.0.1')                              = False
#:     默认 urlopen('http://127.0.0.1:<无人在听的口令端口>')  = URLError
#:                                                            [WinError 10061] 目标计算机积极拒绝
#:     build_opener(ProxyHandler({})).open(同一个地址)         = 200
#:
#:    也就是说，只要环境里挂着 http_proxy（开发者机器上极常见：代理软件设的
#:    系统代理、或 shell profile 里 export 的），本文件的推送就会先连代理。
#:    代理活着时常能转发成功，代理一关就全部失败 —— 而下面两处推送都是
#:    `except Exception: pass`，"代理挂了"于是表现成"桌宠什么都不说"，
#:    日志里零行。这条链路的唯一出口是本机，绕开代理没有任何副作用。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def should_skip(rel_parts) -> bool:
    """是否跳过这个**相对**路径（rel_parts = 相对监听根的各段）。

    ⚠️ 0.3.41 —— 判据只能作用在**监听根之下**的那几段上。以前这里传的是
    绝对路径的 `f.parts`，于是监听根**之上**的祖先目录也被一起判了：

        · 任何以 `.` 开头的祖先（.workbuddy / .cache / .cursor / .vscode…）
        · 或者祖先里有 build / dist / venv / node_modules

    都会让**每一个文件**都被跳过 —— rglob 照常跑、启动信息照常打印，
    然后 `changed_files` 永远是空的：桌宠从此一声不响，用户既看不到错误
    也看不到任何提示，完全无从判断是"没监听"还是"代码没 bug"。

    实测（`probe_041.py` A 段）：
      · 监听根 `.../Temp/.probe_root_xxx/proj` 下放 3 个 .py → 旧写法扫到
        **0** 个，相对写法扫到 3 个
      · 真实目录 `~/.workbuddy` 下 4468 个 .py → 旧写法存活 **0** 个
    """
    return any(part.startswith('.') or part in SKIP_PARTS for part in rel_parts)


def _check_one(key: str, mtime: float):
    """检查单个文件，返回 (key, mtime, errors)；errors=None 表示从缓存摘掉"""
    ext = os.path.splitext(key)[1].lower()
    checker = CHECKERS.get(ext)
    if checker is None or not os.path.isfile(key):
        return (key, mtime, None)
    return (key, mtime, checker(key))


def watch_directory(watch_dir: str, port: int):
    """轮询监听目录中的文件变化"""
    watch_path = Path(watch_dir).resolve()
    if not watch_path.exists():
        print(f'[watcher] ERROR: directory not found: {watch_dir}', flush=True)
        sys.exit(1)

    push_url = f'http://127.0.0.1:{port}/push'
    mtimes = {}        # 文件路径 → 上次修改时间
    known_errors = {}  # 文件路径 → 该文件最近一次检查出的错误（用于整体错误统计）
    last_signature = None

    print(f'[watcher] Watching: {watch_path}', flush=True)
    print(f'[watcher] Push target: {push_url}', flush=True)
    print(f'[watcher] Supported: {", ".join(SUPPORTED)}', flush=True)
    print(f'[watcher] Waiting for file changes... (Ctrl+C to stop)', flush=True)

    while True:
        try:
            changed_files = []  # [(路径, 新mtime)]
            seen = set()

            # 扫描所有支持的文件
            for ext in SUPPORTED:
                for f in watch_path.rglob(f'*{ext}'):
                    # 只对监听根**之下**的相对部分做过滤（见 should_skip 注释）
                    if should_skip(f.relative_to(watch_path).parts):
                        continue
                    key = str(f)
                    seen.add(key)
                    try:
                        current_mtime = f.stat().st_mtime
                        if current_mtime > mtimes.get(key, 0):
                            changed_files.append((key, current_mtime))
                            # 注意：mtime 不在此提交——被限流顺延的文件
                            # 下一轮仍会被判定为变更（提交时机在检查阶段）
                    except OSError:
                        pass

            # 文件被删除/移走时清理其错误缓存和 mtime 缓存
            for cache in (known_errors, mtimes):
                for key in list(cache):
                    if key not in seen:
                        del cache[key]

            # 对变化的文件运行语法检查并更新缓存。
            #
            # 两层限流（0.3.41）：
            # ① 每轮最多检查 20 个 —— git checkout / 切分支时几十个文件同时
            #    变 mtime，一次性全查会把扫描卡住。
            # ② 这 20 个**并发**跑（最多 4 个） —— 只做 ① 不够：C/C++ 检查
            #    是起 gcc/g++ 子进程（各 10s 超时），串行最坏 20×10=**200 秒**，
            #    这三分多钟里 time.sleep(1.5) 和整棵树扫描一行都不会执行，
            #    期间用户保存的 .py 也不播报、all_clear 也要等拖完才发。
            #    并发 4 之后最坏降到 ~50s（Python 检查是进程内 compile，
            #    本来就是毫秒级，占不住线程）。
            MAX_CHECKS_PER_SCAN = 20
            CHECK_WORKERS = 4
            batch = changed_files[:MAX_CHECKS_PER_SCAN]
            if batch:
                workers = min(CHECK_WORKERS, len(batch))
                if workers > 1:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        results = list(pool.map(lambda kv: _check_one(*kv), batch))
                else:
                    results = [_check_one(*batch[0])]
                for key, mtime, errs in results:
                    if errs is None:
                        known_errors.pop(key, None)
                    else:
                        known_errors[key] = errs
                    mtimes[key] = mtime  # 已检查才提交；顺延的文件下一轮重查
            if len(changed_files) > MAX_CHECKS_PER_SCAN:
                print(f'[watcher] {len(changed_files)} file(s) changed, '
                      f'checking {MAX_CHECKS_PER_SCAN} this round', flush=True)

            # 整体错误状态 = 所有已检查文件的错误之和
            # （保存一个干净文件不能触发 all_clear，其他文件可能还有错误）
            all_current_errors = [e for errs in known_errors.values() for e in errs]
            error_count = len(all_current_errors)

            if error_count > 0:
                langs = set(e.get('languageId', '') for e in all_current_errors)
                language = ', '.join(langs) if langs else 'unknown'
                payload = {
                    'type': 'diagnostics',
                    'payload': {
                        'count': error_count,
                        'items': all_current_errors[:10],
                        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                        'language': language,
                    }
                }
            else:
                payload = {
                    'trigger': 'all_clear',
                    'error_count': 0,
                    'language': 'unknown',
                    'files': [],
                    'sample_errors': [],
                }

            # 相同错误状态不重复推送；启动时项目本身干净则不打扰（已有问候语）
            # 签名包含错误总数：只比对前 10 条会漏掉第 10 条之外的增删
            signature = json.dumps([
                payload.get('type', payload.get('trigger')),
                error_count,
                [(e['file'], e['line'], e['message']) for e in all_current_errors[:10]],
            ], ensure_ascii=False)
            is_first_scan = last_signature is None
            if signature != last_signature:
                if is_first_scan and error_count == 0:
                    # 启动时本来就干净：不需要推送，但要记下来，否则每一轮
                    # 都会被判成"第一次扫描"（判定条件就是 last_signature is None）
                    last_signature = signature
                else:
                    # ⚠️ 签名只能在**真的送到之后**提交（0.3.46）。
                    # 以前是"先提交、再推送"，而推送失败几乎是常态：桌宠还没
                    # 起来（用户先改代码后开桌宠）、或者刚被关掉。那时签名已经
                    # 记成"这版状态报过了"，于是**同一批错误再也不会播报** ——
                    # 除非用户又改出新的错误（签名变化）。本文件另外两条链路
                    # （known_errors / all_clear 判定）都在认真区分"说过"和
                    # "送到了"，只有这一处把两者混为一谈。
                    # 不提交的代价只是下一轮重试（1.5 秒一次连接尝试），
                    # 与 push_message 侧"宁可重试也不静默丢单"同一口径。
                    try:
                        data = json.dumps(payload, ensure_ascii=False).encode()
                        req = urllib.request.Request(
                            push_url, data=data,
                            headers={'Content-Type': 'application/json'}
                        )
                        with _LOCAL_OPENER.open(req, timeout=3) as resp:
                            resp.read()
                        last_signature = signature      # 送达才提交
                        if error_count > 0:
                            files_str = ', '.join(os.path.basename(f) for f, _ in changed_files) or '(cached)'
                            print(f'[watcher] → {error_count} error(s) [{files_str}]', flush=True)
                        else:
                            print(f'[watcher] → all clear!', flush=True)
                    except Exception:
                        pass  # 桌宠服务器未运行，静默跳过（下一轮重试）

            time.sleep(1.5)  # 轮询间隔

        except KeyboardInterrupt:
            print('\n[watcher] Stopped.')
            break
        except Exception as e:
            print(f'[watcher] ERROR: {e}', flush=True)
            time.sleep(3)


def watch_single_file(filepath: str, port: int):
    """监听单个文件的变化。

    ⚠️ 推送失败要能重试（0.3.47）。0.3.46 把签名改成"送达之后才提交"，
    但那只是必要条件 —— 本函数的外层门是 `current_mtime > last_mtime`，
    而 mtime 在**检查时**就提交了。于是推送失败（桌宠还没起来 / 刚被关掉）
    之后，文件不再变化就再也不会被检查，那个"没提交的签名"永远等不到
    下一轮 —— 注释承诺的"下一轮重试"在这条路径上根本不存在
    （实测：先让端口无人监听、再起接收端，6 秒内一条都收不到；
      同样的时序下目录模式 watch_directory 会重推）。
    所以未送达的载荷单独挂在 pending 上，**不依赖 mtime 变化**也能重投。
    """
    push_url = f'http://127.0.0.1:{port}/push'
    last_mtime = 0
    last_signature = None
    #: 未送达的 (签名, 载荷, 错误数)；下一轮无条件重试，送达后清空
    pending = None

    ext = os.path.splitext(filepath)[1].lower()
    checker = CHECKERS.get(ext)
    if checker is None:
        print(f'[watcher] Unsupported file type: {ext}', flush=True)
        return

    print(f'[watcher] Watching single file: {filepath}', flush=True)
    print(f'[watcher] Push target: {push_url}', flush=True)
    print(f'[watcher] Waiting for file changes... (Ctrl+C to stop)', flush=True)

    while True:
        try:
            try:
                current_mtime = os.path.getmtime(filepath)
            except OSError:
                print(f'[watcher] File not found: {filepath}', flush=True)
                break

            if current_mtime > last_mtime:
                last_mtime = current_mtime
                errors = checker(filepath)
                error_count = len(errors)

                # 内容签名去重（与目录模式一致）：
                # 错误数量相同但内容变化时仍然推送；
                # 启动时文件本身干净则不打扰
                signature = json.dumps(
                    [(e['line'], e['message']) for e in errors],
                    ensure_ascii=False
                )
                is_first_check = last_signature is None
                if signature != last_signature:
                    if is_first_check and error_count == 0:
                        last_signature = signature   # 干净启动：不推，但要记住
                    else:
                        payload = {
                            'type': 'diagnostics',
                            'payload': {
                                'count': error_count,
                                'items': errors[:10],
                                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                # ⚠️ 单文件模式也必须带 language（0.3.46）。
                                # 目录模式一直有，这里漏了 —— 于是 `--file` 那条
                                # 路径的每条诊断都会让桌宠拿到 'unknown'，而
                                # response_generator 拿 language 拼进台词
                                # （`{language}` 占位符 → "unknown 里还有 2 个 bug"）。
                                # 两份 payload 的形状不一致本身就是隐患。
                                'language': (', '.join(sorted(
                                    {e.get('languageId', '') for e in errors} - {''}
                                )) or 'unknown'),
                            }
                        } if error_count > 0 else {
                            'trigger': 'all_clear',
                            'error_count': 0,
                            'language': 'unknown',
                            'files': [filepath],
                            'sample_errors': [],
                        }

                        # 未送达就挂在 pending 上（0.3.47）—— 投递统一在下面做，
                        # 重试不再依赖"文件又变了一次"（见函数头的说明）。
                        pending = (signature, payload, error_count)

            # ---- 投递（含重试）----
            # 每一轮都看：失败就留着，1.5 秒后再试；成功才提交签名。
            # 与目录模式"宁可重试也不静默丢单"同一口径。
            if pending is not None:
                p_sig, p_payload, p_count = pending
                try:
                    data = json.dumps(p_payload, ensure_ascii=False).encode()
                    req = urllib.request.Request(
                        push_url, data=data,
                        headers={'Content-Type': 'application/json'}
                    )
                    with _LOCAL_OPENER.open(req, timeout=3) as resp:
                        resp.read()
                except Exception:
                    pass        # 桌宠服务器未运行：下一轮重试（不要清 pending）
                else:
                    last_signature = p_sig    # 送达才提交
                    pending = None
                    if p_count > 0:
                        print(f'[watcher] {os.path.basename(filepath)}: {p_count} error(s)', flush=True)
                    else:
                        print(f'[watcher] {os.path.basename(filepath)}: all clear!', flush=True)

            time.sleep(1.5)

        except KeyboardInterrupt:
            print('\n[watcher] Stopped.')
            break
        except Exception as e:
            print(f'[watcher] ERROR: {e}', flush=True)
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description='deepseek file watcher')
    parser.add_argument('--dir', default='.',
                        help='Directory to watch (default: current directory)')
    parser.add_argument('--file', default='',
                        help='Single file to watch (overrides --dir)')
    parser.add_argument('--port', type=int, default=19876,
                        help='Desktop pet server port (default: 19876)')
    args = parser.parse_args()

    if args.file:
        watch_single_file(args.file, args.port)
    else:
        watch_directory(args.dir, args.port)


if __name__ == '__main__':
    main()
