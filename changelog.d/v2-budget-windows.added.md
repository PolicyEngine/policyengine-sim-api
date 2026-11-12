Calculated budget windows on the detailed-save path: a budget-window report's
calculation is one report run per year, at most five running at once. When
a year ends, its coordinator starts the window's next waiting years, and
the window is marked succeeded when every year has succeeded, or failed as
soon as a year fails. The stuck-run sweep leaves a window's waiting years to
the window, fails a window when it fails one of its years, and settles a
window that no year advances any more.
