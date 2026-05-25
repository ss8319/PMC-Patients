import xml.etree.cElementTree as ET
import json
import re
from tqdm import trange, tqdm
import os

# Element node types to be extracted within a paragragh, section or inline.
p_nodes = ['p', 'list-item', 'disp-quote', "AbstractText"]
sec_nodes = ['sec', 'list']
inline_elements = ['italic', 'bold', 'sup', 'strike', 'sub', 'sc', 'named-content', 'underline', \
    'statement', 'monospace', 'roman', 'overline', 'styled-content', 'xref']

# Detect label in titles, such as "3.1" in "3.1 Patient 1"
label_pattern = re.compile(r'^[0-9]\.?[0-9]?\.?[0-9]?\.? ?')
# Replace several kinds of whitespace character into ' ' for convieniece of further processing. 
space = r"[\u3000\u2009\u2002\u2003\u00a0\u200a\xa0]"

"""
    Deal with unexpected and rebundant whitespace.
    Input:
        text: text to be cleaned.
    Output:
        cleaned text.
"""
def clean_text(text):
    text = re.sub(space, ' ', text).replace(u'\u2010', '-').strip()
    text = re.sub(r" +", ' ', text)
    text = re.sub(r"\n+", "\n", text)
    text = clean_text_artifacts(text)
    return text


# Regex set for post-extraction cleanup of xref-stripping artifacts.
# Square/round brackets containing nothing but whitespace and citation-style
# punctuation (commas, semicolons, dashes) \u2014 left over when self-closing
# bibr xrefs were collapsed to nothing. Only matches *fully empty* brackets;
# brackets with any digit/letter content are preserved.
_empty_brackets_pattern = re.compile(r'[\[\(]\s*[,;\-\s]*\s*[\]\)]')
# Trailing punctuation cluster left behind: "  , ," after empty xrefs.
_orphan_punct_pattern = re.compile(r' ([,;:.!?])')
# Collapse multiple consecutive spaces.
_multispace_pattern = re.compile(r' {2,}')


def clean_text_artifacts(text):
    """Post-extraction cleanup of artifacts left by xref stripping.
    Removes empty citation brackets like "[, , ]", "[-]", "()".
    Idempotent \u2014 safe to call multiple times.
    """
    text = _empty_brackets_pattern.sub('', text)
    text = _orphan_punct_pattern.sub(r'\1', text)
    text = _multispace_pattern.sub(' ', text)
    return text


def clean_refs(root):
    """Resolve xref elements in place \u2014 call once before parse_paragraph.

    Strategy:
      - bibr/ref xrefs: leave alone. If they have inner text (e.g. "1"),
        getText preserves it via the inline_elements pickup, giving "[1]".
        If self-closing, they contribute nothing \u2014 surrounding empty
        brackets get post-cleaned by clean_text_artifacts.
      - fig/table xrefs with EMPTY .text: inject the resolved label from
        the document's <fig>/<table-wrap> id-to-label map. Falls back to
        "[<rid>]" if the rid doesn't resolve.
      - fig/table xrefs with existing .text: leave alone (author chose
        their phrasing \u2014 don't overwrite "Fig. 1" with "Figure 1").

    Why: PMC XML often uses self-closing <xref/> elements that rely on the
    publisher's XSLT to render labels. Plain ElementTree parsing gives us
    empty strings. This pass restores the visible content authors intended,
    keeping image-text binding signals (e.g. "Figure 3") intact for Step 2
    image-case-lesion linking.
    """
    id_to_label = {}
    for elem in root.iter():
        if elem.tag in ('fig', 'table-wrap') and elem.get('id'):
            label_el = elem.find('label')
            if label_el is not None:
                label_text = ''.join(label_el.itertext()).strip()
                if label_text:
                    id_to_label[elem.get('id')] = label_text

    for xref in root.iter('xref'):
        ref_type = xref.get('ref-type', '')
        if ref_type not in ('fig', 'table'):
            continue
        # Check ALL descendant text, not just direct .text: PMC often wraps the
        # visible reference in a child (e.g. <xref><bold>Figure 1A</bold></xref>),
        # leaving .text empty. Overwriting it would destroy the panel letter
        # ("1A" -> "1"). Only inject a label when the xref is genuinely empty.
        if ''.join(xref.itertext()).strip():
            continue
        rid = xref.get('rid', '')
        if rid in id_to_label:
            xref.text = id_to_label[rid]
        elif rid:
            xref.text = f'[{rid}]'

    return root

