from __future__ import annotations

import base64
import copy
import socket
import types
from collections import defaultdict
from itertools import count
from queue import Empty
from queue import Queue as _Queue
from typing import TYPE_CHECKING
from unittest.mock import ANY, Mock, call, patch

import pytest

from kombu import Connection, Consumer, Exchange, Producer, Queue
from kombu.exceptions import VersionMismatch
from kombu.transport import virtual
from kombu.utils import eventio  # patch poll
from kombu.utils.json import dumps

if TYPE_CHECKING:
    from types import TracebackType


def _redis_modules():

    class ConnectionError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class InvalidData(Exception):
        pass

    class InvalidResponse(Exception):
        pass

    class ResponseError(Exception):
        pass

    exceptions = types.ModuleType('redis.exceptions')
    exceptions.ConnectionError = ConnectionError
    exceptions.AuthenticationError = AuthenticationError
    exceptions.InvalidData = InvalidData
    exceptions.InvalidResponse = InvalidResponse
    exceptions.ResponseError = ResponseError

    class Redis:
        pass

    myredis = types.ModuleType('redis')
    myredis.exceptions = exceptions
    myredis.Redis = Redis

    return myredis, exceptions


class _poll(eventio._select):

    def register(self, fd, flags):
        if flags & eventio.READ:
            self._rfd.add(fd)

    def poll(self, timeout):
        events = []
        for fd in self._rfd:
            if fd.data:
                events.append((fd.fileno(), eventio.READ))
        return events


eventio.poll = _poll

pytest.importorskip('redis')

# must import after poller patch, pep8 complains
from kombu.transport import redis  # noqa


class ResponseError(Exception):
    pass


class Client:
    queues = {}
    sets = defaultdict(set)
    hashes = defaultdict(dict)
    shard_hint = None

    def __init__(self, db=None, port=None, connection_pool=None, **kwargs):
        self._called = []
        self._connection = None
        self.bgsave_raises_ResponseError = False
        self.connection = self._sconnection(self)

    def bgsave(self):
        self._called.append('BGSAVE')
        if self.bgsave_raises_ResponseError:
            raise ResponseError()

    def delete(self, key):
        self.queues.pop(key, None)

    def exists(self, key):
        return key in self.queues or key in self.sets

    def hset(self, key, k, v):
        self.hashes[key][k] = v

    def hget(self, key, k):
        return self.hashes[key].get(k)

    def hdel(self, key, k):
        self.hashes[key].pop(k, None)

    def sadd(self, key, member, *args):
        self.sets[key].add(member)

    def zadd(self, key, *args):
        if redis.redis.VERSION[0] >= 3:
            (mapping,) = args
            for item in mapping:
                self.sets[key].add(item)
        else:
            # TODO: remove me when we drop support for Redis-py v2
            (score1, member1) = args
            self.sets[key].add(member1)

    def smembers(self, key):
        return self.sets.get(key, set())

    def ping(self, *args, **kwargs):
        return True

    def srem(self, key, *args):
        self.sets.pop(key, None)
    zrem = srem

    def llen(self, key):
        try:
            return self.queues[key].qsize()
        except KeyError:
            return 0

    def lpush(self, key, value):
        self.queues[key].put_nowait(value)

    def parse_response(self, connection, type, **options):
        cmd, queues = self.connection._sock.data.pop()
        queues = list(queues)
        assert cmd == type
        self.connection._sock.data = []
        if type == 'BRPOP':
            timeout = queues.pop()
            item = self.brpop(queues, timeout)
            if item:
                return item
            raise Empty()

    def brpop(self, keys, timeout=None):
        for key in keys:
            try:
                item = self.queues[key].get_nowait()
            except Empty:
                pass
            else:
                return key, item

    def rpop(self, key):
        try:
            return self.queues[key].get_nowait()
        except (KeyError, Empty):
            pass

    def __contains__(self, k):
        return k in self._called

    def pipeline(self):
        return Pipeline(self)

    def encode(self, value):
        return str(value)

    def _new_queue(self, key):
        self.queues[key] = _Queue()

    class _sconnection:
        disconnected = False

        class _socket:
            blocking = True
            filenos = count(30)

            def __init__(self, *args):
                self._fileno = next(self.filenos)
                self.data = []

            def fileno(self):
                return self._fileno

            def setblocking(self, blocking):
                self.blocking = blocking

        def __init__(self, client):
            self.client = client
            self._sock = self._socket()

        def disconnect(self):
            self.disconnected = True

        def send_command(self, cmd, *args):
            self._sock.data.append((cmd, args))

    def info(self):
        return {'foo': 1}

    def pubsub(self, *args, **kwargs):
        connection = self.connection

        class ConnectionPool:

            def get_connection(self, *args, **kwargs):
                return connection
        self.connection_pool = ConnectionPool()

        return self


class Pipeline:

    def __init__(self, client):
        self.client = client
        self.stack = []

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None
    ) -> None:
        pass

    def __getattr__(self, key):
        if key not in self.__dict__:

            def _add(*args, **kwargs):
                self.stack.append((getattr(self.client, key), args, kwargs))
                return self

            return _add
        return self.__dict__[key]

    def execute(self):
        stack = list(self.stack)
        self.stack[:] = []
        return [fun(*args, **kwargs) for fun, args, kwargs in stack]


class Channel(redis.Channel):

    def _get_client(self):
        return Client

    def _get_pool(self, asynchronous=False):
        return Mock()

    def _get_response_error(self):
        return ResponseError

    def _new_queue(self, queue, **kwargs):
        for pri in self.priority_steps:
            self.client._new_queue(self._q_for_pri(queue, pri))

    def pipeline(self):
        return Pipeline(Client())


class Transport(redis.Transport):
    Channel = Channel
    connection_errors = (KeyError,)
    channel_errors = (IndexError,)


