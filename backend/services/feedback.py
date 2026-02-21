"""
Feedback service for handling user bug reports and feature requests.
Stores feedback in a JSON file and optionally sends email notifications.

Supports two email methods:
1. Resend API (recommended) - set RESEND_API_KEY
2. SMTP (fallback) - set SMTP_HOST, SMTP_USER, SMTP_PASSWORD
"""

import json
import logging
import os
import smtplib
import uuid
import httpx
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from ..models import FeedbackRequest, FeedbackResponse

logger = logging.getLogger("openecon.feedback")

# Feedback storage directory
FEEDBACK_DIR = Path(__file__).parent.parent / "data" / "feedback"

# Email configuration (read from environment)
FEEDBACK_EMAIL_TO = os.getenv("FEEDBACK_EMAIL_TO", "hanlulong@gmail.com")

# Resend API (recommended - simpler setup)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM = os.getenv("RESEND_FROM", "econ-data-mcp <feedback@openecon.ai>")

# SMTP configuration (fallback)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@openecon.ai")


class FeedbackService:
    """Service for handling user feedback submissions."""

    def __init__(self):
        # Ensure feedback directory exists
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        self._feedback_file = FEEDBACK_DIR / "feedback.json"
        self._load_feedback()

    def _load_feedback(self):
        """Load existing feedback from file."""
        if self._feedback_file.exists():
            try:
                with open(self._feedback_file, "r") as f:
                    self._feedback = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load feedback file: {e}")
                self._feedback = []
        else:
            self._feedback = []

    def _save_feedback(self):
        """Save feedback to file."""
        try:
            with open(self._feedback_file, "w") as f:
                json.dump(self._feedback, f, indent=2, default=str)
        except IOError as e:
            logger.error(f"Failed to save feedback: {e}")
            raise

    def submit_feedback(self, request: FeedbackRequest) -> FeedbackResponse:
        """
        Submit user feedback.

        Args:
            request: The feedback request containing type, message, and optional session info

        Returns:
            FeedbackResponse with success status and feedback ID
        """
        feedback_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        # Create feedback entry
        feedback_entry = {
            "id": feedback_id,
            "type": request.type,
            "message": request.message,
            "email": request.email,
            "userId": request.userId,
            "userName": request.userName,
            "timestamp": timestamp,
            "sessionInfo": request.sessionInfo.model_dump() if request.sessionInfo else None,
            "conversation": request.conversation.model_dump() if request.conversation else None,
        }

        # Save to file
        self._feedback.append(feedback_entry)
        self._save_feedback()

        logger.info(f"Feedback submitted: {feedback_id} (type={request.type})")

        # Try to send email notification
        email_sent = self._send_email_notification(feedback_entry)
        if email_sent:
            logger.info(f"Email notification sent for feedback {feedback_id}")
        else:
            logger.info(f"Email notification skipped (SMTP not configured) for feedback {feedback_id}")

        return FeedbackResponse(
            success=True,
            message="Thank you for your feedback! We'll review it and get back to you if needed.",
            feedbackId=feedback_id,
        )

    def _send_email_notification(self, feedback: dict) -> bool:
        """
        Send email notification about new feedback.
        Tries Resend API first, falls back to SMTP.

        Args:
            feedback: The feedback entry dictionary

        Returns:
            True if email was sent, False otherwise
        """
        # Try Resend API first (recommended)
        if RESEND_API_KEY:
            return self._send_via_resend(feedback)

        # Fall back to SMTP
        if all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
            return self._send_via_smtp(feedback)

        logger.warning("No email service configured. Set RESEND_API_KEY or SMTP_* environment variables.")
        return False

    def _send_via_resend(self, feedback: dict) -> bool:
        """Send email via Resend API."""
        try:
            subject = f"[econ-data-mcp Feedback] {feedback['type'].upper()}: {feedback['id'][:8]}"
            html_body = self._format_feedback_html(feedback)
            text_body = self._format_feedback_text(feedback)

            # Build email payload
            email_payload = {
                "from": RESEND_FROM,
                "to": [FEEDBACK_EMAIL_TO],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            }

            # Add reply-to if user provided email
            user_email = feedback.get("email")
            if user_email:
                email_payload["reply_to"] = user_email

            response = httpx.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=email_payload,
                timeout=10.0,
            )

            if response.status_code == 200:
                logger.info(f"Email sent via Resend: {response.json()}")
                return True
            else:
                logger.error(f"Resend API error: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            logger.error(f"Failed to send email via Resend: {e}")
            return False

    def _send_via_smtp(self, feedback: dict) -> bool:
        """Send email via SMTP."""
        try:
            # Create email message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[econ-data-mcp Feedback] {feedback['type'].upper()}: {feedback['id'][:8]}"
            msg["From"] = SMTP_FROM
            msg["To"] = FEEDBACK_EMAIL_TO

            # Create plain text body
            text_body = self._format_feedback_text(feedback)

            # Create HTML body
            html_body = self._format_feedback_html(feedback)

            msg.attach(MIMEText(text_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            # Send email
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, FEEDBACK_EMAIL_TO, msg.as_string())

            logger.info("Email sent via SMTP")
            return True

        except Exception as e:
            logger.error(f"Failed to send email via SMTP: {e}")
            return False

    def _format_feedback_text(self, feedback: dict) -> str:
        """Format feedback as plain text for email."""
        lines = [
            f"New Feedback Received",
            f"=====================",
            f"",
            f"Type: {feedback['type'].upper()}",
            f"ID: {feedback['id']}",
            f"Time: {feedback['timestamp']}",
            f"",
        ]

        if feedback.get("userName"):
            lines.append(f"User: {feedback['userName']}")
        if feedback.get("email"):
            lines.append(f"Email: {feedback['email']}")
        if feedback.get("userId"):
            lines.append(f"User ID: {feedback['userId']}")

        lines.append("")
        lines.append("Message:")
        lines.append("-" * 40)
        lines.append(feedback.get("message") or "(No message provided)")
        lines.append("")

        if feedback.get("sessionInfo"):
            session = feedback["sessionInfo"]
            lines.append("Session Info:")
            lines.append("-" * 40)
            lines.append(f"  URL: {session.get('url')}")
            lines.append(f"  Browser: {session.get('userAgent')}")
            lines.append(f"  Screen: {session.get('screenSize')}")
            lines.append(f"  Language: {session.get('language')}")
            lines.append(f"  Timezone: {session.get('timezone')}")
            lines.append("")

        if feedback.get("conversation"):
            conv = feedback["conversation"]
            lines.append("Conversation:")
            lines.append("-" * 40)
            lines.append(f"  Message Count: {conv.get('messageCount')}")
            lines.append(f"  Conversation ID: {conv.get('conversationId')}")
            lines.append("")
            lines.append("Messages:")
            lines.append(conv.get("messages") or "(No messages)")
            lines.append("")

        return "\n".join(lines)

    def _format_feedback_html(self, feedback: dict) -> str:
        """Format feedback as HTML for email."""
        feedback_type_colors = {
            "bug": "#dc2626",
            "feature": "#10b981",
            "other": "#6b7280",
        }
        type_color = feedback_type_colors.get(feedback["type"], "#6b7280")

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #1f2937; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #4f46e5, #7c3aed); color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
                .header h1 {{ margin: 0; font-size: 20px; }}
                .badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; background: {type_color}; color: white; }}
                .content {{ background: #f9fafb; padding: 20px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 8px 8px; }}
                .section {{ margin-bottom: 20px; }}
                .section h3 {{ color: #374151; margin: 0 0 10px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; }}
                .message-box {{ background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 15px; white-space: pre-wrap; }}
                .info-grid {{ display: grid; grid-template-columns: 120px 1fr; gap: 8px; font-size: 14px; }}
                .info-label {{ color: #6b7280; }}
                .info-value {{ color: #1f2937; }}
                .conversation-box {{ background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 15px; font-size: 13px; white-space: pre-wrap; max-height: 300px; overflow-y: auto; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <span class="badge">{feedback['type']}</span>
                    <h1 style="margin-top: 10px;">New econ-data-mcp Feedback</h1>
                    <p style="margin: 5px 0 0; opacity: 0.9; font-size: 14px;">{feedback['timestamp']}</p>
                </div>
                <div class="content">
                    <div class="section">
                        <h3>Details</h3>
                        <div class="info-grid">
                            <span class="info-label">Feedback ID:</span>
                            <span class="info-value">{feedback['id']}</span>
        """

        if feedback.get("userName"):
            html += f"""
                            <span class="info-label">User:</span>
                            <span class="info-value">{feedback['userName']}</span>
            """

        if feedback.get("email"):
            html += f"""
                            <span class="info-label">Email:</span>
                            <span class="info-value"><a href="mailto:{feedback['email']}">{feedback['email']}</a></span>
            """

        if feedback.get("userId"):
            html += f"""
                            <span class="info-label">User ID:</span>
                            <span class="info-value" style="font-family: monospace;">{feedback['userId']}</span>
            """

        html += """
                        </div>
                    </div>

                    <div class="section">
                        <h3>Message</h3>
                        <div class="message-box">
        """

        message = feedback.get("message") or "(No message provided)"
        html += f"{self._escape_html(message)}"

        html += """
                        </div>
                    </div>
        """

        if feedback.get("sessionInfo"):
            session = feedback["sessionInfo"]
            html += f"""
                    <div class="section">
                        <h3>Session Info</h3>
                        <div class="info-grid">
                            <span class="info-label">URL:</span>
                            <span class="info-value" style="word-break: break-all;">{self._escape_html(session.get('url', 'N/A'))}</span>
                            <span class="info-label">Screen:</span>
                            <span class="info-value">{self._escape_html(session.get('screenSize', 'N/A'))}</span>
                            <span class="info-label">Language:</span>
                            <span class="info-value">{self._escape_html(session.get('language', 'N/A'))}</span>
                            <span class="info-label">Timezone:</span>
                            <span class="info-value">{self._escape_html(session.get('timezone', 'N/A'))}</span>
                            <span class="info-label">Browser:</span>
                            <span class="info-value" style="font-size: 12px; word-break: break-all;">{self._escape_html(session.get('userAgent', 'N/A'))}</span>
                        </div>
                    </div>
            """

        if feedback.get("conversation"):
            conv = feedback["conversation"]
            html += f"""
                    <div class="section">
                        <h3>Conversation ({conv.get('messageCount', 0)} messages)</h3>
                        <p style="font-size: 12px; color: #6b7280; margin-bottom: 10px;">Conversation ID: {self._escape_html(conv.get('conversationId') or 'N/A')}</p>
                        <div class="conversation-box">{self._escape_html(conv.get('messages') or '(No messages)')}</div>
                    </div>
            """

        html += """
                </div>
            </div>
        </body>
        </html>
        """

        return html

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters."""
        if not text:
            return ""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    def get_all_feedback(self) -> list:
        """Get all feedback entries (for admin use)."""
        return self._feedback.copy()

    def get_feedback_by_id(self, feedback_id: str) -> Optional[dict]:
        """Get a specific feedback entry by ID."""
        for entry in self._feedback:
            if entry["id"] == feedback_id:
                return entry
        return None


# Singleton instance
feedback_service = FeedbackService()
