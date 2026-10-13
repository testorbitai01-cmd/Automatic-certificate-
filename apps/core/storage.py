"""Private file storage: files are only reachable through permission-checked views."""
from django.core.files.storage import FileSystemStorage


class PrivateFileSystemStorage(FileSystemStorage):
    """FileSystemStorage that never produces a URL.

    With a plain FileSystemStorage, base_url=None silently falls back to MEDIA_URL, so a URL could be
    built (and served, if media serving were ever enabled). Here any attempt to build one fails loudly.
    """

    def url(self, name):
        raise ValueError("Private files have no URL; serve them through a permission-checked view.")