class test_Channel:

    def setup_method(self):
        self.connection = self.create_connection()
        self.channel = self.connection.default_channel

    def create_connection(self, **kwargs):
        kwargs.setdefault('transport_options', {'fanout_patterns': True})
        return Connection(transport=Transport, **kwargs)

    def _get_one_delivery_tag(self, n='test_uniq_tag'):
        with self.create_connection() as conn1:
            chan = conn1.default_channel
            chan.exchange_declare(n)
            chan.queue_declare(n)
            chan.queue_bind(n, n, n)
            msg = chan.prepare_message('quick brown fox')
            chan.basic_publish(msg, n, n)
            payload = chan._get(n)
            assert payload
            pymsg = chan.message_to_python(payload)
            return pymsg.delivery_tag

    def test_delivery_tag_is_uuid(self):
        seen = set()
        for i in range(100):
            tag = self._get_one_delivery_tag()
            assert tag not in seen
            seen.add(tag)
            with pytest.raises(ValueError):
                int(tag)
            assert len(tag) == 36

    def test_disable_ack_emulation(self):
        conn = Connection(transport=Transport, transport_options={
            'ack_emulation': False,
        })

        chan = conn.channel()
        assert not chan.ack_emulation
        assert chan.QoS == virtual.QoS

    def test_redis_ping_raises(self):
        pool = Mock(name='pool')
        pool_at_init = [pool]
        client = Mock(name='client')

        class XChannel(Channel):

            def __init__(self, *args, **kwargs):
                self._pool = pool_at_init[0]
                super().__init__(*args, **kwargs)

            def _get_client(self):
                return lambda *_, **__: client

        class XTransport(Transport):
            Channel = XChannel

        conn = Connection(transport=XTransport)
        conn.transport.cycle = Mock(name='cycle')
        client.ping.side_effect = RuntimeError()
        with pytest.raises(RuntimeError):
            conn.channel()
        pool.disconnect.assert_called_with()
        pool.disconnect.reset_mock()
        # Ensure that the channel without ensured connection to Redis
        # won't be added to the cycle.
        conn.transport.cycle.add.assert_not_called()
        assert len(conn.transport.channels) == 0

        pool_at_init = [None]
        with pytest.raises(RuntimeError):
            conn.channel()
        pool.disconnect.assert_not_called()

    def test_redis_connection_added_to_cycle_if_ping_succeeds(self):
        """Test should check the connection is added to the cycle only
           if the ping to Redis was finished successfully."""
        # given: mock pool and client
        pool = Mock(name='pool')
        client = Mock(name='client')

        # override channel class with given mocks
        class XChannel(Channel):
            def __init__(self, *args, **kwargs):
                self._pool = pool
                super().__init__(*args, **kwargs)

            def _get_client(self):
                return lambda *_, **__: client

        # override Channel in Transport with given channel
        class XTransport(Transport):
            Channel = XChannel

        # when: create connection with overridden transport
        conn = Connection(transport=XTransport)
        conn.transport.cycle = Mock(name='cycle')
        # create the channel
        chan = conn.channel()
        # then: check if ping was called
        client.ping.assert_called_once()
        # the connection was added to the cycle
        conn.transport.cycle.add.assert_called_once()
        assert len(conn.transport.channels) == 1
        # the channel was flagged as registered into poller
        assert chan._registered

    def test_redis_on_disconnect_channel_only_if_was_registered(self):
        """Test should check if the _on_disconnect method is called only
           if the channel was registered into the poller."""
        # given: mock pool and client
        pool = Mock(name='pool')
        client = Mock(
            name='client',
            ping=Mock(return_value=True)
        )

        # create RedisConnectionMock class
        # for the possibility to run disconnect method
        class RedisConnectionMock:
            def disconnect(self, *args):
                pass

        # override Channel method with given mocks
        class XChannel(Channel):
            connection_class = RedisConnectionMock

            def __init__(self, *args, **kwargs):
                self._pool = pool
                # counter to check if the method was called
                self.on_disconect_count = 0
                super().__init__(*args, **kwargs)

            def _get_client(self):
                return lambda *_, **__: client

            def _on_connection_disconnect(self, connection):
                # increment the counter when the method is called
                self.on_disconect_count += 1

        # create the channel
        chan = XChannel(Mock(
            _used_channel_ids=[],
            channel_max=1,
            channels=[],
            client=Mock(
                transport_options={},
                hostname="127.0.0.1",
                virtual_host=None)))
        # create the _connparams with overridden connection_class
        connparams = chan._connparams(asynchronous=True)
        # create redis.Connection
        redis_connection = connparams['connection_class']()
        # the connection was added to the cycle
        chan.connection.cycle.add.assert_called_once()
        # and the ping was called
        client.ping.assert_called_once()
        # the channel was registered
        assert chan._registered
        # than disconnect the Redis connection
        redis_connection.disconnect()
        # the on_disconnect counter should be incremented
        assert chan.on_disconect_count == 1

    def test_redis__on_disconnect_should_not_be_called_if_not_registered(self):
        """Test should check if the _on_disconnect method is not called because
           the connection to Redis isn't established properly."""
        # given: mock pool
        pool = Mock(name='pool')
        # client mock with ping method which return ConnectionError
        from redis.exceptions import ConnectionError
        client = Mock(
            name='client',
            ping=Mock(side_effect=ConnectionError())
        )

        # create RedisConnectionMock
        # for the possibility to run disconnect method
        class RedisConnectionMock:
            def disconnect(self, *args):
                pass

        # override Channel method with given mocks
        class XChannel(Channel):
            connection_class = RedisConnectionMock

            def __init__(self, *args, **kwargs):
                self._pool = pool
                # counter to check if the method was called
                self.on_disconect_count = 0
                super().__init__(*args, **kwargs)

            def _get_client(self):
                return lambda *_, **__: client

            def _on_connection_disconnect(self, connection):
                # increment the counter when the method is called
                self.on_disconect_count += 1

        # then: exception was risen
        with pytest.raises(ConnectionError):
            # when: create the channel
            chan = XChannel(Mock(
                _used_channel_ids=[],
                channel_max=1,
                channels=[],
                client=Mock(
                    transport_options={},
                    hostname="127.0.0.1",
                    virtual_host=None)))
            # create the _connparams with overridden connection_class
            connparams = chan._connparams(asynchronous=True)
            # create redis.Connection
            redis_connection = connparams['connection_class']()
            # the connection wasn't added to the cycle
            chan.connection.cycle.add.assert_not_called()
            # the ping was called once with the exception
            client.ping.assert_called_once()
            # the channel was not registered
            assert not chan._registered
            # then: disconnect the Redis connection
            redis_connection.disconnect()
            # the on_disconnect counter shouldn't be incremented
            assert chan.on_disconect_count == 0

    def test_get_redis_ConnectionError(self):
        from redis.exceptions import ConnectionError

        from kombu.transport.redis import get_redis_ConnectionError
        connection_error = get_redis_ConnectionError()
        assert connection_error == ConnectionError

    def test_after_fork_cleanup_channel(self):
        from kombu.transport.redis import _after_fork_cleanup_channel
        channel = Mock()
        _after_fork_cleanup_channel(channel)
        channel._after_fork.assert_called_once()

    def test_after_fork(self):
        self.channel._pool = None
        self.channel._after_fork()

        pool = self.channel._pool = Mock(name='pool')
        self.channel._after_fork()
        pool.disconnect.assert_called_with()

    def test_next_delivery_tag(self):
        assert (self.channel._next_delivery_tag() !=
                self.channel._next_delivery_tag())

    def test_do_restore_message(self):
        client = Mock(name='client')
        pl1 = {'body': 'BODY'}
        spl1 = dumps(pl1)
        lookup = self.channel._lookup = Mock(name='_lookup')
        lookup.return_value = {'george', 'elaine'}
        self.channel._do_restore_message(
            pl1, 'ex', 'rkey', client,
        )
        client.rpush.assert_has_calls([
            call('george', spl1), call('elaine', spl1),
        ], any_order=True)

        client = Mock(name='client')
        pl2 = {'body': 'BODY2', 'headers': {'x-funny': 1}}
        headers_after = dict(pl2['headers'], redelivered=True)
        spl2 = dumps(dict(pl2, headers=headers_after))
        self.channel._do_restore_message(
            pl2, 'ex', 'rkey', client,
        )
        client.rpush.assert_any_call('george', spl2)
        client.rpush.assert_any_call('elaine', spl2)

        client.rpush.side_effect = KeyError()
        with patch('kombu.transport.redis.crit') as crit:
            self.channel._do_restore_message(
                pl2, 'ex', 'rkey', client,
            )
            crit.assert_called()

    def test_do_restore_message_celery(self):
        # Payload value from real Celery project
        payload = {
            "body": base64.b64encode(dumps([
                [],
                {},
                {
                    "callbacks": None,
                    "errbacks": None,
                    "chain": None,
                    "chord": None,
                },
            ]).encode()).decode(),
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {
                "lang": "py",
                "task": "common.tasks.test_task",
                "id": "980ad2bf-104c-4ce0-8643-67d1947173f6",
                "shadow": None,
                "eta": None,
                "expires": None,
                "group": None,
                "group_index": None,
                "retries": 0,
                "timelimit": [None, None],
                "root_id": "980ad2bf-104c-4ce0-8643-67d1947173f6",
                "parent_id": None,
                "argsrepr": "()",
                "kwargsrepr": "{}",
                "origin": "gen3437@Desktop",
                "ignore_result": False,
            },
            "properties": {
                "correlation_id": "980ad2bf-104c-4ce0-8643-67d1947173f6",
                "reply_to": "512f2489-ca40-3585-bc10-9b801a981782",
                "delivery_mode": 2,
                "delivery_info": {
                    "exchange": "",
                    "routing_key": "celery",
                },
                "priority": 3,
                "body_encoding": "base64",
                "delivery_tag": "badb725e-9c3e-45be-b0a4-07e44630519f",
            },
        }
        result_payload = copy.deepcopy(payload)
        result_payload['headers']['redelivered'] = True
        result_payload['properties']['delivery_info']['redelivered'] = True
        queue = 'celery'

        client = Mock(name='client')
        lookup = self.channel._lookup = Mock(name='_lookup')
        lookup.return_value = [queue]

        self.channel._do_restore_message(
            payload, 'exchange', 'routing_key', client,
        )

        client.rpush.assert_called_with(self.channel._q_for_pri(queue, 3),
                                        dumps(result_payload))

    def test_restore_no_messages(self):
        message = Mock(name='message')

        with patch('kombu.transport.redis.loads') as loads:
            def transaction_handler(restore_transaction, unacked_key):
                assert unacked_key == self.channel.unacked_key
                pipe = Mock(name='pipe')
                pipe.hget.return_value = None

                restore_transaction(pipe)

                pipe.multi.assert_called_once_with()
                pipe.hdel.assert_called_once_with(
                        unacked_key, message.delivery_tag)
                loads.assert_not_called()

            client = self.channel._create_client = Mock(name='client')
            client = client()
            client.transaction.side_effect = transaction_handler
            self.channel._restore(message)
            client.transaction.assert_called()

    def test_restore_messages(self):
        message = Mock(name='message')

        with patch('kombu.transport.redis.loads') as loads:

            def transaction_handler(restore_transaction, unacked_key):
                assert unacked_key == self.channel.unacked_key
                restore = self.channel._do_restore_message = Mock(
                    name='_do_restore_message',
                )
                result = Mock(name='result')
                loads.return_value = 'M', 'EX', 'RK'
                pipe = Mock(name='pipe')
                pipe.hget.return_value = result

                restore_transaction(pipe)

                loads.assert_called_with(result)
                pipe.multi.assert_called_once_with()
                pipe.hdel.assert_called_once_with(
                        unacked_key, message.delivery_tag)
                loads.assert_called()
                restore.assert_called_with('M', 'EX', 'RK', pipe, False)

            client = self.channel._create_client = Mock(name='client')
            client = client()
            client.transaction.side_effect = transaction_handler
            self.channel._restore(message)

    def test_qos_restore_visible(self):
        client = self.channel._create_client = Mock(name='client')
        client = client()

        def pipe(*args, **kwargs):
            return Pipeline(client)
        client.pipeline = pipe
        client.zrevrangebyscore.return_value = [
            (1, 10),
            (2, 20),
            (3, 30),
        ]
        qos = redis.QoS(self.channel)
        restore = qos.restore_by_tag = Mock(name='restore_by_tag')
        qos._vrestore_count = 1
        qos.restore_visible()
        client.zrevrangebyscore.assert_not_called()
        assert qos._vrestore_count == 2

        qos._vrestore_count = 0
        qos.restore_visible()
        restore.assert_has_calls([
            call(1, client), call(2, client), call(3, client),
        ])
        assert qos._vrestore_count == 1

        qos._vrestore_count = 0
        restore.reset_mock()
        client.zrevrangebyscore.return_value = []
        qos.restore_visible()
        restore.assert_not_called()
        assert qos._vrestore_count == 1

        qos._vrestore_count = 0
        client.setnx.side_effect = redis.MutexHeld()
        qos.restore_visible()

    def test_basic_consume_when_fanout_queue(self):
        self.channel.exchange_declare(exchange='txconfan', type='fanout')
        self.channel.queue_declare(queue='txconfanq')
        self.channel.queue_bind(queue='txconfanq', exchange='txconfan')

        assert 'txconfanq' in self.channel._fanout_queues
        self.channel.basic_consume('txconfanq', False, None, 1)
        assert 'txconfanq' in self.channel.active_fanout_queues
        assert self.channel._fanout_to_queue.get('txconfan') == 'txconfanq'

    def test_basic_cancel_unknown_delivery_tag(self):
        assert self.channel.basic_cancel('txaseqwewq') is None

    def test_subscribe_no_queues(self):
        self.channel.subclient = Mock()
        self.channel.active_fanout_queues.clear()
        self.channel._subscribe()
        self.channel.subclient.subscribe.assert_not_called()

    def test_subscribe(self):
        self.channel.subclient = Mock()
        self.channel.active_fanout_queues.add('a')
        self.channel.active_fanout_queues.add('b')
        self.channel._fanout_queues.update(a=('a', ''), b=('b', ''))

        self.channel._subscribe()
        self.channel.subclient.psubscribe.assert_called()
        s_args, _ = self.channel.subclient.psubscribe.call_args
        assert sorted(s_args[0]) == ['/{db}.a', '/{db}.b']

        self.channel.subclient.connection._sock = None
        self.channel._subscribe()
        self.channel.subclient.connection.connect.assert_called_with()

    def test_handle_unsubscribe_message(self):
        s = self.channel.subclient
        s.subscribed = True
        self.channel._handle_message(s, ['unsubscribe', 'a', 0])
        assert not s.subscribed

    def test_handle_pmessage_message(self):
        res = self.channel._handle_message(
            self.channel.subclient,
            ['pmessage', 'pattern', 'channel', 'data'],
        )
        assert res == {
            'type': 'pmessage',
            'pattern': 'pattern',
            'channel': 'channel',
            'data': 'data',
        }

    def test_handle_message(self):
        res = self.channel._handle_message(
            self.channel.subclient,
            ['type', 'channel', 'data'],
        )
        assert res == {
            'type': 'type',
            'pattern': None,
            'channel': 'channel',
            'data': 'data',
        }

    def test_brpop_start_but_no_queues(self):
        assert self.channel._brpop_start() is None

    def test_receive(self):
        s = self.channel.subclient = Mock()
        self.channel._fanout_to_queue['a'] = 'b'
        self.channel.connection._deliver = Mock(name='_deliver')
        message = {
            'body': 'hello',
            'properties': {
                'delivery_tag': 1,
                'delivery_info': {'exchange': 'E', 'routing_key': 'R'},
            },
        }
        s.parse_response.return_value = ['message', 'a', dumps(message)]
        self.channel._receive_one(self.channel.subclient)
        self.channel.connection._deliver.assert_called_once_with(
            message, 'b',
        )

    def test_receive_raises_for_connection_error(self):
        self.channel._in_listen = True
        s = self.channel.subclient = Mock()
        s.parse_response.side_effect = KeyError('foo')

        with pytest.raises(KeyError):
            self.channel._receive_one(self.channel.subclient)
        assert not self.channel._in_listen

    def test_receive_empty(self):
        s = self.channel.subclient = Mock()
        s.parse_response.return_value = None

        assert self.channel._receive_one(self.channel.subclient) is None

    def test_receive_different_message_Type(self):
        s = self.channel.subclient = Mock()
        s.parse_response.return_value = ['message', '/foo/', 0, 'data']

        assert self.channel._receive_one(self.channel.subclient) is None

    def test_receive_invalid_response_type(self):
        s = self.channel.subclient = Mock()
        for resp in ['foo', None]:
            s.parse_response.return_value = resp
            assert self.channel._receive_one(self.channel.subclient) is None

    def test_receive_connection_has_gone(self):
        def _receive_one(c):
            c.connection = None
            _receive_one.called = True
            return True

        _receive_one.called = False
        self.channel._receive_one = _receive_one

        assert self.channel._receive()
        assert _receive_one.called

    def test_brpop_read_raises(self):
        c = self.channel.client = Mock()
        c.parse_response.side_effect = KeyError('foo')

        with pytest.raises(KeyError):
            self.channel._brpop_read()

        c.connection.disconnect.assert_called_with()

    def test_brpop_read_gives_None(self):
        c = self.channel.client = Mock()
        c.parse_response.return_value = None

        with pytest.raises(redis.Empty):
            self.channel._brpop_read()

    def test_poll_error(self):
        c = self.channel.client = Mock()
        c.parse_response = Mock()
        self.channel._poll_error('BRPOP')

        c.parse_response.assert_called_with(c.connection, 'BRPOP')

        c.parse_response.side_effect = KeyError('foo')
        with pytest.raises(KeyError):
            self.channel._poll_error('BRPOP')

    def test_poll_error_on_type_LISTEN(self):
        c = self.channel.subclient = Mock()
        c.parse_response = Mock()
        self.channel._poll_error('LISTEN')

        c.parse_response.assert_called_with()

        c.parse_response.side_effect = KeyError('foo')
        with pytest.raises(KeyError):
            self.channel._poll_error('LISTEN')

    def test_put_fanout(self):
        self.channel._in_poll = False
        c = self.channel._create_client = Mock()

        body = {'hello': 'world'}
        self.channel._put_fanout('exchange', body, '')
        c().publish.assert_called_with('/{db}.exchange', dumps(body))

    def test_put_priority(self):
        client = self.channel._create_client = Mock(name='client')
        msg1 = {'properties': {'priority': 3}}

        self.channel._put('george', msg1)
        client().lpush.assert_called_with(
            self.channel._q_for_pri('george', 3), dumps(msg1),
        )

        msg2 = {'properties': {'priority': 313}}
        self.channel._put('george', msg2)
        client().lpush.assert_called_with(
            self.channel._q_for_pri('george', 9), dumps(msg2),
        )

        msg3 = {'properties': {}}
        self.channel._put('george', msg3)
        client().lpush.assert_called_with(
            self.channel._q_for_pri('george', 0), dumps(msg3),
        )

    def test_delete(self):
        x = self.channel
        x._create_client = Mock()
        x._create_client.return_value = x.client
        delete = x.client.delete = Mock()
        srem = x.client.srem = Mock()

        x._delete('queue', 'exchange', 'routing_key', None)
        delete.assert_has_calls([
            call(x._q_for_pri('queue', pri)) for pri in redis.PRIORITY_STEPS
        ])
        srem.assert_called_with(x.keyprefix_queue % ('exchange',),
                                x.sep.join(['routing_key', '', 'queue']))

    def test_has_queue(self):
        self.channel._create_client = Mock()
        self.channel._create_client.return_value = self.channel.client
        exists = self.channel.client.exists = Mock()
        exists.return_value = True
        assert self.channel._has_queue('foo')
        exists.assert_has_calls([
            call(self.channel._q_for_pri('foo', pri))
            for pri in redis.PRIORITY_STEPS
        ])

        exists.return_value = False
        assert not self.channel._has_queue('foo')

    def test_close_when_closed(self):
        self.channel.closed = True
        self.channel.close()

    def test_close_deletes_autodelete_fanout_queues(self):
        self.channel._fanout_queues = {'foo': ('foo', ''), 'bar': ('bar', '')}
        self.channel.auto_delete_queues = ['foo']
        self.channel.queue_delete = Mock(name='queue_delete')

        client = self.channel.client
        self.channel.close()
        self.channel.queue_delete.assert_has_calls([
            call('foo', client=client),
        ])

    def test_close_client_close_raises(self):
        c = self.channel.client = Mock()
        connection = c.connection
        connection.disconnect.side_effect = self.channel.ResponseError()

        self.channel.close()
        connection.disconnect.assert_called_with()

    def test_invalid_database_raises_ValueError(self):

        with pytest.raises(ValueError):
            self.channel.connection.client.virtual_host = 'dwqeq'
            self.channel._connparams()

    def test_connparams_allows_slash_in_db(self):
        self.channel.connection.client.virtual_host = '/123'
        assert self.channel._connparams()['db'] == 123

    def test_connparams_db_can_be_int(self):
        self.channel.connection.client.virtual_host = 124
        assert self.channel._connparams()['db'] == 124

    def test_new_queue_with_auto_delete(self):
        redis.Channel._new_queue(self.channel, 'george', auto_delete=False)
        assert 'george' not in self.channel.auto_delete_queues
        redis.Channel._new_queue(self.channel, 'elaine', auto_delete=True)
        assert 'elaine' in self.channel.auto_delete_queues

    def test_connparams_regular_hostname(self):
        self.channel.connection.client.hostname = 'george.vandelay.com'
        assert self.channel._connparams()['host'] == 'george.vandelay.com'

    def test_connparams_username(self):
        self.channel.connection.client.userid = 'kombu'
        assert self.channel._connparams()['username'] == 'kombu'

    def test_connparams_client_credentials(self):
        self.channel.connection.client.hostname = \
            'redis://foo:bar@127.0.0.1:6379/0'
        connection_parameters = self.channel._connparams()

        assert connection_parameters['username'] == 'foo'
        assert connection_parameters['password'] == 'bar'

    def test_connparams_password_for_unix_socket(self):
        self.channel.connection.client.hostname = \
            'socket://:foo@/var/run/redis.sock'
        connection_parameters = self.channel._connparams()
        password = connection_parameters['password']
        path = connection_parameters['path']
        assert (password, path) == ('foo', '/var/run/redis.sock')
        self.channel.connection.client.hostname = \
            'socket://@/var/run/redis.sock'
        connection_parameters = self.channel._connparams()
        password = connection_parameters['password']
        path = connection_parameters['path']
        assert (password, path) == (None, '/var/run/redis.sock')

    def test_connparams_health_check_interval_not_supported(self):
        with patch('kombu.transport.redis.Channel._create_client'):
            with Connection('redis+socket:///tmp/redis.sock') as conn:
                conn.default_channel.connection_class = \
                    Mock(name='connection_class')
                connparams = conn.default_channel._connparams()
                assert 'health_check_interval' not in connparams

    def test_connparams_health_check_interval_supported(self):
        with patch('kombu.transport.redis.Channel._create_client'):
            with Connection('redis+socket:///tmp/redis.sock') as conn:
                connparams = conn.default_channel._connparams()
                assert connparams['health_check_interval'] == 25

    def test_rotate_cycle_ValueError(self):
        cycle = self.channel._queue_cycle
        cycle.update(['kramer', 'jerry'])
        cycle.rotate('kramer')
        assert cycle.items, ['jerry' == 'kramer']
        cycle.rotate('elaine')

    def test_get_client(self):
        import redis as R
        KombuRedis = redis.Channel._get_client(self.channel)
        assert isinstance(KombuRedis(), R.StrictRedis)

        Rv = getattr(R, 'VERSION', None)
        try:
            R.VERSION = (2, 4, 0)
            with pytest.raises(VersionMismatch):
                redis.Channel._get_client(self.channel)
        finally:
            if Rv is not None:
                R.VERSION = Rv

    def test_get_prefixed_client(self):
        from kombu.transport.redis import PrefixedStrictRedis
        self.channel.global_keyprefix = "test_"
        PrefixedRedis = redis.Channel._get_client(self.channel)
        assert isinstance(PrefixedRedis(), PrefixedStrictRedis)

    def test_get_response_error(self):
        from redis.exceptions import ResponseError
        assert redis.Channel._get_response_error(self.channel) is ResponseError

    def test_avail_client(self):
        self.channel._pool = Mock()
        cc = self.channel._create_client = Mock()
        with self.channel.conn_or_acquire():
            pass
        cc.assert_called_with()

    def test_register_with_event_loop(self):
        transport = self.connection.transport
        transport.cycle = Mock(name='cycle')
        transport.cycle.fds = {12: 'LISTEN', 13: 'BRPOP'}
        conn = Mock(name='conn')
        conn.client = Mock(name='client', transport_options={})
        loop = Mock(name='loop')
        redis.Transport.register_with_event_loop(transport, conn, loop)
        transport.cycle.on_poll_init.assert_called_with(loop.poller)
        loop.call_repeatedly.assert_has_calls([
            call(10, transport.cycle.maybe_restore_messages),
            call(25, transport.cycle.maybe_check_subclient_health),
        ])
        loop.on_tick.add.assert_called()
        on_poll_start = loop.on_tick.add.call_args[0][0]

        on_poll_start()
        transport.cycle.on_poll_start.assert_called_with()
        loop.add_reader.assert_has_calls([
            call(12, transport.on_readable, 12),
            call(13, transport.on_readable, 13),
        ])

    @pytest.mark.parametrize('fds', [{12: 'LISTEN', 13: 'BRPOP'}, {}])
    def test_register_with_event_loop__on_disconnect__loop_cleanup(self, fds):
        """Ensure event loop polling stops on disconnect (if started)."""
        transport = self.connection.transport
        self.connection._sock = None
        transport.cycle = Mock(name='cycle')
        transport.cycle.fds = fds
        conn = Mock(name='conn')
        conn.client = Mock(name='client', transport_options={})
        loop = Mock(name='loop')
        loop.on_tick = set()
        redis.Transport.register_with_event_loop(transport, conn, loop)
        assert len(loop.on_tick) == 1
        transport.cycle._on_connection_disconnect(self.connection)
        if fds:
            assert len(loop.on_tick) == 0
        else:
            # on_tick shouldn't be cleared when polling hasn't started
            assert len(loop.on_tick) == 1

    def test_configurable_health_check(self):
        transport = self.connection.transport
        transport.cycle = Mock(name='cycle')
        transport.cycle.fds = {12: 'LISTEN', 13: 'BRPOP'}
        conn = Mock(name='conn')
        conn.client = Mock(name='client', transport_options={
            'health_check_interval': 15,
        })
        loop = Mock(name='loop')
        redis.Transport.register_with_event_loop(transport, conn, loop)
        transport.cycle.on_poll_init.assert_called_with(loop.poller)
        loop.call_repeatedly.assert_has_calls([
            call(10, transport.cycle.maybe_restore_messages),
            call(15, transport.cycle.maybe_check_subclient_health),
        ])
        loop.on_tick.add.assert_called()
        on_poll_start = loop.on_tick.add.call_args[0][0]

        on_poll_start()
        transport.cycle.on_poll_start.assert_called_with()
        loop.add_reader.assert_has_calls([
            call(12, transport.on_readable, 12),
            call(13, transport.on_readable, 13),
        ])

    def test_transport_on_readable(self):
        transport = self.connection.transport
        cycle = transport.cycle = Mock(name='cyle')
        cycle.on_readable.return_value = None

        redis.Transport.on_readable(transport, 13)
        cycle.on_readable.assert_called_with(13)

    def test_transport_connection_errors(self):
        """Ensure connection_errors are populated."""
        assert redis.Transport.connection_errors

    def test_transport_channel_errors(self):
        """Ensure connection_errors are populated."""
        assert redis.Transport.channel_errors

    def test_transport_driver_version(self):
        assert redis.Transport.driver_version(self.connection.transport)

    def test_transport_errors_when_InvalidData_used(self):
        from redis import exceptions

        from kombu.transport.redis import get_redis_error_classes

        class ID(Exception):
            pass

        DataError = getattr(exceptions, 'DataError', None)
        InvalidData = getattr(exceptions, 'InvalidData', None)
        exceptions.InvalidData = ID
        exceptions.DataError = None
        try:
            errors = get_redis_error_classes()
            assert errors
            assert ID in errors[1]
        finally:
            if DataError is not None:
                exceptions.DataError = DataError
            if InvalidData is not None:
                exceptions.InvalidData = InvalidData

    def test_empty_queues_key(self):
        channel = self.channel
        channel._in_poll = False
        key = channel.keyprefix_queue % 'celery'

        # Everything is fine, there is a list of queues.
        channel.client.sadd(key, 'celery\x06\x16\x06\x16celery')
        assert channel.get_table('celery') == [
            ('celery', '', 'celery'),
        ]

        # Remove one last queue from exchange. After this call no queue
        # is in bound to exchange.
        channel.client.srem(key)

        # get_table() should return empty list of queues
        assert self.channel.get_table('celery') == []

    def test_socket_connection(self):
        with patch('kombu.transport.redis.Channel._create_client'):
            with Connection('redis+socket:///tmp/redis.sock') as conn:
                connparams = conn.default_channel._connparams()
                assert issubclass(
                    connparams['connection_class'],
                    redis.redis.UnixDomainSocketConnection,
                )
                assert connparams['path'] == '/tmp/redis.sock'

    def test_ssl_argument__dict(self):
        with patch('kombu.transport.redis.Channel._create_client'):
            # Expected format for redis-py's SSLConnection class
            ssl_params = {
                'ssl_cert_reqs': 2,
                'ssl_ca_certs': '/foo/ca.pem',
                'ssl_certfile': '/foo/cert.crt',
                'ssl_keyfile': '/foo/pkey.key'
            }
            with Connection('redis://', ssl=ssl_params) as conn:
                params = conn.default_channel._connparams()
                assert params['ssl_cert_reqs'] == ssl_params['ssl_cert_reqs']
                assert params['ssl_ca_certs'] == ssl_params['ssl_ca_certs']
                assert params['ssl_certfile'] == ssl_params['ssl_certfile']
                assert params['ssl_keyfile'] == ssl_params['ssl_keyfile']
                assert params.get('ssl') is None

    def test_ssl_connection(self):
        with patch('kombu.transport.redis.Channel._create_client'):
            with Connection('redis://', ssl={'ssl_cert_reqs': 2}) as conn:
                connparams = conn.default_channel._connparams()
                assert issubclass(
                    connparams['connection_class'],
                    redis.redis.SSLConnection,
                )

    def test_rediss_connection(self):
        with patch('kombu.transport.redis.Channel._create_client'):
            with Connection('rediss://') as conn:
                connparams = conn.default_channel._connparams()
                assert issubclass(
                    connparams['connection_class'],
                    redis.redis.SSLConnection,
                )

    def test_sep_transport_option(self):
        with Connection(transport=Transport, transport_options={
            'sep': ':',
        }) as conn:
            key = conn.default_channel.keyprefix_queue % 'celery'
            conn.default_channel.client.sadd(key, 'celery::celery')

            assert conn.default_channel.sep == ':'
            assert conn.default_channel.get_table('celery') == [
                ('celery', '', 'celery'),
            ]

    @patch("redis.StrictRedis.execute_command")
    def test_global_keyprefix(self, mock_execute_command):
        from kombu.transport.redis import PrefixedStrictRedis

        with Connection(transport=Transport) as conn:
            client = PrefixedStrictRedis(global_keyprefix='foo_')

            channel = conn.channel()
            channel._create_client = Mock()
            channel._create_client.return_value = client

            body = {'hello': 'world'}
            channel._put_fanout('exchange', body, '')
            mock_execute_command.assert_called_with(
                'PUBLISH',
                'foo_/{db}.exchange',
                dumps(body)
            )

    @patch("redis.StrictRedis.execute_command")
    def test_global_keyprefix_queue_bind(self, mock_execute_command):
        from kombu.transport.redis import PrefixedStrictRedis

        with Connection(transport=Transport) as conn:
            client = PrefixedStrictRedis(global_keyprefix='foo_')

            channel = conn.channel()
            channel._create_client = Mock()
            channel._create_client.return_value = client

            channel._queue_bind('default', '', None, 'queue')
            mock_execute_command.assert_called_with(
                'SADD',
                'foo__kombu.binding.default',
                '\x06\x16\x06\x16queue'
            )

    @patch("redis.client.PubSub.execute_command")
    def test_global_keyprefix_pubsub(self, mock_execute_command):
        from kombu.transport.redis import PrefixedStrictRedis

        with Connection(transport=Transport) as conn:
            client = PrefixedStrictRedis(global_keyprefix='foo_')

            channel = conn.channel()
            channel.global_keyprefix = 'foo_'
            channel._create_client = Mock()
            channel._create_client.return_value = client
            channel.subclient.connection = Mock()
            channel.active_fanout_queues.add('a')

            channel._subscribe()
            mock_execute_command.assert_called_with(
                'PSUBSCRIBE',
                'foo_/{db}.a',
            )

    @patch("redis.client.Pipeline.execute_command")
    def test_global_keyprefix_transaction(self, mock_execute_command):
        from kombu.transport.redis import PrefixedStrictRedis

        with Connection(transport=Transport) as conn:
            def pipeline(transaction=True, shard_hint=None):
                pipeline_obj = original_pipeline(
                    transaction=transaction, shard_hint=shard_hint
                )
                mock_execute_command.side_effect = [
                    None, None, pipeline_obj, pipeline_obj
                ]
                return pipeline_obj

            client = PrefixedStrictRedis(global_keyprefix='foo_')
            original_pipeline = client.pipeline
            client.pipeline = pipeline

            channel = conn.channel()
            channel._create_client = Mock()
            channel._create_client.return_value = client

            channel.qos.restore_by_tag('test-tag')
            assert mock_execute_command is not None
            # https://github.com/redis/redis-py/pull/3038 (redis>=5.1.0a1)
            # adds keyword argument `keys` to redis client.
            # To be compatible with all supported redis versions,
            # take into account only `call.args`.
            call_args = [call.args for call in mock_execute_command.mock_calls]
            assert call_args == [
                ('WATCH', 'foo_unacked'),
                ('HGET', 'foo_unacked', 'test-tag'),
                ('ZREM', 'foo_unacked_index', 'test-tag'),
                ('HDEL', 'foo_unacked', 'test-tag')
            ]


