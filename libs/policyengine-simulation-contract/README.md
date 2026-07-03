# policyengine-simulation-contract

The contract between the simulation gateway and executor: request/response
models (`gateway_models`), shared budget-window job state over `modal.Dict`
(`budget_window_state`), and dataset reference resolution
(`dataset_uri`, `hf_dataset`).

The gateway and executor never import each other — they communicate only
through the models and state helpers in this lib.
