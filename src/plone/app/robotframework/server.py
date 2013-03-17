# -*- coding: utf-8 -*-
import argparse
import logging
import select
import os
import sys
import time
import xmlrpclib
from SimpleXMLRPCServer import SimpleXMLRPCServer

import pkg_resources


try:
    pkg_resources.get_distribution('watchdog')
except pkg_resources.DistributionNotFound:
    HAS_RELOAD = False
else:
    from plone.app.robotframework.reload import ForkLoop
    from plone.app.robotframework.reload import Watcher
    HAS_RELOAD = True


HAS_VERBOSE_CONSOLE = False

LISTENER_PORT = int(os.getenv("LISTENER_PORT", 10001))

TIME = lambda: time.strftime('%H:%M:%S')
WAIT = lambda msg:  '{0} [\033[33m wait \033[0m] {1}'.format(TIME(), msg)
ERROR = lambda msg: '{0} [\033[31m ERROR \033[0m] {1}'.format(TIME(), msg)
READY = lambda msg: '{0} [\033[32m ready \033[0m] {1}'.format(TIME(), msg)


def start(zope_layer_dotted_name):

    print WAIT("Starting Zope 2 server")

    zsl = Zope2ServerLibrary()
    zsl.start_zope_server(zope_layer_dotted_name)

    print READY("Started Zope 2 server")

    listener = SimpleXMLRPCServer(('localhost', LISTENER_PORT),
                                  logRequests=False)
    listener.allow_none = True
    listener.register_function(zsl.zodb_setup, 'zodb_setup')
    listener.register_function(zsl.zodb_teardown, 'zodb_teardown')

    try:
        listener.serve_forever()
    finally:
        print
        print WAIT("Stopping Zope 2 server")

        zsl.stop_zope_server()

        print READY("Zope 2 server stopped")


def start_reload(zope_layer_dotted_name, reload_paths=('src',),
                 preload_layer_dotted_name='plone.app.testing.PLONE_FIXTURE'):

    print WAIT("Starting Zope 2 server")

    zsl = Zope2ServerLibrary()
    zsl.start_zope_server(preload_layer_dotted_name)

    forkloop = ForkLoop()
    Watcher(reload_paths, forkloop).start()
    forkloop.start()

    if forkloop.exit:
        print WAIT("Stopping Zope 2 server")
        zsl.stop_zope_server()
        print READY("Zope 2 server stopped")
        return

    hostname = os.environ.get('ZSERVER_HOST', 'localhost')

    # XXX: For unknown reason call to socket.gethostbyaddr may cause malloc
    # errors on OSX in forked child when called from medusa http_server, but
    # proper sleep seem to fix it:
    import time
    import socket
    import platform
    if 'Darwin' in platform.uname():
        gethostbyaddr = socket.gethostbyaddr
        socket.gethostbyaddr = lambda x: time.sleep(0.5) or (hostname,)

    # Setting smaller asyncore poll timeout will speed up restart a bit
    import plone.testing.z2
    plone.testing.z2.ZServer.timeout = 0.5

    zsl.amend_zope_server(zope_layer_dotted_name)

    if 'Darwin' in platform.uname():
        socket.gethostbyaddr = gethostbyaddr

    print READY("Zope 2 server started")

    try:
        listener = SimpleXMLRPCServer((hostname, LISTENER_PORT),
                                      logRequests=False)
    except socket.error as e:
        print ERROR(str(e))
        print WAIT("Pruning Zope 2 server")
        zsl.prune_zope_server()
        return

    listener.timeout = 0.5
    listener.allow_none = True
    listener.register_function(zsl.zodb_setup, 'zodb_setup')
    listener.register_function(zsl.zodb_teardown, 'zodb_teardown')

    try:
        while not forkloop.exit:
            listener.handle_request()
    except select.error:  # Interrupted system call
        pass
    finally:
        print WAIT("Pruning Zope 2 server")
        zsl.prune_zope_server()