class test_Redis:

    def setup_method(self):
        self.connection = Connection(transport=Transport)
        self.exchange = Exchange('test_Redis', type='direct')
        self.queue = Queue('test_Redis', self.exchange, 'test_Redis')

    def teardown_method(self):
        self.connection.close()

    @pytest.mark.replace_module_value(redis.redis, 'VERSION', [3, 0, 0])
    def test_publish__get_redispyv3(self, replace_module_value):
        channel = self.connection.channel()
        producer = Producer(channel, self.exchange, routing_key='test_Redis')
        self.queue(channel).declare()

        producer.publish({'hello': 'world'})

        assert self.queue(channel).get().payload == {'hello': 'world'}
        assert self.queue(channel).get() is None
        assert self.queue(channel).get() is None
        assert self.queue(channel).get() is None

    @pytest.mark.replace_module_value(redis.redis, 'VERSION', [2, 5, 10])
    def test_publish__get_redispyv2(self, replace_module_value):
        channel = self.connection.channel()
        producer = Producer(channel, self.exchange, routing_key='test_Redis')
        self.queue(channel).declare()

        producer.publish({'hello': 'world'})

        assert self.queue(channel).get().payload == {'hello': 'world'}
        assert self.queue(channel).get() is None
        assert self.queue(channel).get() is None
        assert self.queue(channel).get() is None

    def test_publish__consume(self):
        connection = Connection(transport=Transport)
        channel = connection.channel()
        producer = Producer(channel, self.exchange, routing_key='test_Redis')
        consumer = Consumer(channel, queues=[self.queue])

        producer.publish({'hello2': 'world2'})
        _received = []

        def callback(message_data, message):
            _received.append(message_data)
            message.ack()

        consumer.register_callback(callback)
        consumer.consume()

        assert channel in channel.connection.cycle._channels
        try:
            connection.drain_events(timeout=1)
            assert _received
            with pytest.raises(socket.timeout):
                connection.drain_events(timeout=0.01)
        finally:
            channel.close()

    def test_purge(self):
        channel = self.connection.channel()
        producer = Producer(channel, self.exchange, routing_key='test_Redis')
        self.queue(channel).declare()

        for i in range(10):
            producer.publish({'hello': f'world-{i}'})

        assert channel._size('test_Redis') == 10
        assert self.queue(channel).purge() == 10
        channel.close()

    def test_db_values(self):
        Connection(virtual_host=1,
                   transport=Transport).channel()

        Connection(virtual_host='1',
                   transport=Transport).channel()

        Connection(virtual_host='/1',
                   transport=Transport).channel()

        with pytest.raises(Exception):
            Connection('redis:///foo').channel()

    def test_db_port(self):
        c1 = Connection(port=None, transport=Transport).channel()
        c1.close()

        c2 = Connection(port=9999, transport=Transport).channel()
        c2.close()

    def test_close_poller_not_active(self):
        c = Connection(transport=Transport).channel()
        cycle = c.connection.cycle
        c.client.connection
        c.close()
        assert c not in cycle._channels

    def test_close_ResponseError(self):
        c = Connection(transport=Transport).channel()
        c.client.bgsave_raises_ResponseError = True
        c.close()

    def test_close_disconnects(self):
        c = Connection(transport=Transport).channel()
        conn1 = c.client.connection
        conn2 = c.subclient.connection
        c.close()
        assert conn1.disconnected
        assert conn2.disconnected

    def test_close_in_poll(self):
        c = Connection(transport=Transport).channel()
        conn1 = c.client.connection
        conn1._sock.data = [('BRPOP', ('test_Redis',))]
        c._in_poll = True
        c.close()
        assert conn1.disconnected
        assert conn1._sock.data == []

    def test_get__Empty(self):
        channel = self.connection.channel()
        with pytest.raises(Empty):
            channel._get('does-not-exist')
        channel.close()

    @pytest.mark.ensured_modules(*_redis_modules())
    def test_get_client(self, module_exists):
        # with module_exists(*_redis_modules()):
        conn = Connection(transport=Transport)
        chan = conn.channel()
        assert chan.Client
        assert chan.ResponseError
        assert conn.transport.connection_errors
        assert conn.transport.channel_errors

    def test_check_at_least_we_try_to_connect_and_fail(self):
        import redis
        connection = Connection('redis://localhost:65534/')

        with pytest.raises(redis.exceptions.ConnectionError):
            chan = connection.channel()
            chan._size('some_queue')


