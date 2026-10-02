class Source:
    """A place leads come from.

    ``fetch`` yields :class:`leadgen.models.Lead` objects for events between
    ``since`` and ``until`` (ISO dates, inclusive). Sources that read local
    files take them through ``paths``.
    """

    name = ""
    description = ""

    def fetch(self, since, until, paths=None, **options):
        raise NotImplementedError
