"""One conservative text redactor for source excerpts and model-facing context."""

import re


# Start once per identifier instead of rescanning every suffix of a long non-key token. Include the full
# optional identifier prefix so camelCase, digits and Unicode prefixes keep the same conservative masking.
_KEY = r"[\w-]*?(?:password|passwd|api[_-]?key|secret|secret[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|token|authorization)"
_PREFIX = rf'''((?<![\w-])["']?{_KEY}["']?\s*[:=]\s*)'''
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)
_TRIPLES = (re.compile(_PREFIX + r'("{3}.*?"{3})', re.I | re.S),
            re.compile(_PREFIX + r"('{3}.*?'{3})", re.I | re.S))
_QUOTED = re.compile(_PREFIX + r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', re.I | re.S)
_UNQUOTED = re.compile(_PREFIX + r'''([^\s"'#,;}]+)''', re.I)
_AUTH = re.compile(r"(?im)(\bauthorization\s*:\s*)(?:bearer|basic)\s+[^\r\n]+")
_USERINFO = re.compile(r"(?i)(https?://)[^/\s\"'<>]+@")
_QUERY = re.compile(r"(?i)([?&](?:api_key|token|access_token|password|secret)=)[^&#\s\"'<>]+")

# Common XML credential forms, matched as plain text: nothing is parsed, so entities, DTDs, includes and
# URLs are never resolved. Only an element name or a name/key attribute value that ends in a credential
# word counts (any case, optional namespace prefix); ordinary logging/config XML keeps its text.
_XWORD = (r"(?i:password|passwd|api[._-]?key|secret|secret[._-]?key|access[._-]?token|refresh[._-]?token"
          r"|client[._-]?secret|token|authorization)")
_XCRED = rf"[\w.:/-]*?{_XWORD}"
_XNS = r"(?:[A-Za-z_][\w.-]*:)?"
_XATTRS = r"""(?:\s+[A-Za-z_][\w.:-]*\s*=\s*(?:"[^"]*"|'[^']*'))*\s*"""
_XTEXT = r"(?P<text>(?:[^<]|<!\[CDATA\[.*?\]\]>|<!--.*?-->)*)"  # text, CDATA or comments; no child element
# <password>…</password>, <db:Password>…</db:Password>. The closing tag repeats the exact name, as XML
# requires, so source code such as List<Token> is not mistaken for an element.
_XELEMENT = re.compile(rf"(?P<head><(?P<tag>{_XNS}[\w.-]*?{_XWORD}){_XATTRS}>){_XTEXT}(?=</(?P=tag)\s*>)", re.S)
# <prop key="password">…</prop>, <property name="db.password"><value>…</value></property>
_XNAMED = re.compile(rf"""(?P<head><{_XNS}[\w.-]+{_XATTRS}\s{_XNS}(?:name|key)\s*=\s*(?:"{_XCRED}"|'{_XCRED}')"""
                     rf"{_XATTRS}>(?:\s*<{_XNS}value{_XATTRS}>)?){_XTEXT}(?=</)", re.I | re.S)
# <name>db.password</name><value>…</value>, <param-name>…</param-name><param-value>…</param-value>
_XPAIR = re.compile(rf"(?P<head><{_XNS}(?:[\w.-]*[_-])?(?:name|key){_XATTRS}>\s*{_XCRED}\s*</[^<>]+>\s*"
                    rf"(?:<!--.*?-->\s*)*<{_XNS}(?:[\w.-]*[_-])?value{_XATTRS}>){_XTEXT}(?=</)", re.I | re.S)
# <property name="password" value="…"/> with attributes in any order
_XTAG = re.compile(rf"""<{_XNS}[\w.-]+(?:\s+[A-Za-z_][\w.:-]*\s*=\s*(?:"[^"]*"|'[^']*'))+\s*/?>""")
_XATTR = re.compile(r"""([A-Za-z_][\w.:-]*)(\s*=\s*)(?:"([^"]*)"|'([^']*)')""")
_XCRED_NAME = re.compile(_XCRED, re.I)


def _mask(value):
    return "[REDACTED]" + "\n" * value.count("\n")


def _xml_text(match):
    text = match.group("text")
    return match.group("head") + (_mask(text) if text.strip() else text)


def _xml_tag(match):
    """Mask value attributes of a start tag whose name/key attribute names a credential."""
    def parts(item):
        quoted = item.group(3) is not None
        return item.group(1).rsplit(":", 1)[-1].lower(), '"' if quoted else "'", item.group(3 if quoted else 4)

    def masked(item):
        name, quote, value = parts(item)
        if name != "value" or not value.strip():
            return item.group(0)
        return item.group(1) + item.group(2) + quote + _mask(value) + quote

    tag = match.group(0)
    if not any(name in {"name", "key"} and _XCRED_NAME.fullmatch(value)
               for name, _, value in map(parts, _XATTR.finditer(tag))):
        return tag
    return _XATTR.sub(masked, tag)


def redact_text(text):
    """Preserve physical line count so frozen-source anchors remain meaningful.

    Masking is pattern-based and conservative; it cannot detect every possible secret form.
    """
    if type(text) is not str:
        raise ValueError("Redaction input must be text")
    text = _PEM.sub(lambda m: _mask(m.group(0)), text)
    for pattern in (_XELEMENT, _XNAMED, _XPAIR):
        text = pattern.sub(_xml_text, text)
    text = _XTAG.sub(_xml_tag, text)
    text = _AUTH.sub(lambda m: m.group(1) + "[REDACTED]", text)
    for pattern in _TRIPLES:
        text = pattern.sub(lambda m: m.group(1) + '"' + _mask(m.group(2)) + '"', text)
    text = _QUOTED.sub(lambda m: m.group(1) + '"' + _mask(m.group(2)) + '"', text)
    text = _UNQUOTED.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _AUTH.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _USERINFO.sub(r"\1[REDACTED]@", text)
    return _QUERY.sub(r"\1[REDACTED]", text)