class test_MultiChannelPoller:

    def setup_method(self):
        self.Poller = redis.MultiChannelPoller

    def test_on_poll_start(self):
        p = self.Poller()
        p._channels = []
        p.on_poll_start()
        p._register_BRPOP = Mock(name='_register_BRPOP')
        p._register_LISTEN = Mock(name='_register_LISTEN')

        chan1 = Mock(name='chan1')
        p._channels = [chan1]
        chan1.active_queues = []
        chan1.active_fanout_queues = []
        p.on_poll_start()

        chan1.active_queues = ['q1']
        chan1.active_fanout_queues = ['q2']
        chan1.qos.can_consume.return_value = False

        p.on_poll_start()
        p._register_LISTEN.assert_called_with(chan1)
        p._register_BRPOP.assert_not_called()

        chan1.qos.can_consume.return_value = True
        p._register_LISTEN.reset_mock()
        p.on_poll_start()

        p._register_BRPOP.assert_called_with(chan1)
        p._register_LISTEN.assert_called_with(chan1)

    def test_on_poll_init(self):
        p = self.Poller()
        chan1 = Mock(name='chan1')
        p._channels = []
        poller = Mock(name='poller')
        p.on_poll_init(poller)
        assert p.poller is poller

        p._channels = [chan1]
        p.on_poll_init(poller)
        chan1.qos.restore_visible.assert_called_with(
            num=chan1.unacked_restore_limit,
        )

    def test_handle_event(self):
        p = self.Poller()
        chan = Mock(name='chan')
        p._fd_to_chan[13] = chan, 'BRPOP'
        chan.handlers = {'BRPOP': Mock(name='BRPOP')}

        chan.qos.can_consume.return_value = False
        p.handle_event(13, redis.READ)
        chan.handlers['BRPOP'].assert_not_called()

        chan.qos.can_consume.return_value = True
        p.handle_event(13, redis.READ)
        chan.handlers['BRPOP'].assert_called_with()

        p.handle_event(13, redis.ERR)
        chan._poll_error.assert_called_with('BRPOP')

        p.handle_event(13, ~(redis.READ | redis.ERR))

    def test_fds(self):
        p = self.Poller()
        p._fd_to_chan = {1: 2}
        assert p.fds == p._fd_to_chan

    def test_close_unregisters_fds(self):
        p = self.Poller()
        poller = p.poller = Mock()
        p._chan_to_sock.update({1: 1, 2: 2, 3: 3})

        p.close()

        assert poller.unregister.call_count == 3
        u_args = poller.unregister.call_args_list

        assert sorted(u_args) == [
            ((1,), {}),
            ((2,), {}),
            ((3,), {}),
        ]

    def test_close_when_unregister_raises_KeyError(self):
        p = self.Poller()
        p.poller = Mock()
        p._chan_to_sock.update({1: 1})
        p.poller.unregister.side_effect = KeyError(1)
        p.close()

    def test_close_resets_state(self):
        p = self.Poller()
        p.poller = Mock()
        p._channels = Mock()
        p._fd_to_chan = Mock()
        p._chan_to_sock = Mock()

        p._chan_to_sock.itervalues.return_value = []
        p._chan_to_sock.values.return_value = []  # py3k

        p.close()
        p._channels.clear.assert_called_with()
        p._fd_to_chan.clear.assert_called_with()
        p._chan_to_sock.clear.assert_called_with()

    def test_register_when_registered_reregisters(self):
        p = self.Poller()
        p.poller = Mock()
        channel, client, type = Mock(), Mock(), Mock()
        sock = client.connection._sock = Mock()
        sock.fileno.return_value = 10

        p._chan_to_sock = {(channel, client, type): 6}
        p._register(channel, client, type)
        p.poller.unregister.assert_called_with(6)
        assert p._fd_to_chan[10] == (channel, type)
        assert p._chan_to_sock[(channel, client, type)] == sock
        p.poller.register.assert_called_with(sock, p.eventflags)

        # when client not connected yet
        client.connection._sock = None

        def after_connected():
            client.connection._sock = Mock()
        client.connection.connect.side_effect = after_connected

        p._register(channel, client, type)
        client.connection.connect.assert_called_with()

    def test_register_BRPOP(self):
        p = self.Poller()
        channel = Mock()
        channel.client.connection._sock = None
        p._register = Mock()

        channel._in_poll = False
        p._register_BRPOP(channel)
        assert channel._brpop_start.call_count == 1
        assert p._register.call_count == 1

        channel.client.connection._sock = Mock()
        p._chan_to_sock[(channel, channel.client, 'BRPOP')] = True
        channel._in_poll = True
        p._register_BRPOP(channel)
        assert channel._brpop_start.call_count == 1
        assert p._register.call_count == 1

    def test_register_LISTEN(self):
        p = self.Poller()
        channel = Mock()
        channel.subclient.connection._sock = None
        channel._in_listen = False
        p._register = Mock()

        p._register_LISTEN(channel)
        p._register.assert_called_with(channel, channel.subclient, 'LISTEN')
        assert p._register.call_count == 1
        assert channel._subscribe.call_count == 1

        channel._in_listen = True
        p._chan_to_sock[(channel, channel.subclient, 'LISTEN')] = 3
        channel.subclient.connection._sock = Mock()
        p._register_LISTEN(channel)
        assert p._register.call_count == 1
        assert channel._subscribe.call_count == 1

    def create_get(self, events=None, queues=None, fanouts=None):
        _pr = [] if events is None else events
        _aq = [] if queues is None else queues
        _af = [] if fanouts is None else fanouts
        p = self.Poller()
        p.poller = Mock()
        p.poller.poll.return_value = _pr

        p._register_BRPOP = Mock()
        p._register_LISTEN = Mock()

        channel = Mock()
        p._channels = [channel]
        channel.active_queues = _aq
        channel.active_fanout_queues = _af

        return p, channel

    def test_get_no_actions(self):
        p, channel = self.create_get()

        with pytest.raises(redis.Empty):
            p.get(Mock())

    def test_qos_reject(self):
        p, channel = self.create_get()
        qos = redis.QoS(channel)
        qos._remove_from_indices = Mock(name='_remove_from_indices')
        qos.reject(1234)
        qos._remove_from_indices.assert_called_with(1234)

    def test_qos_requeue(self):
        p, channel = self.create_get()
        qos = redis.QoS(channel)
        qos.restore_by_tag = Mock(name='restore_by_tag')
        qos.reject(1234, True)
        qos.restore_by_tag.assert_called_with(1234, leftmost=True)

    def test_get_brpop_qos_allow(self):
        p, channel = self.create_get(queues=['a_queue'])
        channel.qos.can_consume.return_value = True

        with pytest.raises(redis.Empty):
            p.get(Mock())

        p._register_BRPOP.assert_called_with(channel)

    def test_get_brpop_qos_disallow(self):
        p, channel = self.create_get(queues=['a_queue'])
        channel.qos.can_consume.return_value = False

        with pytest.raises(redis.Empty):
            p.get(Mock())

        p._register_BRPOP.assert_not_called()

    def test_get_listen(self):
        p, channel = self.create_get(fanouts=['f_queue'])

        with pytest.raises(redis.Empty):
            p.get(Mock())

        p._register_LISTEN.assert_called_with(channel)

    def test_get_receives_ERR(self):
        p, channel = self.create_get(events=[(1, eventio.ERR)])
        p._fd_to_chan[1] = (channel, 'BRPOP')

        with pytest.raises(redis.Empty):
            p.get(Mock())

        channel._poll_error.assert_called_with('BRPOP')

    def test_get_receives_multiple(self):
        p, channel = self.create_get(events=[(1, eventio.ERR),
                                             (1, eventio.ERR)])
        p._fd_to_chan[1] = (channel, 'BRPOP')

        with pytest.raises(redis.Empty):
            p.get(Mock())

        channel._poll_error.assert_called_with('BRPOP')


