# -*- coding: utf-8 -*-
"""绘制两张中文信息图：传统 RAG 的缺陷 / LLM Wiki 的改进（Pillow 矢量级布局）。"""
import math
from PIL import Image, ImageDraw, ImageFont

W, H = 1024, 1536
MSYH = "C:/Windows/Fonts/msyh.ttc"
MSYHBD = "C:/Windows/Fonts/msyhbd.ttc"


def font(size, bold=False):
    return ImageFont.truetype(MSYHBD if bold else MSYH, size)


def draw_arrow(d, x1, y1, x2, y2, color, width=3, dash=False):
    if dash:
        # 简单虚线：分 5 段
        import math as m
        seg = 8
        n = max(1, int(m.hypot(x2 - x1, y2 - y1) / (seg * 2)))
        dx, dy = (x2 - x1) / n, (y2 - y1) / n
        for i in range(n):
            px1, py1 = x1 + dx * i * 2, y1 + dy * i * 2
            px2, py2 = x1 + dx * (i * 2 + 1), y1 + dy * (i * 2 + 1)
            d.line([(px1, py1), (px2, py2)], fill=color, width=width)
    else:
        d.line([(x1, y1), (x2, y2)], fill=color, width=width)
    ang = math.atan2(y2 - y1, x2 - x1)
    s = 9
    ax = x2 - s * math.cos(ang - 0.42)
    ay = y2 - s * math.sin(ang - 0.42)
    bx = x2 - s * math.cos(ang + 0.42)
    by = y2 - s * math.sin(ang + 0.42)
    d.polygon([(x2, y2), (ax, ay), (bx, by)], fill=color)


def card(d, x, y, w, h, bg, outline, ow=2, radius=16):
    d.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=bg, outline=outline, width=ow)


def node(d, x, y, w, h, title, lines, accent=None):
    """白色节点卡片：标题（粗体）+ 若干行说明。accent 控制描边颜色。"""
    border = accent if accent else "#D6E2F0"
    card(d, x, y, w, h, "#FFFFFF", border, ow=3 if accent else 2)
    cy = y + (h - len(lines) * 26) // 2 + 10
    d.text((x + w / 2, cy), title, font=font(21, True), fill="#334155", anchor="mm")
    yy = cy + 32
    for ln in lines:
        d.text((x + w / 2, yy), ln, font=font(15), fill=accent if accent else "#64748B", anchor="mm")
        yy += (24 if len(lines) > 1 else 30)


