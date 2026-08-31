import logging


class SuppressExtractMsgBodyError(logging.Filter):
    """Suppress extract_msg's expected ERROR log for unreadable MSG bodies.

    The application catches the resulting ``UnicodeDecodeError`` and handles it
    gracefully (see ``indexer.utils.typesense_index_record``), so the library's
    internal ERROR log is noise in the Celery worker output.
    """

    def filter(self, record):
        return "Critical error accessing the body" not in record.getMessage()
