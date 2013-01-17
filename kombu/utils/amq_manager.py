from __future__ import absolute_import


def get_manager(client, hostname=None, port=None, userid=None,
                password=None):
    import pyrabbit
    opt = client.transport_options.get

    def get(name, val, default):
        return (val if val is not None
                else opt.get('manager_%s' % name)
                or getattr(client, name, None) or default)

    host = get('hostname', hostname, 'localhost')
    port = port if port is not None else opt('manager_port', 55672)
    userid = get('userid', userid, 'guest')
    password = get('password', password, 'guest')
    return pyrabbit.Client('%s:%s' % (host, port), userid, password)