# ============================================================ 图1：传统 RAG 的缺陷
def draw_rag_flaws(path):
    im = Image.new("RGB", (W, H), "#F6F8FB")
    d = ImageDraw.Draw(im)

    # 标题栏
    d.rectangle([0, 0, W, 150], fill="#1F3A5F")
    d.ellipse([50, 41, 118, 109], fill="#2E5B8F")
    d.text((84, 75), "!", font=font(38, True), fill="#FFFFFF", anchor="mm")
    d.text((140, 62), "传统 RAG 的缺陷", font=font(42, True), fill="#FFFFFF", anchor="lm")
    d.text((140, 112), "查询时才临时找资料：碎片化检索的四大痛点", font=font(24), fill="#B9D0EA", anchor="lm")

    # 流程节点
    node(d, 40, 200, 180, 190, "文档库", ["原始资料语料"], accent="#D6E2F0")
    draw_arrow(d, 222, 295, 276, 295, "#5B7A9E")
    node(d, 288, 200, 188, 190, "硬切分＋向量化", ["固定长度切块"], accent="#B91C1C")
    draw_arrow(d, 478, 295, 532, 295, "#5B7A9E")
    node(d, 544, 200, 180, 190, "向量数据库", ["Top-k 召回"], accent="#D6E2F0")
    draw_arrow(d, 726, 295, 780, 295, "#5B7A9E")
    node(d, 792, 200, 192, 190, "top-k 检索·拼接", ["机械拼接片段"], accent="#B91C1C")

    draw_arrow(d, 634, 392, 634, 466, "#5B7A9E")
    draw_arrow(d, 888, 392, 888, 466, "#DC2626", dash=True)

    # LLM 生成回答
    card(d, 420, 478, 300, 120, "#FFFFFF", "#D6E2F0")
    d.rounded_rectangle([430, 492, 710, 584], radius=10, fill="#EEF2FF")
    d.text((570, 534), "大语言模型", font=font(26, True), fill="#1D4ED8", anchor="mm")
    d.text((570, 566), "在超长拼接上下文中生成回答", font=font(16), fill="#475569", anchor="mm")
    # 回答气泡
    d.ellipse([682, 492, 898, 584], fill="#DBEAFE")
    d.polygon([(700, 556), (668, 588), (716, 568)], fill="#DBEAFE")
    d.text((790, 538), "回答", font=font(22, True), fill="#1E40AF", anchor="mm")

    # 缺陷卡片
    d.text((60, 636), "四大缺陷", font=font(30, True), fill="#B91C1C", anchor="lm")
    flaws = [
        ("1", "碎片化切分，上下文断裂",
         "文档按固定长度被硬切成孤立片段，段落逻辑与章节前因后果被拦腰截断，",
         "检索到手的往往是「缺头少尾」的碎片，跨段语义联系天然丢失。"),
        ("2", "固定检索策略，召回不全",
         "top-k 相似度检索只看词面与向量距离，跨章节、跨文档的隐性联系召回不到，",
         "复杂问题常检索到不相关片段——论文实测跨单元类问题得分仅 8.14/10。"),
        ("3", "拼接片段，上下文超长、成本高",
         "多个检索片段被机械拼进 Prompt，挤占上下文窗口，稀释模型注意力，",
         "推理质量不升反降，Token 成本也随片段数量水涨船高。"),
        ("4", "来源不可追溯，易生幻觉",
         "碎片与原始出处失去关联，模型难以交代依据来自哪里；",
         "论文实测跨单元问题仅 64% 的回答有引用支撑，其余难以核实。"),
    ]
    y0 = 664
    for i, (num, title, l1, l2) in enumerate(flaws):
        yy = y0 + i * 196
        card(d, 60, yy, 904, 176, "#FFFFFF", "#FECACA", ow=2)
        d.rounded_rectangle([60, yy, 70, yy + 176], radius=5, fill="#DC2626")
        d.ellipse([76, yy + 48, 156, yy + 128], fill="#DC2626")
        d.text((116, yy + 88), num, font=font(34, True), fill="#FFFFFF", anchor="mm")
        d.text((184, yy + 66), title, font=font(27, True), fill="#7F1D1D", anchor="lm")
        d.text((184, yy + 104), l1, font=font(18), fill="#475569", anchor="lm")
        d.text((184, yy + 134), l2, font=font(18), fill="#475569", anchor="lm")

    d.text((512, 1492), "资料来源：arXiv 2609.25303 对照实验（向量 RAG vs LLM 编译 Wiki）",
           font=font(17), fill="#94A3B8", anchor="mm")
    im.save(path, "PNG")
    print("saved", path)


