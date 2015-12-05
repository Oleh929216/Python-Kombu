"""
kombu.transport.beanstalk
=========================

Beanstalk transport.

:copyright: (c) 2010 - 2013 by David Ziegler.
:license: BSD, see LICENSE for more details.

"""
from __future__ import absolute_import

import socket

from kombu.five import Empty
from kombu.utils.encoding import bytes_to_str
from kombu.utils.json import loads, dumps

from . import virtual

try:
    import beanstalkc
except ImportError:  # pragma: no cover
    beanstalkc = None  # noqa

DEFAULT_PORT = 11300

__author__ = 'David Ziegler <david.ziegler@gmail.com>'


class Channel(virtual.Channel):
    _client = None
    _tube_map = {}

    def _format_tube_name(self, tube_name):
        """Valid tube should contain:
        ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-+/;.$_().
        https://github.com/kr/beanstalkd/blob/5fce2d1e14b61b0ffcb5d67f85f35792f8542d60/prot.c#L21

        So the tube_name is generated by `Mailbox` will be invalid.

        This function will change '@' to '.'.
        """
        if "@" not in tube_name:
            new_tube_name = tube_name
        else:
            new_tube_name = tube_name.replace("@", ".")
        self._tube_map[new_tube_name] = tube_name
        return new_tube_name

    def _parse_job(self, job):
        item, dest = None, None
        if job:
            try:
                item = loads(bytes_to_str(job.body))
                dest = job.stats()['tube']
                dest = self._tube_map[dest]
            except Exception:
                job.bury()
            else:
                job.delete()
        else:
            raise Empty()
        return item, dest

    def _put(self, queue, message, **kwargs):
        extra = {}
        priority = message['properties']['delivery_info']['priority']
        ttr = message['properties'].get('ttr')
        if ttr is not None:
            extra['ttr'] = ttr

        self.client.use(queue)
        self.client.put(dumps(message), priority=priority, **extra)

    def _get(self, queue):
        if queue not in self.client.watching():
            queue = self._format_tube_name(queue)
            self.client.watch(queue)

        [self.client.ignore(active) for active in self.client.watching()
         if active != queue]

        job = self.client.reserve(timeout=1)
        item, dest = self._parse_job(job)
        return item

    def _get_many(self, queues, timeout=1):
        # timeout of None will cause beanstalk to timeout waiting
        # for a new request
        if timeout is None:
            timeout = 1

        watching = self.client.watching()
        for queue in queues:
            self._format_tube_name(queue)

        [self.client.watch(tube) for tube in self._tube_map
         if tube not in watching]

        [self.client.ignore(active) for active in watching
         if active not in self._tube_map]

        job = self.client.reserve(timeout=timeout)
        return self._parse_job(job)

    def _purge(self, queue):
        if queue not in self.client.watching():
            self.client.watch(queue)

        [self.client.ignore(active)
         for active in self.client.watching()
         if active != queue]
        count = 0
        while 1:
            job = self.client.reserve(timeout=1)
            if job:
                job.delete()
                count += 1
            else:
                break
        return count

    def _size(self, queue):
        return 0

    def _open(self):
        conninfo = self.connection.client
        host = conninfo.hostname or 'localhost'
        port = conninfo.port or DEFAULT_PORT
        conn = beanstalkc.Connection(host=host, port=port)
        conn.connect()
        return conn

    def close(self):
        if self._client is not None:
            return self._client.close()
        super(Channel, self).close()

    @property
    def client(self):
        if self._client is None:
            self._client = self._open()
        return self._client


class Transport(virtual.Transport):
    Channel = Channel

    polling_interval = 1
    default_port = DEFAULT_PORT
    connection_errors = (
        virtual.Transport.connection_errors + (
            socket.error, IOError,
            getattr(beanstalkc, 'SocketError', None),
        )
    )
    channel_errors = (
        virtual.Transport.channel_errors + (
            socket.error, IOError,
            getattr(beanstalkc, 'SocketError', None),
            getattr(beanstalkc, 'BeanstalkcException', None),
        )
    )
    driver_type = 'beanstalk'
    driver_name = 'beanstalkc'

    def __init__(self, *args, **kwargs):
        if beanstalkc is None:
            raise ImportError(
                'Missing beanstalkc library (pip install beanstalkc)')
        super(Transport, self).__init__(*args, **kwargs)

    def driver_version(self):
        return beanstalkc.__version__
