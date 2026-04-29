import smtplib
import os
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_SMTP_SERVER = "smtp.gmail.com"
DEFAULT_SMTP_PORT   = 587
DEFAULT_SMTP_USER   = ""   # Must be set in Super Admin → SMTP Settings
DEFAULT_SMTP_PASS   = ""

BRAND_COLOR   = "#6366f1"
BRAND_NAME    = "Sphoorthy EventSphere"
COLLEGE_NAME  = "Sphoorthy Engineering College"

# Global executor for async mailing to prevent resource exhaustion
_email_executor = ThreadPoolExecutor(max_workers=10)

# ── HTML base template ─────────────────────────────────────────────────────────
def _html_wrap(title: str, body_html: str, cta_url: str = None, cta_label: str = None) -> str:
    cta_block = ""
    if cta_url and cta_label:
        cta_block = f"""
        <div style="text-align:center;margin:32px 0;">
          <a href="{cta_url}"
             style="background:{BRAND_COLOR};color:#fff;padding:14px 32px;border-radius:8px;
                    text-decoration:none;font-weight:700;font-size:15px;display:inline-block;">
            {cta_label}
          </a>
        </div>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f8;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:32px 16px;">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:16px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(0,0,0,.10);max-width:600px;width:100%;">
        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,{BRAND_COLOR},#8b5cf6);padding:32px 40px;text-align:center;">
            <h1 style="margin:0;color:#fff;font-size:22px;font-weight:800;letter-spacing:-0.5px;">
              🎓 {BRAND_NAME}
            </h1>
            <p style="margin:6px 0 0;color:rgba(255,255,255,.75);font-size:13px;">{COLLEGE_NAME}</p>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:36px 40px;">
            {body_html}
            {cta_block}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#f9f9fb;border-top:1px solid #ebebef;
                     padding:20px 40px;text-align:center;">
            <p style="margin:0;color:#888;font-size:12px;">
              © {COLLEGE_NAME} · {BRAND_NAME}<br>
              This is an automated message — please do not reply.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── SMTP helpers ───────────────────────────────────────────────────────────────
class Mailer:

    # ── Config resolution ──────────────────────────────────────────────────────
    @staticmethod
    def _get_smtp_settings(club_id: str = None):
        """
        Priority: club smtp_config → global settings.json → env vars → defaults.
        User only needs to supply email + app-password (SMTP server is pre-set to Gmail).
        """
        # 1. Club-level config
        if club_id:
            try:
                from app.models import DB
                club = DB.get_club_by_id(club_id)
                if club:
                    cfg = club.get('smtp_config', {})
                    if cfg.get('user') and cfg.get('password'):
                        return (
                            cfg.get('server', DEFAULT_SMTP_SERVER),
                            int(cfg.get('port', DEFAULT_SMTP_PORT)),
                            cfg.get('user'),
                            cfg.get('password'),
                        )
            except Exception:
                pass

        # 2. Global settings.json (super-admin managed)
        try:
            import json, os as _os
            from app.models import DATA_DIR
            settings_path = _os.path.join(DATA_DIR, 'em', 'settings.json')
            if _os.path.exists(settings_path):
                with open(settings_path) as f:
                    s = json.load(f)
                smtp_user = s.get('smtp_user') or s.get('smtp_email', '')
                smtp_pass = s.get('smtp_password', '')
                if smtp_user and smtp_pass:
                    return (
                        s.get('smtp_server', DEFAULT_SMTP_SERVER),
                        int(s.get('smtp_port', DEFAULT_SMTP_PORT)),
                        smtp_user,
                        smtp_pass,
                    )
        except Exception:
            pass

        # 3. Environment variables
        env_user = os.environ.get('SMTP_USER', DEFAULT_SMTP_USER)
        env_pass = os.environ.get('SMTP_PASS', DEFAULT_SMTP_PASS)
        return (DEFAULT_SMTP_SERVER, DEFAULT_SMTP_PORT, env_user, env_pass)

    # ── Core send ──────────────────────────────────────────────────────────────
    @staticmethod
    def send_email(
        to_email:        str,
        subject:         str,
        body:            str,
        html_body:       str  = None,
        image_path:      str  = None,   # embedded CID image (QR)
        club_id:         str  = None,
        attachment_path: str  = None,   # generic attachment
        extra_attachments: list = None, # list of (path, display_name)
    ) -> bool:
        try:
            server_addr, port, user, password = Mailer._get_smtp_settings(club_id)
            if not user or not password:
                print(f"[Mailer] SMTP not configured (club={club_id}). Email skipped.")
                return False

            msg = MIMEMultipart('mixed')
            msg['From']    = f"{BRAND_NAME} <{user}>"
            msg['To']      = to_email
            msg['Subject'] = subject

            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(body, 'plain'))
            if html_body:
                alt.attach(MIMEText(html_body, 'html'))
            msg.attach(alt)

            # Embedded CID image (QR code shown inline in HTML)
            if image_path and os.path.exists(image_path):
                with open(image_path, 'rb') as f:
                    img = MIMEImage(f.read())
                    img.add_header('Content-ID', '<qr_code>')
                    img.add_header('Content-Disposition', 'inline', filename='qr_code.png')
                    msg.attach(img)

            # Generic attachment
            if attachment_path and os.path.exists(attachment_path):
                Mailer._attach_file(msg, attachment_path)

            # Extra attachments list
            for item in (extra_attachments or []):
                if isinstance(item, tuple):
                    path, name = item
                else:
                    path, name = item, os.path.basename(item)
                if os.path.exists(path):
                    Mailer._attach_file(msg, path, name)

            smtp = smtplib.SMTP(server_addr, port, timeout=15)
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
            smtp.quit()
            return True
        except Exception as e:
            print(f"[Mailer] Error sending to {to_email} (club={club_id}): {e}")
            return False

    @staticmethod
    def _attach_file(msg, path, display_name=None):
        name = display_name or os.path.basename(path)
        with open(path, 'rb') as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{name}"')
        msg.attach(part)

    # ── Async wrapper ──────────────────────────────────────────────────────────
    @staticmethod
    def send_async(to_email, subject, body, html_body=None, image_path=None,
                   club_id=None, attachment_path=None, extra_attachments=None):
        """Fire-and-forget (non-blocking) using managed thread pool."""
        _email_executor.submit(
            Mailer.send_email,
            to_email, subject, body, html_body, image_path, club_id,
            attachment_path, extra_attachments
        )

    # ── Bulk ──────────────────────────────────────────────────────────────────
    @staticmethod
    def send_bulk_email(recipient_list, subject, content, html_content=None, club_id=None):
        """Send emails in bulk using the managed thread pool."""
        for email in recipient_list:
            Mailer.send_async(email, subject, content, html_content, club_id=club_id)

    # ══════════════════════════════════════════════════════════════════════════
    #  PURPOSE-BUILT EMAIL HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    # ── 1. Registration confirmation + QR ─────────────────────────────────────
    @staticmethod
    def send_registration_confirmation(reg: dict, qr_image_path: str = None):
        """
        Sent to the student after successful event registration.
        Attaches the QR code PNG.
        """
        name       = reg.get('name', 'Student')
        email      = reg.get('email')
        event      = reg.get('event_title', 'the event')
        reg_id     = reg.get('id', '')
        club_id    = reg.get('club_id')
        reg_type   = reg.get('reg_type', 'individual')
        team_name  = reg.get('team_name', '')

        if not email:
            return

        team_row = ""
        if reg_type == 'team' and team_name:
            team_row = f"<tr><td style='color:#888;padding:6px 0;'>Team</td><td style='font-weight:600;'>{team_name}</td></tr>"

        body_html = _html_wrap(
            title=f"Registration Confirmed – {event}",
            body_html=f"""
            <h2 style="margin:0 0 8px;color:#1a1a2e;font-size:20px;">🎉 You're Registered!</h2>
            <p style="color:#444;margin:0 0 24px;">Hi <strong>{name}</strong>, your spot for
            <strong>{event}</strong> is confirmed. Show the QR code attached to this email
            at the venue entrance to mark your attendance.</p>

            <table style="width:100%;border-collapse:collapse;background:#f9f9fb;
                          border-radius:10px;overflow:hidden;font-size:14px;">
              <tr><td style="color:#888;padding:8px 16px;">Name</td>
                  <td style="font-weight:600;padding:8px 16px;">{name}</td></tr>
              <tr style="background:#f0f0f5;">
                  <td style="color:#888;padding:8px 16px;">Roll Number</td>
                  <td style="font-weight:600;padding:8px 16px;">{reg.get('roll_number','—')}</td></tr>
              <tr><td style="color:#888;padding:8px 16px;">Department</td>
                  <td style="font-weight:600;padding:8px 16px;">{reg.get('department','—')}</td></tr>
              {team_row}
              <tr style="background:#f0f0f5;">
                  <td style="color:#888;padding:8px 16px;">Event</td>
                  <td style="font-weight:600;padding:8px 16px;">{event}</td></tr>
              <tr><td style="color:#888;padding:8px 16px;">Registration ID</td>
                  <td style="font-family:monospace;font-size:12px;padding:8px 16px;">{reg_id}</td></tr>
            </table>

            <p style="color:#888;font-size:13px;margin-top:24px;">
              ⚠️ Do <strong>not</strong> share your QR code. It is unique to you and will be
              scanned to verify your attendance.
            </p>
            """,
        )

        plain = (
            f"Hi {name},\n\nYou are registered for {event}.\n"
            f"Registration ID: {reg_id}\n\n"
            "Please show the attached QR code at the venue.\n\n"
            f"– {BRAND_NAME}"
        )

        Mailer.send_async(
            to_email=email,
            subject=f"✅ Registration Confirmed: {event}",
            body=plain,
            html_body=body_html,
            image_path=qr_image_path,
            club_id=club_id,
        )

    # ── 2. Payment verification confirmation ──────────────────────────────────
    @staticmethod
    def send_payment_verified(reg: dict, qr_image_path: str = None):
        name    = reg.get('name', 'Student')
        email   = reg.get('email')
        event   = reg.get('event_title', 'the event')
        club_id = reg.get('club_id')

        if not email:
            return

        body_html = _html_wrap(
            title=f"Payment Verified – {event}",
            body_html=f"""
            <h2 style="margin:0 0 8px;color:#1a1a2e;">💳 Payment Verified!</h2>
            <p style="color:#444;margin:0 0 24px;">Hi <strong>{name}</strong>,
            your payment for <strong>{event}</strong> has been successfully verified.
            Your QR code (attached) is now active for attendance.</p>
            <p style="color:#888;font-size:13px;">If you have any questions, reach out to your club admin.</p>
            """,
        )

        plain = (
            f"Hi {name},\n\nYour payment for {event} has been verified.\n"
            "Please use the attached QR code at the venue.\n\n"
            f"– {BRAND_NAME}"
        )

        Mailer.send_async(
            to_email=email,
            subject=f"💳 Payment Verified: {event}",
            body=plain,
            html_body=body_html,
            image_path=qr_image_path,
            club_id=club_id,
        )

    # ── 3. Report submitted → super admin notification ─────────────────────────
    @staticmethod
    def send_report_submitted_to_admin(event: dict, club: dict, admin_email: str,
                                        review_url: str):
        club_name  = club.get('name', 'A Club')
        event_name = event.get('title', 'an event')

        body_html = _html_wrap(
            title="New Event Report Submitted",
            body_html=f"""
            <h2 style="margin:0 0 8px;color:#1a1a2e;">📋 New Report Awaiting Review</h2>
            <p style="color:#444;margin:0 0 20px;">
              <strong>{club_name}</strong> has submitted an event report for
              <strong>{event_name}</strong>. Please review and verify it.
            </p>
            <table style="width:100%;border-collapse:collapse;background:#f9f9fb;
                          border-radius:10px;font-size:14px;">
              <tr><td style="color:#888;padding:8px 16px;">Club</td>
                  <td style="font-weight:600;padding:8px 16px;">{club_name}</td></tr>
              <tr style="background:#f0f0f5;">
                  <td style="color:#888;padding:8px 16px;">Event</td>
                  <td style="font-weight:600;padding:8px 16px;">{event_name}</td></tr>
              <tr><td style="color:#888;padding:8px 16px;">Date</td>
                  <td style="padding:8px 16px;">{event.get('date','—')}</td></tr>
            </table>
            """,
            cta_url=review_url,
            cta_label="Review Report →",
        )

        plain = (
            f"{club_name} has submitted a report for {event_name}.\n"
            f"Review it here: {review_url}\n\n– {BRAND_NAME}"
        )

        Mailer.send_async(
            to_email=admin_email,
            subject=f"📋 Report Submitted: {event_name} by {club_name}",
            body=plain,
            html_body=body_html,
        )

    # ── 4. Report approved → club admin notification ───────────────────────────
    @staticmethod
    def send_report_approved_to_club(event: dict, club: dict, club_admin_email: str):
        club_name  = club.get('name', 'Your Club')
        event_name = event.get('title', 'your event')

        body_html = _html_wrap(
            title="Event Report Approved",
            body_html=f"""
            <h2 style="margin:0 0 8px;color:#1a1a2e;">✅ Report Approved!</h2>
            <p style="color:#444;margin:0 0 20px;">
              Congratulations, <strong>{club_name}</strong>! Your event report for
              <strong>{event_name}</strong> has been reviewed and approved by the
              Super Admin.
            </p>
            <p style="color:#444;">You are now eligible to create your next event.
              Keep up the great work! 🎉</p>
            """,
        )

        plain = (
            f"Your report for {event_name} has been approved!\n"
            f"You may now create your next event.\n\n– {BRAND_NAME}"
        )

        Mailer.send_async(
            to_email=club_admin_email,
            subject=f"✅ Report Approved: {event_name}",
            body=plain,
            html_body=body_html,
        )

    # ── 5. New event promotional blast to past registrants ────────────────────
    @staticmethod
    def send_new_event_promo(
        event: dict,
        club: dict,
        recipient_emails: list,
        event_url: str,
        poster_path: str = None,
    ):
        club_name  = club.get('name', 'A Club')
        event_name = event.get('title', 'New Event')
        event_date = event.get('date', 'TBA')
        event_venue= event.get('venue', 'TBA')
        description= event.get('description', '')

        body_html = _html_wrap(
            title=f"New Event: {event_name}",
            body_html=f"""
            <h2 style="margin:0 0 8px;color:#1a1a2e;">🚀 New Event Alert!</h2>
            <p style="color:#444;margin:0 0 20px;">
              <strong>{club_name}</strong> is thrilled to announce a new event:
            </p>
            <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);
                        border-radius:12px;padding:24px;color:#fff;margin-bottom:24px;">
              <h3 style="margin:0 0 8px;font-size:20px;">{event_name}</h3>
              <p style="margin:0 0 4px;">📅 {event_date}</p>
              <p style="margin:0;">📍 {event_venue}</p>
            </div>
            {'<p style="color:#444;margin:0 0 20px;">'+description+'</p>' if description else ''}
            {'<p style="color:#888;font-size:13px;">📎 Event poster is attached below.</p>' if poster_path else ''}
            """,
            cta_url=event_url,
            cta_label="Register Now →",
        )

        plain = (
            f"{club_name} is conducting: {event_name}\n"
            f"Date: {event_date} | Venue: {event_venue}\n\n"
            f"{description}\n\n"
            f"Register here: {event_url}\n\n– {BRAND_NAME}"
        )

        extra = [(poster_path, f"poster_{event_name}.jpg")] if poster_path and os.path.exists(poster_path) else None

        for email in recipient_emails:
            Mailer.send_async(
                to_email=email,
                subject=f"🎉 {club_name} is conducting: {event_name}",
                body=plain,
                html_body=body_html,
                club_id=club.get('id'),
                extra_attachments=extra,
            )


    # ── 7. SMTP test ──────────────────────────────────────────────────────────
    @staticmethod
    def test_smtp(club_id=None, to_email: str = None) -> tuple:
        """Returns (success: bool, message: str)"""
        try:
            server_addr, port, user, password = Mailer._get_smtp_settings(club_id)
            if not user or not password:
                return False, "SMTP credentials not configured."
            smtp = smtplib.SMTP(server_addr, port, timeout=10)
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            if to_email:
                msg = MIMEMultipart()
                msg['From'] = user
                msg['To'] = to_email
                msg['Subject'] = f"[{BRAND_NAME}] SMTP Test Successful"
                msg.attach(MIMEText("This is a test email from EventSphere. Your SMTP is configured correctly!", 'plain'))
                smtp.send_message(msg)
            smtp.quit()
            return True, "SMTP connected and authenticated successfully."
        except Exception as e:
            return False, str(e)
