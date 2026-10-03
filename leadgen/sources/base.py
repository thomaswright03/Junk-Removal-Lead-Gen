from typing import Any, Iterator, Optional

from ..models import Lead


class Source:
    """A place leads come from.

    ``fetch`` yields :class:`leadgen.models.Lead` objects for events between
    ``since`` and ``until`` (ISO dates, inclusive). Sources that read local
    files take them through ``paths``.
    """

    name = ""
    description = ""

    def fetch(self, since: str, until: str, paths: Optional[list] = None, **options: Any) -> Iterator[Lead]:
        raise NotImplementedError
