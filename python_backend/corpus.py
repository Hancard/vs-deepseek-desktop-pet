# -*- coding: utf-8 -*-
"""
============================================================================
corpus.py — 桌宠台词库（内置台词 + 用户可编辑覆盖）
============================================================================

【三层结构】
  1. 内置台词：本文件里的 SYNTAX_ERROR / ... / CLICK_LINES 常量（出厂默认）
  2. 用户语料：~/.dsh/pets/airi-pet-corpus.jsonc
     由右键菜单「编辑语料」生成并打开，用户随便改
  3. 读取时**逐场景合并**：用户写了某场景就用用户的；没写/写坏了就回落到

【为什么用户语料放在 ~/.dsh/pets/ 而不是仓库里】
  桌宠跑的是 **VSIX 安装副本**（extension.ts 会拉起安装目录下的
  standalone.py）。语料文件若放在安装目录里，下次升级扩展就被覆盖，
  用户改的台词全丢。放用户目录还顺带保证"只有一份"，与装的版本无关。
  同目录还有 Live2D core 缓存与设置文件（见 standalone.SETTINGS_PATH）。

【热重载】
  get_lines() 每次都用 (mtime, size) 当缓存键，文件一变立刻重读 —— 用户
  在编辑器里改完保存，最多一个响应周期（默认 30 秒）后就用上新台词，
  不用重启桌宠、更不用重装扩展。

【安全回落 —— 本文件最重要的部分】
  语料现在是**用户手写**的，必须假设它随时是坏的：
    · 文件不存在 / 不是 UTF-8 / JSON 语法错      → 全部用内置
    · 某场景不是数组 / 数组为空 / 元素不是字符串  → 该场景用内置
    · 台词里出现**该场景用不到的占位符**          → 丢弃这一条
    · 点击台词的情绪词不在合法集合里              → 丢弃这一条
  前两条是为了"别崩"，后两条是为了**别把 {line} 这种东西原样说进气泡**
  —— 这个坑在 0.3.18 / 0.3.20 反复出现过（当时是内置语料自己犯的错，
  pick_corpus 用 str.replace，缺键就留着占位符）。现在语料交给用户写，
  同一个坑的暴露面大得多，所以把校验放在入口。

【为什么占位符白名单取"交集"而不是"并集"】
  同一个场景可能被多条代码路径调用，各自传入的变量键**不一样**。例如
  many_errors：
    · diagnostics 路径（错误 >= 5）传 {count, language}
    · file_scan 路径（错误 >= 5）传 {file, count, language, line, lines}
  若用户写了含 {file} 的 many_errors 台词，走到 diagnostics 路径时
  {file} 无处可换 → 原样进气泡。所以白名单只收**每条路径都满足的键**
  （交集），宁可少给。要放宽某个场景，就得先让两条路径传同一套变量。
"""

import json
import os
import re
import threading

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
# 与 standalone.SETTINGS_PATH 同目录（~/.dsh/pets/，Live2D core 缓存也在那儿）。
# ⚠️ 可用 AIRI_PET_CORPUS 覆盖，**测试必须覆盖它**（与 AIRI_PET_LOG 同理）：
#   测试若直接调 ensure_corpus_file()，写的是用户真实的语料文件。
# 运行时要改也是改这个模块级全局（get_lines 按名字查），别 `from X import`。
CORPUS_PATH = (os.environ.get('AIRI_PET_CORPUS')
               or os.path.join(os.path.expanduser('~'), '.dsh', 'pets',
                               'airi-pet-corpus.jsonc'))


# ---------------------------------------------------------------------------
# 内置台词（出厂默认；用户语料缺失或写坏时逐场景回落到这里）
# ---------------------------------------------------------------------------

