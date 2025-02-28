"""
    This module provides more sophisticated flow tracking and provides filtering and interception facilities.
"""
from __future__ import absolute_import
from abc import abstractmethod, ABCMeta
import hashlib
import Cookie
import cookielib
import os
import re
from netlib import odict, wsgi, tcp
import netlib.http
from . import controller, protocol, tnetstring, filt, script, version
from .onboarding import app
from .protocol import http, handle
from .proxy.config import HostMatcher
from .proxy.connection import ClientConnection, ServerConnection
import urlparse


class AppRegistry:
    def __init__(self):
        self.apps = {}

    def add(self, app, domain, port):
        """
            Add a WSGI app to the registry, to be served for requests to the
            specified domain, on the specified port.
        """
        self.apps[(domain, port)] = wsgi.WSGIAdaptor(
            app,
            domain,
            port,
            version.NAMEVERSION
        )

    def get(self, request):
        """
            Returns an WSGIAdaptor instance if request matches an app, or None.
        """
        if (request.host, request.port) in self.apps:
            return self.apps[(request.host, request.port)]
        if "host" in request.headers:
            host = request.headers["host"][0]
            return self.apps.get((host, request.port), None)


class ReplaceHooks:
    def __init__(self):
        self.lst = []

    def set(self, r):
        self.clear()
        for i in r:
            self.add(*i)

    def add(self, fpatt, rex, s):
        """
            add a replacement hook.

            fpatt: a string specifying a filter pattern.
            rex: a regular expression.
            s: the replacement string

            returns true if hook was added, false if the pattern could not be
            parsed.
        """
        cpatt = filt.parse(fpatt)
        if not cpatt:
            return False
        try:
            re.compile(rex)
        except re.error:
            return False
        self.lst.append((fpatt, rex, s, cpatt))
        return True

    def get_specs(self):
        """
            Retrieve the hook specifcations. Returns a list of (fpatt, rex, s)
            tuples.
        """
        return [i[:3] for i in self.lst]

    def count(self):
        return len(self.lst)

    def run(self, f):
        for _, rex, s, cpatt in self.lst:
            if cpatt(f):
                if f.response:
                    f.response.replace(rex, s)
                else:
                    f.request.replace(rex, s)

    def clear(self):
        self.lst = []


class SetHeaders:
    def __init__(self):
        self.lst = []

    def set(self, r):
        self.clear()
        for i in r:
            self.add(*i)

    def add(self, fpatt, header, value):
        """
            Add a set header hook.

            fpatt: String specifying a filter pattern.
            header: Header name.
            value: Header value string

            Returns True if hook was added, False if the pattern could not be
            parsed.
        """
        cpatt = filt.parse(fpatt)
        if not cpatt:
            return False
        self.lst.append((fpatt, header, value, cpatt))
        return True

    def get_specs(self):
        """
            Retrieve the hook specifcations. Returns a list of (fpatt, rex, s)
            tuples.
        """
        return [i[:3] for i in self.lst]

    def count(self):
        return len(self.lst)

    def clear(self):
        self.lst = []

    def run(self, f):
        for _, header, value, cpatt in self.lst:
            if cpatt(f):
                if f.response:
                    del f.response.headers[header]
                else:
                    del f.request.headers[header]
        for _, header, value, cpatt in self.lst:
            if cpatt(f):
                if f.response:
                    f.response.headers.add(header, value)
                else:
                    f.request.headers.add(header, value)


class StreamLargeBodies(object):
    def __init__(self, max_size):
        self.max_size = max_size

    def run(self, flow, is_request):
        r = flow.request if is_request else flow.response
        code = flow.response.code if flow.response else None
        expected_size = netlib.http.http1.HTTP1Protocol.expected_http_body_size(
            r.headers, is_request, flow.request.method, code
        )
        if not (0 <= expected_size <= self.max_size):
            # r.stream may already be a callable, which we want to preserve.
            r.stream = r.stream or True


class ClientPlaybackState:
    def __init__(self, flows, exit):
        self.flows, self.exit = flows, exit
        self.current = None
        self.testing = False  # Disables actual replay for testing.

    def count(self):
        return len(self.flows)

    def done(self):
        if len(self.flows) == 0 and not self.current:
            return True
        return False

    def clear(self, flow):
        """
           A request has returned in some way - if this is the one we're
           servicing, go to the next flow.
        """
        if flow is self.current:
            self.current = None

    def tick(self, master):
        if self.flows and not self.current:
            self.current = self.flows.pop(0).copy()
            if not self.testing:
                master.replay_request(self.current)
            else:
                self.current.reply = controller.DummyReply()
                master.handle_request(self.current)
                if self.current.response:
                    master.handle_response(self.current)


