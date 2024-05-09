# import atexit
import os
import struct
import libvirt
import select
import errno
import time
import threading
# from argparse import ArgumentParser
from typing import Any, Callable, Dict, List, Optional, TypeVar  # noqa F401
_T = TypeVar("_T")
_EventCallback = Callable[[int, int, int, _T], None]
_TimerCallback = Callable[[int, _T], None]

device_xml = """
<hostdev mode="subsystem" type="pci" managed="yes">
  <source>
    <address domain="0x0000" bus="0x00" slot="0x14" function="0x0"/>
  </source>
  <address type="pci" domain="0x0000" bus="0x10" slot="0x01" function="0x0"/>
</hostdev>
"""

active_device: libvirt.virDomain = None

def toggle_device():
    if active_device is None:
        return
    
    vm_desc: str = active_device.XMLDesc()
    found = vm_desc.find("slot='0x14'")
    if found == -1:
        print("Attaching device")
        active_device.attachDevice(device_xml)
    else:
        print("Detaching device")
        active_device.detachDevice(device_xml)

do_debug = False

def debug(msg: str) -> None:
    if do_debug:
        print(msg)

class virEventLoopPoll:
    # This class contains the data we need to track for a
    # single file handle
    class virEventLoopPollHandle:
        def __init__(self, handle: int, fd: int, events: int, cb: _EventCallback, opaque: _T):
            self.handle = handle
            self.fd = fd
            self.events = events
            self.cb = cb
            self.opaque = opaque

        def get_id(self) -> int:
            return self.handle

        def get_fd(self) -> int:
            return self.fd

        def get_events(self) -> int:
            return self.events

        def set_events(self, events: int):
            self.events = events

        def dispatch(self, events: int):
            self.cb(self.handle,
                    self.fd,
                    events,
                    self.opaque)

    # This class contains the data we need to track for a
    # single periodic timer
    class virEventLoopPollTimer:
        def __init__(self, timer: int, interval: int, cb: _TimerCallback, opaque: _T):
            self.timer = timer
            self.interval = interval
            self.cb = cb
            self.opaque = opaque
            self.lastfired = 0

        def get_id(self) -> int:
            return self.timer

        def get_interval(self) -> int:
            return self.interval

        def set_interval(self, interval: int):
            self.interval = interval

        def get_last_fired(self) -> int:
            return self.lastfired

        def set_last_fired(self, now: int):
            self.lastfired = now

        def dispatch(self) -> None:
            self.cb(self.timer,
                    self.opaque)

    def __init__(self):
        self.poll = select.poll()
        self.pipetrick = os.pipe()
        self.pendingWakeup = False
        self.runningPoll = False
        self.nextHandleID = 1
        self.nextTimerID = 1
        self.handles = []  # type: List[virEventLoopPollHandle]
        self.timers = []  # type: List[virEventLoopPollTimer]
        self.cleanup = []
        self.quit = False

        # The event loop can be used from multiple threads at once.
        # Specifically while the main thread is sleeping in poll()
        # waiting for events to occur, another thread may come along
        # and add/update/remove a file handle, or timer. When this
        # happens we need to interrupt the poll() sleep in the other
        # thread, so that it'll see the file handle / timer changes.
        #
        # Using OS level signals for this is very unreliable and
        # hard to implement correctly. Thus we use the real classic
        # "self pipe" trick. A anonymous pipe, with one end registered
        # with the event loop for input events. When we need to force
        # the main thread out of a poll() sleep, we simple write a
        # single byte of data to the other end of the pipe.
        debug("Self pipe watch %d write %d" % (self.pipetrick[0], self.pipetrick[1]))
        self.poll.register(self.pipetrick[0], select.POLLIN)

    # Calculate when the next timeout is due to occur, returning
    # the absolute timestamp for the next timeout, or 0 if there is
    # no timeout due
    def next_timeout(self) -> int:
        next = 0
        for t in self.timers:
            last = t.get_last_fired()
            interval = t.get_interval()
            if interval < 0:
                continue
            if next == 0 or (last + interval) < next:
                next = last + interval

        return next

    # Lookup a virEventLoopPollHandle object based on file descriptor
    def get_handle_by_fd(self, fd: int) -> Optional[virEventLoopPollHandle]:
        for h in self.handles:
            if h.get_fd() == fd:
                return h
        return None

    # Lookup a virEventLoopPollHandle object based on its event loop ID
    def get_handle_by_id(self, handleID: int) -> Optional[virEventLoopPollHandle]:
        for h in self.handles:
            if h.get_id() == handleID:
                return h
        return None

    # This is the heart of the event loop, performing one single
    # iteration. It asks when the next timeout is due, and then
    # calculates the maximum amount of time it is able to sleep
    # for in poll() pending file handle events.
    #
    # It then goes into the poll() sleep.
    #
    # When poll() returns, there will zero or more file handle
    # events which need to be dispatched to registered callbacks
    # It may also be time to fire some periodic timers.
    #
    # Due to the coarse granularity of scheduler timeslices, if
    # we ask for a sleep of 500ms in order to satisfy a timer, we
    # may return up to 1 scheduler timeslice early. So even though
    # our sleep timeout was reached, the registered timer may not
    # technically be at its expiry point. This leads to us going
    # back around the loop with a crazy 5ms sleep. So when checking
    # if timeouts are due, we allow a margin of 20ms, to avoid
    # these pointless repeated tiny sleeps.
    def run_once(self) -> None:
        sleep = -1  # type: float
        self.runningPoll = True

        for opaque in self.cleanup:
            libvirt.virEventInvokeFreeCallback(opaque)
        self.cleanup = []

        try:
            next = self.next_timeout()
            debug("Next timeout due at %d" % next)
            if next > 0:
                now = int(time.time() * 1000)
                if now >= next:
                    sleep = 0
                else:
                    sleep = (next - now) / 1000.0

            debug("Poll with a sleep of %d" % sleep)
            events = self.poll.poll(sleep)

            # Dispatch any file handle events that occurred
            for (fd, revents) in events:
                # See if the events was from the self-pipe
                # telling us to wakup. if so, then discard
                # the data just continue
                if fd == self.pipetrick[0]:
                    self.pendingWakeup = False
                    os.read(fd, 1)
                    continue

                h = self.get_handle_by_fd(fd)
                if h:
                    debug("Dispatch fd %d handle %d events %d" % (fd, h.get_id(), revents))
                    h.dispatch(self.events_from_poll(revents))

            now = int(time.time() * 1000)
            for t in self.timers:
                interval = t.get_interval()
                if interval < 0:
                    continue

                want = t.get_last_fired() + interval
                # Deduct 20ms, since scheduler timeslice
                # means we could be ever so slightly early
                if now >= want - 20:
                    debug("Dispatch timer %d now %s want %s" % (t.get_id(), str(now), str(want)))
                    t.set_last_fired(now)
                    t.dispatch()

        except (os.error, select.error) as e:
            if e.args[0] != errno.EINTR:
                raise
        finally:
            self.runningPoll = False

    # Actually run the event loop forever
    def run_loop(self) -> None:
        self.quit = False
        while not self.quit:
            self.run_once()

    def interrupt(self) -> None:
        if self.runningPoll and not self.pendingWakeup:
            self.pendingWakeup = True
            os.write(self.pipetrick[1], 'c'.encode("UTF-8"))

    # Registers a new file handle 'fd', monitoring  for 'events' (libvirt
    # event constants), firing the callback  cb() when an event occurs.
    # Returns a unique integer identier for this handle, that should be
    # used to later update/remove it
    def add_handle(self, fd: int, events: int, cb: _EventCallback, opaque: _T) -> int:
        handleID = self.nextHandleID + 1
        self.nextHandleID = self.nextHandleID + 1

        h = self.virEventLoopPollHandle(handleID, fd, events, cb, opaque)
        self.handles.append(h)

        self.poll.register(fd, self.events_to_poll(events))
        self.interrupt()

        debug("Add handle %d fd %d events %d" % (handleID, fd, events))

        return handleID

    # Registers a new timer with periodic expiry at 'interval' ms,
    # firing cb() each time the timer expires. If 'interval' is -1,
    # then the timer is registered, but not enabled
    # Returns a unique integer identier for this handle, that should be
    # used to later update/remove it
    def add_timer(self, interval: int, cb: _TimerCallback, opaque: _T) -> int:
        timerID = self.nextTimerID + 1
        self.nextTimerID = self.nextTimerID + 1

        h = self.virEventLoopPollTimer(timerID, interval, cb, opaque)
        self.timers.append(h)
        self.interrupt()

        debug("Add timer %d interval %d" % (timerID, interval))

        return timerID

    # Change the set of events to be monitored on the file handle
    def update_handle(self, handleID: int, events: int) -> None:
        h = self.get_handle_by_id(handleID)
        if h:
            h.set_events(events)
            self.poll.unregister(h.get_fd())
            self.poll.register(h.get_fd(), self.events_to_poll(events))
            self.interrupt()

            debug("Update handle %d fd %d events %d" % (handleID, h.get_fd(), events))

    # Change the periodic frequency of the timer
    def update_timer(self, timerID: int, interval: int) -> None:
        for h in self.timers:
            if h.get_id() == timerID:
                h.set_interval(interval)
                self.interrupt()

                debug("Update timer %d interval %d" % (timerID, interval))
                break

    # Stop monitoring for events on the file handle
    def remove_handle(self, handleID: int) -> int:
        handles = []
        for h in self.handles:
            if h.get_id() == handleID:
                debug("Remove handle %d fd %d" % (handleID, h.get_fd()))
                self.poll.unregister(h.get_fd())
                self.cleanup.append(h.opaque)
            else:
                handles.append(h)
        self.handles = handles
        self.interrupt()
        return 0

    # Stop firing the periodic timer
    def remove_timer(self, timerID: int) -> int:
        timers = []
        for h in self.timers:
            if h.get_id() != timerID:
                timers.append(h)
            else:
                debug("Remove timer %d" % timerID)
                self.cleanup.append(h.opaque)
        self.timers = timers
        self.interrupt()
        return 0

    # Convert from libvirt event constants, to poll() events constants
    def events_to_poll(self, events: int) -> int:
        ret = 0
        if events & libvirt.VIR_EVENT_HANDLE_READABLE:
            ret |= select.POLLIN
        if events & libvirt.VIR_EVENT_HANDLE_WRITABLE:
            ret |= select.POLLOUT
        if events & libvirt.VIR_EVENT_HANDLE_ERROR:
            ret |= select.POLLERR
        if events & libvirt.VIR_EVENT_HANDLE_HANGUP:
            ret |= select.POLLHUP
        return ret

    # Convert from poll() event constants, to libvirt events constants
    def events_from_poll(self, events: int) -> int:
        ret = 0
        if events & select.POLLIN:
            ret |= libvirt.VIR_EVENT_HANDLE_READABLE
        if events & select.POLLOUT:
            ret |= libvirt.VIR_EVENT_HANDLE_WRITABLE
        if events & select.POLLNVAL:
            ret |= libvirt.VIR_EVENT_HANDLE_ERROR
        if events & select.POLLERR:
            ret |= libvirt.VIR_EVENT_HANDLE_ERROR
        if events & select.POLLHUP:
            ret |= libvirt.VIR_EVENT_HANDLE_HANGUP
        return ret

