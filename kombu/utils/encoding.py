"""
kombu.utils.encoding
====================

Utilities to encode text, and to safely emit text from running
applications without crashing with the infamous :exc:`UnicodeDecodeError`
exception.

"""
from __future__ import absolute_import

import sys
import traceback

__all__ = ["str_to_bytes", "bytes_to_str", "from_utf8",
           "default_encoding", "safe_str", "safe_repr",
           "bytes_t", "str_t"]
is_py3k = sys.version_info >= (3, 0)


if is_py3k:

    def str_to_bytes(s):
        if isinstance(s, str):
            return s.encode()
        return s

    def bytes_to_str(s):
        if isinstance(s, bytes):
            return s.decode()
        return s

    def from_utf8(s, *args, **kwargs):
        return s

    bytes_t = bytes
    str_t = str

else:

    def str_to_bytes(s):                # noqa
        if isinstance(s, unicode):
            return s.encode()
        return s

    def bytes_to_str(s):                # noqa
        return s

    def from_utf8(s, *args, **kwargs):  # noqa
        return s.encode("utf-8", *args, **kwargs)

    bytes_t = str
    str_t = unicode


if sys.platform.startswith("java"):

    def default_encoding():
        return "utf-8"
else:

    def default_encoding():       # noqa
        return sys.getfilesystemencoding()


def safe_str(s, errors="replace"):
    s = bytes_to_str(s)
    if not isinstance(s, basestring):
        return safe_repr(s, errors)
    return _safe_str(s, errors)


def _safe_str(s, errors="replace"):
    if is_py3k:
        return s
    encoding = default_encoding()
    try:
        if isinstance(s, unicode):
            return s.encode(encoding, errors)
        return unicode(s, encoding, errors)
    except Exception, exc:
        return "<Unrepresentable %r: %r %r>" % (
                type(s), exc, "\n".join(traceback.format_stack()))


def safe_repr(o, errors="replace"):
    try:
        return repr(o)
    except Exception:
        return _safe_str(o, errors)