class ServerPlaybackState:
    def __init__(
            self,
            headers,
            flows,
            exit,
            nopop,
            ignore_params,
            ignore_content,
            ignore_payload_params,
            ignore_host):
        """
            headers: Case-insensitive list of request headers that should be
            included in request-response matching.
        """
        self.headers = headers
        self.exit = exit
        self.nopop = nopop
        self.ignore_params = ignore_params
        self.ignore_content = ignore_content
        self.ignore_payload_params = ignore_payload_params
        self.ignore_host = ignore_host
        self.fmap = {}
        for i in flows:
            if i.response:
                l = self.fmap.setdefault(self._hash(i), [])
                l.append(i)

    def count(self):
        return sum(len(i) for i in self.fmap.values())

    def _hash(self, flow):
        """
            Calculates a loose hash of the flow request.
        """
        r = flow.request

        _, _, path, _, query, _ = urlparse.urlparse(r.url)
        queriesArray = urlparse.parse_qsl(query, keep_blank_values=True)

        # scheme should match the client connection to be able to replay
        # although r.scheme may have been changed to http to connect to upstream server
        scheme = "https" if flow.client_conn and flow.client_conn.ssl_established else "http"

        key = [
            str(r.port),
            str(scheme),
            str(r.method),
            str(path),
        ]

        if not self.ignore_content:
            form_contents = r.get_form()
            if self.ignore_payload_params and form_contents:
                key.extend(
                    p for p in form_contents
                    if p[0] not in self.ignore_payload_params
                )
            else:
                key.append(str(r.content))

        if not self.ignore_host:
            key.append(r.host)

        filtered = []
        ignore_params = self.ignore_params or []
        for p in queriesArray:
            if p[0] not in ignore_params:
                filtered.append(p)
        for p in filtered:
            key.append(p[0])
            key.append(p[1])

        if self.headers:
            hdrs = []
            for i in self.headers:
                v = r.headers[i]
                # Slightly subtle: we need to convert everything to strings
                # to prevent a mismatch between unicode/non-unicode.
                v = [str(x) for x in v]
                hdrs.append((i, v))
            key.append(hdrs)
        return hashlib.sha256(repr(key)).digest()

    def next_flow(self, request):
        """
            Returns the next flow object, or None if no matching flow was
            found.
        """
        l = self.fmap.get(self._hash(request))
        if not l:
            return None

        if self.nopop:
            return l[0]
        else:
            return l.pop(0)


class StickyCookieState:
    def __init__(self, flt):
        """
            flt: Compiled filter.
        """
        self.jar = {}
        self.flt = flt

    def ckey(self, m, f):
        """
            Returns a (domain, port, path) tuple.
        """
        return (
            m["domain"] or f.request.host,
            f.request.port,
            m["path"] or "/"
        )

    def domain_match(self, a, b):
        if cookielib.domain_match(a, b):
            return True
        elif cookielib.domain_match(a, b.strip(".")):
            return True
        return False

    def handle_response(self, f):
        for i in f.response.headers["set-cookie"]:
            # FIXME: We now know that Cookie.py screws up some cookies with
            # valid RFC 822/1123 datetime specifications for expiry. Sigh.
            c = Cookie.SimpleCookie(str(i))
            for m in c.values():
                k = self.ckey(m, f)
                if self.domain_match(f.request.host, k[0]):
                    self.jar[k] = m

    def handle_request(self, f):
        l = []
        if f.match(self.flt):
            for i in self.jar.keys():
                match = [
                    self.domain_match(f.request.host, i[0]),
                    f.request.port == i[1],
                    f.request.path.startswith(i[2])
                ]
                if all(match):
                    l.append(self.jar[i].output(header="").strip())
        if l:
            f.request.stickycookie = True
            f.request.headers["cookie"] = l


class StickyAuthState:
    def __init__(self, flt):
        """
            flt: Compiled filter.
        """
        self.flt = flt
        self.hosts = {}

    def handle_request(self, f):
        host = f.request.host
        if "authorization" in f.request.headers:
            self.hosts[host] = f.request.headers["authorization"]
        elif f.match(self.flt):
            if host in self.hosts:
                f.request.headers["authorization"] = self.hosts[host]


