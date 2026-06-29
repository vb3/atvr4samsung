"""NSKeyedArchiver reader for RTI payloads. Origin: pyatv v0.18.0 (MIT), adapted."""

import plistlib
from typing import Any, List, Optional, Tuple


def read_archive_properties(archive, *paths: List[str]) -> Tuple[Optional[Any], ...]:
    """Follow UID references for selected paths without a full NSKeyedArchiver implementation."""
    data = plistlib.loads(archive)
    results = []

    objects = data["$objects"]
    for path in paths:
        element = data["$top"]
        try:
            for key in path:
                element = element[key]
                if isinstance(element, plistlib.UID):
                    element = objects[element]
            results.append(element)
        except (IndexError, KeyError):
            results.append(None)

    return tuple(results)
