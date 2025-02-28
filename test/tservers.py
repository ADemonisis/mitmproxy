import os.path
import threading
import Queue
import shutil
import tempfile
import flask
import mock

from libmproxy.proxy.config import ProxyConfig
from libmproxy.proxy.server import ProxyServer
from libmproxy.proxy.primitives import TransparentProxyMode
import libpathod.test
import libpathod.pathoc
from libmproxy import flow, controller
from libmproxy.cmdline import APP_HOST, APP_PORT
import tutils

testapp = flask.Flask(__name__)


@testapp.route("/")
def hello():
    return "testapp"


@testapp.route("/error")
def error():
    raise ValueError("An exception...")


def errapp(environ, start_response):
    raise ValueError("errapp")


class TestMaster(flow.FlowMaster):
    def __init__(self, config):
        config.port = 0
        s = ProxyServer(config)
        state = flow.State()
        flow.FlowMaster.__init__(self, s, state)
        self.apps.add(testapp, "testapp", 80)
        self.apps.add(errapp, "errapp", 80)
        self.clear_log()

    def handle_request(self, f):
        flow.FlowMaster.handle_request(self, f)
        f.reply()

    def handle_response(self, f):
        flow.FlowMaster.handle_response(self, f)
        f.reply()

    def clear_log(self):
        self.log = []

    def handle_log(self, l):
        self.log.append(l.msg)
        l.reply()


class ProxyThread(threading.Thread):
    def __init__(self, tmaster):
        threading.Thread.__init__(self)
        self.tmaster = tmaster
        self.name = "ProxyThread (%s:%s)" % (
            tmaster.server.address.host, tmaster.server.address.port)
        controller.should_exit = False

    @property
    def port(self):
        return self.tmaster.server.address.port

    @property
    def log(self):
        return self.tmaster.log

    def run(self):
        self.tmaster.run()

    def shutdown(self):
        self.tmaster.shutdown()


class ProxTestBase(object):
    # Test Configuration
    ssl = None
    ssloptions = False
    clientcerts = False
    no_upstream_cert = False
    authenticator = None
    masterclass = TestMaster

    @classmethod
    def setupAll(cls):
        cls.server = libpathod.test.Daemon(
            ssl=cls.ssl,
            ssloptions=cls.ssloptions)
        cls.server2 = libpathod.test.Daemon(
            ssl=cls.ssl,
            ssloptions=cls.ssloptions)

        cls.config = ProxyConfig(**cls.get_proxy_config())

        tmaster = cls.masterclass(cls.config)
        tmaster.start_app(APP_HOST, APP_PORT)
        cls.proxy = ProxyThread(tmaster)
        cls.proxy.start()

    @classmethod
    def teardownAll(cls):
        shutil.rmtree(cls.cadir)
        cls.proxy.shutdown()
        cls.server.shutdown()
        cls.server2.shutdown()

    def setUp(self):
        self.master.clear_log()
        self.master.state.clear()
        self.server.clear_log()
        self.server2.clear_log()

    @property
    def master(self):
        return self.proxy.tmaster

    @classmethod
    def get_proxy_config(cls):
        cls.cadir = os.path.join(tempfile.gettempdir(), "mitmproxy")
        return dict(
            no_upstream_cert = cls.no_upstream_cert,
            cadir = cls.cadir,
            authenticator = cls.authenticator,
            ssl_ports=([cls.server.port, cls.server2.port] if cls.ssl else []),
            clientcerts = tutils.test_data.path("data/clientcert") if cls.clientcerts else None
        )


class HTTPProxTest(ProxTestBase):
    def pathoc_raw(self):
        return libpathod.pathoc.Pathoc(("127.0.0.1", self.proxy.port), fp=None)

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = libpathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        if self.ssl:
            p.connect(("127.0.0.1", self.server.port))
        else:
            p.connect()
        return p

    def pathod(self, spec, sni=None):
        """
            Constructs a pathod GET request, with the appropriate base and proxy.
        """
        p = self.pathoc(sni=sni)
        spec = spec.encode("string_escape")
        if self.ssl:
            q = "get:'/p/%s'" % spec
        else:
            q = "get:'%s/p/%s'" % (self.server.urlbase, spec)
        return p.request(q)

    def app(self, page):
        if self.ssl:
            p = libpathod.pathoc.Pathoc(
                ("127.0.0.1", self.proxy.port), True, fp=None
            )
            p.connect((APP_HOST, APP_PORT))
            return p.request("get:'%s'" % page)
        else:
            p = self.pathoc()
            return p.request("get:'http://%s%s'" % (APP_HOST, page))