"""
    Extract title in a title node.
    Input:
        sec: An element node of type 'sec'.
    Output:
        title of the section or string "" if no title node detected.
"""
def getTitle(sec):
    for child in sec:
        if child.tag == "title":
            title = getText(child)
            return clean_text(re.sub(label_pattern, '', title))
    return ""

"""
    Extract text from a given node and its successive children.
    Input:
        para: An element node of type 'p' or others.
    Output:
        text within the node and its successive children.
""" 
def getText(para):
    text = para.text if para.text else ""
    for child in para:
        if child.tag == 'xref':
            # xref content may live in children (e.g. <xref><bold>Figure 1A</bold>
            # </xref>); itertext() captures the panel letter that plain .text drops.
            text += ''.join(child.itertext())
        elif child.tag in inline_elements:
            text += child.text if child.text else ""
        if child.tag in sec_nodes or child.tag in p_nodes:
            text += getText(child) + ' '
        text += child.tail if child.tail else ""
    
    return clean_text(text)

"""
    Parse paragraph for an article or section. 
    Input:
        body: Element node to be parsed. If an article is to be parsed, input body node of xml.
        secname: Section name that will be concatenated ahead to all parsed paragraph.
            If an article is to be parsed, input empty string.
    Output:
        List of tuples (titles, text), where titles are section names (if subsection involved, titles are seperated by '[SEP]' token).
"""
def parse_paragraph(body, secname = ""):
    results = []
    title = getTitle(body)
    titles = secname + title
    if title:
        titles += "[SEP]"
    for child in body:
        if child.tag in p_nodes:
            text = getText(child)
            if len(text) > 1:
                results.append((titles, text))
        if child.tag in sec_nodes:
            results += parse_paragraph(child, titles)

    return results

"""
    Extract text of a section.
    Input:
        sec: Element node of type 'sec'.
    Output:
        Texts within this section, paragraphs seperated by '\n'.
"""
def getSection(sec):
    paras = parse_paragraph(sec)
    text = ""
    for para in paras:
        text += para[1] + '\n'
    return clean_text(text)


def extract_article_tables(root):
    """Walk <table-wrap> elements anywhere in the article, return list of structured dicts.

    Each entry: {table_id, label, caption, structured_rows}.
    structured_rows is a best-effort list[list[str]] of cell text. Handles
    both HTML-style tables (<table><tr><td>) and OASIS-CALS tables
    (<tgroup><row><entry>) — PMC articles use either convention.

    Walks from the article root (not just <body>) because PMC articles
    commonly place tables inside <floats-group>, a sibling of <body>
    used by publishers to hold floating elements (tables, figures, boxed text)
    referenced via <xref> rather than inlined.

    Call AFTER clean_refs(root) so xref labels inside captions are resolved.
    """
    if root is None:
        return []
    tables = []
    for tw in root.iter('table-wrap'):
        table_id = tw.get('id', '')
        label_el = tw.find('label')
        label = getText(label_el) if label_el is not None else ''
        caption_el = tw.find('caption')
        caption = getText(caption_el) if caption_el is not None else ''

        rows = []
        for table_el in tw.iter('table'):
            for tr in table_el.iter('tr'):
                row_cells = [getText(c) for c in tr if c.tag in ('td', 'th')]
                if row_cells:
                    rows.append(row_cells)
        if not rows:
            for row in tw.iter('row'):
                row_cells = [getText(c) for c in row.findall('entry')]
                if row_cells:
                    rows.append(row_cells)

        # label_number — int N parsed from "Table N" — downstream bind.py uses
        # this to match [TABLE:N] markers against tables.
        m = re.search(r'(\d+)', label)
        label_number = int(m.group(1)) if m else None

        tables.append({
            'table_id': table_id,
            'label': label,
            'label_number': label_number,
            'caption': caption,
            'structured_rows': rows,
        })
    return tables


