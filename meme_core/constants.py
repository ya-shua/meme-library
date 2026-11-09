"""表情包收录库的常量定义。"""

# 插件版本：唯一来源，必须与 metadata.json 的 version 一致。
# 契约接口 GET /api/health 报的是这个值（元数据只是被人读的副本）。
PLUGIN_VERSION = "1.1.1"

# 标准分类：标准标识 -> 中文名
STANDARD_TAGS = {
    "happy": "开心",
    "angry": "生气",
    "sad": "难过",
    "shy": "害羞",
    "confuse": "疑惑",
    "speechless": "无语",
    "neutral": "中性",
}

# 本地推断别名表：标准分类 -> 可能出现的目录名/文件名/心情词（全部小写匹配，0 token）
TAG_ALIASES = {
    "happy": ["happy", "开心", "高兴", "快乐", "愉快", "兴奋", "喜悦", "笑", "好耶", "嘻嘻", "哈哈", "美滋滋", "愉悦",
              # 表情包常见分类
              "like", "affection", "greeting", "morning", "meow", "喜欢", "打招呼", "早安", "喵"],
    "angry": ["angry", "生气", "愤怒", "恼火", "暴怒", "发火", "不爽", "气"],
    "sad": ["sad", "难过", "悲伤", "伤心", "哭", "委屈", "失落", "沮丧", "泪"],
    "shy": ["shy", "害羞", "羞涩", "脸红", "不好意思", "娇羞", "teasing", "color", "调戏", "暧昧"],
    "confuse": ["confuse", "confused", "疑惑", "困惑", "疑问", "不解", "迷惑", "啥", "surprised", "cpu", "惊讶", "震惊"],
    "speechless": ["speechless", "无语", "汗", "无奈", "扶额", "黑线", "离谱",
                   "baka", "fool", "sigh", "work", "givemoney", "money", "叹气", "笨蛋", "打钱", "加班"],
    "neutral": ["neutral", "正常", "普通", "一般", "平静", "中性",
                "food", "entertainment", "reply", "see", "sleep", "吃饭", "睡觉", "回复"],
}

# 集合类型
PACK_EMOTION = "emotion"  # 情绪包，参与自动配图
PACK_MEME = "meme"        # 梗图包，不自动发，仅手动

# 审核状态
REVIEW_CONFIRMED = "confirmed"      # 已确认入库
REVIEW_PENDING_HIGH = "pending_high"  # 高置信待确认（小概率出错）
REVIEW_PENDING_LOW = "pending_low"    # 低置信待审核
REVIEW_UNCATEGORIZED = "uncategorized"  # 未分类

# 这些目录里的一般是封面拼图、文档配图、示例图，不算表情本体
NON_MEME_DIRS = {"previews", "preview", "docs", "assets", ".github", ".git", "__pycache__"}

# 同一批图存了多个尺寸的目录：只留原图那份，别的当重复
SIZE_DIRS = {"large", "big", "small", "thumb", "thumbs", "medium", "mini", "压缩", "缩略图"}

# 支持的图片后缀
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# 内建的仓库地址库：地址抄在本地，运行时不再依赖任何外部索引。
# 名称和说明一律用我们自己的口径，不带别家框架的名字。
REPO_SOURCES = [
    {"name": "基础表情包1号", "repo": "anka-afk/astrbot-meme-pack-official-01",
     "ref": "main", "note": "通用基础表情，398 张，覆盖面最广，适合打底"},
    {"name": "星野表情包", "repo": "anka-afk/seio-stickers",
     "ref": "v2026.09.0", "note": "星野动态表情，208 张，分 15 组"},
    {"name": "ATRI 表情包", "repo": "anka-afk/atri-meme-pack",
     "ref": "v1.0.0", "note": "551 张静动态，15 组"},
    {"name": "丛雨表情包", "repo": "anka-afk/congyu-meme-pack",
     "ref": "v1.0.0", "note": "83 张，12 组"},
    {"name": "橘雪莉表情包", "repo": "anka-afk/juxueli-meme-pack",
     "ref": "v1.0.0", "note": "38 张静态，10 组"},
    {"name": "拉菲2表情包", "repo": "anka-afk/laffey2-meme-pack",
     "ref": "v1.0.0", "note": "148 张动态，16 组"},
    {"name": "Clover Days · 鹰仓杏铃", "repo": "anka-afk/clover-days-meme-pack",
     "ref": "v1.0.0", "note": "78 张静态，12 组"},
    {"name": "蓝色大肥鱼表情包", "repo": "anka-afk/blue-fat-fish-meme-pack",
     "ref": "v1.0.0", "note": "157 张动态，15 组"},
    {"name": "DeepSeek 二创表情包", "repo": "anka-afk/deepseek-fanart-meme-pack",
     "ref": "v1.0.0", "note": "174 张二创，13 组"},
    {"name": "1号·社区增强版", "repo": "DDZS987/astrbot-meme-pack-semantic-01",
     "ref": "v1.1.0", "note": "398 张，附语义描述"},
    {"name": "永雏塔菲表情包", "repo": "flybode/yctf-taffy-meme-pack",
     "ref": "main", "note": "19 组动图，B 站素材整理"},
    {"name": "Kipfel 表情包", "repo": "momo-OwO-qwq/astrbot-meme-pack-kipfel",
     "ref": "master", "note": "按聊天语义分组整理"},
    {"name": "爱蜜莉雅 Q版表情包", "repo": "just1cez/emilia-meme-pack",
     "ref": "main", "note": "120 张 PNG，21 组"},
    {"name": "命运石之门0", "repo": "Shennoter/astrbot-meme-pack-steins-gate-0",
     "ref": "main", "note": "命门角色聊天梗图"},
    {"name": "魔理沙表情包", "repo": "liquidtoy001/astrbot-meme-pack-marisa",
     "ref": "v1.0.0", "note": "165 张 GIF，22 组"},
    {"name": "和泉妃爱表情包", "repo": "NooAcc/xmfy-meme-pack",
     "ref": "main", "note": "99 张，18 组"},
    {"name": "中文表情包合集", "repo": "DumKing/ChineseBQB",
     "ref": "main", "note": "5173 张，28 组，量最大"},
    {"name": "蓝色大肥鱼档案馆", "repo": "EDMOK/blue-fish-archive",
     "ref": "main", "note": "鲸鱼娘同人表情收藏站，量大管饱，原图不加水印"},
    {"name": "梗图表情包", "repo": "yyh-001/dsh-meme",
     "ref": "main", "note": "梗图向表情，目录已经分好类，能直接映射成标签"},
    {"name": "大肥鱼表情包合集", "repo": "the-beating-light-of-the-nail/deepseek-chan-meme-pack",
     "ref": "main", "note": "鲸鱼娘梗图合集，偏聊天斗图向"},
]

# 兜底分类
FALLBACK_TAG = "neutral"