# 语法错误场景
SYNTAX_ERROR = [
    "笨蛋！括号都配不对的吗？好好数数行不行！",
    "语法错误…你是刚睡醒吗？这种低级错误也写得出来？",
    "哼，编译器都比你细心。去看看第{line}行吧！",
    "又少写冒号/分号了吧？我都替你感到丢人了…",
    "语法都搞不定还想写代码？（…不过刚开始学的话，出点错也正常啦）",
    "{language}的语法不难吧？你怎么还能写错啊…",
    "第 {line} 行少了个括号…你是打算自己数着玩吗？",
    "缩进都能错，你是用记事本在写 {language} 吗？",
    "又是 unexpected token…这代码是你闭着眼睛随机敲出来的吧。",
]

# 类型错误场景
TYPE_ERROR = [
    "类型又搞错了…你是故意惹编译器生气的吧？",
    "int 和 string 能一样吗！？给我认真一点啊笨蛋！",
    "类型不匹配…这么基础的东西，去翻翻书好不好？",
    "你的类型注解呢？写代码不用脑子的吗…（下次记得加啦）",
    "哼，类型系统都看不下去了。不过这种错误确实容易犯…",
    "{language} 的类型不是摆设！看看你传的都是些什么东西。",
    "字符串和数字加在一起，你是想得到什么？幸福吗？",
    "类型错了…把报错信息重读一遍会怎样嘛，都替你写清楚了啦。",
]

# 导入/模块错误场景
IMPORT_ERROR = [
    "模块都找不到！你倒是先 pip install 啊！！",
    "import 的包不存在…你确定你装了？要不要我帮你查？…才、才不是特意帮你！",
    "找不到模块？去检查一下 requirements.txt 好不好！",
    "导入路径错了吧？文件在哪你自己不清楚吗…",
    "哼，连 import 都能写错…（悄悄说：检查下 PYTHONPATH 吧）",
    "ModuleNotFoundError 都写明缺什么了，照着装一遍很难吗？",
    "虚拟环境呢？你该不会把包装到全局去了吧…（叹气）",
    "import 报错先检查拼写和大小写，十个里有九个是这个原因。",
]

# 名称/未定义错误场景
NAME_ERROR = [
    "这个变量根本没定义啊！你是想变魔术吗？",
    "变量名又打错了…手指不好使的话要不要我帮你敲？",
    "未定义的变量…你以为 Python 能读懂你的心思吗？",
    "函数名写错了吧？我建议你多用用自动补全，别自己瞎敲…",
    "哼，变量都找不到，你是不是根本没声明？",
    "又是 NameError…变量先定义再使用，这是规矩！",
    "拼写检查一下会累死吗？length 和 lenght 可不是一回事！",
    "未定义的函数也敢直接调用？编译器表示很无辜。",
]

# 大量错误场景
MANY_ERRORS = [
    "…你是想把编译器气死吗？一口气写了 {count} 个错误出来！",
    "{count} 个错误！？你这是在写代码还是在写 bug 生成器啊？！",
    "呃…{count} 个错误。我有点不知道该从哪里开始吐槽了…",
    "天哪 {count} 个错误！…别慌，一个一个来，先修最简单的。",
    "刚才还觉得你挺靠谱的…结果一看 {count} 个错误。当我没说！",
    "你是不是一次都没编译就直接写了这么多？先 build 一下好不好！",
    "{count} 个错误…我数着数着都替你头疼了。",
    "一口气 {count} 个？你是在参加写 bug 大赛吗？",
    "{count} 个错误摆在这，从第一行开始按顺序来，别东一榔头西一棒子。",
]

# 错误清零场景
ALL_CLEAR = [
    "哼，终于修好了。不过别得意，下一轮肯定还会出错的。",
    "零错误？…巧合而已吧。不过，嘛，还是夸你一下好了。",
    "居然全部修完了？我还准备了好多吐槽呢…浪费我感情！",
    "好了好了，你最厉害了行了吧？…赶紧继续写下一段代码！",
    "哼，这次算你过关。不过我会一直盯着你的，别想偷懒。",
    "错误清零…行吧，这次勉强像样。别骄傲！",
    "全修好了？哼，看来我说的话你还是听进去了一点嘛。",
    "看着舒服多了。继续保持，我可不想再看到满屏红色。",
]

