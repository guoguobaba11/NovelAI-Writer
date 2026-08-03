"""
纯 stdlib 写 .docx 文件（避免 python-docx 打包问题）
.docx = 一个 zip，包含 [Content_Types].xml + _rels/.rels + word/document.xml
支持原生批注 (word/comments.xml) — 出版社打开 .docx 能直接看到红头批注
"""
from __future__ import annotations
import io
import zipfile
from typing import Iterable
from xml.sax.saxutils import escape as xml_escape
import datetime
import time


# .docx 必须的 XML 模板（最小集）
_CONTENT_TYPES_BASE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
"""
_CONTENT_TYPES_COMMENT_OVERRIDE = '  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>\n'
_CONTENT_TYPES_TAIL = "</Types>\n"


def _content_types_xml(has_comments: bool) -> str:
    body = _CONTENT_TYPES_BASE
    if has_comments:
        body += _CONTENT_TYPES_COMMENT_OVERRIDE
    body += _CONTENT_TYPES_TAIL
    return body


_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

# Document 命名空间
_DOC_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)

# 批注命名空间（comments.xml 用）
_COMMENTS_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _xml_paragraph(text: str) -> str:
    """把一段文本转成 <w:p><w:r><w:t>text</w:t></w:r></w:p>"""
    # 处理换行
    parts = text.split("\n")
    runs = []
    for i, line in enumerate(parts):
        if i > 0:
            runs.append('<w:br/>')
        if line:
            runs.append(f'<w:t xml:space="preserve">{xml_escape(line)}</w:t>')
    return f'<w:p><w:r>{"".join(runs)}</w:r></w:p>'


def _xml_paragraph_with_comments(text: str, comments_in_para: list) -> str:
    """带批注的段落。
    comments_in_para: [{snippet, cid, body, author}] — 该段内的批注列表
    返回带 <w:commentRangeStart/End> + <w:commentReference> 的段落 XML
    OpenXML 结构：commentRangeStart/End 是段落级元素，runs 是同级兄弟节点，不嵌套
    """
    if not comments_in_para:
        return _xml_paragraph(text)
    # 在段落里顺序查找每个 snippet
    parts = []  # list of XML fragments at paragraph level
    cursor = 0
    positions = []
    for c in comments_in_para:
        idx = text.find(c["snippet"], cursor)
        if idx < 0:
            idx = text.find(c["snippet"])
        if idx < 0:
            continue
        positions.append((idx, idx + len(c["snippet"]), c))
    positions.sort(key=lambda x: x[0])
    for start, end, c in positions:
        if start < cursor:
            continue
        # 选中片段前的文本 → 独立 <w:r>
        if start > cursor:
            mid = text[cursor:start]
            parts.append(_xml_run_text(mid))
        # commentRangeStart (段落级)
        parts.append(f'<w:commentRangeStart w:id="{c["cid"]}"/>')
        # 选中片段 → 独立 <w:r>
        parts.append(_xml_run_text(text[start:end]))
        # commentRangeEnd (段落级) + commentReference (独立 <w:r>)
        parts.append(f'<w:commentRangeEnd w:id="{c["cid"]}"/>')
        parts.append(f'<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="{c["cid"]}"/></w:r>')
        cursor = end
    # 剩余
    if cursor < len(text):
        parts.append(_xml_run_text(text[cursor:]))
    return f'<w:p>{"".join(parts)}</w:p>'


def _xml_run_text(text: str) -> str:
    """把一段文本转成单个 <w:r>（不带外层 <w:p>）"""
    runs = []
    for i, line in enumerate(text.split("\n")):
        if i > 0:
            runs.append('<w:br/>')
        if line:
            runs.append(f'<w:t xml:space="preserve">{xml_escape(line)}</w:t>')
    if not runs:
        return ""
    return f'<w:r>{"".join(runs)}</w:r>'


def _xml_heading(text: str, level: int = 1) -> str:
    """标题"""
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r></w:p>'


def _xml_title(text: str) -> str:
    """居中大标题"""
    return (
        f'<w:p><w:pPr><w:jc w:val="center"/><w:rPr><w:sz w:val="40"/><w:b/></w:rPr></w:pPr>'
        f'<w:r><w:rPr><w:sz w:val="40"/><w:b/></w:rPr><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r></w:p>'
    )


def _xml_meta(text: str) -> str:
    """元信息（居中灰字）"""
    return (
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        f'<w:r><w:rPr><w:i/><w:color w:val="666666"/><w:sz w:val="20"/></w:rPr><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r></w:p>'
    )


def _xml_page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _xml_hr() -> str:
    return '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="auto"/></w:pBdr></w:pPr></w:p>'


def _build_comments_xml(comments: list) -> str:
    """生成 word/comments.xml — Word 原生批注"""
    if not comments:
        return ""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<w:comments {_COMMENTS_NS}>',
    ]
    for c in comments:
        # 转换时间戳为 ISO8601
        try:
            ts = float(c.get("created_at", time.time()))
            dt = datetime.datetime.fromtimestamp(ts)
            iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        author = c.get("author") or "editor"
        # 名字首字母做 initials
        initials = "".join([w[0] for w in author.split() if w])[:3] or "E"
        body = c.get("body", "")
        # 批注正文里支持多行（按 \n 切）
        body_paras = []
        for i, para in enumerate(body.split("\n")):
            if not para.strip():
                continue
            body_paras.append(
                f'<w:p><w:r><w:t xml:space="preserve">{xml_escape(para)}</w:t></w:r></w:p>'
            )
        if not body_paras:
            body_paras = ['<w:p><w:r><w:t xml:space="preserve">(空批注)</w:t></w:r></w:p>']
        parts.append(
            f'<w:comment w:id="{c["cid"]}" w:author="{xml_escape(author)}" '
            f'w:date="{iso}" w:initials="{xml_escape(initials)}">'
            + "".join(body_paras)
            + '</w:comment>'
        )
    parts.append('</w:comments>')
    return "\n".join(parts)


def build_docx(parts: Iterable[dict], comments: list = None) -> bytes:
    """
    parts: [
      {"type": "title", "text": "书名"},
      {"type": "meta", "text": "20万字 · 4卷 · 40回"},
      {"type": "hr"},
      {"type": "heading", "text": "第一卷 长安惊变", "level": 1},
      {"type": "heading", "text": "第一回 雨夜仵作", "level": 2},
      {"type": "meta", "text": "4,221 字"},
      {"type": "para", "text": "正文段落..."},
      ...
    ]
    comments: 可选 — Word 原生批注列表 [{id, body, author, created_at}]
    返回 .docx bytes
    """
    body_xml = []
    for p in parts:
        t = p.get("type")
        if t == "title":
            body_xml.append(_xml_title(p["text"]))
        elif t == "meta":
            body_xml.append(_xml_meta(p["text"]))
        elif t == "hr":
            body_xml.append(_xml_hr())
        elif t == "heading":
            body_xml.append(_xml_heading(p["text"], p.get("level", 2)))
        elif t == "page_break":
            body_xml.append(_xml_page_break())
        elif t == "para":
            body_xml.append(_xml_paragraph(p["text"]))
        else:
            body_xml.append(_xml_paragraph(str(p)))

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document {_DOC_NS}><w:body>{"".join(body_xml)}</w:body></w:document>'
    )

    has_comments = bool(comments)
    # 打包到 zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types_xml(has_comments))
        z.writestr("_rels/.rels", _RELS_XML)
        z.writestr("word/document.xml", document_xml)
        if has_comments:
            z.writestr("word/comments.xml", _build_comments_xml(comments))
    buf.seek(0)
    return buf.read()


def _split_text_with_comments(text: str, comments: list) -> list:
    """把章节文本按段落切分，每段附带该段内的批注。
    comments: [{id, body, author, snippet, anchor_start, anchor_end}]  — anchor_* 是字符 index
    返回 [(paragraph_text, [comment_dict_with_cid])] 列表
    """
    # 切段 — 用 split + find 拿每段在原文本的精确起止
    parts = text.split("\n\n")
    segs_with_pos = []
    cursor = 0
    for para in parts:
        idx = text.find(para, cursor)
        if idx < 0:
            cursor += len(para) + 2
            continue
        para_start = idx
        para_end = idx + len(para)
        cursor = para_end + 2
        if para.strip():
            segs_with_pos.append((para, para_start, para_end))
    # 第一遍：给每个 comment 算所有段的 clip，选 clip 长度最大的段作为主段
    comment_main_seg = {}  # comment_index -> seg_index
    for ci, c in enumerate(comments):
        s = c.get("anchor_start", 0)
        e = c.get("anchor_end", 0)
        if e < s:
            s, e = e, s
        best_seg = None
        best_len = 0
        for seg_i, (_, ps, pe) in enumerate(segs_with_pos):
            cs = max(s, ps)
            ce = min(e, pe)
            clip_len = ce - cs
            if clip_len > best_len:
                best_len = clip_len
                best_seg = seg_i
        if best_seg is not None and best_len > 0:
            comment_main_seg[ci] = best_seg
    # 第二遍：每段把属于自己的主段 comment 收为 hit
    out = []
    cid = 0
    for seg_i, (para, para_start, para_end) in enumerate(segs_with_pos):
        hits = []
        for ci, c in enumerate(comments):
            if comment_main_seg.get(ci) != seg_i:
                continue
            s = c.get("anchor_start", 0)
            e = c.get("anchor_end", 0)
            if e < s:
                s, e = e, s
            cs = max(s, para_start)
            ce = min(e, para_end)
            rel_start = cs - para_start
            rel_end = ce - para_start
            snippet_rel = para[rel_start:rel_end]
            if snippet_rel.strip():
                cid += 1
                hits.append({
                    "cid": cid,
                    "snippet": snippet_rel,
                    "body": c.get("body", ""),
                    "author": c.get("author", "editor"),
                    "created_at": c.get("created_at", time.time()),
                })
        out.append((para, hits))  # B-新30: 不 strip, 保留原段; 上游 _xml_paragraph 内部已处理
    return out


def build_chapter_docx(chapter: dict, book_title: str = "", comments: list = None) -> bytes:
    """从 chapter 字典构建单章 .docx
    comments: Word 原生批注列表 [{id, body, author, snippet, anchor_start, anchor_end}]
    """
    idx = chapter.get("idx", 0)
    ch_title = chapter.get("title", "")
    parts = []
    if book_title:
        parts.append({"type": "title", "text": book_title})
        parts.append({"type": "hr"})
    parts.append({"type": "title", "text": f"第 {idx} 回《{ch_title}》"})
    parts.append({"type": "meta", "text": f"{(chapter.get('word_count') or 0):,} 字"})
    meta_lines = []
    if chapter.get("location"):
        meta_lines.append(f"地点：{chapter['location']}")
    if chapter.get("story_time_start"):
        meta_lines.append(f"时间：{chapter['story_time_start']}")
    if chapter.get("outline"):
        meta_lines.append(f"大纲：{chapter['outline']}")
    if meta_lines:
        parts.append({"type": "meta", "text": " · ".join(meta_lines)})
    parts.append({"type": "hr"})
    text = chapter.get("final_text") or chapter.get("draft") or ""
    if not text:
        parts.append({"type": "meta", "text": "(本章暂无正文)"})
    else:
        if comments:
            # 段落切分 + 命中批注 → 用带批注段落 XML
            segs = _split_text_with_comments(text, comments)
            # 收集所有 cid 用于 comments.xml（去重并保持 cid 一致）
            all_cids = []
            for para_text, hits in segs:
                if hits:
                    # _xml_paragraph_with_comments 用 c["cid"] 索引
                    parts.append({"type": "_para_with_comments", "text": para_text, "comments_in_para": hits})
                    all_cids.extend(hits)
                else:
                    parts.append({"type": "para", "text": para_text})
            # 渲染带批注段落（在 parts 渲染阶段处理）
            # 实际：我们先用 _xml_paragraph 渲染所有，然后替换 _para_with_comments
            # 简化：直接在此处生成 XML，绕过 parts 流
            body_xml = []
            for p in parts:
                t = p.get("type")
                if t == "_para_with_comments":
                    body_xml.append(_xml_paragraph_with_comments(p["text"], p["comments_in_para"]))
                elif t == "title":
                    body_xml.append(_xml_title(p["text"]))
                elif t == "meta":
                    body_xml.append(_xml_meta(p["text"]))
                elif t == "hr":
                    body_xml.append(_xml_hr())
                elif t == "heading":
                    body_xml.append(_xml_heading(p["text"], p.get("level", 2)))
                elif t == "page_break":
                    body_xml.append(_xml_page_break())
                elif t == "para":
                    body_xml.append(_xml_paragraph(p["text"]))
                else:
                    body_xml.append(_xml_paragraph(str(p)))
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                f'<w:document {_DOC_NS}><w:body>{"".join(body_xml)}</w:body></w:document>'
            )
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("[Content_Types].xml", _content_types_xml(True))
                z.writestr("_rels/.rels", _RELS_XML)
                z.writestr("word/document.xml", document_xml)
                z.writestr("word/comments.xml", _build_comments_xml(all_cids))
            buf.seek(0)
            return buf.read()
        else:
            for para in text.split("\n\n"):
                para = para.strip()
                if para:
                    parts.append({"type": "para", "text": para})
    return build_docx(parts, comments=None)


def build_book_docx(chapters: list, volumes: list, project: dict, comments_by_chapter: dict = None) -> bytes:
    """从 chapters + volumes + project 构建整本 .docx
    comments_by_chapter: {chapter_id: [comments]} — 每章批注
    """
    vol_map = {v.get("idx"): v for v in volumes}
    book_title = project.get("title", "未命名") or "未命名"
    body_xml = []
    body_xml.append(_xml_title(book_title))
    if project.get("synopsis"):
        body_xml.append(_xml_meta("【简介】" + project["synopsis"]))
    body_xml.append(_xml_hr())
    cur_vol = None
    all_cids = []
    for c in chapters:
        v_idx = c.get("volume_idx")
        v = vol_map.get(v_idx) if v_idx else None
        if v and v_idx != cur_vol:
            cur_vol = v_idx
            v_label = f"第{v_idx}卷 {v.get('title','')}"
            body_xml.append(_xml_heading(v_label, 1))
        body_xml.append(_xml_heading(f"第 {c.get('idx',0)} 回《{c.get('title','')}》", 2))
        if c.get("word_count"):
            body_xml.append(_xml_meta(f"({c['word_count']:,} 字)"))
        text = c.get("final_text") or c.get("draft") or ""
        if not text:
            body_xml.append(_xml_meta("(本章暂无正文)"))
        else:
            chap_comments = (comments_by_chapter or {}).get(c.get("id"), [])
            if chap_comments:
                segs = _split_text_with_comments(text, chap_comments)
                for para_text, hits in segs:
                    if hits:
                        body_xml.append(_xml_paragraph_with_comments(para_text, hits))
                        all_cids.extend(hits)
                    else:
                        body_xml.append(_xml_paragraph(para_text))
            else:
                for para in text.split("\n\n"):
                    para = para.strip()
                    if para:
                        body_xml.append(_xml_paragraph(para))
        body_xml.append(_xml_page_break())

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document {_DOC_NS}><w:body>{"".join(body_xml)}</w:body></w:document>'
    )
    has_comments = bool(all_cids)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types_xml(has_comments))
        z.writestr("_rels/.rels", _RELS_XML)
        z.writestr("word/document.xml", document_xml)
        if has_comments:
            z.writestr("word/comments.xml", _build_comments_xml(all_cids))
    buf.seek(0)
    return buf.read()
