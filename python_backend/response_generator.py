"""
============================================================================
response_generator.py — 回复生成器
============================================================================

负责根据上下文生成 deepseek 的傲娇台词。

生成策略：
1. 如果设置了环境变量 DEEPSEEK_API_KEY，调用 DeepSeek API
2. 否则从本地语料库随机抽取匹配场景的台词

返回格式（v2 — 含情绪）：
{
    "type": "chatMessage" | "errorAlert",
    "payload": {
        "text": "回复文本",
        "emotion": "angry" | "happy" | "surprised" | "greeting" | "idle"
    }
}
"""

import os
import random
import json
from typing import Optional
import corpus
from corpus import get_emotion


def classify_error_category(message: str, source: str = "") -> str:
    """根据错误消息文本推断错误类别

    注意：外部 /push 传来的 sample_errors 里 message 可能是数字/None，
    直接 .lower() 会 AttributeError 崩掉整个推送（连接被断）。全部转 str。
    """
    msg_lower = str(message if message is not None else "").lower()

    # 匹配顺序：具体的类别优先。
    # type 的 "is not" 是宽泛前缀，必须放在 name/import 之后，
    # 否则 "name 'x' is not defined" 会被误判为类型错误
    syntax_keywords = ["syntax", "invalid syntax", "expected", "unexpected",
                       "missing", "eof", "indentation", "indent", "token"]
    if any(kw in msg_lower for kw in syntax_keywords):
        return "syntax_error"

    name_keywords = ["is not defined", "undefined", "unresolved reference",
                     "cannot find name", "undeclared", "nameerror"]
    if any(kw in msg_lower for kw in name_keywords):
        return "name_error"

    import_keywords = ["module", "import", "no module named", "cannot find",
                       "unresolved", "could not find", "not found"]
    if any(kw in msg_lower for kw in import_keywords):
        return "import_error"

    type_keywords = ["type", "cannot be", "not assignable", "has no attribute",
                     "is not", "incompatible", "cast", "conversion", "convert"]
    if any(kw in msg_lower for kw in type_keywords):
        return "type_error"

    return "syntax_error"


def categorize_errors(errors: list) -> str:
    """根据错误列表推断主要错误类别"""
    if not errors:
        return "syntax_error"

    counts = {}
    for err in errors:
        if not isinstance(err, dict):
            continue
        cat = classify_error_category(err.get("message", ""), err.get("source", ""))
        counts[cat] = counts.get(cat, 0) + 1

    return max(counts, key=counts.get) if counts else "syntax_error"


def _nvl(value, fallback="?"):
    """把 JSON null / 空串换成占位符（0.3.40）。

    ⚠️ 不能用 `value or fallback`：行号**真可以是 0**（扩展发的就是
    `range.start.line + 1`，最小 1；watcher 那条链路会发 0），`or` 会把
    合法值一并吃掉。而 `dict.get(k, default)` 只在键不存在时生效，键存在
    但值是 null 时照样返回 None —— 直接拼进气泡就成了"第 None 行：None"。
    """
    if value is None or value == "":
        return fallback
    return value


def pick_corpus(category: str, variables: dict = None) -> str:
    """从语料库中随机选取一条台词，并替换变量占位符

    台词来源由 corpus.get_lines() 决定：**用户语料优先，缺失/写坏回落内置**
    （右键菜单「编辑语料」可改，见 corpus.py 文件头）。这里不再直接引用那些
    模块级列表常量 —— 它们只是"出厂默认值"，会被用户语料逐场景覆盖。

    为什么不能在 import 期把列表绑进来：corpus 支持热重载（用户改完保存立刻
    生效），`from corpus import FILE_SCAN` 绑的是**当初那个 list 对象**，用户
    改完文件这边还指着旧的，表现成"我改了怎么没生效"。

    """
    lines = corpus.get_lines(category)
    text = random.choice(lines or corpus.ENCOURAGE)

    if variables:
        for key, value in variables.items():
            text = text.replace("{" + key + "}", str(value))

    return text


