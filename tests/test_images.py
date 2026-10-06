"""The verifier's image installs the packages these tests run against, at the same versions.

The answer key is computed in that image. If it installed a different version of anything world/ imports, the
tests here would be checking code the grader does not run. `uv run` installs what uv.lock names, so the versions
installed in this environment are the lock's.
"""

import re
from importlib.metadata import requires, version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ENVIRONMENT = Path(__file__).resolve().parents[1] / "evals/environment"
WORLD_IMPORTS = ["pydantic", "langgraph"]
# The image is python:3.12-slim, on either architecture. A requirement's marker is read for those, not for this machine.
IMAGES = [{"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": machine,
           "python_version": "3.12", "python_full_version": "3.12.0", "implementation_name": "cpython",
           "platform_python_implementation": "CPython", "extra": ""} for machine in ("x86_64", "aarch64")]


def pins(text: str) -> dict[str, str]:
    """Each `name==version` in a requirements file or a Dockerfile."""
    return {canonicalize_name(name): pinned for name, pinned in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)$", text, re.M)}


def installed_with(names: list[str]) -> set[str]:
    """These packages and everything pip installs with them in the image, by the metadata of what is installed here."""
    todo, found = [canonicalize_name(n) for n in names], set()
    while todo:
        name = todo.pop()
        if name not in found:
            found.add(name)
            needs = [Requirement(r) for r in requires(name) or []]
            todo += [canonicalize_name(r.name) for r in needs
                     if r.marker is None or any(r.marker.evaluate(image) for image in IMAGES)]
    return found


def test_the_verifier_pins_every_package_the_oracle_imports_at_the_locked_version():
    pinned = pins((ENVIRONMENT / "verifier/requirements.txt").read_text())
    assert set(pinned) == installed_with(WORLD_IMPORTS)
    assert pinned == {name: version(name) for name in pinned}
