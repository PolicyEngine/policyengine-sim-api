"""Modal deployment package for PolicyEngine Simulation API.

Deploy from projects/policyengine-simulation-executor with:

    uv run --frozen modal deploy src/modal/app.py

so the deployed app's containers get the Modal client locked in uv.lock (1.6.4
or newer, which reloads Secret values when a container restores from a memory
snapshot), not whichever modal is installed elsewhere.

The app.py file contains all Modal wiring. The simulation.py file
contains the implementation with snapshotted policyengine imports.
"""
