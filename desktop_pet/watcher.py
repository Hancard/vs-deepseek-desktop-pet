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
    ext = os.path.splitext(filepath)[1]
    is_cpp = ext in ('.cpp', '.cxx', '.cc')
    compiler = 'g++' if is_cpp else 'gcc'
    language_id = 'cpp' if is_cpp else 'c'
    errors = []
    try:
        result = subprocess.run(
            [compiler, '-fsyntax-only', filepath],
            capture_output=True, text=True, timeout=10
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
                    # 跳过隐藏目录和虚拟环境
                    if any(part.startswith('.') or part in ('node_modules', 'venv', '__pycache__', 'build', 'dist', '.git')
                           for part in f.parts):
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
            # 每轮限流：git checkout / 切分支等场景几十个文件同时变化时，
            # 顺序跑几十个编译器子进程（各 10s 超时）会把扫描卡住几分钟
            MAX_CHECKS_PER_SCAN = 20
            for key, mtime in changed_files[:MAX_CHECKS_PER_SCAN]:
                ext = os.path.splitext(key)[1].lower()
                checker = CHECKERS.get(ext)
                if checker is None:
                    known_errors.pop(key, None)
                else:
                    known_errors[key] = checker(key)
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
                last_signature = signature
                if not (is_first_scan and error_count == 0):
                    # 推送到桌宠
                    try:
                        data = json.dumps(payload, ensure_ascii=False).encode()
                        req = urllib.request.Request(
                            push_url, data=data,
                            headers={'Content-Type': 'application/json'}
                        )
                        with urllib.request.urlopen(req, timeout=3) as resp:
                            resp.read()
                        if error_count > 0:
                            files_str = ', '.join(os.path.basename(f) for f, _ in changed_files) or '(cached)'
                            print(f'[watcher] → {error_count} error(s) [{files_str}]', flush=True)
                        else:
                            print(f'[watcher] → all clear!', flush=True)
                    except Exception:
                        pass  # 桌宠服务器未运行，静默跳过

            time.sleep(1.5)  # 轮询间隔

        except KeyboardInterrupt:
            print('\n[watcher] Stopped.')
            break
        except Exception as e:
            print(f'[watcher] ERROR: {e}', flush=True)
            time.sleep(3)


def watch_single_file(filepath: str, port: int):
    """监听单个文件的变化"""
    push_url = f'http://127.0.0.1:{port}/push'
    last_mtime = 0
    last_signature = None

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
                    last_signature = signature
                    if not (is_first_check and error_count == 0):
                        payload = {
                            'type': 'diagnostics',
                            'payload': {
                                'count': error_count,
                                'items': errors[:10],
                                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                            }
                        } if error_count > 0 else {
                            'trigger': 'all_clear',
                            'error_count': 0,
                            'language': 'unknown',
                            'files': [filepath],
                            'sample_errors': [],
                        }

                        try:
                            data = json.dumps(payload, ensure_ascii=False).encode()
                            req = urllib.request.Request(
                                push_url, data=data,
                                headers={'Content-Type': 'application/json'}
                            )
                            with urllib.request.urlopen(req, timeout=3) as resp:
                                resp.read()
                            if error_count > 0:
                                print(f'[watcher] {os.path.basename(filepath)}: {error_count} error(s)', flush=True)
                            else:
                                print(f'[watcher] {os.path.basename(filepath)}: all clear!', flush=True)
                        except Exception:
                            pass

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
