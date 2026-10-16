"""Vercel routes the dashboard to the existing HTTP handler."""

from tubescribe.hosting import hosted_handler


class handler(hosted_handler()):
    """Named class entrypoint for Vercel's file-based function detection."""