# 启动问候场景
GREETING = [
    "哼，终于启动了。我可不是为了帮你才出来的…只是刚好有空而已。",
    "deepseek 已上线。…干嘛用那种眼神看我？有错误我会骂你的哦。",
    "哟，又是你啊。今天写的代码能看吗？我会好好监督的！",
    "启动完毕。你写你的代码，我在旁边看着…才、才不是关心你！",
    "我来了。今天的错误就交给我来吐槽…咳，来监督。",
    "这么早就开始写代码？太阳打西边出来了？",
    "开机完毕。有 bug 的话，我可是会第一时间嘲笑你的哦。",
]

# 空闲提示场景（目前没有触发点，保留给后续的"发呆检测"）
IDLE = [
    "盯着屏幕发呆有什么用，快点写代码啊笨蛋！",
    "…你不写代码的话那我在这干嘛？给我动起来！",
    "你的咖啡都凉了，还没开始写吗？",
    "喂，再不写代码我就走了哦。（其实也不会走啦…）",
    "发呆是不会让进度条走的，笨蛋。",
    "…再不动手，我可要替你写了哦？会吗？才、才不会。",
    "键盘都要打瞌睡了，你好意思吗？",
]

# 通用鼓励场景
ENCOURAGE = [
    "哼，虽然不想承认…但你的代码确实有进步了。继续保持！",
    "加油啦～当然我可不是在关心你，只是怕你拖慢进度。",
    "写累了就休息一下，别死撑…我可不是在担心你！",
    "哼，虽然你这人挺笨的，但认真起来也不是没救嘛。",
    "今天也在努力呢…才、才不是在夸你！",
    "慢慢来嘛，谁一开始就写得好？…我就写得挺好。",
    "累了就喝口水休息一下。代码跑不起来，人可别先趴下。",
]

# 当前文件 bug 播报场景（file_scan，bug 内容有变化时）
FILE_SCAN = [
    "喂，{file} 里还躺着 {count} 个 bug，别装没看见！",
    "{file} 的 bug 有 {count} 个…你是打算留着过年吗？",
    "咳咳，{file} 第 {line} 行附近有问题。才、才不是特意帮你看的！",
    "{count} 个错误就在 {file} 里，你还悠哉悠哉的？",
    "哼，{file} 病得不轻（{count} 个 bug）。从第 {line} 行开始修吧。",
    "{file} 里还有 {count} 个 bug 在冲你招手呢。",
    "别装死，{file} 的 {count} 个错误不会自己消失的。",
    "瞄了一眼 {file}…第 {line} 行那个 bug 藏得可真深，还好被我逮到了。",
]

# 当前文件 bug 未变化的重复提醒（file_scan remind）
FILE_REMIND = [
    "那 {count} 个 bug 还在 {file} 里呢，我可都记着账的。",
    "提醒一下，{file} 的 {count} 个错误一个都没少。别想蒙混过关！",
    "还在偷懒？{file} 里 {count} 个 bug 正盯着你看呢。",
    "…我不是唠叨，只是 {file} 的 {count} 个 bug 让我无法沉默。",
    "还在磨蹭？{file} 的 {count} 个 bug 一个都没动呢。",
    "{file} 那边毫无进展…{count} 个 bug 都开起茶话会了。",
    "叮咚，{count} 个 bug 还住在 {file} 里，房租可是你付的哦。",
]

# 当前文件修干净场景（file_scan 清零）
FILE_CLEAR = [
    "{file} 干净了，零 bug。哼，勉强表扬你一下。",
    "{file} 的错误全修完了？算你有点本事…才不是夸你呢！",
    "好哦，{file} 清清爽爽。继续保持，别让我逮到新错误！",
    "{file} 全绿了！…哼，我只是陈述事实，才没有替你高兴。",
    "零错误的 {file} 看起来顺眼多了。保持住！",
    "{file} 修干净了。看吧，你不是做不到嘛。",
]


