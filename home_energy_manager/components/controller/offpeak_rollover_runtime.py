"""Regular off-peak rollover guard for controller issue #91.

The legacy minimise-export policy distinguishes an active persisted cheap window
from the forecaster-supplied next window. If the supplied window itself is the
currently active tariff period, treating it as the following window inverts the
active-end -> next-start bridge. Patch that helper at module scope so every
legacy policy calculation uses the same chronological interpretation.
"""
import legacy_minimise_export_core as runtime

core = runtime.core
_shift_local_day = runtime._shift_local_day
_apply_axle_overlay = runtime._apply_axle_overlay
_original_current_regular_offpeak = runtime._current_regular_offpeak


def _current_regular_offpeak(controller, next_window):
    """Return persisted active off-peak only when ``next_window`` is not itself active."""
    now = controller.now()
    if next_window['start'] <= now < next_window['end']:
        return None
    return _original_current_regular_offpeak(controller, next_window)


runtime._current_regular_offpeak = _current_regular_offpeak


def __getattr__(name):
    return getattr(runtime, name)