# ============================================================ 图2：LLM Wiki 的改进
def draw_llm_wiki(path):
    im = Image.new("RGB", (W, H), "#F7FAF9")
    d = ImageDraw.Draw(im)

    # 标题栏
    d.rectangle([0, 0, W, 150], fill="#14532D")
    d.ellipse([50, 41, 118, 109], fill="#047857")
    d.line([(66, 76), (82, 92), (104, 60)], fill="#FFFFFF", width=8, joint="curve")
    d.text((140, 62), "LLM Wiki 的改进", font=font(42, True), fill="#FFFFFF", anchor="lm")
    d.text((140, 112), "把「查询时检索」升级为「摄入时结构化」：编译互链的知识维基",
           font=font(24), fill="#BBF7D0", anchor="lm")

    # 阶段一
    d.text((60, 184), "阶段一 · 摄入：LLM 离线编译知识库", font=font(22, True), fill="#047857", anchor="lm")

    node(d, 40, 220, 180, 170, "原始语料", ["课程文档/手册/网页"], accent="#D6E2F0")
    draw_arrow(d, 222, 305, 276, 305, "#0E7490")
    node(d, 288, 220, 200, 170, "LLM 离线编译", ["精读·去噪·归纳·建链", "一次性成本"], accent="#059669")
    draw_arrow(d, 490, 305, 544, 305, "#0E7490")

    # 互链概念页
    card(d, 556, 220, 428, 170, "#FFFFFF", "#34D399", ow=3)
    d.text((770, 248), "互相链接的概念页（Wiki 风格）", font=font(19, True), fill="#065F46", anchor="mm")
    pages = [("概念A", 588), ("概念B", 722), ("概念C", 856)]
    for name, px in pages:
        d.rounded_rectangle([px, 272, px + 104, 330], radius=10, fill="#D1FAE5")
        d.text((px + 52, 300), name, font=font(17, True), fill="#047857", anchor="mm")
    for px1, px2 in [(692, 720), (826, 854)]:
        draw_arrow(d, px1, 300, px2, 300, "#059669", width=2)
    d.text((770, 356), "每页带「原文出处」回链 ↪ 第X章 / 第X讲", font=font(14), fill="#065F46", anchor="mm")
    d.text((770, 378), "论断可一键追溯到原始来源", font=font(14), fill="#065F46", anchor="mm")

    # 阶段二
    d.text((60, 436), "阶段二 · 查询：顺着链接导航，而非碎片拼接", font=font(22, True), fill="#047857", anchor="lm")

    card(d, 40, 472, 200, 150, "#FFFFFF", "#A7F3D0", ow=2)
    d.text((140, 512), "用户提问", font=font(19, True), fill="#065F46", anchor="mm")
    d.rounded_rectangle([74, 538, 206, 590], radius=12, fill="#ECFDF5")
    d.text((140, 552), "为什么 A 影响 B？", font=font(16), fill="#047857", anchor="ma")
    d.text((140, 596), "跨章节综合问题", font=font(15), fill="#64748B", anchor="ma")
    draw_arrow(d, 242, 547, 296, 547, "#0E7490")

    card(d, 308, 472, 240, 150, "#FFFFFF", "#34D399", ow=2)
    d.text((428, 512), "精确定位相关概念页", font=font(18, True), fill="#065F46", anchor="mm")
    d.text((428, 552), "顺着交叉引用跳转导航", font=font(15), fill="#047857", anchor="mm")
    d.text((428, 586), "跨章节联系有图可循", font=font(15), fill="#047857", anchor="mm")
    draw_arrow(d, 550, 547, 604, 547, "#0E7490")

    card(d, 616, 472, 368, 150, "#FFFFFF", "#34D399", ow=2)
    d.rounded_rectangle([640, 492, 960, 602], radius=10, fill="#ECFDF5")
    d.text((800, 530), "大语言模型生成回答", font=font(20, True), fill="#065F46", anchor="mm")
    d.text((800, 560), "以完整概念页为上下文，结构完整", font=font(15), fill="#047857", anchor="mm")
    d.text((800, 586), "每一论断都带「可点击引用」", font=font(15), fill="#047857", anchor="mm")

    # 底部对比数据
    card(d, 60, 664, 904, 760, "#FFFFFF", "#A7F3D0", ow=2)
    d.text((512, 716), "对照实验结果（同一课堂语料，LLM 裁判按人工标准评分）",
           font=font(26, True), fill="#065F46", anchor="mm")

    def bar(row_title, ty, r1, t1, c1, r2, t2, c2):
        d.text((110, ty), row_title, font=font(22, True), fill="#334155", anchor="lm")
        d.rounded_rectangle([110, ty + 30, 950, ty + 82], radius=10, fill="#F1F5F9")
        d.rounded_rectangle([110, ty + 30, 110 + int(840 * r1), ty + 82], radius=10, fill=c1)
        d.text((130, ty + 56), t1, font=font(20, True), fill="#FFFFFF", anchor="lm")
        d.rounded_rectangle([110, ty + 98, 950, ty + 150], radius=10, fill="#F1F5F9")
        d.rounded_rectangle([110, ty + 98, 110 + int(840 * r2), ty + 150], radius=10, fill=c2)
        d.text((130, ty + 124), t2, font=font(20, True), fill="#FFFFFF", anchor="lm")

    bar("单事实问答", 786, 0.933, "向量 RAG：9.33 / 10", "#94A3B8", 0.996, "LLM Wiki：9.96 / 10", "#10B981")
    bar("跨单元综合推理", 1006, 0.814, "向量 RAG：8.14 / 10", "#DC2626", 0.993, "LLM Wiki：9.93 / 10", "#10B981")
    bar("回答来源可追溯性", 1226, 0.64, "向量 RAG：64%", "#DC2626", 1.00, "LLM Wiki：100% 可溯源", "#10B981")

    d.text((512, 1460), "结论：简单事实两者旗鼓相当；一旦需要跨章节联系，Wiki 结构全面领先。",
           font=font(18), fill="#64748B", anchor="mm")
    im.save(path, "PNG")
    print("saved", path)


if __name__ == "__main__":
    draw_rag_flaws("D:/小林coding的项目/outputs/rag_flaws.png")
    draw_llm_wiki("D:/小林coding的项目/outputs/llm_wiki_fix.png")