class TResolver:
    def __init__(self, port):
        self.port = port

    def original_addr(self, sock):
        return ("127.0.0.1", self.port)


class TransparentProxTest(ProxTestBase):
    ssl = None
    resolver = TResolver

    @classmethod
    @mock.patch("libmproxy.platform.resolver")
    def setupAll(cls, _):
        super(TransparentProxTest, cls).setupAll()
        if cls.ssl:
            ports = [cls.server.port, cls.server2.port]
        else:
            ports = []
        cls.config.mode = TransparentProxyMode(
            cls.resolver(
                cls.server.port),
            ports)

    @classmethod
    def get_proxy_config(cls):
        d = ProxTestBase.get_proxy_config()
        d["mode"] = "transparent"
        return d

    def pathod(self, spec, sni=None):
        """
            Constructs a pathod GET request, with the appropriate base and proxy.
        """
        if self.ssl:
            p = self.pathoc(sni=sni)
            q = "get:'/p/%s'" % spec
        else:
            p = self.pathoc()
            q = "get:'/p/%s'" % spec
        return p.request(q)

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = libpathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        p.connect()
        return p


class ReverseProxTest(ProxTestBase):
    ssl = None

    @classmethod
    def get_proxy_config(cls):
        d = ProxTestBase.get_proxy_config()
        d["upstream_server"] = [
            True if cls.ssl else False,
            True if cls.ssl else False,
            "127.0.0.1",
            cls.server.port
        ]
        d["mode"] = "reverse"
        return d

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = libpathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        p.connect()
        return p

    def pathod(self, spec, sni=None):
        """
            Constructs a pathod GET request, with the appropriate base and proxy.
        """
        if self.ssl:
            p = self.pathoc(sni=sni)
            q = "get:'/p/%s'" % spec
        else:
            p = self.pathoc()
            q = "get:'/p/%s'" % spec
        return p.request(q)


class SocksModeTest(HTTPProxTest):
    @classmethod
    def get_proxy_config(cls):
        d = ProxTestBase.get_proxy_config()
        d["mode"] = "socks5"
        return d

class SpoofModeTest(ProxTestBase):
    ssl = None

    @classmethod
    def get_proxy_config(cls):
        d = ProxTestBase.get_proxy_config()
        d["upstream_server"] = None
        d["mode"] = "spoof"
        return d

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = libpathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        p.connect()
        return p


class SSLSpoofModeTest(ProxTestBase):
    ssl = True

    @classmethod
    def get_proxy_config(cls):
        d = ProxTestBase.get_proxy_config()
        d["upstream_server"] = None
        d["mode"] = "sslspoof"
        d["spoofed_ssl_port"] = 443
        return d

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = libpathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        p.connect()
        return p


class ChainProxTest(ProxTestBase):
    """
    Chain three instances of mitmproxy in a row to test upstream mode.
    Proxy order is cls.proxy -> cls.chain[0] -> cls.chain[1]
    cls.proxy and cls.chain[0] are in upstream mode,
    cls.chain[1] is in regular mode.
    """
    chain = None
    n = 2

    @classmethod
    def setupAll(cls):
        cls.chain = []
        super(ChainProxTest, cls).setupAll()
        for _ in range(cls.n):
            config = ProxyConfig(**cls.get_proxy_config())
            tmaster = cls.masterclass(config)
            proxy = ProxyThread(tmaster)
            proxy.start()
            cls.chain.insert(0, proxy)

        # Patch the orginal proxy to upstream mode
        cls.config = cls.proxy.tmaster.config = cls.proxy.tmaster.server.config = ProxyConfig(
            **cls.get_proxy_config())

    @classmethod
    def teardownAll(cls):
        super(ChainProxTest, cls).teardownAll()
        for proxy in cls.chain:
            proxy.shutdown()

    def setUp(self):
        super(ChainProxTest, self).setUp()
        for proxy in self.chain:
            proxy.tmaster.clear_log()
            proxy.tmaster.state.clear()

    @classmethod
    def get_proxy_config(cls):
        d = super(ChainProxTest, cls).get_proxy_config()
        if cls.chain:  # First proxy is in normal mode.
            d.update(
                mode="upstream",
                upstream_server=(False, False, "127.0.0.1", cls.chain[0].port)
            )
        return d


class HTTPUpstreamProxTest(ChainProxTest, HTTPProxTest):
    pass
