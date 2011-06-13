
"""
kombu.transport.SQS
===================

Amazon SQS transport.

:copyright: (c) 2010 - 2011 by Ask Solem
:license: BSD, see LICENSE for more details.

"""
import socket
import string

from Queue import Empty

from anyjson import serialize, deserialize

import boto
from boto import exception
from boto import sdb as _sdb
from boto import sqs as _sqs
from boto.sdb.domain import Domain
from boto.sdb.connection import SDBConnection
from boto.sqs.connection import SQSConnection
from boto.sqs.message import Message

from kombu.transport import virtual
from kombu.utils import cached_property, gen_unique_id


# dots are replaced by dash, all other punctuation
# replaced by underscore.
CHARS_REPLACE = string.punctuation.replace('-', '') \
                                  .replace('_', '') \
                                  .replace('.', '')
CHARS_REPLACE_TABLE = string.maketrans(CHARS_REPLACE + '.',
                                       "_" * len(CHARS_REPLACE) + '-')


class Table(Domain):
    """Amazon SimpleDB domain describing the message routing table."""

    def routes_for(self, exchange):
        """Iterator giving all routes for an exchange."""
        return self.select("""exchange = '%s'""" % exchange)

    def get_queue(self, queue):
        """Get binding for queue."""
        qid = self._get_queue_id(queue)
        if qid:
            return self.get_item(qid)

    def create_binding(self, queue):
        """Get binding item for queue.

        Creates the item if it doesn't exist.

        """
        item = self.get_queue(queue)
        if item:
            return item, item["id"]
        id = gen_unique_id()
        return self.new_item(id), id

    def queue_delete(self, queue):
        """delete queue by name."""
        qid = self._get_queue_id(queue)
        if qid:
            self.delete_item(qid)

    def exchange_delete(self, exchange):
        """Delete all routes for `exchange`."""
        for item in self.routes_for(exchange):
            domain.delete_item(item["id"])

    def get_item(self, item_name, consistent_read=True):
        """Uses `consistent_read` by default."""
        # Domain is an old-style class, can't use super().
        return Domain.get_item(self, item_name, consistent_read)

    def select(self, query='', next_token=None, consistent_read=True,
            max_items=None):
        """Uses `consistent_read` by default."""
        query = """SELECT * FROM `%s` WHERE %s""" % (self.name, query)
        return Domain.select(self, query, next_token,
                                   consistent_read, max_items)

    def _get_queue_id(self, queue):
        for item in self.select("""queue = '%s' limit 1""" % queue,
                                max_items=1):
            return item["id"]



