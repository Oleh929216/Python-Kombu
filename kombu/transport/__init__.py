"""Built-in transports."""
from __future__ import absolute_import, unicode_literals

from kombu.five import string_t
from kombu.utils.compat import _detect_environment
from kombu.utils.imports import symbol_by_name


def supports_librabbitmq():
    """Return true if :pypi:`librabbitmq` can be used."""
    if _detect_environment() == 'default':
        try:
            import librabbitmq  # noqa
        except ImportError:  # pragma: no cover
            pass
        else:                # pragma: no cover
            return True


TRANSPORT_ALIASES = {
    'amqp': 'kombu.transport.pyamqp:Transport',
    'amqps': 'kombu.transport.pyamqp:SSLTransport',
    'pyamqp': 'kombu.transport.pyamqp:Transport',
    'librabbitmq': 'kombu.transport.librabbitmq:Transport',
    'memory': 'kombu.transport.memory:Transport',
    'redis': 'kombu.transport.redis:Transport',
    'rediss': 'kombu.transport.redis:Transport',
    'SQS': 'kombu.transport.SQS:Transport',
    'sqs': 'kombu.transport.SQS:Transport',
    'mongodb': 'kombu.transport.mongodb:Transport',
    'zookeeper': 'kombu.transport.zookeeper:Transport',
    'sqlalchemy': 'kombu.transport.sqlalchemy:Transport',
    'sqla': 'kombu.transport.sqlalchemy:Transport',
    'SLMQ': 'kombu.transport.SLMQ.Transport',
    'slmq': 'kombu.transport.SLMQ.Transport',
    'filesystem': 'kombu.transport.filesystem:Transport',
    'qpid': 'kombu.transport.qpid:Transport',
    'sentinel': 'kombu.transport.redis:SentinelTransport',
    'consul': 'kombu.transport.consul:Transport',
    'etcd': 'kombu.transport.etcd:Transport',
}

_transport_cache = {}


def resolve_transport(transport=None):
    """Get transport by name.

    Arguments:
        transport (Union[str, type]): This can be either
            an actual transport class, or the fully qualified
            path to a transport class, or the alias of a transport.
    """
    if isinstance(transport, string_t):
        try:
            transport = TRANSPORT_ALIASES[transport]
        except KeyError:
            if '.' not in transport and ':' not in transport:
                from kombu.utils.text import fmatch_best
                alt = fmatch_best(transport, TRANSPORT_ALIASES)
                if alt:
                    raise KeyError(
                        'No such transport: {0}.  Did you mean {1}?'.format(
                            transport, alt))
                raise KeyError('No such transport: {0}'.format(transport))
        else:
            if callable(transport):
                transport = transport()
        return symbol_by_name(transport)
    return transport


def get_transport_cls(transport=None):
    """Get transport class by name.

    The transport string is the full path to a transport class, e.g.::

        "kombu.transport.pyamqp:Transport"

    If the name does not include `"."` (is not fully qualified),
    the alias table will be consulted.
    """
    if transport not in _transport_cache:
        _transport_cache[transport] = resolve_transport(transport)
    return _transport_cache[transport]