class FlowList(object):
    __metaclass__ = ABCMeta

    def __iter__(self):
        return iter(self._list)

    def __contains__(self, item):
        return item in self._list

    def __getitem__(self, item):
        return self._list[item]

    def __nonzero__(self):
        return bool(self._list)

    def __len__(self):
        return len(self._list)

    def index(self, f):
        return self._list.index(f)

    @abstractmethod
    def _add(self, f):
        return

    @abstractmethod
    def _update(self, f):
        return

    @abstractmethod
    def _remove(self, f):
        return


class FlowView(FlowList):
    def __init__(self, store, filt=None):
        self._list = []
        if not filt:
            filt = lambda flow: True
        self._build(store, filt)

        self.store = store
        self.store.views.append(self)

    def _close(self):
        self.store.views.remove(self)

    def _build(self, flows, filt=None):
        if filt:
            self.filt = filt
        self._list = list(filter(self.filt, flows))

    def _add(self, f):
        if self.filt(f):
            self._list.append(f)

    def _update(self, f):
        if f not in self._list:
            self._add(f)
        elif not self.filt(f):
            self._remove(f)

    def _remove(self, f):
        if f in self._list:
            self._list.remove(f)

    def _recalculate(self, flows):
        self._build(flows)


class FlowStore(FlowList):
    """
    Responsible for handling flows in the state:
    Keeps a list of all flows and provides views on them.
    """

    def __init__(self):
        self._list = []
        self._set = set()  # Used for O(1) lookups
        self.views = []
        self._recalculate_views()

    def get(self, flow_id):
        for f in self._list:
            if f.id == flow_id:
                return f

    def __contains__(self, f):
        return f in self._set

    def _add(self, f):
        """
        Adds a flow to the state.
        The flow to add must not be present in the state.
        """
        self._list.append(f)
        self._set.add(f)
        for view in self.views:
            view._add(f)

    def _update(self, f):
        """
        Notifies the state that a flow has been updated.
        The flow must be present in the state.
        """
        if f in self:
            for view in self.views:
                view._update(f)

    def _remove(self, f):
        """
        Deletes a flow from the state.
        The flow must be present in the state.
        """
        self._list.remove(f)
        self._set.remove(f)
        for view in self.views:
            view._remove(f)

    # Expensive bulk operations

    def _extend(self, flows):
        """
        Adds a list of flows to the state.
        The list of flows to add must not contain flows that are already in the state.
        """
        self._list.extend(flows)
        self._set.update(flows)
        self._recalculate_views()

    def _clear(self):
        self._list = []
        self._set = set()
        self._recalculate_views()

    def _recalculate_views(self):
        """
        Expensive operation: Recalculate all the views after a bulk change.
        """
        for view in self.views:
            view._recalculate(self)

    # Utility functions.
    # There are some common cases where we need to argue about all flows
    # irrespective of filters on the view etc (i.e. on shutdown).

    def active_count(self):
        c = 0
        for i in self._list:
            if not i.response and not i.error:
                c += 1
        return c

    # TODO: Should accept_all operate on views or on all flows?
    def accept_all(self, master):
        for f in self._list:
            f.accept_intercept(master)

    def kill_all(self, master):
        for f in self._list:
            f.kill(master)


class State(object):
    def __init__(self):
        self.flows = FlowStore()
        self.view = FlowView(self.flows, None)

        # These are compiled filt expressions:
        self.intercept = None

    @property
    def limit_txt(self):
        return getattr(self.view.filt, "pattern", None)

    def flow_count(self):
        return len(self.flows)

    # TODO: All functions regarding flows that don't cause side-effects should
    # be moved into FlowStore.
    def index(self, f):
        return self.flows.index(f)

    def active_flow_count(self):
        return self.flows.active_count()

    def add_flow(self, f):
        """
            Add a request to the state.
        """
        self.flows._add(f)
        return f

    def update_flow(self, f):
        """
            Add a response to the state.
        """
        self.flows._update(f)
        return f

    def delete_flow(self, f):
        self.flows._remove(f)

    def load_flows(self, flows):
        self.flows._extend(flows)

    def set_limit(self, txt):
        if txt == self.limit_txt:
            return
        if txt:
            f = filt.parse(txt)
            if not f:
                return "Invalid filter expression."
            self.view._close()
            self.view = FlowView(self.flows, f)
        else:
            self.view._close()
            self.view = FlowView(self.flows, None)

    def set_intercept(self, txt):
        if txt:
            f = filt.parse(txt)
            if not f:
                return "Invalid filter expression."
            self.intercept = f
        else:
            self.intercept = None

    @property
    def intercept_txt(self):
        return getattr(self.intercept, "pattern", None)

    def clear(self):
        self.flows._clear()

    def accept_all(self, master):
        self.flows.accept_all(master)

    def backup(self, f):
        f.backup()
        self.update_flow(f)

    def revert(self, f):
        f.revert()
        self.update_flow(f)

    def killall(self, master):
        self.flows.kill_all(master)


