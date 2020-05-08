from __future__ import absolute_import, unicode_literals

import socket
from contextlib import closing
from time import sleep

import pytest
import kombu


class BasicFunctionality(object):

    def test_connect(self, connection):
        connection.connect()
        connection.close()

    def test_publish_consume(self, connection):
        test_queue = kombu.Queue('test', routing_key='test')

        def callback(body, message):
            assert body == {'hello': 'world'}
            assert message.content_type == 'application/x-python-serialize'
            message.delivery_info['routing_key'] == 'test'
            message.delivery_info['exchange'] == ''
            message.ack()
            assert message.payload == body

        with connection as conn:
            with conn.channel() as channel:
                producer = kombu.Producer(channel)
                producer.publish(
                    {'hello': 'world'},
                    retry=True,
                    exchange=test_queue.exchange,
                    routing_key=test_queue.routing_key,
                    declare=[test_queue],
                    serializer='pickle'
                )

                consumer = kombu.Consumer(
                    conn, [test_queue], accept=['pickle']
                )
                consumer.register_callback(callback)
                with consumer:
                    conn.drain_events(timeout=1)

    def test_simple_queue_publish_consume(self, connection):
        with connection as conn:
            with closing(conn.SimpleQueue('simple_queue_test')) as queue:
                queue.put({'Hello': 'World'}, headers={'k1': 'v1'})
                message = queue.get(timeout=1)
                assert message.payload == {'Hello': 'World'}
                assert message.content_type == 'application/json'
                assert message.content_encoding == 'utf-8'
                assert message.headers == {'k1': 'v1'}
                message.ack()

    def test_simple_buffer_publish_consume(self, connection):
        with connection as conn:
            with closing(conn.SimpleBuffer('simple_buffer_test')) as buf:
                buf.put({'Hello': 'World'}, headers={'k1': 'v1'})
                message = buf.get(timeout=1)
                assert message.payload == {'Hello': 'World'}
                assert message.content_type == 'application/json'
                assert message.content_encoding == 'utf-8'
                assert message.headers == {'k1': 'v1'}
                message.ack()


class BaseExchangeTypes(object):

    def _callback(self, body, message):
        message.ack()
        assert body == {'hello': 'world'}
        assert message.content_type == 'application/x-python-serialize'
        message.delivery_info['routing_key'] == 'test'
        message.delivery_info['exchange'] == ''
        assert message.payload == body

    def _consume(self, connection, queue):
        consumer = kombu.Consumer(
            connection, [queue], accept=['pickle']
        )
        consumer.register_callback(self._callback)
        with consumer:
            connection.drain_events(timeout=1)

    def _publish(self, channel, exchange, queues, routing_key=None):
        producer = kombu.Producer(channel, exchange=exchange)
        if routing_key:
            producer.publish(
                {'hello': 'world'},
                declare=list(queues),
                serializer='pickle',
                routing_key=routing_key
            )
        else:
            producer.publish(
                {'hello': 'world'},
                declare=list(queues),
                serializer='pickle'
            )

    def test_direct(self, connection):
        ex = kombu.Exchange('test_direct', type='direct')
        test_queue = kombu.Queue('direct1', exchange=ex)

        with connection as conn:
            with conn.channel() as channel:
                self._publish(channel, ex, [test_queue])
                self._consume(conn, test_queue)

    def test_direct_routing_keys(self, connection):
        ex = kombu.Exchange('test_rk_direct', type='direct')
        test_queue1 = kombu.Queue('rk_direct1', exchange=ex, routing_key='d1')
        test_queue2 = kombu.Queue('rk_direct2', exchange=ex, routing_key='d2')

        with connection as conn:
            with conn.channel() as channel:
                self._publish(channel, ex, [test_queue1, test_queue2], 'd1')
                self._consume(conn, test_queue1)
                # direct2 queue should not have data
                with pytest.raises(socket.timeout):
                    self._consume(conn, test_queue2)

    def test_fanout(self, connection):
        ex = kombu.Exchange('test_fanout', type='fanout')
        test_queue1 = kombu.Queue('fanout1', exchange=ex)
        test_queue2 = kombu.Queue('fanout2', exchange=ex)

        with connection as conn:
            with conn.channel() as channel:
                self._publish(channel, ex, [test_queue1, test_queue2])

                self._consume(conn, test_queue1)
                self._consume(conn, test_queue2)

    def test_topic(self, connection):
        ex = kombu.Exchange('test_topic', type='topic')
        test_queue1 = kombu.Queue('topic1', exchange=ex, routing_key='t.*')
        test_queue2 = kombu.Queue('topic2', exchange=ex, routing_key='t.*')
        test_queue3 = kombu.Queue('topic3', exchange=ex, routing_key='t')

        with connection as conn:
            with conn.channel() as channel:
                self._publish(
                    channel, ex, [test_queue1, test_queue2, test_queue3],
                    routing_key='t.1'
                )

                self._consume(conn, test_queue1)
                self._consume(conn, test_queue2)
                with pytest.raises(socket.timeout):
                    # topic3 queue should not have data
                    self._consume(conn, test_queue3)


