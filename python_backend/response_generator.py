"""
============================================================================
response_generator.py — 回复生成器
============================================================================

负责根据上下文生成 Airi 的傲娇台词。

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
from corpus import (
    SYNTAX_ERROR, TYPE_ERROR, IMPORT_ERROR, NAME_ERROR,
    MANY_ERRORS, ALL_CLEAR, GREETING, IDLE, ENCOURAGE,
    FILE_SCAN, FILE_REMIND, FILE_CLEAR,
    get_emotion,
)


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


def pick_corpus(category: str, variables: dict = None) -> str:
    """从语料库中随机选取一条台词，并替换变量占位符"""
    corpus_map = {
        "syntax_error": SYNTAX_ERROR,
        "type_error": TYPE_ERROR,
        "import_error": IMPORT_ERROR,
        "name_error": NAME_ERROR,
        "many_errors": MANY_ERRORS,
        "all_clear": ALL_CLEAR,
        "greeting": GREETING,
        "idle": IDLE,
        "encourage": ENCOURAGE,
        "file_scan": FILE_SCAN,
        "file_remind": FILE_REMIND,
        "file_clear": FILE_CLEAR,
    }

    lines = corpus_map.get(category, ENCOURAGE)
    text = random.choice(lines)

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

        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
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
    language = context.get("language", "python")
    # sample_errors 元素不一定是 dict（外部 /push 可传字符串数组），
    # 统一归一化成 dict，categorize_errors / try_deepseek_api 都按 dict 消费
    raw = context.get("sample_errors") or []
    # sample_errors 本身是字符串时不能迭代它 —— 否则会被拆成单个字符
    # （"oops" → 'o','o','p','s'，气泡里出现"第?行：o"这种垃圾）
    if isinstance(raw, (str, bytes)):
        raw = [raw]
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
                variables["line"] = errors[0].get("line", "?")
                variables["lines"] = "、".join(
                    "第%s行" % e.get("line", "?") for e in errors[:3])
            text = pick_corpus(corpus_key, variables)
            # 点名具体 bug（行号 + 内容，最多 3 条）
            detail = "；".join(
                "第%s行：%s" % (e.get("line", "?"),
                                str(e.get("message", ""))[:40])
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
