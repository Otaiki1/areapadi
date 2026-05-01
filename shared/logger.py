import structlog
import logging
import hashlib
import os


def get_logger(service_name: str) -> structlog.BoundLogger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    return structlog.get_logger(service=service_name)


def hash_phone(phone_number: str) -> str:
    """One-way hash phone number for safe logging. Never log raw phone numbers."""
    return hashlib.sha256(phone_number.encode()).hexdigest()[:12]