# ---------------------------------------------------------------------------
# 点击互动台词 —— 点角色身上不同位置，说不同的话
# ---------------------------------------------------------------------------
# 分区由 ui.html 的 zoneAt() 给出：canvas 上段是 head（ry<0.45）、中段是 body
# （ry<0.62）、下段是 desk。边界是拿运行中的桌宠实测出来的（0.3.36，
# live2d_probe/measure_region.py）—— 旧边界 desk=ry>=0.85 整条落在角色卡外，
# 一条都触发不了。
# 每池多放几条，点着才不像复读机。
# 情绪词必须和 ui.html 的 EMOTION_EXPRESSION 对得上，否则表情切不过去
# —— 用户语料里的情绪词同样按这个集合校验（CLICK_EMOTIONS）。
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

# 情绪标识：必须与 ui.html 的 EMOTION_EXPRESSION 键一一对应
CLICK_EMOTIONS = frozenset({'idle', 'greeting', 'angry', 'happy', 'surprised'})

# 点击分区顺序（导出语料时按这个顺序排，让文件可读）
CLICK_ZONES = ('head', 'body', 'desk')


# ---------------------------------------------------------------------------
# 场景表与占位符白名单
# ---------------------------------------------------------------------------

#: 场景 → 中文名
SCENE_NAMES = {
    "syntax_error": "语法错误",
    "type_error": "类型错误",
    "import_error": "导入错误",
    "name_error": "名称错误",
    "many_errors": "大量错误",
    "all_clear": "错误清零",
    "greeting": "启动问候",
    "idle": "空闲提示",
    "encourage": "鼓励",
    "file_scan": "文件bug播报",
    "file_remind": "文件bug提醒",
    "file_clear": "文件修干净",
}

#: 场景顺序（= 导出语料文件里的顺序）
SCENE_ORDER = [
    "syntax_error", "type_error", "import_error", "name_error",
    "many_errors", "all_clear", "greeting", "idle", "encourage",
    "file_scan", "file_remind", "file_clear",
]

