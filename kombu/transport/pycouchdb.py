"""
kombu.transport.pycouchdb
=========================

CouchDB transport.

:copyright: (c) 2010 by David Clymer.
:license: BSD, see LICENSE for more details.

"""
from Queue import Empty

import couchdb

from anyjson import serialize, deserialize

from kombu.transport import virtual
from kombu.utils import uuid4

DEFAULT_PORT = 5984
DEFAULT_DATABASE = "kombu_default"

__author__ = "David Clymer <david@zettazebra.com>"


def create_message_view(db):
    from couchdb import design

    view = design.ViewDefinition("kombu", "messages", """
        function (doc) {
          if (doc.queue && doc.payload)
            emit(doc.queue, doc);
        }
        """)
    if not view.get_doc(db):
        view.sync(db)


class Channel(virtual.Channel):
    _client = None

    view_created = False

    def _put(self, queue, message, **kwargs):
        self.client.save({'_id': uuid4().hex,
                          'queue': queue,
                          'payload': message})

    def _get(self, queue):
        result = self._query(queue, limit=1)
        if not result:
            raise Empty()

        item = result.rows[0].value
        self.client.delete(item)
        return deserialize(item["payload"])

    def _purge(self, queue):
        result = self._query(queue)
        for item in result:
            self.client.delete(item.value)
        return len(result)

    def _size(self, queue):
        return len(self._query(queue))

    def _open(self):
        conninfo = self.connection.client
        dbname = conninfo.virtual_host
        proto = conninfo.ssl and "https" or "http"
        if not dbname or dbname == "/":
            dbname = DEFAULT_DATABASE
        server = couchdb.Server('%s://%s:%s/' % (proto,
                                                 conninfo.hostname,
                                                 conninfo.port))
        try:
            return server.create(dbname)
        except couchdb.PreconditionFailed:
            return server[dbname]

    def _query(self, queue, **kwargs):
        if not self.view_created:
            # if the message view is not yet set up, we'll need it now.
            create_message_view(self.client)
            self.view_created = True
        return self.client.view("kombu/messages", key=queue, **kwargs)

    @property
    def client(self):
        if self._client is None:
            self._client = self._open()
        return self._client


class Transport(virtual.Transport):
    Channel = Channel

    interval = 1
    default_port = DEFAULT_PORT
    connection_errors = (couchdb.HTTPError,
                         couchdb.ServerError,
                         couchdb.Unauthorized)
    channel_errors = (couchdb.HTTPError,
                      couchdb.ServerError,
                      couchdb.PreconditionFailed,
                      couchdb.ResourceConflict,
                      couchdb.ResourceNotFound)
