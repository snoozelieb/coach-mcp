"""
Notification system for training briefs.

Supported channels:
- Console: Print to terminal
- Log: Save to file
- Email: SMTP (Gmail, Outlook, etc.)
"""
import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class Notifier:
    """Base notifier class."""

    def send(self, message: str, title: Optional[str] = None) -> bool:
        """
        Send a notification.

        Args:
            message: The message content
            title: Optional title/subject

        Returns:
            True if sent successfully, False otherwise
        """
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    """Simple console output notifier."""

    def send(self, message: str, title: Optional[str] = None) -> bool:
        if title:
            print(f"\n{'=' * 50}")
            print(f"  {title}")
            print(f"{'=' * 50}")
        print(message)
        return True


class LogNotifier(Notifier):
    """Log file notifier."""

    def __init__(self, log_file: str = "morning_briefs.log"):
        self.log_file = log_file

    def send(self, message: str, title: Optional[str] = None) -> bool:
        try:
            with open(self.log_file, 'a') as f:
                f.write(f"\n{'=' * 50}\n")
                if title:
                    f.write(f"{title}\n")
                f.write(f"Date: {date.today().isoformat()}\n")
                f.write(f"{'=' * 50}\n")
                f.write(message)
                f.write("\n")
            return True
        except Exception as e:
            logger.error(f"Failed to write to log: {e}")
            return False


class EmailNotifier(Notifier):
    """
    Email notifier via SMTP.

    Requires in .env:
    - EMAIL_SMTP_SERVER: SMTP server (e.g., smtp.gmail.com)
    - EMAIL_SMTP_PORT: SMTP port (587 for TLS, 465 for SSL)
    - EMAIL_ADDRESS: Your email address (sender)
    - EMAIL_PASSWORD: App password (NOT your regular password for Gmail)
    - EMAIL_TO: Recipient email (can be same as EMAIL_ADDRESS)

    For Gmail:
    1. Enable 2FA on your Google account
    2. Go to Google Account > Security > App passwords
    3. Generate an app password for "Mail"
    4. Use that password in EMAIL_PASSWORD
    """

    def __init__(self):
        self.smtp_server = os.getenv('EMAIL_SMTP_SERVER', 'smtp.gmail.com')
        self.smtp_port = int(os.getenv('EMAIL_SMTP_PORT', '587'))
        self.email_address = os.getenv('EMAIL_ADDRESS')
        self.email_password = os.getenv('EMAIL_PASSWORD')
        self.email_to = os.getenv('EMAIL_TO') or self.email_address

    def send(self, message: str, title: Optional[str] = None) -> bool:
        if not self.email_address or not self.email_password:
            logger.warning("Email not configured. Set EMAIL_ADDRESS and EMAIL_PASSWORD in .env")
            return False

        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = title or f"Training Brief - {date.today().strftime('%A, %B %d')}"
            msg['From'] = self.email_address
            msg['To'] = self.email_to

            # Plain text version
            text_part = MIMEText(message, 'plain')
            msg.attach(text_part)

            # HTML version (convert markdown-ish to basic HTML)
            html_message = self._to_html(message, title)
            html_part = MIMEText(html_message, 'html')
            msg.attach(html_part)

            # Send
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_address, self.email_password)
                server.send_message(msg)

            logger.info(f"Email sent to {self.email_to}")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("Email auth failed. For Gmail, use an App Password (not your regular password)")
            return False
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    def _to_html(self, message: str, title: Optional[str] = None) -> str:
        """Convert message to basic HTML."""
        html = ['<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">']

        if title:
            html.append(f'<h2 style="color: #333;">{title}</h2>')

        # Convert markdown-ish formatting
        lines = message.split('\n')
        for line in lines:
            if line.startswith('## '):
                html.append(f'<h3 style="color: #444; margin-top: 20px;">{line[3:]}</h3>')
            elif line.startswith('**') and line.endswith('**'):
                html.append(f'<p><strong>{line[2:-2]}</strong></p>')
            elif line.startswith('**'):
                # Bold at start of line
                parts = line.split('**')
                if len(parts) >= 3:
                    html.append(f'<p><strong>{parts[1]}</strong>{parts[2]}</p>')
                else:
                    html.append(f'<p>{line}</p>')
            elif line.startswith('  - '):
                html.append(f'<li style="margin-left: 20px;">{line[4:]}</li>')
            elif line.startswith('- '):
                html.append(f'<li>{line[2:]}</li>')
            elif line.strip():
                html.append(f'<p>{line}</p>')

        html.append('</body></html>')
        return '\n'.join(html)


def get_notifier(notifier_type: Optional[str] = None) -> Notifier:
    """
    Get the appropriate notifier based on configuration.

    Args:
        notifier_type: Override type ('console', 'log', 'email', 'telegram')
                      If None, auto-detects based on environment

    Returns:
        Configured Notifier instance
    """
    if notifier_type:
        notifier_type = notifier_type.lower()
    else:
        # Auto-detect based on environment (priority: email > console)
        if os.getenv('EMAIL_ADDRESS') and os.getenv('EMAIL_PASSWORD'):
            notifier_type = 'email'
        else:
            notifier_type = 'console'

    if notifier_type == 'email':
        return EmailNotifier()
    elif notifier_type == 'log':
        return LogNotifier()
    else:
        return ConsoleNotifier()


def send_morning_brief(brief: str) -> bool:
    """
    Send the morning brief via configured notifier.

    Args:
        brief: The morning brief content

    Returns:
        True if sent successfully
    """
    today = date.today()
    title = f"Training Brief - {today.strftime('%A, %B %d')}"

    notifier = get_notifier()
    return notifier.send(brief, title)


# For testing
if __name__ == "__main__":
    test_brief = """## Morning Brief - Monday, 2026-01-05

**Recovery:** RHR=42 | BB=75 | Readiness=68 (MODERATE)

**Yesterday:** Completed planned long run

**Pillars:** All compliant

**Today:** Strength Training
  Duration: 45 mins

**Next Event:** Cape Town Half Marathon in 69 days
"""

    send_morning_brief(test_brief)