def extract_article_figures(root):
    """Walk <fig> elements anywhere in the article, return list of structured dicts.

    Each entry: {fig_id, label, label_number, caption, panels}.
      - fig_id: XML id attribute
      - label: e.g., "Figure 1", "Fig. 2"
      - label_number: int N parsed from label (downstream binding + the
        rephrase.py caption-aware criterion 5 use this to match
        [IMAGE_FINDING:N] markers against figures)
      - caption: full caption text — used by rephrase.py as GROUND TRUTH
        for what is image-recoverable (caption-aware masking rule, 2026-05-21)
      - panels: list of {panel_id, label, caption} for any nested <fig> sub-panels
        (PMC encodes multi-panel figures either as nested <fig> children of a
        <fig-group> or as panel-letter prefixes embedded in the caption text).
        Empty list for flat single-panel figures.

    Mirrors extract_article_tables(): walks from article root (not just <body>)
    because PMC commonly places figures in <floats-group> as a sibling of <body>,
    referenced via <xref> rather than inlined.

    Call AFTER clean_refs(root) so xref labels inside captions are resolved.
    """
    if root is None:
        return []

    def _full_text(el):
        """Concatenate all text descendants. Use itertext() because PMC commonly
        wraps caption content in <title>/<p>/inline tags, and the existing
        getText() helper does not recurse into <title>."""
        if el is None:
            return ''
        out = ' '.join(t.strip() for t in el.itertext() if t and t.strip())
        return clean_text(out)

    figs = []
    for fg in root.iter('fig'):
        fig_id = fg.get('id', '')
        label_el = fg.find('label')
        label = _full_text(label_el)
        caption_el = fg.find('caption')
        caption = _full_text(caption_el)

        # label_number — int N parsed from "Figure N" — downstream uses this
        # to match [IMAGE_FINDING:N] markers against figures.
        m = re.search(r'(\d+)', label)
        label_number = int(m.group(1)) if m else None

        # Sub-panels: PMC encodes panel labels (A, B, C) either as nested
        # <fig> children of a <fig-group> OR via panel letters embedded in
        # the caption ("Fig. 1A: ...; Fig. 1B: ..."). We capture the structural
        # path here; downstream code can also parse caption text for embedded
        # panel labels.
        panels = []
        for sub in fg.findall('fig'):
            sub_label_el = sub.find('label')
            sub_label = _full_text(sub_label_el)
            sub_cap_el = sub.find('caption')
            sub_caption = _full_text(sub_cap_el)
            panels.append({
                'panel_id': sub.get('id', ''),
                'label': sub_label,
                'caption': sub_caption,
            })

        figs.append({
            'fig_id': fig_id,
            'label': label,
            'label_number': label_number,
            'caption': caption,
            'panels': panels,
        })
    return figs


'''
if __name__ == "__main__":
    cases = json.load(open("../../meta_data/PMC-Patients.txt", "r"))
    directory = "../../../PMC_OA"

    for case in tqdm(cases):
        file_name = case['file_name']
        tree = ET.parse(os.path.join(directory, file_name))
        root = tree.getroot()
        body = root.find(".//body")
        paras = parse_paragraph('', body)
        print(file_name)
        for para in paras:
            print(para[0])
            print(para[1])
        import ipdb; ipdb.set_trace()
'''