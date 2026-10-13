"""
ClassVision AI - Email Delivery for Parent Alerts
--------------------------------------------------
The one place a warning actually leaves the building.

What this is

    An implementation of NotificationProvider.send() that speaks SMTP. It is
    the missing half of the alert system described in
    backend/services/notification_service.py: that module decides WHETHER a
    parent should be told something, and this one puts it in front of them.

Configuration is environment, not database

    An SMTP password in a table is an SMTP password in every backup, every
    copy somebody takes for debugging, and every screenshot of the admin
    page. So the credentials are read from the environment and never written
    anywhere this project controls:

        CLASSVISION_SMTP_HOST       smtp.gmail.com
        CLASSVISION_SMTP_PORT       587
        CLASSVISION_SMTP_USER       the account that authenticates
        CLASSVISION_SMTP_PASSWORD   an app password, not the login password
        CLASSVISION_SMTP_FROM       what a parent sees in From:
        CLASSVISION_SMTP_SECURITY   starttls (default) | ssl | none
        CLASSVISION_SMTP_TIMEOUT    seconds, default 20

    Nothing here is required. With no host configured, configured_provider()
    returns the RecordingProvider the project already shipped with, alerts are
    still raised and still visible in the dashboard, and nothing is sent. An
    installation that has not been given a mail account behaves exactly as it
    did before this module existed.

Why it degrades instead of failing

    A school that has not finished setting up email should still be able to
    run attendance. Refusing to start, or throwing on every evaluation, would
    make the alerting feature a reason to avoid the release.

What is never logged

    The parent's address appears in the message envelope and nowhere else.
    Delivery failures are recorded against the notification row as the SMTP
    server's own words, truncated, with no address, no credentials and no
    traceback - the same rule deliver() already applies to every provider.
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage

from backend.services.notification_service import (
    NotificationError,
    NotificationProvider,
)

ENV_PREFIX = "CLASSVISION_SMTP_"

DEFAULT_PORT = 587
DEFAULT_TIMEOUT = 20
DEFAULT_SECURITY = "starttls"

# Deliberately permissive. This is a guard against an obviously unusable
# value - an empty string, a name with no @ - not an attempt to decide which
# addresses a mail server will accept. Over-strict validation here would mean
# refusing to notify a parent whose perfectly valid address offends a regex.
ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _env(name, default=None):
    value = os.environ.get(ENV_PREFIX + name)
    if value is None:
        return default
    value = value.strip()
    return value or default


class SmtpSettings:
    """What the environment says about sending mail, read once."""

    __slots__ = ("host", "port", "username", "password", "sender",
                 "security", "timeout")

    def __init__(self, host=None, port=None, username=None, password=None,
                 sender=None, security=None, timeout=None):
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.username = username
        self.password = password
        self.security = (security or DEFAULT_SECURITY).lower()
        self.timeout = int(timeout or DEFAULT_TIMEOUT)

        # Falling back to the authenticating user is what most providers
        # require anyway: Gmail rewrites From: to the authenticated account,
        # so pretending otherwise would produce mail that says one thing in
        # the dashboard and another in the parent's inbox.
        self.sender = sender or username

    @classmethod
    def from_environment(cls):
        return cls(host=_env("HOST"), port=_env("PORT"),
                   username=_env("USER"), password=_env("PASSWORD"),
                   sender=_env("FROM"), security=_env("SECURITY"),
                   timeout=_env("TIMEOUT"))

    @property
    def configured(self):
        """A host and a From address are the minimum worth attempting."""
        return bool(self.host and self.sender)

    def describe(self):
        """
        Safe to show an administrator and safe to log.

        Reports WHETHER a password is set, never what it is, and never any
        part of it - a masked password with a visible prefix is still a
        useful hint to somebody who should not have one.
        """
        return {
            "configured": self.configured,
            "host": self.host or "",
            "port": self.port,
            "sender": self.sender or "",
            "security": self.security,
            "username_set": bool(self.username),
            "password_set": bool(self.password),
        }


class SmtpProvider(NotificationProvider):
    """
    Sends a parent alert as an email over SMTP.

    One connection per message. That is slower than holding one open, and it
    is the right trade for a job that sends a handful of messages a night:
    a long-lived connection to a mail server is a thing that silently dies
    and takes the next fortnight of alerts with it.
    """

    name = "smtp"

    def __init__(self, settings=None):
        self.settings = settings or SmtpSettings.from_environment()

        if not self.settings.configured:
            raise NotificationError(
                "SMTP is not configured: set %sHOST and %sFROM."
                % (ENV_PREFIX, ENV_PREFIX))

    # -- composing ---------------------------------------------------------

    def compose(self, notification, recipient):
        """
        Build the message. Separated from sending so a test can read what a
        parent would receive without a mail server existing.

        The student's NAME is in the body and their id is not: a parent knows
        their child's name and has no use for an internal identifier, and an
        email that quotes a database key is an email that ends up in a
        support ticket.
        """
        address = (recipient.get("email") or "").strip()
        if not ADDRESS.match(address):
            raise NotificationError(
                "no usable parent email address is on file")

        message = EmailMessage()
        message["Subject"] = notification.get("subject") or \
            "Attendance notice"
        message["From"] = self.settings.sender
        message["To"] = address

        parent = (recipient.get("name") or "").strip()
        greeting = "Dear %s," % parent if parent else "Dear parent or guardian,"

        message.set_content(
            "%s\n\n%s\n\n"
            "This message was sent automatically by the school's attendance "
            "system. If you believe it is mistaken, please contact the "
            "school office - replies to this address are not monitored.\n"
            % (greeting, (notification.get("body") or "").strip()))

        return message

    # -- sending -----------------------------------------------------------

    def _connect(self):
        settings = self.settings

        if settings.security == "ssl":
            return smtplib.SMTP_SSL(settings.host, settings.port,
                                    timeout=settings.timeout,
                                    context=ssl.create_default_context())

        connection = smtplib.SMTP(settings.host, settings.port,
                                  timeout=settings.timeout)
        if settings.security == "starttls":
            connection.starttls(context=ssl.create_default_context())
        return connection

    def send(self, notification, recipient):
        """
        Deliver one message, or raise NotificationError saying why not.

        Every SMTP failure is turned into the project's own exception type
        carrying the server's message. The caller - deliver() in
        notification_service.py - records that string against the row and
        marks it FAILED, so a bounce is visible in the dashboard rather than
        lost in a log nobody reads.
        """
        message = self.compose(notification, recipient)
        settings = self.settings

        try:
            connection = self._connect()
        except Exception as error:                       # noqa: BLE001
            raise NotificationError(
                "could not reach the mail server: %s" % error)

        try:
            if settings.username and settings.password:
                connection.login(settings.username, settings.password)
            connection.send_message(message)
        except smtplib.SMTPAuthenticationError:
            # Deliberately not echoing the server's text here: some servers
            # include the username in the rejection.
            raise NotificationError(
                "the mail server rejected the credentials")
        except Exception as error:                       # noqa: BLE001
            raise NotificationError("the mail server refused the message: %s"
                                    % error)
        finally:
            try:
                connection.quit()
            except Exception:                            # noqa: BLE001
                pass

        return {"reference": message["To"].split("@")[-1],
                "provider": self.name}


def configured_provider(settings=None):
    """
    The provider this installation should use, given its environment.

    Returns SmtpProvider when a host and sender are configured, and the
    existing RecordingProvider otherwise - so an installation with no mail
    account keeps working exactly as it did, raising alerts and delivering
    none.
    """
    from backend.services.notification_service import RecordingProvider

    settings = settings or SmtpSettings.from_environment()
    if not settings.configured:
        return RecordingProvider()

    try:
        return SmtpProvider(settings)
    except NotificationError:
        return RecordingProvider()


def describe_configuration():
    """What the dashboard shows on the alerts page. Never a secret."""
    return SmtpSettings.from_environment().describe()
