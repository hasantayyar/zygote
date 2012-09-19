import atexit
import datetime
import errno
import fcntl
import logging
import os
import signal
import socket
import sys
import time

import tornado.ioloop
import zygote.handlers

from .util import safe_kill, close_fds, setproctitle, ZygoteIOLoop, wait_for_pids
from zygote import message
from zygote import accounting
from zygote.worker import ZygoteWorker, INIT_FAILURE_EXIT_CODE

if hasattr(logging, 'NullHandler'):
    NullHandler = logging.NullHandler
else:
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass

log = logging.getLogger('zygote.master')

try:
    import ssl # Python 2.6+
except ImportError:
    ssl = None


class ZygoteMaster(object):

    instantiated = False

    RECV_SIZE = 8192

    # number of seconds to wait between polls
    POLL_INTERVAL = 1.0

    # how many seconds to wait before sending SIGKILL to children
    WAIT_FOR_KILL_TIME = 10.0

    def __init__(self,
                sock,
                basepath,
                module,
                num_workers,
                control_port,
                application_args=None,
                max_requests=None,
                zygote_base=None,
                ssl_options=None,
        ):

        if self.__class__.instantiated:
            log.error('cannot instantiate zygote master more than once')
            sys.exit(1)
        self.__class__.instantiated = True
        self.stopped = False
        self.started_transition = None

        self.application_args = application_args or []
        self.io_loop = ZygoteIOLoop(log_name='zygote.master.ioloop')
        self.sock = sock
        self.ssl_options = ssl_options
        self.basepath = basepath
        self.module = module
        self.num_workers = num_workers
        self.max_requests = max_requests
        self.time_created = datetime.datetime.now()

        self.prev_zygote = None
        self.current_zygote = None
        self.zygote_collection = accounting.ZygoteCollection()

        # create an abstract unix domain socket. this socket will be used to
        # receive messages from zygotes and their children
        log.debug("binding to domain socket")
        self.domain_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.domain_socket.bind('\0zygote_%d' % os.getpid())
        self.io_loop.add_handler(self.domain_socket.fileno(), self.recv_protocol_msg, self.io_loop.READ)

        signal.signal(signal.SIGCHLD, self.reap_child)
        signal.signal(signal.SIGHUP, self.update_revision)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
            signal.signal(sig, self.stop)

        self.open_fds, self.status_http_server = zygote.handlers.get_httpserver(
                self.io_loop,
                control_port,
                self,
                zygote_base=zygote_base,
                ssl_options=self.ssl_options,
        )

    def reap_child(self, signum, frame):
        """Signal handler for SIGCHLD. Reaps children and updates
        self.zygote_collection.
        """
        assert signum == signal.SIGCHLD
        while True:
            try:
                # The Zygotes are in their own process group, so need to
                # call waitpid() with -1 instead of 0. See waitpid(2).
                pid, status = os.waitpid(-1, os.WNOHANG)
            except OSError, e:
                if e.errno == errno.ECHILD:
                    break
                elif e.errno == errno.EINTR:
                    continue
                raise
            if pid == 0:
                break

            status_code = os.WEXITSTATUS(status)
            log.info('zygote %d exited with status %d', pid, status_code)

            # the zygote died. if the zygote was not the current zygote it's OK;
            # otherwise, we need to start a new one
            try:
                self.zygote_collection.remove_zygote(pid)
            except KeyError:
                pass

            if status_code == INIT_FAILURE_EXIT_CODE:
                if pid == self.current_zygote.pid and self.current_zygote.canary:
                    if self.prev_zygote:
                        self.curent_zygote = self.prev_zygote
                    log.error("Could not initialize canary worker. Giving up trying to respawn")
                else:
                    log.error("Could not initialize zygote worker, giving up")
                    self.really_stop()
                return

            if not self.stopped:
                active_zygote = self.current_zygote

                if pid == self.current_zygote.pid:
                    self.current_zygote = self.create_zygote()
                    active_zygote = self.current_zygote
                elif self.prev_zygote and pid == self.prev_zygote.pid:
                    self.prev_zygote = self.create_zygote()
                    active_zygote = self.prev_zygote

                # we may need to create new workers for the active zygote... this
                # is a bit racy, although that seems to be pretty unlikely in
                # practice
                workers_needed = self.num_workers - self.zygote_collection.worker_count()
                for x in xrange(workers_needed):
                    active_zygote.request_spawn()

            elif len(self.zygote_collection.zygote_map.values()) == 0:
                self.really_stop()

    def stop(self, signum=None, frame=None):
        """
        Stop the zygote master. Steps:
          * Ask all zygotes to kill and wait on their children
          * Wait for zygotes to exit
          * Kill anything left over if necessary
        """
        if self.stopped:
            return
        # kill all of the workers
        log.info('stopping all zygotes and workers')
        pids = set()
        for zygote in self.zygote_collection:
            pids.add(zygote.pid)
            log.debug('requesting shutdown on %d', zygote.pid)
            zygote.request_shut_down()

        # now we have to wait until all of the workers actually exit... at that
        # point self.really_stop() will be called
        log.debug('setting self.stopped')
        self.stopped = True
        if getattr(self, 'io_loop', None) is not None:
            self.io_loop.stop()
        wait_for_pids(pids, self.WAIT_FOR_KILL_TIME, log, kill_pgroup=True)
        log.info('all zygotes exited; good night')
        self.really_stop(0)

    def really_stop(self, status=0):
        sys.exit(status)

    def recv_protocol_msg(self, fd, events):
        """Callback for messages received on the domain_socket"""
        assert fd == self.domain_socket.fileno()
        data = self.domain_socket.recv(self.RECV_SIZE)
        msg = message.Message.parse(data)
        msg_type = type(msg)
        log.debug('received message of type %s from pid %d', msg_type.__name__, msg.pid)

        if msg_type is message.MessageCanaryInit:
            log.info("Canary zygote initialized. Transitioning idle workers.")
            # This is not the canary zygote anymore
            self.current_zygote.canary = False
            # We can also release the handle on the previous
            # zygote. It is already in the zygote_collection for
            # accounting purposses, but we won't need to keep track of
            # it anymore.
            self.prev_zygote = None
            # Canary initialization was successful, we can now transition workers
            self.io_loop.add_callback(self.transition_idle_workers)
        elif msg_type is message.MessageWorkerStart:
            # a new worker was spawned by one of our zygotes; add it to
            # zygote_collection, and note the time created and the zygote parent
            self.zygote_collection[msg.worker_ppid].add_worker(msg.pid, msg.time_created)
        elif msg_type is message.MessageWorkerExitInitFail:
            if not self.current_zygote.canary:
                log.error("A worker initialization failed, giving up")
                self.stop()
                return
        elif msg_type is message.MessageWorkerExit:
            # a worker exited. tell the current/active zygote to spawn a new
            # child. if this was the last child of a different (non-current)
            # zygote, kill that zygote
            zygote = self.zygote_collection[msg.pid]
            zygote.remove_worker(msg.child_pid)
            log.debug('Removed worker from zygote %d, there are now %d left', msg.pid, len(zygote.workers()))

            if self.stopped:
                # if we're in stopping mode better kill the zygote
                # too. self.kill_zygote will kill the zygote if it
                # doesn't have any children. this should not happen
                # using the new shutdown logic, but it doesn't hurt to
                # handle it anyway
                self.kill_zygote(zygote)
            else:
                if zygote == self.current_zygote:
                    self.current_zygote.request_spawn()
                elif zygote == self.prev_zygote:
                    self.prev_zygote.request_spawn()
                else:
                    # Not a zygote that we care about.
                    self.kill_zygote(zygote)
        elif msg_type is message.MessageHTTPBegin:
            # a worker started servicing an HTTP request
            worker = self.zygote_collection.get_worker(msg.pid)
            worker.start_request(msg.remote_ip, msg.http_line)
        elif msg_type is message.MessageHTTPEnd:
            # a worker finished servicing an HTTP request
            worker = self.zygote_collection.get_worker(msg.pid)
            worker.end_request()
            if self.max_requests is not None and worker.request_count >= self.max_requests:
                log.info('child %d reached max_requests %d, killing it', worker.pid, self.max_requests)
                os.kill(worker.pid, signal.SIGQUIT)
        else:
            log.warning('master got unexpected message of type %s', msg_type)

    def transition_idle_workers(self):
        """Transition idle HTTP workers from old zygotes to the current
        zygote.
        """
        if not self.started_transition:
            self.started_transition = time.time()
        if (time.time() - self.started_transition) > self.WAIT_FOR_KILL_TIME:
            log.debug("sending SIGKILL for transition because it was Too Damn Slow")
            sig = signal.SIGKILL
        else:
            sig = signal.SIGQUIT
        other_zygote_count = 0
        kill_count = 0
        for z in self.zygote_collection.other_zygotes(self.current_zygote):
            other_zygote_count += 1
            for worker in z.idle_workers():
                log.debug("killing worker %d with signal %d", worker.pid, sig)
                if safe_kill(worker.pid, sig):
                    kill_count += 1
        log.info('Attempted to transition %d workers from %d zygotes', kill_count, other_zygote_count)

        if other_zygote_count:
            # The list of other zygotes was at least one, so we should
            # reschedule another call to transition_idle_workers. When a zygote
            # runs out of worker children, the recv_protocol_msg function will
            # notice this fact when it receives the final MessageWorkerExit, and
            # at that time it will kill the worker, which is how this timeout
            # loop gets ended.
            self.io_loop.add_timeout(time.time() + self.POLL_INTERVAL, self.transition_idle_workers)
        else:
            self.started_transition = None

        # Cleanup empty zygotes for the next iteration of the transition.
        for z in self.zygote_collection.other_zygotes(self.current_zygote):
            if z.worker_count == 0:
                self.kill_zygote(z)

    def kill_zygote(self, zygote):
        """Send zygote SIGQUIT if it has zero workers. """
        # The only valid time to kill a zygote is if it doesn't have
        # any workers left.
        if zygote.worker_count == 0:
            log.info("killing zygote with pid %d" % zygote.pid)
            os.kill(zygote.pid, signal.SIGQUIT)

    def update_revision(self, signum=None, frame=None):
        """The SIGHUP handler, calls create_zygote and possibly initiates the
        transition of idle workers.

        This preserves the current zygote and initializes a "canary"
        zygote as the current one.
        """
        self.prev_zygote = self.current_zygote
        self.current_zygote = self.create_zygote(canary=True)

    def create_zygote(self, canary=False):
        """"Create a new zygote"""
        # read the basepath symlink
        realbase = os.path.realpath(self.basepath)

        pid = os.fork()
        if pid:
            log.info('started zygote %d pointed at base %r', pid, realbase)
            z = self.zygote_collection.add_zygote(pid, realbase, self.io_loop, canary=canary)
            if not canary:
                self.io_loop.add_callback(self.transition_idle_workers)
            return z
        else:
            # Try to clean up some of the file descriptors and whatnot that
            # exist in the parent before continuing. Strictly speaking, this
            # isn't necessary, but it seems good to remove these resources
            # if they're not needed in the child.
            del self.io_loop
            close_fds(self.sock.fileno())
            signal.signal(signal.SIGHUP, signal.SIG_DFL)

            # Make the zygote a process group leader
            os.setpgid(os.getpid(), os.getpid())
            # create the zygote
            z = ZygoteWorker(
                    sock=self.sock,
                    basepath=realbase,
                    module=self.module,
                    args=self.application_args,
                    ssl_options=self.ssl_options,
                    canary=canary
            )
            z.loop()

    def start(self):
        self.current_zygote = self.create_zygote()
        for x in xrange(self.num_workers):
            self.current_zygote.request_spawn()
        self.io_loop.start()

