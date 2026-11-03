"""Caller-correctable simulation errors that every service recognises by code.

Each error has a stable ``code`` that clients branch on and a ``message`` a
person can read. The SPM selection errors live in ``spm``. This module adds
``DATASET_YEAR_UNAVAILABLE``, for a year the country model cannot project its
dataset to, and one recogniser for every such code, which a service uses
wherever it turns a worker failure into a 400 response or a stored failure.
"""

from __future__ import annotations

from policyengine_simulation_contract.spm import SPM_ERROR_CODES, SPMErrorDetail

DATASET_YEAR_UNAVAILABLE = "DATASET_YEAR_UNAVAILABLE"
INPUT_ERROR_CODES = SPM_ERROR_CODES | {DATASET_YEAR_UNAVAILABLE}


class DatasetYearUnavailableError(ValueError):
    """A requested year is outside the years the model projects the dataset to.

    Carries the requested year and the first and last supported years. The
    positional arguments are kept as the exception's ``args``, so the error
    survives pickling across Modal into a service without country packages.
    """

    code = DATASET_YEAR_UNAVAILABLE

    def __init__(
        self,
        country: str,
        requested_year: int,
        first_supported_year: int,
        last_supported_year: int,
    ) -> None:
        """Record the country, the requested year and the supported range."""
        self.country = country
        self.requested_year = requested_year
        self.first_supported_year = first_supported_year
        self.last_supported_year = last_supported_year
        country_label = country.upper()
        self.message = (
            f"{country_label} results are available for {first_supported_year} "
            f"to {last_supported_year}. {requested_year} is outside the years the "
            f"current {country_label} model and data cover."
        )
        super().__init__(
            country, requested_year, first_supported_year, last_supported_year
        )

    def __str__(self) -> str:
        """Return the readable message alone."""
        return self.message

    def to_dict(self) -> dict[str, str]:
        """Return the ``{"code", "message"}`` wire form of this error."""
        return {"code": self.code, "message": self.message}


def input_error_detail(exc: BaseException) -> SPMErrorDetail | None:
    """Return the code and message of a caller-correctable error, or None.

    Follows the cause and context chain, as ``spm_error_detail`` does, and
    recognises any ``ValueError`` whose ``code`` is an SPM input code or
    ``DATASET_YEAR_UNAVAILABLE``, whatever class carried it. The result is the
    same ``{"code", "message"}`` model the SPM errors use on the wire.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(current, ValueError) and code in INPUT_ERROR_CODES:
            return SPMErrorDetail(code=code, message=str(current))
        current = current.__cause__ or current.__context__
    return None
