class InvalidStorageKeyError(ValueError):
    """A storage key is empty, absolute, contains "..", or points outside the storage root."""
