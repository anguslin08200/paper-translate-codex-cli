"""各翻譯服務共用的英文技術術語政策。"""

# 基礎技術詞也保留英文；使用原文中的完整片語，不以中文加括號替代。
ENGLISH_TERMS_POLICY = (
    "Use Traditional Chinese for connective prose, but retain technical nouns, "
    "adjectives and complete technical phrases in their original English wording. "
    "This includes basic terms, not only proper names: register, memory, cache, "
    "kernel, thread, warp, dataflow, throughput, latent state, hidden state, "
    "high-dimensional latent state, state space model, SSM, "
    "discrete, continuous, discrete-time, continuous-time, discretization, "
    "discretize, convolution, attention, gating, recurrence, "
    "parallel associative scan, activation, projection and initialization. "
    "Preserve the source spelling, abbreviation, modifiers, hyphenation and plural form; "
    "keep the full phrase together, e.g. high-dimensional latent state, "
    "rather than translating its modifiers into Chinese. "
    "Do not replace these with Chinese equivalents or Chinese followed by English "
    "in parentheses. Preserve English model, algorithm, dataset and metric names. "
    "Retain the English actually used by the source; do not invent technical terms "
    "for ordinary nontechnical prose (e.g. contiguous is not continuous). "
    "A supplied glossary controls other terms, but these listed terms stay in English. "
)
