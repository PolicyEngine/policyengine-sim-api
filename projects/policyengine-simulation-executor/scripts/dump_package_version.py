import tomllib
import sys


def get_package_version_from_lock(package_name, lock_file_path="poetry.lock"):
    try:
        with open(lock_file_path, "rb") as f:
            lock_data = tomllib.load(f)

        for package in lock_data.get("package", []):
            if package.get("name") == package_name:
                return package.get("version")

        return None
    except FileNotFoundError:
        return None


version = get_package_version_from_lock(sys.argv[2], sys.argv[1])

if version is None:
    sys.exit(1)

print(version)
sys.exit(0)