def server():
    parser = argparse.ArgumentParser()
    parser.add_argument('layer')
    parser.add_argument('--verbose', '-v', action='count')
    if HAS_RELOAD:
        parser.add_argument('--reload-path', '-p', dest='reload_paths',
                            action='append')
        parser.add_argument('--preload-layer', '-l', dest='preload_layer')
        parser.add_argument('--no-reload', '-n', dest='reload',
                            action='store_false')
    args = parser.parse_args()
    if args.verbose:
        global HAS_VERBOSE_CONSOLE
        HAS_VERBOSE_CONSOLE = True
    logging.basicConfig(level=logging.ERROR)

    if not HAS_RELOAD or args.reload is False:
        try:
            start(args.layer)
        except KeyboardInterrupt:
            pass
    else:
        start_reload(args.layer, args.reload_paths or ['src'],
                     args.preload_layer or 'plone.app.testing.PLONE_FIXTURE')


class ZODB(object):

    ROBOT_LISTENER_API_VERSION = 2

    def __init__(self):
        server_listener_address = 'http://localhost:%s' % LISTENER_PORT
        self.server = xmlrpclib.ServerProxy(server_listener_address)

    def start_test(self, name, attrs):
        self.server.zodb_setup()

    def end_test(self, name, attrs):
        self.server.zodb_teardown()


class Zope2ServerLibrary(object):

    def __init__(self):
        self.zope_layer = None
        self.extra_layers = {}

    def _import_layer(self, layer_dotted_name):
        parts = layer_dotted_name.split('.')
        if len(parts) < 2:
            raise ValueError('no dot in layer dotted name')
        module_name = '.'.join(parts[:-1])
        layer_name = parts[-1]
        __import__(module_name)
        module = sys.modules[module_name]
        layer = getattr(module, layer_name)
        return layer

    def start_zope_server(self, layer_dotted_name):
        new_layer = self._import_layer(layer_dotted_name)
        if self.zope_layer and self.zope_layer is not new_layer:
            self.stop_zope_server()
        setup_layer(new_layer)
        self.zope_layer = new_layer

    def amend_zope_server(self, layer_dotted_name):
        """Set up extra layers up to given layer_dotted_name
        """
        old_layers = setup_layers.copy()
        new_layer = self._import_layer(layer_dotted_name)
        setup_layer(new_layer)
        for key in setup_layers.keys():
            if key not in old_layers:
                self.extra_layers[key] = 1
        self.zope_layer = new_layer

    def prune_zope_server(self):
        """Tear down the last set of layers set up with amend_zope_server
        """
        tear_down(self.extra_layers)
        self.extra_layers = {}
        self.zope_layer = None

    def stop_zope_server(self):
        tear_down()
        self.zope_layer = None

    def zodb_setup(self):
        from zope.testing.testrunner.runner import order_by_bases
        layers = order_by_bases([self.zope_layer])
        for layer in layers:
            if hasattr(layer, 'testSetUp'):
                layer.testSetUp()

    def zodb_teardown(self):
        from zope.testing.testrunner.runner import order_by_bases
        layers = order_by_bases([self.zope_layer])
        layers.reverse()
        for layer in layers:
            if hasattr(layer, 'testTearDown'):
                layer.testTearDown()


setup_layers = {}


def setup_layer(layer, setup_layers=setup_layers):
    assert layer is not object
    if layer not in setup_layers:
        for base in layer.__bases__:
            if base is not object:
                setup_layer(base, setup_layers)
        if hasattr(layer, 'setUp'):
            if HAS_VERBOSE_CONSOLE:
                print WAIT("Set up {0}.{1}".format(layer.__module__,
                                                   layer.__name__))
            layer.setUp()
        setup_layers[layer] = 1


def tear_down(setup_layers=setup_layers):
    from zope.testing.testrunner.runner import order_by_bases
    # Tear down any layers not needed for these tests. The unneeded layers
    # might interfere.
    unneeded = [l for l in setup_layers]
    unneeded = order_by_bases(unneeded)
    unneeded.reverse()
    for l in unneeded:
        try:
            try:
                if hasattr(l, 'tearDown'):
                    if HAS_VERBOSE_CONSOLE:
                        print WAIT("Tear down {0}.{1}".format(l.__module__,
                                                              l.__name__))
                    l.tearDown()
            except NotImplementedError:
                pass
        finally:
            del setup_layers[l]