class test_Mutex:

    def test_mutex(self, lock_id='xxx'):
        client = Mock(name='client')
        lock = client.lock.return_value = Mock(name='lock')

        # Won
        lock.acquire.return_value = True
        held = False
        with redis.Mutex(client, 'foo1', 100):
            held = True
        assert held
        lock.acquire.assert_called_with(blocking=False)
        client.lock.assert_called_with('foo1', timeout=100)

        client.reset_mock()
        lock.reset_mock()

        # Did not win
        lock.acquire.return_value = False
        held = False
        with pytest.raises(redis.MutexHeld):
            with redis.Mutex(client, 'foo1', 100):
                held = True
            assert not held
        lock.acquire.assert_called_with(blocking=False)
        client.lock.assert_called_with('foo1', timeout=100)

        client.reset_mock()
        lock.reset_mock()

        # Wins but raises LockNotOwnedError (and that is ignored)
        lock.acquire.return_value = True
        lock.release.side_effect = redis.redis.exceptions.LockNotOwnedError()
        held = False
        with redis.Mutex(client, 'foo1', 100):
            held = True
        assert held


class test_RedisSentinel:

    def test_method_called(self):
        from kombu.transport.redis import SentinelChannel

        with patch.object(SentinelChannel, '_sentinel_managed_pool') as p:
            connection = Connection(
                'sentinel://localhost:65534/',
                transport_options={
                    'master_name': 'not_important',
                },
            )

            connection.channel()
            p.assert_called()

    def test_keyprefix_fanout(self):
        from kombu.transport.redis import SentinelChannel
        with patch.object(SentinelChannel, '_sentinel_managed_pool'):
            connection = Connection(
                'sentinel://localhost:65532/1',
                transport_options={
                    'master_name': 'not_important',
                },
            )
            channel = connection.channel()
            assert channel.keyprefix_fanout == '/1.'

    def test_getting_master_from_sentinel(self):
        with patch('redis.sentinel.Sentinel') as patched:
            connection = Connection(
                'sentinel://localhost/;'
                'sentinel://localhost:65532/;'
                'sentinel://user@localhost:65533/;'
                'sentinel://:password@localhost:65534/;'
                'sentinel://user:password@localhost:65535/;',
                transport_options={
                    'master_name': 'not_important',
                },
            )

            connection.channel()
            patched.assert_called_once_with(
                [
                    ('localhost', 26379),
                    ('localhost', 65532),
                    ('localhost', 65533),
                    ('localhost', 65534),
                    ('localhost', 65535),
                ],
                connection_class=ANY, db=0, max_connections=10,
                min_other_sentinels=0, password=None, sentinel_kwargs=None,
                socket_connect_timeout=None, socket_keepalive=None,
                socket_keepalive_options=None, socket_timeout=None,
                username=None, retry_on_timeout=None)

            master_for = patched.return_value.master_for
            master_for.assert_called()
            master_for.assert_called_with('not_important', ANY)
            master_for().connection_pool.get_connection.assert_called()

    def test_getting_master_from_sentinel_single_node(self):
        with patch('redis.sentinel.Sentinel') as patched:
            connection = Connection(
                'sentinel://localhost:65532/',
                transport_options={
                    'master_name': 'not_important',
                },
            )

            connection.channel()
            patched.assert_called_once_with(
                [('localhost', 65532)],
                connection_class=ANY, db=0, max_connections=10,
                min_other_sentinels=0, password=None, sentinel_kwargs=None,
                socket_connect_timeout=None, socket_keepalive=None,
                socket_keepalive_options=None, socket_timeout=None,
                username=None, retry_on_timeout=None)

            master_for = patched.return_value.master_for
            master_for.assert_called()
            master_for.assert_called_with('not_important', ANY)
            master_for().connection_pool.get_connection.assert_called()

    def test_can_create_connection(self):
        from redis.exceptions import ConnectionError

        connection = Connection(
            'sentinel://localhost:65534/',
            transport_options={
                'master_name': 'not_important',
            },
        )
        with pytest.raises(ConnectionError):
            connection.channel()

    def test_missing_master_name_transport_option(self):
        connection = Connection(
            'sentinel://localhost:65534/',
        )
        with patch('redis.sentinel.Sentinel'),  \
             pytest.raises(ValueError) as excinfo:
            connection.connect()
        expected = "'master_name' transport option must be specified."
        assert expected == excinfo.value.args[0]

    def test_sentinel_with_ssl(self):
        ssl_params = {
            'ssl_cert_reqs': 2,
            'ssl_ca_certs': '/foo/ca.pem',
            'ssl_certfile': '/foo/cert.crt',
            'ssl_keyfile': '/foo/pkey.key'
        }
        with patch('redis.sentinel.Sentinel'):
            with Connection(
                    'sentinel://',
                    transport_options={'master_name': 'not_important'},
                    ssl=ssl_params) as conn:
                params = conn.default_channel._connparams()
                assert params['ssl_cert_reqs'] == ssl_params['ssl_cert_reqs']
                assert params['ssl_ca_certs'] == ssl_params['ssl_ca_certs']
                assert params['ssl_certfile'] == ssl_params['ssl_certfile']
                assert params['ssl_keyfile'] == ssl_params['ssl_keyfile']
                assert params.get('ssl') is None
                from kombu.transport.redis import SentinelManagedSSLConnection
                assert (params['connection_class'] is
                        SentinelManagedSSLConnection)

    def test_can_create_connection_with_global_keyprefix(self):
        from redis.exceptions import ConnectionError

        try:
            connection = Connection(
                'sentinel://localhost:65534/',
                transport_options={
                    'global_keyprefix': 'some_prefix',
                    'master_name': 'not_important',
                },
            )
            with pytest.raises(ConnectionError):
                connection.channel()
        finally:
            connection.close()

    def test_can_create_correct_mixin_with_global_keyprefix(self):
        from kombu.transport.redis import GlobalKeyPrefixMixin

        with patch('redis.sentinel.Sentinel'):
            connection = Connection(
                'sentinel://localhost:65534/',
                transport_options={
                    'global_keyprefix': 'some_prefix',
                    'master_name': 'not_important',
                },
            )

            assert isinstance(
                connection.channel().client,
                GlobalKeyPrefixMixin
            )
            assert (
                connection.channel().client.global_keyprefix
                == 'some_prefix'
            )
            connection.close()