class BaseTimeToLive(object):
    def test_publish_consume(self, connection):
        test_queue = kombu.Queue('ttl_test', routing_key='ttl_test')

        def callback(body, message):
            message.ack()

        with connection as conn:
            with conn.channel() as channel:
                producer = kombu.Producer(channel)
                producer.publish(
                    {'hello': 'world'},
                    retry=True,
                    exchange=test_queue.exchange,
                    routing_key=test_queue.routing_key,
                    declare=[test_queue],
                    serializer='pickle',
                    expiration=2
                )

                consumer = kombu.Consumer(
                    conn, [test_queue], accept=['pickle']
                )
                consumer.register_callback(callback)
                sleep(3)
                with consumer:
                    with pytest.raises(socket.timeout):
                        conn.drain_events(timeout=1)

    def test_simple_queue_publish_consume(self, connection):
        with connection as conn:
            with closing(conn.SimpleQueue('ttl_simple_queue_test')) as queue:
                queue.put(
                    {'Hello': 'World'}, headers={'k1': 'v1'}, expiration=2
                )
                sleep(3)
                with pytest.raises(queue.Empty):
                    queue.get(timeout=1)

    def test_simple_buffer_publish_consume(self, connection):
        with connection as conn:
            with closing(conn.SimpleBuffer('ttl_simple_buffer_test')) as buf:
                buf.put({'Hello': 'World'}, headers={'k1': 'v1'}, expiration=2)
                sleep(3)
                with pytest.raises(buf.Empty):
                    buf.get(timeout=1)


class BasePriority(object):

    PRIORITY_ORDER = 'asc'

    def test_publish_consume(self, connection):

        # py-amqp transport has higher numbers higher priority
        # redis transport has lower numbers higher priority
        if self.PRIORITY_ORDER == 'asc':
            prio_high = 6
            prio_low = 3
        else:
            prio_high = 3
            prio_low = 6

        test_queue = kombu.Queue(
            'priority_test', routing_key='priority_test', max_priority=10
        )

        received_messages = []

        def callback(body, message):
            received_messages.append(body)
            message.ack()

        with connection as conn:
            with conn.channel() as channel:
                producer = kombu.Producer(channel)
                for msg, prio in [
                    [{'msg': 'first'}, prio_low],
                    [{'msg': 'second'}, prio_high]
                ]:
                    producer.publish(
                        msg,
                        retry=True,
                        exchange=test_queue.exchange,
                        routing_key=test_queue.routing_key,
                        declare=[test_queue],
                        serializer='pickle',
                        priority=prio
                    )
                # Sleep to make sure that queue sorted based on priority
                sleep(0.5)
                consumer = kombu.Consumer(
                    conn, [test_queue], accept=['pickle']
                )
                consumer.register_callback(callback)
                with consumer:
                    conn.drain_events(timeout=1)
                # Second message must be received first
                assert received_messages[0] == {'msg': 'second'}
                assert received_messages[1] == {'msg': 'first'}

    def test_simple_queue_publish_consume(self, connection):
        if self.PRIORITY_ORDER == 'asc':
            prio_high = 7
            prio_low = 1
        else:
            prio_high = 1
            prio_low = 7
        with connection as conn:
            with closing(
                conn.SimpleQueue(
                    'priority_simple_queue_test',
                    queue_opts={'max_priority': 10}
                )
            ) as queue:
                queue.put(
                    {'msg': 'first'}, headers={'k1': 'v1'}, priority=prio_low
                )
                queue.put(
                    {'msg': 'second'}, headers={'k1': 'v1'}, priority=prio_high
                )
                # Sleep to make sure that queue sorted based on priority
                sleep(0.5)
                # Second message must be received first
                msg = queue.get(timeout=1)
                msg.ack()
                assert msg.payload == {'msg': 'second'}
                msg = queue.get(timeout=1)
                msg.ack()
                assert msg.payload == {'msg': 'first'}

    def test_simple_buffer_publish_consume(self, connection):
        if self.PRIORITY_ORDER == 'asc':
            prio_high = 6
            prio_low = 2
        else:
            prio_high = 2
            prio_low = 6
        with connection as conn:
            with closing(
                conn.SimpleBuffer(
                    'priority_simple_buffer_test',
                    queue_opts={'max_priority': 10}
                )
            ) as buf:
                buf.put(
                    {'msg': 'first'}, headers={'k1': 'v1'}, priority=prio_low
                )
                buf.put(
                    {'msg': 'second'}, headers={'k1': 'v1'}, priority=prio_high
                )
                # Sleep to make sure that queue sorted based on priority
                sleep(0.5)
                # Second message must be received first
                msg = buf.get(timeout=1)
                msg.ack()
                assert msg.payload == {'msg': 'second'}
                msg = buf.get(timeout=1)
                msg.ack()
                assert msg.payload == {'msg': 'first'}