#: 内置语料总表（导出与回落都用它）
BUILTIN_CORPUS = {
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

#: 每个场景**允许出现**的占位符（取所有调用路径的交集，理由见文件头）。
#: 用户台词里出现白名单外的占位符 → 整条丢弃，避免 {xxx} 原样进气泡。
ALLOWED_PLACEHOLDERS = {
    "syntax_error": frozenset({"line", "language"}),
    "type_error": frozenset({"line", "language"}),
    "import_error": frozenset({"line", "language"}),
    "name_error": frozenset({"line", "language"}),
    # many_errors 的两条路径只共有 count/language（见文件头）
    "many_errors": frozenset({"count", "language"}),
    "all_clear": frozenset(),
    "greeting": frozenset(),
    "idle": frozenset(),
    "encourage": frozenset(),
    "file_scan": frozenset({"file", "count", "line", "lines", "language"}),
    "file_remind": frozenset({"file", "count", "line", "lines", "language"}),
    "file_clear": frozenset({"file"}),
}

_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


def get_all_scenes():
    """返回所有场景名称列表（保持原签名：外部与测试都在用）"""
    return list(SCENE_ORDER)


def get_scene_name(category: str) -> str:
    """返回场景的中文描述"""
    return SCENE_NAMES.get(category, "未知场景")


# ============================================================================
# 情绪映射：场景 → 立绘表情
# ============================================================================
EMOTION_MAP = {
    "syntax_error": "angry",
    "type_error": "angry",
    "import_error": "angry",
    "name_error": "angry",
    "many_errors": "surprised",
    "all_clear": "happy",
    "greeting": "greeting",
    "idle": "idle",
    "encourage": "happy",
    "file_scan": "angry",
    "file_remind": "idle",
    "file_clear": "happy",
}


def get_emotion(category: str) -> str:
    """返回场景对应的情绪标识，用于驱动立绘表情切换"""
    return EMOTION_MAP.get(category, "idle")


# ============================================================================
# JSONC 解析
# ============================================================================
def _skip_blanks(text: str, j: int) -> int:
    """从 j 起跳过空白**和注释**，返回下一个有效字符的下标。

    ⚠️ 尾随逗号的前瞻必须用这个而不是"只跳空白"：0.3.38 之前逗号后面
    跟注释时，前瞻看到的是 `/` 而不是 `}`/`]`，于是尾随逗号被保留；等
    注释被剥掉后，JSON 里就剩下 `[..., "b", ]` —— `json.loads` 直接报错，
    **整份用户语料（12 个场景 + 3 个点击分区）全部回落内置**。

    而文件头 `_FILE_HEADER` 是这么写的：「本文件允许注释和多余的尾逗号」，
    用户照着做（加一条台词 + 在行尾写个注释说明）就正好踩中。实测：
    加台词后写 `// 新加的` → scenes 从 12 变 0，用户改的台词一条不生效。
    """
    n = len(text)
    while j < n:
        ch = text[j]
        if ch in ' \t\r\n':
            j += 1
            continue
        if ch == '/' and j + 1 < n:
            if text[j + 1] == '/':
                k = text.find('\n', j)
                j = n if k < 0 else k
                continue
            if text[j + 1] == '*':
                k = text.find('*/', j + 2)
                j = n if k < 0 else k + 2
                continue
        break
    return j


def strip_jsonc(text: str) -> str:
    """剥掉 JSONC 的注释与尾随逗号，得到可以被 json.loads 吃下的纯 JSON。

    为什么要自己写：语料文件是给用户手改的，写注释（VS Code 原生支持
    .jsonc 的注释与尾随逗号）会显著降低"改坏"的概率。但 Python 的
    json.loads 不认注释，所以读之前先剥一层。

    保守实现 —— 用状态机而不是正则，避免把**字符串内部**的 `//` 或 `/*`
    当成注释（台词里出现 `//` 完全可能，例如 "// TODO 还没写"）：
      · 字符串内：原样复制，处理 \\ 转义，遇 " 才结束
      · 字符串外：`//` 到行尾、`/* */` 到闭合符，整段丢掉
      · 字符串外的 `,` 若后面（跳过空白**与注释**）就遇到 `}` / `]`
        —— 尾随逗号，丢掉。见 `_skip_blanks` 里那条 0.3.38 的实测。
    """
    if text.startswith('\ufeff'):        # BOM：Windows 编辑器存 UTF-8 时常见
        text = text[1:]
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n:
            nxt = text[i + 1]
            if nxt == '/':
                j = text.find('\n', i)
                i = n if j < 0 else j
                continue
            if nxt == '*':
                j = text.find('*/', i + 2)
                i = n if j < 0 else j + 2
                continue
        if ch == ',':
            # 尾随逗号：向后跳过空白**与注释**，若撞上 } 或 ] 就吞掉这个逗号
            j = _skip_blanks(text, i + 1)
            if j < n and text[j] in '}]':
                i += 1
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


# ============================================================================
# 用户语料：读取 / 校验 / 回落
# ============================================================================
#: 解耦日志：corpus 不该自己 print（standalone 才有日志文件），由使用方注入。
_log_hook = None


def set_log_hook(fn):
    """注入日志函数（standalone 启动时传自己的 log）。传 None 表示不记日志。"""
    global _log_hook
    _log_hook = fn


def _log(msg):
    if _log_hook is None:
        return
    try:
        _log_hook(msg)
    except Exception:
        pass


#: 缓存：(mtime_ns, size) → 解析结果。用户改完保存，键一变立刻重读。
#: `err_logged` 只用来给"读失败"的日志去重（0.3.44）—— 失败时不再推进 key，
#: 于是每一拍都会重读，日志不能跟着每一拍刷一遍。
_cache = {'key': None, 'scenes': {}, 'clicks': {}, 'error': None, 'dropped': 0,
          'err_logged': None}
_cache_lock = threading.Lock()
_ABSENT = ('__absent__', 0)


def _clean_scene_lines(scene, value):
    """把一个场景的值收敛成"可安全说出口"的台词列表。

    @returns (lines 或 None, 丢弃条数)。None 表示"这个场景没得用，走内置"。
    """
    allowed = ALLOWED_PLACEHOLDERS.get(scene, frozenset())
    if not isinstance(value, list):
        return None, 1 if value is not None else 0
    kept, dropped = [], 0
    for item in value:
        if not isinstance(item, str):
            dropped += 1
            continue
        text = item.strip()
        if not text:
            dropped += 1
            continue
        bad = set(_PLACEHOLDER_RE.findall(text)) - allowed
        if bad:
            # 该场景拿不到这些变量（见文件头"交集"那段）→ 说了会原样残留
            dropped += 1
            continue
        kept.append(text)
    return (kept or None), dropped


def _clean_click_pool(zone, value):
    """点击台词是 [文本, 情绪] 二元组；情绪词必须在 CLICK_EMOTIONS 里。"""
    if not isinstance(value, list):
        return None, 1 if value is not None else 0
    kept, dropped = [], 0
    for item in value:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not isinstance(item[0], str) or not isinstance(item[1], str)
                or not item[0].strip() or item[1] not in CLICK_EMOTIONS):
            dropped += 1
            continue
        kept.append((item[0].strip(), item[1]))
    return (kept or None), dropped


