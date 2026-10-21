"""The main path's folder layout for baked per-state datasets."""

from policyengine_simulation_executor import state_datasets


def test_state_dataset_relative_folder_names_the_dataset_and_the_state():
    assert (
        state_datasets.state_dataset_relative_folder("populace_us_2024_acs_local", "ut")
        == "state-datasets/populace_us_2024_acs_local/UT"
    )


def test_state_dataset_folder_sits_under_the_data_folder():
    assert (
        state_datasets.state_dataset_folder(
            "/opt/policyengine/data", "populace_us_2024_acs_local", "UT"
        )
        == "/opt/policyengine/data/state-datasets/populace_us_2024_acs_local/UT"
    )
