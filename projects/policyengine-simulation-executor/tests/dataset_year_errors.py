"""A worker error carrying the stable ``DATASET_YEAR_UNAVAILABLE`` code.

Services recognise a caller-correctable error by its stable code, as they
recognise the SPM input errors, whatever class carried it across Modal. The
tests raise this stand-in from a child, a worker or a calculator.
"""

DATASET_YEAR_UNAVAILABLE = "DATASET_YEAR_UNAVAILABLE"
DATASET_YEAR_MESSAGE = (
    "US results are available for 2024 to 2035. 2036 is outside the years the "
    "current US model and data cover."
)


class WorkerDatasetYearError(ValueError):
    """A ``ValueError`` whose ``code`` is ``DATASET_YEAR_UNAVAILABLE``."""

    code = DATASET_YEAR_UNAVAILABLE


def dataset_year_error():
    """Return the stand-in error with the shared readable message."""
    return WorkerDatasetYearError(DATASET_YEAR_MESSAGE)