class Channel(virtual.Channel):
    Table = Table

    default_region = "us-east-1"
    domain_format = "kombu%(vhost)s"
    _sdb = None
    _sqs = None

    def entity_name(self, name, table=CHARS_REPLACE_TABLE):
        """Format AMQP queue name into a legal SQS queue name."""
        return name.encode(errors="replace").translate(table)

    def _new_queue(self, queue, **kwargs):
        """Ensures a queue exists in SQS."""
        return self.sqs.create_queue(self.entity_name(queue),
                                     self.visibility_timeout)

    def _queue_bind(self, exchange, routing_key, pattern, queue):
        """Bind ``queue`` to ``exchange`` with routing key.

        Route will be stored in SDB if so enabled.

        """
        if not self.supports_fanout:
            return

        binding, id = self.table.create_binding(queue)
        binding.update(exchange=exchange,
                       routing_key=routing_key or "",
                       pattern=pattern or "",
                       queue=queue or "",
                       id=id)
        binding.save()

    def get_table(self, exchange):
        """Get routing table.

        Retrieved from SDB if :attr:`supports_fanout`.

        """
        if self.supports_fanout:
            return [(r["routing_key"], r["pattern"], r["queue"])
                        for r in self.table.routes_for(exchange)]
        return super(Channel, self).get_table(exchange)

    def _delete(self, queue):
        """delete queue by name."""
        self.table.queue_delete(queue)
        super(Channel, self)._delete(queue)

    def exchange_delete(self, exchange, **kwargs):
        """Delete exchange by name."""
        if self.supports_fanout:
            self.table.exchange_delete(exchange)
        super(Channel, self).exchange_delete(exchange, **kwargs)

    def _has_queue(self, queue, **kwargs):
        """Returns True if ``queue`` has been previously declared."""
        if self.supports_fanout:
            return bool(self.table.get_queue(queue))
        return super(Channel, self)._has_queue(queue)

    def _put(self, queue, message, **kwargs):
        """Put message onto queue."""
        q = self._new_queue(queue)
        m = Message()
        m.set_body(serialize(message))
        q.write(m)

    def _put_fanout(self, exchange, message, **kwargs):
        """Deliver fanout message to all queues in ``exchange``."""
        for route in self.table.routes_for(exchange):
            self._put(route["queue"], message, **kwargs)

    def _get(self, queue):
        """Try to retrieve a single message off ``queue``."""
        q = self._new_queue(queue)
        rs = q.get_messages(1)
        if rs:
            m = rs[0]
            payload = deserialize(rs[0].get_body())
            payload["properties"]["delivery_info"].update({
                "sqs_message": m, "sqs_queue": q})
            return payload
        raise Empty()

    def basic_ack(self, delivery_tag):
        qos = self.qos
        delivery_info = self.qos.get(delivery_tag).delivery_info
        delivery_info["sqs_queue"].delete_message(delivery_info["sqs_message"])
        super(Channel, self).basic_ack(delivery_tag)

    def _size(self, queue):
        """Returns the number of messages in a queue."""
        return self._new_queue(queue).count()

    def _purge(self, queue):
        """Deletes all current messages in a queue."""
        q = self._new_queue(queue)
        # SQS is slow at registering messages, so run for a few
        # iterations to ensure messages are deleted.
        size = 0
        for i in xrange(10):
            size += q.count()
            if not size:
                break
            q.clear()
        return size

    def close(self):
        super(Channel, self).close()
        for conn in (self._sqs, self._sdb):
            if conn:
                try:
                    conn.close()
                except AttributeError, exc:  # FIXME ???
                    if "can't set attribute" not in str(exc):
                        raise

    def _get_regioninfo(self, regions):
        if self.region:
            for _r in regions:
                if _r.name == self.region:
                    return _r

    def _aws_connect_to(self, fun, regions):
        conninfo = self.conninfo
        region = self._get_regioninfo(regions)
        return fun(region=region,
                   aws_access_key_id=conninfo.userid,
                   aws_secret_access_key=conninfo.password,
                   port=conninfo.port)

    @property
    def sqs(self):
        if self._sqs is None:
            self._sqs = self._aws_connect_to(SQSConnection, _sqs.regions())
        return self._sqs

    @property
    def sdb(self):
        if self._sdb is None:
            self._sdb = self._aws_connect_to(SDBConnection, _sdb.regions())
        return self._sdb

    @property
    def table(self):
        name = self.entity_name(self.domain_format % {
                                    "vhost": self.conninfo.virtual_host})
        d = self.sdb.get_object("CreateDomain", {"DomainName": name},
                                self.Table)
        d.name = name
        return d

    @property
    def conninfo(self):
        return self.connection.client

    @property
    def transport_options(self):
        return self.connection.client.transport_options

    @cached_property
    def visibility_timeout(self):
        return self.transport_options.get("visibility_timeout")

    @cached_property
    def supports_fanout(self):
        return self.transport_options.get("sdb_persistence", True)

    @cached_property
    def region(self):
        return self.transport_options.get("region") or self.default_region


class Transport(virtual.Transport):
    Channel = Channel

    interval = 1
    default_port = None
    connection_errors = (exception.SQSError, socket.error)
    channel_errors = (exception.SQSDecodeError, )