class FlowMaster(controller.Master):
    def __init__(self, server, state):
        controller.Master.__init__(self, server)
        self.state = state
        self.server_playback = None
        self.client_playback = None
        self.kill_nonreplay = False
        self.scripts = []
        self.pause_scripts = False

        self.stickycookie_state = False
        self.stickycookie_txt = None

        self.stickyauth_state = False
        self.stickyauth_txt = None

        self.anticache = False
        self.anticomp = False
        self.stream_large_bodies = False
        self.refresh_server_playback = False
        self.replacehooks = ReplaceHooks()
        self.setheaders = SetHeaders()
        self.replay_ignore_params = False
        self.replay_ignore_content = None
        self.replay_ignore_host = False

        self.stream = None
        self.apps = AppRegistry()

    def start_app(self, host, port):
        self.apps.add(
            app.mapp,
            host,
            port
        )

    def add_event(self, e, level="info"):
        """
            level: debug, info, error
        """
        pass

    def unload_scripts(self):
        for s in self.scripts[:]:
            self.unload_script(s)

    def unload_script(self, script_obj):
        try:
            script_obj.unload()
        except script.ScriptError as e:
            self.add_event("Script error:\n" + str(e), "error")
        self.scripts.remove(script_obj)

    def load_script(self, command):
        """
            Loads a script. Returns an error description if something went
            wrong.
        """
        try:
            s = script.Script(command, self)
        except script.ScriptError as v:
            return v.args[0]
        self.scripts.append(s)

    def _run_single_script_hook(self, script_obj, name, *args, **kwargs):
        if script_obj and not self.pause_scripts:
            try:
                script_obj.run(name, *args, **kwargs)
            except script.ScriptError as e:
                self.add_event("Script error:\n" + str(e), "error")

    def run_script_hook(self, name, *args, **kwargs):
        for script_obj in self.scripts:
            self._run_single_script_hook(script_obj, name, *args, **kwargs)

    def get_ignore_filter(self):
        return self.server.config.check_ignore.patterns

    def set_ignore_filter(self, host_patterns):
        self.server.config.check_ignore = HostMatcher(host_patterns)

    def get_tcp_filter(self):
        return self.server.config.check_tcp.patterns

    def set_tcp_filter(self, host_patterns):
        self.server.config.check_tcp = HostMatcher(host_patterns)

    def set_stickycookie(self, txt):
        if txt:
            flt = filt.parse(txt)
            if not flt:
                return "Invalid filter expression."
            self.stickycookie_state = StickyCookieState(flt)
            self.stickycookie_txt = txt
        else:
            self.stickycookie_state = None
            self.stickycookie_txt = None

    def set_stream_large_bodies(self, max_size):
        if max_size is not None:
            self.stream_large_bodies = StreamLargeBodies(max_size)
        else:
            self.stream_large_bodies = False

    def set_stickyauth(self, txt):
        if txt:
            flt = filt.parse(txt)
            if not flt:
                return "Invalid filter expression."
            self.stickyauth_state = StickyAuthState(flt)
            self.stickyauth_txt = txt
        else:
            self.stickyauth_state = None
            self.stickyauth_txt = None

    def start_client_playback(self, flows, exit):
        """
            flows: List of flows.
        """
        self.client_playback = ClientPlaybackState(flows, exit)

    def stop_client_playback(self):
        self.client_playback = None

    def start_server_playback(
            self,
            flows,
            kill,
            headers,
            exit,
            nopop,
            ignore_params,
            ignore_content,
            ignore_payload_params,
            ignore_host):
        """
            flows: List of flows.
            kill: Boolean, should we kill requests not part of the replay?
            ignore_params: list of parameters to ignore in server replay
            ignore_content: true if request content should be ignored in server replay
            ignore_payload_params: list of content params to ignore in server replay
            ignore_host: true if request host should be ignored in server replay
        """
        self.server_playback = ServerPlaybackState(
            headers,
            flows,
            exit,
            nopop,
            ignore_params,
            ignore_content,
            ignore_payload_params,
            ignore_host)
        self.kill_nonreplay = kill

    def stop_server_playback(self):
        if self.server_playback.exit:
            self.shutdown()
        self.server_playback = None

    def do_server_playback(self, flow):
        """
            This method should be called by child classes in the handle_request
            handler. Returns True if playback has taken place, None if not.
        """
        if self.server_playback:
            rflow = self.server_playback.next_flow(flow)
            if not rflow:
                return None
            response = http.HTTPResponse.from_state(rflow.response.get_state())
            response.is_replay = True
            if self.refresh_server_playback:
                response.refresh()
            flow.reply(response)
            if self.server_playback.count() == 0:
                self.stop_server_playback()
            return True
        return None

    def tick(self, q, timeout):
        if self.client_playback:
            e = [
                self.client_playback.done(),
                self.client_playback.exit,
                self.state.active_flow_count() == 0
            ]
            if all(e):
                self.shutdown()
            self.client_playback.tick(self)
            if self.client_playback.done():
                self.client_playback = None

        return super(FlowMaster, self).tick(q, timeout)

    def duplicate_flow(self, f):
        return self.load_flow(f.copy())

    def create_request(self, method, scheme, host, port, path):
        """
            this method creates a new artificial and minimalist request also adds it to flowlist
        """
        c = ClientConnection.from_state(dict(
            address=dict(address=(host, port), use_ipv6=False),
            clientcert=None
        ))

        s = ServerConnection.from_state(dict(
            address=dict(address=(host, port), use_ipv6=False),
            state=[],
            source_address=None,
            # source_address=dict(address=(host, port), use_ipv6=False),
            cert=None,
            sni=host,
            ssl_established=True
        ))
        f = http.HTTPFlow(c, s)
        headers = odict.ODictCaseless()

        req = http.HTTPRequest(
            "absolute",
            method,
            scheme,
            host,
            port,
            path,
            (1,
             1),
            headers,
            None,
            None,
            None,
            None)
        f.request = req
        return self.load_flow(f)

    def load_flow(self, f):
        """
            Loads a flow, and returns a new flow object.
        """

        if self.server and self.server.config.mode == "reverse":
            f.request.host, f.request.port = self.server.config.mode.dst[2:]
            f.request.scheme = "https" if self.server.config.mode.dst[
                1] else "http"

        f.reply = controller.DummyReply()
        if f.request:
            self.handle_request(f)
        if f.response:
            self.handle_responseheaders(f)
            self.handle_response(f)
        if f.error:
            self.handle_error(f)
        return f

    def load_flows(self, fr):
        """
            Load flows from a FlowReader object.
        """
        cnt = 0
        for i in fr.stream():
            cnt += 1
            self.load_flow(i)
        return cnt

    def load_flows_file(self, path):
        path = os.path.expanduser(path)
        try:
            f = file(path, "rb")
            freader = FlowReader(f)
        except IOError as v:
            raise FlowReadError(v.strerror)
        return self.load_flows(freader)

    def process_new_request(self, f):
        if self.stickycookie_state:
            self.stickycookie_state.handle_request(f)
        if self.stickyauth_state:
            self.stickyauth_state.handle_request(f)

        if self.anticache:
            f.request.anticache()
        if self.anticomp:
            f.request.anticomp()

        if self.server_playback:
            pb = self.do_server_playback(f)
            if not pb:
                if self.kill_nonreplay:
                    f.kill(self)
                else:
                    f.reply()

    def process_new_response(self, f):
        if self.stickycookie_state:
            self.stickycookie_state.handle_response(f)

    def replay_request(self, f, block=False, run_scripthooks=True):
        """
            Returns None if successful, or error message if not.
        """
        if f.live and run_scripthooks:
            return "Can't replay live request."
        if f.intercepted:
            return "Can't replay while intercepting..."
        if f.request.content == http.CONTENT_MISSING:
            return "Can't replay request with missing content..."
        if f.request:
            f.backup()
            f.request.is_replay = True
            if f.request.content:
                f.request.headers[
                    "Content-Length"] = [str(len(f.request.content))]
            f.response = None
            f.error = None
            self.process_new_request(f)
            rt = http.RequestReplayThread(
                self.server.config,
                f,
                self.masterq if run_scripthooks else False,
                self.should_exit
            )
            rt.start()  # pragma: no cover
            if block:
                rt.join()

    def handle_log(self, l):
        self.add_event(l.msg, l.level)
        l.reply()

    def handle_clientconnect(self, cc):
        self.run_script_hook("clientconnect", cc)
        cc.reply()

    def handle_clientdisconnect(self, r):
        self.run_script_hook("clientdisconnect", r)
        r.reply()

    def handle_serverconnect(self, sc):
        self.run_script_hook("serverconnect", sc)
        sc.reply()

    def handle_error(self, f):
        self.state.update_flow(f)
        self.run_script_hook("error", f)
        if self.client_playback:
            self.client_playback.clear(f)
        f.reply()
        return f

    def handle_request(self, f):
        if f.live:
            app = self.apps.get(f.request)
            if app:
                err = app.serve(
                    f,
                    f.client_conn.wfile,
                    **{"mitmproxy.master": self}
                )
                if err:
                    self.add_event("Error in wsgi app. %s" % err, "error")
                f.reply(protocol.KILL)
                return
        if f not in self.state.flows:  # don't add again on replay
            self.state.add_flow(f)
        self.replacehooks.run(f)
        self.setheaders.run(f)
        self.run_script_hook("request", f)
        self.process_new_request(f)
        return f

    def handle_responseheaders(self, f):
        self.run_script_hook("responseheaders", f)

        try:
            if self.stream_large_bodies:
                self.stream_large_bodies.run(f, False)
        except netlib.http.HttpError:
            f.reply(protocol.KILL)
            return

        f.reply()
        return f

    def handle_response(self, f):
        self.state.update_flow(f)
        self.replacehooks.run(f)
        self.setheaders.run(f)
        self.run_script_hook("response", f)
        if self.client_playback:
            self.client_playback.clear(f)
        self.process_new_response(f)
        if self.stream:
            self.stream.add(f)
        return f

    def handle_intercept(self, f):
        self.state.update_flow(f)

    def handle_accept_intercept(self, f):
        self.state.update_flow(f)

    def shutdown(self):
        self.unload_scripts()
        controller.Master.shutdown(self)
        if self.stream:
            for i in self.state.flows:
                if not i.response:
                    self.stream.add(i)
            self.stop_stream()

    def start_stream(self, fp, filt):
        self.stream = FilteredFlowWriter(fp, filt)

    def stop_stream(self):
        self.stream.fo.close()
        self.stream = None


