"""Explicit ownership of Tk timers, independent of widget lifetime.

Only the Tk thread calls these methods. Cancelling a Future does not cancel its
Tk poll, and destroying a widget deletes Python commands without cancelling the
Tcl timers that reference them. Keep both lifetimes explicit.
"""


class OwnedTimers:
    def __init__(self, root, on_destroy):
        self.root = root
        self.pending = {}
        self.closed = False
        self.on_destroy = on_destroy
        root.bind('<Destroy>', self._destroyed, add='+')

    def _destroyed(self, event):
        if not self.closed and event.widget is self.root:
            callback = self.on_destroy
            self.close()
            callback()

    def schedule(self, name, delay, callback, *args):
        if self.closed:
            return
        self.cancel(name)

        def invoke():
            self.pending.pop(name, None)
            if not self.closed:
                callback(*args)

        self.pending[name] = self.root.after(delay, invoke)

    def cancel(self, name):
        identifier = self.pending.pop(name, None)
        if identifier is not None:
            self.root.after_cancel(identifier)

    def close(self):
        self.closed = True
        for name in tuple(self.pending):
            self.cancel(name)
        self.root = None
        self.on_destroy = None
        # Leave the Destroy binding registered until Tk destroys the root.
        # Another owner may close us from an earlier handler in the same event;
        # deleting our command then would invalidate that event's binding script.
        # The inert binding must not retain an owner/root/interpreter cycle.


class TkTimerOwner:
    def _init_timers(self, root, on_destroy=None):
        self._tk_timers = OwnedTimers(root, on_destroy or self.close)

    def _schedule_after(self, root, name, delay, callback, *args):
        # Lazy initialization also supports the lightweight, widget-free panel
        # fixtures used in the unit tests.
        if not hasattr(self, '_tk_timers'):
            self._init_timers(root)
        self._tk_timers.schedule(name, delay, callback, *args)

    def _close_timers(self):
        if hasattr(self, '_tk_timers'):
            self._tk_timers.close()
