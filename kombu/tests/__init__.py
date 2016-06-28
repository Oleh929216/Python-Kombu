from __future__ import absolute_import, unicode_literals

import atexit
import os
import sys

from kombu.exceptions import VersionMismatch


def teardown():
    # Workaround for multiprocessing bug where logging
    # is attempted after global already collected at shutdown.
    canceled = set()
    try:
        import multiprocessing.util
        canceled.add(multiprocessing.util._exit_function)
    except (AttributeError, ImportError):
        pass

    try:
        atexit._exithandlers[:] = [
            e for e in atexit._exithandlers if e[0] not in canceled
        ]
    except AttributeError:  # pragma: no cover
        pass  # Py3 missing _exithandlers


def find_distribution_modules(name=__name__, file=__file__):
    current_dist_depth = len(name.split('.')) - 1
    current_dist = os.path.join(os.path.dirname(file),
                                *([os.pardir] * current_dist_depth))
    abs = os.path.abspath(current_dist)
    dist_name = os.path.basename(abs)

    for dirpath, dirnames, filenames in os.walk(abs):
        package = (dist_name + dirpath[len(abs):]).replace('/', '.')
        if '__init__.py' in filenames:
            yield package
            for filename in filenames:
                if filename.endswith('.py') and filename != '__init__.py':
                    yield '.'.join([package, filename])[:-3]


def import_all_modules(name=__name__, file=__file__, skip=[]):
    for module in find_distribution_modules(name, file):
        if module not in skip:
            print('preimporting %r for coverage...' % (module,))
            try:
                __import__(module)
            except (ImportError, VersionMismatch, AttributeError):
                pass


def is_in_coverage():
    return (os.environ.get('COVER_ALL_MODULES') or
            '--with-coverage3' in sys.argv)


def setup():
    # so coverage sees all our modules.
    if is_in_coverage():
        import_all_modules()