def main(opts, extra_args):
    setproctitle('zygote master %s' % (opts.module,))

    # Initialize the logging module
    formatter = logging.Formatter('[%(process)d] %(asctime)s :: %(levelname)-7s :: %(name)s - %(message)s')
    zygote_logger = logging.getLogger('zygote')

    # TODO: support logging to things other than stderr
    if os.isatty(sys.stderr.fileno()):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if opts.debug else logging.INFO)
        console_handler.setFormatter(formatter)
        zygote_logger.addHandler(console_handler)

    if not logging.root.handlers:
        # XXX: WARNING
        #
        # We're disabling the root logger. Tornado's RequestHandler ONLY
        # supports logging uncaught errors to the root logger. This will end
        # poorly for you!
        #
        # We should probably provide a RequestHandler subclass that has
        # _handle_request_exception overridden to do something useful.
        # That might be hard to do without adding a tight version dependency
        # on tornado.
        logging.root.addHandler(NullHandler())

    if opts.debug:
        logging.root.setLevel(logging.DEBUG)
        zygote_logger.setLevel(logging.DEBUG)
    else:
        logging.root.setLevel(logging.INFO)
        zygote_logger.setLevel(logging.INFO)
    log.info('main started')

    # Create the TCP listen socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    flags = fcntl.fcntl(sock.fileno(), fcntl.F_GETFD)
    flags |= fcntl.FD_CLOEXEC
    fcntl.fcntl(sock.fileno(), fcntl.F_SETFD, flags)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(0)
    sock.bind((opts.interface, opts.port))
    sock.listen(128)

    ssl_options=None
    if opts.cert:
        ssl_options = dict(
                certfile=opts.cert,
                keyfile=opts.key,
                ca_certs=opts.cacerts,
                cert_reqs=ssl.CERT_OPTIONAL if opts.cacerts else ssl.CERT_NONE,
        )
        log.info('using SSL with %s', ssl_options)

        sock = ssl.wrap_socket(sock,
                server_side=True,
                do_handshake_on_connect=False,
                **ssl_options
        )

    master = ZygoteMaster(sock,
            basepath=opts.basepath,
            module=opts.module,
            num_workers=opts.num_workers,
            control_port=opts.control_port,
            application_args=extra_args,
            max_requests=opts.max_requests,
            zygote_base=opts.zygote_base,
            ssl_options=ssl_options,
    )
    atexit.register(master.stop)
    master.start()