def read_flows_from_paths(paths):
    """
    Given a list of filepaths, read all flows and return a list of them.
    From a performance perspective, streaming would be advisable -
    however, if there's an error with one of the files, we want it to be raised immediately.

    If an error occurs, a FlowReadError will be raised.
    """
    try:
        flows = []
        for path in paths:
            path = os.path.expanduser(path)
            with file(path, "rb") as f:
                flows.extend(FlowReader(f).stream())
    except IOError as e:
        raise FlowReadError(e.strerror)
    return flows


class FlowWriter:
    def __init__(self, fo):
        self.fo = fo

    def add(self, flow):
        d = flow.get_state()
        tnetstring.dump(d, self.fo)


class FlowReadError(Exception):
    @property
    def strerror(self):
        return self.args[0]


class FlowReader:
    def __init__(self, fo):
        self.fo = fo

    def stream(self):
        """
            Yields Flow objects from the dump.
        """
        off = 0
        try:
            while True:
                data = tnetstring.load(self.fo)
                if tuple(data["version"][:2]) != version.IVERSION[:2]:
                    v = ".".join(str(i) for i in data["version"])
                    raise FlowReadError(
                        "Incompatible serialized data version: %s" % v
                    )
                off = self.fo.tell()
                yield handle.protocols[data["type"]]["flow"].from_state(data)
        except ValueError as v:
            # Error is due to EOF
            if self.fo.tell() == off and self.fo.read() == '':
                return
            raise FlowReadError("Invalid data format.")


class FilteredFlowWriter:
    def __init__(self, fo, filt):
        self.fo = fo
        self.filt = filt

    def add(self, f):
        if self.filt and not f.match(self.filt):
            return
        d = f.get_state()
        tnetstring.dump(d, self.fo)
