"""Make directly uploaded objects visible through OpenList before publication."""

from pathlib import PurePosixPath

PUBLICATION_FILES = ("original_file", "playback_file", "cover_file")


def publication_directories(publications, primary_base):
    directories = set()
    for publication in publications:
        for field in PUBLICATION_FILES:
            relative = publication.get(field)
            if not relative:
                continue
            parent = PurePosixPath(primary_base)
            directories.add(str(parent))
            for segment in PurePosixPath(relative).parts[:-1]:
                parent = parent / segment
                directories.add(str(parent))
    return sorted(directories, key=lambda path: (path.count("/"), path))


class PublicationDirectoryRefresher:
    """Refresh each shared ancestor once within one fetched receipt batch."""

    def __init__(self, *, primary_base, refresh_directory):
        self._primary_base = primary_base
        self._refresh_directory = refresh_directory
        self._refreshed = set()

    async def __call__(self, publications):
        for directory in publication_directories(publications, self._primary_base):
            if directory in self._refreshed:
                continue
            await self._refresh_directory(directory)
            self._refreshed.add(directory)