eventLoop = virEventLoopPoll()

def virEventLoopPollRun() -> None:
    eventLoop.run_loop()

def virEventAddHandleImpl(fd: int, events: int, cb: _EventCallback, opaque: _T) -> int:
    return eventLoop.add_handle(fd, events, cb, opaque)


def virEventUpdateHandleImpl(handleID: int, events: int) -> None:
    return eventLoop.update_handle(handleID, events)


def virEventRemoveHandleImpl(handleID: int) -> int:
    return eventLoop.remove_handle(handleID)


def virEventAddTimerImpl(interval: int, cb: _TimerCallback, opaque: _T) -> int:
    return eventLoop.add_timer(interval, cb, opaque)


def virEventUpdateTimerImpl(timerID: int, interval: int) -> None:
    return eventLoop.update_timer(timerID, interval)


def virEventRemoveTimerImpl(timerID: int) -> int:
    return eventLoop.remove_timer(timerID)

def virEventLoopPollRegister() -> None:
    libvirt.virEventRegisterImpl(virEventAddHandleImpl,
                                 virEventUpdateHandleImpl,
                                 virEventRemoveHandleImpl,
                                 virEventAddTimerImpl,
                                 virEventUpdateTimerImpl,
                                 virEventRemoveTimerImpl)

# Spawn a background thread to run the event loop
def virEventLoopPollStart() -> None:
    global eventLoopThread
    virEventLoopPollRegister()
    eventLoopThread = threading.Thread(target=virEventLoopPollRun,
                                       name="libvirtEventLoop",
                                       daemon=True)
    eventLoopThread.start()

