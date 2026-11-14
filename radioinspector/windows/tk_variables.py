"""Release variable callbacks when the complete owning application closes."""


def release_variable_traces(owner):
    """Detach this application's Tcl callbacks on its UI thread.

    Tcl holds Python trace callbacks outside Python's GC graph. Their closures
    can retain an app, its variables and the native interpreter after destroy.
    Walk only the app's own Windows objects/containers, not global GC state or
    other Tk interpreters. Individual panel close paths keep their live traces.
    """
    from tkinter import TclError, Variable

    interpreter = getattr(getattr(owner, 'root', None), 'tk', None)
    if interpreter is None:
        return
    pending = [owner]
    seen = set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, Variable):
            if item._tk is interpreter:
                for modes, name in item.trace_info():
                    try:
                        item.trace_remove(modes, name)
                    except TclError:
                        # A panel may already have removed its own callback.
                        pass
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (tuple, list)):
            pending.extend(item)
        elif type(item).__module__.startswith('radioinspector.windows.'):
            pending.extend(vars(item).values())
