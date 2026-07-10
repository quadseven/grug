import json
import logging


class StructuredLogger(logging.LoggerAdapter):
    """
    A logger adapter to prepend a structured dictionary to the log message.
    """

    def process(self, msg, kwargs):
        extra = self.extra.copy()
        if "extra" in kwargs:
            extra.update(kwargs["extra"])

        # The main message is merged into the structure under the 'message' key
        log_struct = {"message": msg}
        log_struct.update(extra)

        return json.dumps(log_struct), kwargs


def get_logger(name, **kwargs):
    """
    Get a logger instance that will produce structured JSON logs.

    :param name: The name of the logger.
    :param kwargs: Key-value pairs to be included in every log message.
    """
    logger = logging.getLogger(name)
    return StructuredLogger(logger, kwargs)


if __name__ == "__main__":
    # --- Example Usage ---
    # Configure the basic logger
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=[handler])

    # Get a structured logger with some default context
    log = get_logger(__name__, service="grug-bot", version="1.0")

    # --- Log some messages ---
    log.info("Grug is waking up.")

    # You can add extra context on a per-message basis
    log.info("User performed an action.", extra={"user_id": "12345", "command": "/verify"})

    try:
        1 / 0
    except ZeroDivisionError:
        log.error("Grug tried to divide by zero.", extra={"error_code": "E001"}, exc_info=True)