def domain_event_callback(conn, dom, event, detail, opaque):
    global active_device
    if event == libvirt.VIR_DOMAIN_EVENT_STARTED:
        print(f"Domain {dom.name()} started")
        active_device = dom
    elif event == libvirt.VIR_DOMAIN_EVENT_STOPPED:
        active_device = None
        print(f"Domain {dom.name()} stopped")

def get_first_active_domain(conn):
    # Get IDs of all active domains
    domain_ids = conn.listDomainsID()
    
    if not domain_ids:
        print("No active domains found")
        return None
    
    # Get the domain object for the first active domain
    domain_id = domain_ids[0]
    domain = conn.lookupByID(domain_id)
    
    return domain

def main():
    virEventLoopPollStart()

    conn = libvirt.open("qemu:///system")  # Connect to the local hypervisor
    if conn is None:
        print('Failed to open connection to hypervisor')
        return

    global active_device
    active_device = get_first_active_domain(conn)

    # Register domain event callbacks
    conn.domainEventRegister(domain_event_callback, None)
    
    toggle_executing = False
    with open("/dev/input/event2", "rb") as device:
        try:
            while True:
                if active_device == None:
                    continue
                
                event = device.read(24)
                if not event: continue
                time_sec, time_usec, type_, code, value = struct.unpack('llHHI', event)
                if type_ != 1 or code != 116 or value != 1:  # Type EV_KEY, code KEY_POWER, value 1 (pressed)
                    continue
                
                print("Power Button Pressed")
                if toggle_executing == True:
                    print("Toggle blocked")
                    continue
                try:
                    toggle_executing = True
                    toggle_device()
                except:
                    toggle_executing = False
                    continue
                finally:
                    toggle_executing = False
                pass
        except KeyboardInterrupt:
            conn.close()

if __name__ == '__main__':
    main()