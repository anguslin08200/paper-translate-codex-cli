"""統一模型輸出的繁體字形，同時保留公式、標記與程式碼。"""
from functools import lru_cache
import re
from opencc import OpenCC

# 只轉換文字敘述；標記屬性、數學與程式碼不可套用字形轉換。
PROTECTED = re.compile(r"(```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]|</?[^>\n]+>|\{\{[^{}\n]+\}\}|\{v\d+\}|\[v\d+\])")

@lru_cache(maxsize=1)
def converter():
    # 字形轉換在本機完成，不產生額外模型請求或傳送文件。
    return OpenCC("s2twp")

def to_traditional(text: str) -> str:
    pieces = PROTECTED.split(text)
    return "".join(part if index % 2 else converter().convert(part)
                   for index, part in enumerate(pieces))