def try_deepseek_api(context: dict) -> Optional[str]:
    """尝试调用 DeepSeek API 生成回复。失败返回 None。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None

    try:
        from openai import OpenAI

        # ⚠️ 必须给超时（0.3.40）：OpenAI 客户端默认 httpx timeout=600s 且
        # max_retries=2 —— 网络不通/被墙时一次生成最坏挂 ~30 分钟。0.3.39 把
        # 生成挪到了后台线程，请求线程不再被拖住，但线程本身会一直堆在那儿
        #（并在几十分钟后突然弹出一句与当时代码完全无关的台词）。8 秒足够。
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            timeout=8.0,
            max_retries=1,
        )

        from character import SYSTEM_PROMPT

        trigger = context.get("trigger", "diagnostics")
        error_count = context.get("error_count", 0)
        language = context.get("language", "unknown")
        files = context.get("files", [])

        sample_text = ""
        sample_errors = context.get("sample_errors", [])
        if sample_errors:
            for e in sample_errors[:3]:
                # file_scan 的条目只有 line/message，落回 context 里的文件名
                loc = e.get("file") or context.get("file", "")
                sample_text += f"- {loc}:{e.get('line', '?')} → {e.get('message', '')}\n"

        user_prompt = f"[触发事件: {trigger}] 当前错误数: {error_count}, 语言: {language}, 涉及文件: {', '.join(files[:3])}"

        if sample_text:
            user_prompt += f"\n错误示例:\n{sample_text}"

        if trigger == "greeting":
            user_prompt += "\n请用傲娇的方式打招呼。"
        elif trigger == "all_clear":
            user_prompt += "\n程序员刚才把错误全修好了，请用傲娇的方式表示一下。"
        elif trigger == "file_scan":
            if error_count > 0:
                user_prompt += (
                    f"\n程序员当前打开的文件 {context.get('file', '')} 有 "
                    f"{error_count} 个未修复的错误。请用傲娇的方式提醒，"
                    "并点名最前面几处（行号+内容），一段话以内。"
                )
            else:
                user_prompt += (
                    f"\n程序员刚把 {context.get('file', '当前文件')} 的错误全部修好，"
                    "请用傲娇方式表示认可，一句话即可。"
                )
        elif trigger == "diagnostics":
            user_prompt += "\n程序员刚写了一堆错误，请用傲娇的方式吐槽并悄悄给出建议。"

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=150,
            temperature=0.9,
        )

        content = response.choices[0].message.content
        if content and content.strip():
            return content.strip()

    except Exception:
        pass

    return None


def generate_response(context: dict) -> dict:
    """
    主入口：根据上下文生成回复消息（含情绪标签）。

    返回格式：
    {
        "type": "chatMessage" | "errorAlert",
        "payload": {
            "text": "回复文本",
            "emotion": "angry" | "happy" | "surprised" | "greeting" | "idle"
        }
    }
    """
    trigger = context.get("trigger", "diagnostics")
    # 外部输入不可信：error_count 传 "3"/None 等会让后面的 >= 比较抛 TypeError
    try:
        error_count = int(context.get("error_count", 0) or 0)
    except (TypeError, ValueError):
        error_count = 0
    # ⚠️ 必须回写（0.3.40）：try_deepseek_api 读的是 **context 里的原始值**，
    # 不回写的话 error_count="3"（字符串）到那边就是 `str > int` → TypeError
    # 被 167 行吞掉 → 静默回落本地语料，表现为"配了 API key 却永远只说内置
    # 台词"且毫无提示。同理 files 可能是 None。
    context["error_count"] = error_count
    if not isinstance(context.get("files"), (list, tuple)):
        context["files"] = []
    language = context.get("language", "python")
    # sample_errors 元素不一定是 dict（外部 /push 可传字符串数组），
    # 统一归一化成 dict，categorize_errors / try_deepseek_api 都按 dict 消费
    # 外部输入不可信：sample_errors 传标量（数字/布尔）时 `for e in raw`
    # 会抛 TypeError（实测 sample_errors=5 → "'int' object is not
    # iterable"），整条播报变成空气泡。非列表一律按空处理。
    raw = context.get("sample_errors") or []
    if isinstance(raw, (str, bytes)):
        # 字符串可迭代，但会被拆成单个字符（"oops" → 'o','o','p','s'，
        # 气泡里出现"第?行：o"这种垃圾），所以单独包成单元素
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        raw = []
    errors = [
        e if isinstance(e, dict) else {"message": str(e)}
        for e in raw
        if isinstance(e, (dict, str, int, float, bool))
    ]
    context["sample_errors"] = errors

    # 确定消息类型
    if trigger in ("diagnostics", "file_scan") and error_count > 0:
        msg_type = "errorAlert"
    else:
        msg_type = "chatMessage"

    # 情绪计算
    if trigger == "greeting":
        emotion = "greeting"
        category = "greeting"
    elif trigger == "all_clear":
        emotion = "happy"
        category = "all_clear"
    elif trigger == "file_scan":
        # 当前文件 bug 播报：>0 按严重度选情绪；==0 视为文件修干净
        if error_count > 0:
            if error_count >= 5:
                emotion = "surprised"
                category = "many_errors"
            else:
                category = categorize_errors(errors)
                emotion = get_emotion(category)
        else:
            emotion = "happy"
            category = "file_clear"
    elif trigger == "diagnostics":
        if error_count >= 5:
            emotion = "surprised"
            category = "many_errors"
        else:
            category = categorize_errors(errors)
            emotion = get_emotion(category)
    elif trigger == "heartbeat":
        return None
    else:
        emotion = "idle"
        category = "encourage"

    # 尝试 DeepSeek API
    api_result = try_deepseek_api(context)
    if api_result:
        return {"type": msg_type, "payload": {"text": api_result, "emotion": emotion}}

    # Fallback: 从语料库抽取
    if trigger == "file_scan":
        file_name = str(context.get("file", "") or "当前文件")
        if error_count > 0:
            # reason=remind 表示数量没变的周期提醒，换一套台词避免复读
            reason = str(context.get("reason", "changed"))
            if error_count >= 5:
                corpus_key = "many_errors"
            elif reason == "remind":
                corpus_key = "file_remind"
            else:
                corpus_key = "file_scan"
            # line/lines 必须预置默认值：FILE_SCAN 语料两条含 {line}，
            # 外部 /push 传 error_count>0 而 sample_errors 为空时
            # pick_corpus 的 replace 找不到键，占位符会原样残留进气泡
            variables = {"file": file_name, "count": error_count,
                         "language": language, "line": "?", "lines": "?"}
            if errors:
                # ⚠️ dict.get(k, default) 只在**键不存在**时给默认值：值是
                # JSON null 时返回 None，气泡里就原样念出"第 None 行：None"
                # （实测）。注意不能用 `or`：watcher 与扩展都会发 line=0，
                # `or` 会把合法的 0 也吃掉。
                variables["line"] = _nvl(errors[0].get("line"))
                variables["lines"] = "、".join(
                    "第%s行" % _nvl(e.get("line")) for e in errors[:3])
            text = pick_corpus(corpus_key, variables)
            # 点名具体 bug（行号 + 内容，最多 3 条）
            detail = "；".join(
                "第%s行：%s" % (_nvl(e.get("line")),
                                str(_nvl(e.get("message"), ""))[:40])
                for e in errors[:3])
            if detail:
                text = text + " " + detail
        else:
            text = pick_corpus("file_clear", {"file": file_name})
    elif trigger == "diagnostics":
        if error_count >= 5:
            text = pick_corpus("many_errors", {"count": error_count, "language": language})
        else:
            # line 必须预置默认值：SYNTAX_ERROR 语料里有一条含 {line}，
            # 而 pick_corpus 用 str.replace 替换，缺键就原样残留进气泡。
            # 触发条件与 file_scan 分支同源：外部 /push 传 error_count>0
            # 而 sample_errors 为空（此时 categorize_errors([]) 恒返回
            # syntax_error），占位符就会说给用户听。
            variables = {"language": language, "line": "?"}
            if errors:
                variables["line"] = errors[0].get("line", "?")
            text = pick_corpus(category, variables)
    else:
        text = pick_corpus(category)

    return {"type": msg_type, "payload": {"text": text, "emotion": emotion}}