def _read_corpus_file(key):
    """真正读盘并校验。@returns (scenes, clicks, dropped, error)"""
    if key == _ABSENT:
        # 文件还没生成 —— 这是**正常**状态（用户没点过「编辑语料」），
        # 不算错误，别把日志刷满。
        return {}, {}, 0, None
    try:
        with open(CORPUS_PATH, encoding='utf-8-sig') as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        # ⚠️ UnicodeDecodeError 是 ValueError 的子类，**不是 OSError** ——
        # 0.3.38 之前只写 `except OSError`，于是用记事本把语料另存成
        # ANSI(GBK) / Unicode(UTF-16LE) 之后，异常从这里一路穿出去。
        # 取词路径（get_lines）有 except Exception 兜着，看起来只是"回落
        # 内置"；但 corpus_status() 没有兜底，而它在 standalone.py 的启动
        # 路径上是**顶层裸调用**（在 main() 之前）→ 进程直接没了，桌宠
        # 根本不出现，且 traceback 走 stderr 被扩展以 stdio:'ignore' 丢掉。
        # 实测两种编码都抛（0.3.38）。
        return {}, {}, 0, repr(exc)
    try:
        # encoding='utf-8-sig' 已经吃掉 BOM，这里再兜一次手动粘贴的
        obj = json.loads(strip_jsonc(raw.lstrip('\ufeff')))
    except Exception as exc:
        return {}, {}, 0, repr(exc)
    if not isinstance(obj, dict):
        return {}, {}, 0, 'top-level is not an object'

    scenes, clicks, dropped = {}, {}, 0
    raw_scenes = obj.get('scenes')
    if isinstance(raw_scenes, dict):
        for scene, value in raw_scenes.items():
            if scene not in BUILTIN_CORPUS:
                continue                      # 未知场景名：忽略（不改用户文件）
            lines, d = _clean_scene_lines(scene, value)
            dropped += d
            if lines:
                scenes[scene] = lines
    raw_clicks = obj.get('click_lines')
    if isinstance(raw_clicks, dict):
        for zone, value in raw_clicks.items():
            if zone not in CLICK_LINES:
                continue
            pool, d = _clean_click_pool(zone, value)
            dropped += d
            if pool:
                clicks[zone] = pool
    return scenes, clicks, dropped, None


