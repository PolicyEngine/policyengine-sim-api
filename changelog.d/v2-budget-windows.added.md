Calculated budget windows on the detailed-save path. `POST /reports` takes
`report_type` `budget_window` with `budget_window_size` (1 to 75 years,
ending by 2200) and creates a report run per year, answering 201 with the
report, the first year's run, the window run and every year's runs. At
most five years run at once: the service starts the first ones, and when a
year ends its coordinator starts the window's next waiting years. The
window is marked succeeded when every year has succeeded, or failed as
soon as a year fails. The stuck-run sweep leaves a window's waiting years
to the window, fails a window when it fails one of its years, and settles
a window that no year advances any more.
