"""The dataset-year error and the recogniser for every caller-correctable code."""

import pickle

import pytest
from policyengine_simulation_contract.input_errors import (
    DATASET_YEAR_UNAVAILABLE,
    DatasetYearUnavailableError,
    input_error_detail,
)
from policyengine_simulation_contract.spm import SPMInputError

UK_2033_MESSAGE = (
    "UK results are available for 2024 to 2032. 2033 is outside the years the "
    "current UK model and data cover."
)


class CodedCountryError(ValueError):
    """A country-package error that carries a public code, as SPM errors do."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def test_the_error_names_the_requested_year_and_the_supported_range():
    error = DatasetYearUnavailableError("uk", 2033, 2024, 2032)

    assert error.code == DATASET_YEAR_UNAVAILABLE
    assert str(error) == UK_2033_MESSAGE
    assert error.to_dict() == {
        "code": DATASET_YEAR_UNAVAILABLE,
        "message": UK_2033_MESSAGE,
    }
    assert (
        error.country,
        error.requested_year,
        error.first_supported_year,
        error.last_supported_year,
    ) == ("uk", 2033, 2024, 2032)


def test_the_error_survives_pickling_without_country_packages():
    transported = pickle.loads(
        pickle.dumps(DatasetYearUnavailableError("us", 2036, 2024, 2035))
    )

    assert type(transported) is DatasetYearUnavailableError
    assert transported.requested_year == 2036
    assert transported.last_supported_year == 2035
    assert str(transported).startswith("US results are available for 2024 to 2035.")


def test_input_error_detail_recognises_the_dataset_year_error():
    error = DatasetYearUnavailableError("uk", 2033, 2024, 2032)

    detail = input_error_detail(error)

    assert detail is not None
    assert detail.model_dump() == error.to_dict()


@pytest.mark.parametrize(
    "error",
    [
        CodedCountryError(DATASET_YEAR_UNAVAILABLE, UK_2033_MESSAGE),
        SPMInputError("SPM_YEAR_UNAVAILABLE", "No SPM values for 2033"),
        CodedCountryError("SPM_GEOGRAPHY_REQUIRED", "County required"),
    ],
)
def test_input_error_detail_recognises_every_public_code_through_the_chain(error):
    try:
        try:
            raise error
        except ValueError as cause:
            msg = "wrapped by dependency tracing"
            raise RuntimeError(msg) from cause
    except RuntimeError as wrapped:
        detail = input_error_detail(wrapped)

    assert detail is not None
    assert detail.model_dump() == {"code": error.code, "message": str(error)}


@pytest.mark.parametrize(
    "error",
    [
        ValueError("No dataset found for year 2033."),
        CodedCountryError("SOME_OTHER_CODE", "Not a public input error"),
        RuntimeError("worker crashed"),
    ],
)
def test_input_error_detail_ignores_other_errors(error):
    assert input_error_detail(error) is None
