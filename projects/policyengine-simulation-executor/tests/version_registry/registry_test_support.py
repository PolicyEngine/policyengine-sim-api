"""Fakes and helpers shared by the routing publisher's tests."""

from __future__ import annotations

import json
import sys

from src.modal.routing_publisher import state as routing_state
from src.modal.utils import update_version_registry as registry


class FakeDict:
    def __init__(self, initial: dict | None = None):
        self._data = dict(initial or {})

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def items(self):
        return self._data.items()

    def pop(self, key):
        return self._data.pop(key)

    def snapshot(self) -> dict:
        return dict(self._data)

    def hydrate(self):
        return self


class NotFoundError(Exception):
    """Named like modal.exception.NotFoundError: the routing code matches the name."""


class FakeWorkerClassLookups:
    """``modal.Cls`` for the routing tests: records each class lookup.

    Every app defines the ``SimulationWorker`` class except the ones in
    ``apps_without_worker_class``, such as apps deployed before the workers
    were classes: resolving their class handle raises ``NotFoundError``.
    """

    def __init__(self) -> None:
        self.apps_without_worker_class: set[str] = set()
        self.lookups: list[tuple[str, str, str]] = []

    def from_name(
        self, app_name: str, class_name: str, *, environment_name: str
    ) -> FakeWorkerClassHandle:
        self.lookups.append((environment_name, app_name, class_name))
        return FakeWorkerClassHandle(
            app_name,
            class_name,
            defined=app_name not in self.apps_without_worker_class,
        )


class FakeWorkerClassHandle:
    """A lazy class handle: resolving it raises if the app has no such class."""

    def __init__(self, app_name: str, class_name: str, *, defined: bool) -> None:
        self.app_name = app_name
        self.class_name = class_name
        self.defined = defined

    def hydrate(self) -> FakeWorkerClassHandle:
        if not self.defined:
            msg = (
                f"Lookup failed for Cls '{self.class_name}' "
                f"from the '{self.app_name}' app"
            )
            raise NotFoundError(msg)
        return self


def _country(country: str, model_version: str) -> registry.CountryBundleMetadata:
    return {
        "country": country,
        "model_package_name": f"policyengine-{country}",
        "model_version": model_version,
        "data_package_name": "populace-data",
        "data_package_version": "0.1.0",
        "data_version": f"populace-{country}-build",
        "data_artifact_revision": f"populace-{country}-build",
        "default_dataset": f"populace_{country}",
        "default_dataset_uri": f"hf://datasets/policyengine/{country}/default",
        "dataset_uris": {},
        "dataset_repo_types": {},
    }


def versioned_app_name(policyengine_version: str) -> str:
    return "policyengine-simulation-py" + policyengine_version.replace(".", "-")


def install_bundle_manifests(monkeypatch, models: dict[str, tuple[str, str]]):
    def manifest(*, app_name, policyengine_version):
        us_version, uk_version = models[policyengine_version]
        return {
            "app_name": app_name,
            "policyengine_version": policyengine_version,
            "us": _country("us", us_version),
            "uk": {**_country("uk", uk_version), "default_dataset_sha256": "5" * 64},
        }

    monkeypatch.setattr(routing_state, "build_bundle_manifest_metadata", manifest)


def published_routing_state(
    monkeypatch, state, policyengine_version, us_version, uk_version
):
    install_bundle_manifests(
        monkeypatch, {policyengine_version: (us_version, uk_version)}
    )
    return registry.build_next_routing_state(
        current_state=state,
        deployed=registry.DeployedAppVersions(
            app_name=versioned_app_name(policyengine_version),
            policyengine_version=policyengine_version,
            us_version=us_version,
            uk_version=uk_version,
        ),
    )


def publication_plan(monkeypatch, state, versions, *, force=False, publication_id=None):
    """Plan publishing ``versions``: a policyengine, US and UK version."""
    version, us_version, uk_version = versions
    install_bundle_manifests(monkeypatch, {version: (us_version, uk_version)})
    return registry.plan_routing_publication(
        current_state=state,
        deployed=registry.DeployedAppVersions(
            app_name=versioned_app_name(version),
            policyengine_version=version,
            us_version=us_version,
            uk_version=uk_version,
        ),
        force_latest=force,
        publication_id=publication_id,
    )


def install_routing_store(patched_modal, state):
    store = FakeDict({} if state is None else {"active": json.loads(json.dumps(state))})
    patched_modal["staging/simulation-api-routing-state"] = store
    return store


def run_registry_main(monkeypatch, *args):
    monkeypatch.setattr(
        sys, "argv", ["update_version_registry", "--environment", "staging", *args]
    )
    registry.main()