def _user_corpus():
    """取用户语料（带缓存 + 热重载）。@returns (scenes, clicks, error)"""
    try:
        st = os.stat(CORPUS_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = _ABSENT
    with _cache_lock:
        if _cache['key'] == key:
            return _cache['scenes'], _cache['clicks'], _cache['error']
    scenes, clicks, dropped, error = _read_corpus_file(key)
    with _cache_lock:
        if error:
            # ⚠️ 读坏了**不能把上一版好语料抹掉**（0.3.38）。
            #
            # 原来这里是无条件 _cache.update(scenes={}, clicks={})：
            # 编辑器保存语料时是"截断 → 原地写"，桌宠 30 秒的检测周期完全
            # 可能正好落在中间，读到 0 字节；或者语料在网盘/OneDrive 目录下
            # 处于仅联机占位状态，open() 干脆失败。这两种情况下旧语料本来
            # 是完好的，却被清成空 —— 用户看到的是"我写的台词全都不认了"。
            #
            # ⚠️ 而且**不能推进 key**（0.3.44）：0.3.38 只修了一半 ——
            # 保住了 last-known-good，却仍然 `key=key`。凡是"stat 成功、open
            # 失败"的情形（OneDrive 仅联机占位 / 杀软短暂锁文件 / 编辑器保存
            # 窗口），文件的 (mtime, size) 已经是新的、不会再变 —— 于是这个
            # 键被记进缓存，之后每一次调用都命中缓存、永远不重试：用户解锁
            # 文件、把语料改好，本次运行内**再也不会生效**（只有"截断成 0
            # 字节再写"那条能自愈，因为 size 又变了）。现在失败一律不推进
            # key，下一拍就重试，日志按错误内容去重（见 err_logged）。
            prev_err = _cache['err_logged']
            _cache.update(error=error)
            kept_prev = False
            if _cache['scenes'] or _cache['clicks']:
                # 缓存里留着好的、返回的却是这次读坏的 {}，等于白保留
                scenes, clicks = _cache['scenes'], _cache['clicks']
                kept_prev = True
            else:
                scenes, clicks = {}, {}
            # 只在**错误内容变化**时留痕：失败不再推进 key、每一拍都会重读，
            # 无条件 log 会变成每 30 秒刷一行（get_lines 还会更频繁）。
            log_it = error != prev_err
            _cache['err_logged'] = error
        else:
            kept_prev = False
            log_it = False
            _cache.update(key=key, scenes=scenes, clicks=clicks,
                          error=error, dropped=dropped, err_logged=None)
    if error:
        if log_it:
            _log('[airi-corpus] %s (%s): %s'
                 % ('keep last good corpus' if kept_prev
                    else 'user corpus unreadable -> built-in lines',
                    CORPUS_PATH, error))
    elif scenes or clicks:
        _log('[airi-corpus] user corpus loaded: %d/%d scenes, %d/%d click pools,'
             ' %d line(s) dropped'
             % (len(scenes), len(SCENE_ORDER), len(clicks), len(CLICK_ZONES),
                dropped))
    return scenes, clicks, error


def get_lines(scene: str):
    """取某场景的台词列表：用户语料优先，缺失/写坏回落内置。

    ⚠️ 返回值可能是**内置的同一个 list 对象**，调用方不要就地修改它。
    """
    try:
        scenes, _clicks, _err = _user_corpus()
    except Exception:
        scenes = {}
    lines = scenes.get(scene)
    if lines:
        return lines
    return BUILTIN_CORPUS.get(scene) or ENCOURAGE


def get_click_lines(zone: str):
    """取某个点击分区的台词池（用户语料优先，回落内置）。

    @returns [(文本, 情绪), ...]
    """
    try:
        _scenes, clicks, _err = _user_corpus()
    except Exception:
        clicks = {}
    pool = clicks.get(zone)
    if pool:
        return pool
    builtin = CLICK_LINES.get(zone) or CLICK_LINES['body']
    return list(builtin)


def corpus_status():
    """当前语料来源概况，供启动日志/排障用。

    ⚠️ 这个函数**必须自己不抛异常**：它是排障用的，而调用点
    （standalone.py 启动段）是**顶层裸调用**，在 main() 之前 —— 它一抛，
    进程就没了，桌宠起不来，而 traceback 走 stderr 被扩展以
    stdio:'ignore' 丢弃，用户侧零提示（0.3.38）。
    """
    try:
        scenes, clicks, error = _user_corpus()
    except Exception as exc:      # 排障函数自己崩了是最糟的情况
        scenes, clicks, error = {}, {}, 'corpus_status: %r' % (exc,)
    try:
        with _cache_lock:
            dropped = _cache['dropped']
    except Exception:
        dropped = 0
    return {
        'path': CORPUS_PATH,
        'exists': os.path.exists(CORPUS_PATH),
        'scenes_from_user': len(scenes),
        'clicks_from_user': len(clicks),
        'dropped': dropped,
        'error': error,
    }


# ============================================================================
# 导出：生成用户可编辑的语料文件
# ============================================================================
_FILE_HEADER = """\
// ===========================================================================
// deepseek 桌宠 · 台词库（语料）
// ===========================================================================
//
// 这里就是桌宠平时说的话。每个场景是一组句子，桌宠每次**随机挑一条**说出来。
//
// 【改完就生效】保存这个文件即可 —— 桌面宠物会在几秒内自动用上新台词，
// 不用重启桌宠，也不用重启 VS Code。（关掉没保存的话当然不算。）
//
// 【占位符】有些句子里的 {file} {count} 这类东西会被自动换成真实内容：
//     {file}     当前文件名            {count}    错误数量
//     {line}     某一行行号            {lines}    若干行号（如"第3行、第7行"）
//     {language} 语言名（如 python）
// 每个场景能用哪几个，写在它上面那行注释里 —— **只写该场景支持的占位符**。
// 写了用不到的占位符，那一条会被忽略（免得气泡里出现 "{xxx}" 这种东西）。
//
// 【改坏了也不怕】文件写坏、某段留空、甚至整个文件删掉 —— 桌宠会自动
// 回退到内置台词，不会崩。删掉这个文件后，下次点「编辑语料」会重新生成。
//
// 注意：JSON 里字符串要用英文双引号 \"，每行结尾的逗号别漏（多一个也行，
// 本文件允许注释和多余的尾逗号）。
// ===========================================================================

{
  "scenes": {
"""


def export_default_corpus(path=None, overwrite=False):
    """把内置语料导出成用户可编辑的 JSONC 文件。

    · 默认**不覆盖**已存在的文件（用户的修改比内置的更新更值钱）
    · 原子写：先写同目录临时文件再 os.replace —— 读取方可能正好在 stat/watch
    @returns (是否写了, 目标路径, 错误信息或 None)
    """
    target = path or CORPUS_PATH
    if os.path.exists(target) and not overwrite:
        return False, target, None

    parts = [_FILE_HEADER]
    for idx, scene in enumerate(SCENE_ORDER):
        allowed = sorted(ALLOWED_PLACEHOLDERS.get(scene, frozenset()))
        hint = ('可用占位符：' + ' '.join('{%s}' % p for p in allowed)
                if allowed else '不能使用占位符')
        parts.append('    // %s —— %s\n' % (get_scene_name(scene), hint))
        parts.append('    "%s": [\n' % scene)
        lines = BUILTIN_CORPUS[scene]
        for k, line in enumerate(lines):
            comma = ',' if k < len(lines) - 1 else ''
            parts.append('      %s%s\n' % (json.dumps(line, ensure_ascii=False),
                                           comma))
        parts.append('    ],\n')
    parts.append('  },\n\n')
    parts.append('  // 点击互动：点角色身上不同位置说的话，格式是 ["台词", "情绪"]。\n')
    parts.append('  // 情绪只能是这五个之一：idle / greeting / angry / happy / surprised\n')
    parts.append('  "click_lines": {\n')
    for zi, zone in enumerate(CLICK_ZONES):
        parts.append('    "%s": [\n' % zone)
        pool = CLICK_LINES[zone]
        for k, (text, emo) in enumerate(pool):
            comma = ',' if k < len(pool) - 1 else ''
            parts.append('      %s%s\n' % (
                json.dumps([text, emo], ensure_ascii=False), comma))
        parts.append('    ],\n' if zi < len(CLICK_ZONES) - 1 else '    ]\n')
    parts.append('  }\n')
    parts.append('}\n')
    body = ''.join(parts)

    tmp = target + '.tmp'
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(body)
        os.replace(tmp, target)
        return True, target, None
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, target, repr(exc)


def ensure_corpus_file():
    """确保用户语料文件存在（不存在就导出内置的）。@returns 目标路径"""
    ok, target, err = export_default_corpus()
    if err:
        _log('[airi-corpus] export failed: %s' % err)
    elif ok:
        _log('[airi-corpus] created editable corpus: %s' % target)
    return target