class test_GlobalKeyPrefixMixin:

    from kombu.transport.redis import GlobalKeyPrefixMixin

    global_keyprefix = "prefix_"
    mixin = GlobalKeyPrefixMixin()
    mixin.global_keyprefix = global_keyprefix

    def test_prefix_simple_args(self):
        for command in self.mixin.PREFIXED_SIMPLE_COMMANDS:
            prefixed_args = self.mixin._prefix_args([command, "fake_key"])
            assert prefixed_args == [
                command,
                f"{self.global_keyprefix}fake_key"
            ]

    def test_prefix_delete_args(self):
        prefixed_args = self.mixin._prefix_args([
            "DEL",
            "fake_key",
            "fake_key2",
            "fake_key3"
        ])

        assert prefixed_args == [
            "DEL",
            f"{self.global_keyprefix}fake_key",
            f"{self.global_keyprefix}fake_key2",
            f"{self.global_keyprefix}fake_key3",
        ]

    def test_prefix_brpop_args(self):
        prefixed_args = self.mixin._prefix_args([
            "BRPOP",
            "fake_key",
            "fake_key2",
            "not_prefixed"
        ])

        assert prefixed_args == [
            "BRPOP",
            f"{self.global_keyprefix}fake_key",
            f"{self.global_keyprefix}fake_key2",
            "not_prefixed",
        ]

    def test_prefix_evalsha_args(self):
        prefixed_args = self.mixin._prefix_args([
            "EVALSHA",
            "not_prefixed",
            "not_prefixed",
            "fake_key",
            "not_prefixed",
        ])

        assert prefixed_args == [
            "EVALSHA",
            "not_prefixed",
            "not_prefixed",
            f"{self.global_keyprefix}fake_key",
            "not_prefixed",
        ]
