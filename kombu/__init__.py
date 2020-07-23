"""Messaging library for Python."""

import os
import re
import sys

if sys.version_info < (2, 7):  # pragma: no cover
    raise Exception('Kombu 4.6 requires Python versions 2.7 or later.')

from collections import namedtuple  # noqa

__version__ = '4.6.11'
__author__ = 'Ask Solem'
__contact__ = 'auvipy@gmail.com, ask@celeryproject.org'
__homepage__ = 'https://kombu.readthedocs.io'
__docformat__ = 'restructuredtext en'

# -eof meta-

version_info_t = namedtuple('version_info_t', (
    'major', 'minor', 'micro', 'releaselevel', 'serial',
))

# bumpversion can only search for {current_version}
# so we have to parse the version here.
_temp = re.match(
    r'(\d+)\.(\d+).(\d+)(.+)?', __version__).groups()
VERSION = version_info = version_info_t(
    int(_temp[0]), int(_temp[1]), int(_temp[2]), _temp[3] or '', '')
del(_temp)
del(re)

STATICA_HACK = True
globals()['kcah_acitats'[::-1].upper()] = False
if STATICA_HACK:  # pragma: no cover
    # This is never executed, but tricks static analyzers (PyDev, PyCharm,
    # pylint, etc.) into knowing the types of these symbols, and what
    # they contain.
    from kombu.connection import Connection, BrokerConnection   # noqa
    from kombu.entity import Exchange, Queue, binding           # noqa
    from kombu.message import Message                           # noqa
    from kombu.messaging import Consumer, Producer              # noqa
    from kombu.pools import connections, producers              # noqa
    from kombu.utils.url import parse_url                       # noqa
    from kombu.common import eventloop, uuid                    # noqa
    from kombu.serialization import (                           # noqa
        enable_insecure_serializers,
        disable_insecure_serializers,
    )

# Lazy loading.
# - See werkzeug/__init__.py for the rationale behind this.
from types import ModuleType  # noqa

all_by_module = {
    'kombu.connection': ['Connection', 'BrokerConnection'],
    'kombu.entity': ['Exchange', 'Queue', 'binding'],
    'kombu.message': ['Message'],
    'kombu.messaging': ['Consumer', 'Producer'],
    'kombu.pools': ['connections', 'producers'],
    'kombu.utils.url': ['parse_url'],
    'kombu.common': ['eventloop', 'uuid'],
    'kombu.serialization': [
        'enable_insecure_serializers',
        'disable_insecure_serializers',
    ],
}

object_origins = {}
for module, items in all_by_module.items():
    for item in items:
        object_origins[item] = module


class module(ModuleType):
    """Customized Python module."""

    def __getattr__(self, name):
        if name in object_origins:
            module = __import__(object_origins[name], None, None, [name])
            for extra_name in all_by_module[module.__name__]:
                setattr(self, extra_name, getattr(module, extra_name))
            return getattr(module, name)
        return ModuleType.__getattribute__(self, name)

    def __dir__(self):
        result = list(new_module.__all__)
        result.extend(('__file__', '__path__', '__doc__', '__all__',
                       '__docformat__', '__name__', '__path__', 'VERSION',
                       '__package__', '__version__', '__author__',
                       '__contact__', '__homepage__', '__docformat__'))
        return result


# 2.5 does not define __package__
try:
    package = __package__
except NameError:  # pragma: no cover
    package = 'kombu'

# keep a reference to this module so that it's not garbage collected
old_module = sys.modules[__name__]

new_module = sys.modules[__name__] = module(__name__)
new_module.__dict__.update({
    '__file__': __file__,
    '__path__': __path__,
    '__doc__': __doc__,
    '__all__': tuple(object_origins),
    '__version__': __version__,
    '__author__': __author__,
    '__contact__': __contact__,
    '__homepage__': __homepage__,
    '__docformat__': __docformat__,
    '__package__': package,
    'version_info_t': version_info_t,
    'version_info': version_info,
    'VERSION': VERSION
})

if os.environ.get('KOMBU_LOG_DEBUG'):  # pragma: no cover
    os.environ.update(KOMBU_LOG_CHANNEL='1', KOMBU_LOG_CONNECTION='1')
    from .utils import debug
    debug.setup_logging()
