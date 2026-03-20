"""Brand-consistent HTML templates for server-rendered pages and emails.

Design system reference: /DESIGN_SYSTEM.md
- Background: #000000
- Text: #fafafa
- Muted: #888888
- Surface: #111111
- Code bg: #0D0D0D
- Borders: rgba(250,250,250,0.06)
- Button: bg #FFFFFF, text #000000, radius 7px
- Fonts: Archivo (body), JetBrains Mono (code)
"""
from __future__ import annotations

# ── Shared constants ──────────────────────────────────────────────

_FONT_STACK = "'Archivo', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
_MONO_STACK = "'JetBrains Mono', 'Courier New', monospace"


# ── Server-rendered HTML pages ────────────────────────────────────

def brand_page(title: str, body_html: str) -> str:
    """Wrap body_html in a brand-consistent full HTML page.

    Loads Archivo + JetBrains Mono via Google Fonts.
    Black background, white text, centered 460px max-width.
    Logo at top, footer at bottom.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: {_FONT_STACK};
            background: #000000;
            color: #fafafa;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 0 20px;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }}
        .container {{
            width: 100%;
            max-width: 460px;
            margin: 0 auto;
            padding-top: 60px;
            flex: 1;
        }}
        .logo {{
            font-family: {_FONT_STACK};
            font-weight: 900;
            font-size: 20px;
            color: #ffffff;
            letter-spacing: -0.5px;
            margin-bottom: 48px;
        }}
        h1 {{
            font-family: {_FONT_STACK};
            font-weight: 700;
            font-size: 28px;
            color: #ffffff;
            margin-bottom: 12px;
        }}
        p {{
            font-size: 16px;
            color: #888888;
            line-height: 1.6;
            margin-bottom: 16px;
        }}
        .code {{
            font-family: {_MONO_STACK};
            font-size: 28px;
            letter-spacing: 3px;
            background: #0D0D0D;
            color: #fafafa;
            padding: 10px 20px;
            border-radius: 4px;
            display: inline-block;
            border: 1px solid rgba(250,250,250,0.06);
        }}
        input[type="email"] {{
            width: 100%;
            padding: 14px 16px;
            font-family: {_FONT_STACK};
            font-size: 16px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            color: #fafafa;
            outline: none;
            transition: border-color 200ms;
        }}
        input[type="email"]::placeholder {{
            color: rgba(255,255,255,0.3);
        }}
        input[type="email"]:focus {{
            border-color: rgba(255,255,255,0.2);
        }}
        button {{
            margin-top: 16px;
            padding: 14px 32px;
            font-family: {_FONT_STACK};
            font-size: 16px;
            font-weight: 600;
            background: #ffffff;
            color: #000000;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            transition: box-shadow 200ms, transform 200ms;
        }}
        button:hover {{
            box-shadow: 0 0 20px rgba(255,255,255,0.1);
            transform: scale(1.02);
        }}
        button:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }}
        /* ── Animated checkmark (success) ── */
        .checkmark-wrap {{
            width: 80px;
            height: 80px;
            margin: 0 auto 24px;
        }}
        .checkmark-circle {{
            stroke: #22C55E;
            stroke-width: 2;
            fill: none;
            stroke-dasharray: 251;
            stroke-dashoffset: 251;
            animation: checkmark-circle 0.6s ease-out forwards;
        }}
        .checkmark-check {{
            stroke: #22C55E;
            stroke-width: 3;
            fill: none;
            stroke-dasharray: 50;
            stroke-dashoffset: 50;
            animation: checkmark-check 0.4s 0.4s ease-out forwards;
            stroke-linecap: round;
            stroke-linejoin: round;
        }}
        @keyframes checkmark-circle {{
            to {{ stroke-dashoffset: 0; }}
        }}
        @keyframes checkmark-check {{
            to {{ stroke-dashoffset: 0; }}
        }}

        /* ── Animated X mark (error) ── */
        .xmark-wrap {{
            width: 80px;
            height: 80px;
            margin: 0 auto 24px;
        }}
        .xmark-circle {{
            stroke: #EF4444;
            stroke-width: 2;
            fill: none;
            stroke-dasharray: 251;
            stroke-dashoffset: 251;
            animation: checkmark-circle 0.6s ease-out forwards;
        }}
        .xmark-line {{
            stroke: #EF4444;
            stroke-width: 3;
            fill: none;
            stroke-dasharray: 30;
            stroke-dashoffset: 30;
            animation: xmark-line 0.3s 0.4s ease-out forwards;
            stroke-linecap: round;
        }}
        @keyframes xmark-line {{
            to {{ stroke-dashoffset: 0; }}
        }}

        /* ── Fade-in animation ── */
        .fade-in {{
            opacity: 0;
            animation: fadeIn 0.5s 0.2s ease-out forwards;
        }}
        @keyframes fadeIn {{
            to {{ opacity: 1; }}
        }}

        /* ── Confetti animation ── */
        .confetti-wrap {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            overflow: hidden;
            z-index: 10;
        }}
        .confetti {{
            position: absolute;
            width: 8px;
            height: 8px;
            border-radius: 2px;
            animation: float-up 2.5s ease-out forwards;
        }}
        @keyframes float-up {{
            0% {{ transform: translateY(100vh) rotate(0deg); opacity: 1; }}
            80% {{ opacity: 1; }}
            100% {{ transform: translateY(-20vh) rotate(720deg); opacity: 0; }}
        }}

        /* ── Respect reduced motion ── */
        @media (prefers-reduced-motion: reduce) {{
            .checkmark-circle,
            .checkmark-check,
            .xmark-circle,
            .xmark-line,
            .confetti,
            .fade-in {{
                animation: none !important;
                stroke-dashoffset: 0 !important;
                opacity: 1 !important;
            }}
        }}

        /* ── Action buttons (inline styles are fine, but define link variant) ── */
        .btn-primary {{
            display: inline-block;
            margin-top: 24px;
            padding: 14px 32px;
            font-family: {_FONT_STACK};
            font-size: 16px;
            font-weight: 600;
            background: #ffffff;
            color: #000000;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            text-decoration: none;
            transition: box-shadow 200ms, transform 200ms;
        }}
        .btn-primary:hover {{
            box-shadow: 0 0 20px rgba(255,255,255,0.1);
            transform: scale(1.02);
        }}
        .btn-secondary {{
            display: inline-block;
            margin-top: 12px;
            padding: 10px 24px;
            font-family: {_FONT_STACK};
            font-size: 14px;
            font-weight: 500;
            color: #888888;
            text-decoration: none;
            transition: color 200ms;
        }}
        .btn-secondary:hover {{
            color: #fafafa;
        }}
        .error-code {{
            font-family: {_MONO_STACK};
            font-size: 13px;
            background: #0D0D0D;
            color: #EF4444;
            padding: 12px 16px;
            border-radius: 6px;
            border: 1px solid rgba(239,68,68,0.2);
            margin: 16px 0;
            word-break: break-all;
        }}
        .plan-detail {{
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid rgba(250,250,250,0.06);
            font-size: 15px;
        }}
        .plan-detail span:first-child {{
            color: #888888;
        }}
        .plan-detail span:last-child {{
            color: #fafafa;
            font-weight: 600;
        }}
        .footer {{
            width: 100%;
            max-width: 460px;
            text-align: center;
            padding: 40px 0 24px;
            font-size: 12px;
            color: #888888;
        }}
        .footer a {{
            color: #888888;
            text-decoration: none;
        }}
        .footer a:hover {{
            color: #fafafa;
        }}
        a {{
            color: #fafafa;
        }}
        .result-msg {{
            padding: 16px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(250,250,250,0.06);
            border-radius: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo"><img src="https://docs.cueapi.ai/images/cueapi-logo.png" alt="CueAPI" height="32"></div>
        {body_html}
    </div>
    <div class="footer">&copy; 2026 Vector Apps Inc. &middot; <a href="https://cueapi.ai/privacy">Privacy</a> &middot; <a href="https://cueapi.ai/terms">Terms</a> &middot; <a href="https://cueapi.ai/security">Security</a></div>
</body>
</html>"""


# ── Email templates (table-based for email clients) ───────────────

def brand_email(title: str, body_html: str) -> str:
    """Wrap body_html in a brand-consistent email template.

    Table-based layout for maximum email client compatibility.
    Black background, Archivo font with system fallbacks.
    Logo at top, footer at bottom, 460px max-width centered.
    """
    return f"""\
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#000000;margin:0;padding:0;">
  <tr>
    <td align="center" style="padding:40px 20px;">
      <table width="460" cellpadding="0" cellspacing="0" border="0" style="max-width:460px;width:100%;">
        <!-- Logo -->
        <tr>
          <td style="padding-bottom:32px;">
            <img src="https://docs.cueapi.ai/images/cueapi-logo.png" alt="CueAPI" height="32" style="display:block;margin-bottom:24px;">
          </td>
        </tr>
        <!-- Title -->
        <tr>
          <td style="padding-bottom:24px;">
            <h1 style="margin:0;font-family:{_FONT_STACK};font-weight:700;font-size:28px;color:#ffffff;line-height:1.3;">{title}</h1>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="font-family:{_FONT_STACK};font-size:16px;color:#888888;line-height:1.6;">
            {body_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:40px;border-top:1px solid #222222;padding-top:24px;">
              <tr>
                <td style="font-family:{_FONT_STACK};font-size:12px;color:#333333;line-height:1.6;text-align:center;">
                  CueAPI by Vector Apps Inc.<br>
                  455 Market St Ste 1940 PMB 203190<br>
                  San Francisco, California 94105-2448 US<br><br>
                  &copy; 2026 Vector Apps Inc. All rights reserved.<br><br>
                  <a href="https://cueapi.ai/privacy" style="color:#555555;text-decoration:underline;">Privacy Policy</a> &middot;
                  <a href="https://cueapi.ai/terms" style="color:#555555;text-decoration:underline;">Terms of Service</a> &middot;
                  <a href="https://cueapi.ai/security" style="color:#555555;text-decoration:underline;">Security</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def email_button(text: str, url: str) -> str:
    """Render a brand-consistent CTA button for emails."""
    return (
        f'<a href="{url}" style="display:inline-block;padding:14px 32px;'
        f"background-color:#ffffff;color:#000000;font-family:{_FONT_STACK};"
        f'font-size:16px;font-weight:600;text-decoration:none;border-radius:7px;">'
        f"{text}</a>"
    )


def email_code(text: str) -> str:
    """Render inline code in email."""
    return (
        f"<code style=\"font-family:'JetBrains Mono','Courier New',monospace;"
        f'background-color:#111111;padding:2px 8px;border-radius:4px;'
        f'color:#ffffff;font-size:14px;">{text}</code>'
    )


def email_paragraph(text: str) -> str:
    """Render a paragraph with consistent spacing."""
    return f'<p style="margin:0 0 16px 0;font-family:{_FONT_STACK};font-size:16px;color:#888888;line-height:1.6;">{text}</p>'


def email_heading(text: str) -> str:
    """Render a sub-heading in email body."""
    return f'<p style="margin:0 0 8px 0;font-family:{_FONT_STACK};font-size:16px;font-weight:600;color:#ffffff;">{text}</p>'


def worker_down_email_body(worker_id: str, minutes_offline: int, pending_count: int) -> str:
    """Build HTML body for worker-down alert email."""
    parts = [
        email_paragraph(
            f"Your worker {email_code(worker_id)} has been offline for "
            f"<strong style='color:#fafafa;'>{minutes_offline} minutes</strong>."
        ),
    ]
    if pending_count > 0:
        parts.append(
            email_paragraph(
                f"There {'is' if pending_count == 1 else 'are'} "
                f"<strong style='color:#fafafa;'>{pending_count}</strong> pending "
                f"execution{'s' if pending_count != 1 else ''} waiting for a worker."
            )
        )
    parts.append(
        email_paragraph(
            "Check that the worker process is running and can reach the CueAPI server. "
            "If the worker crashed, restart it with:"
        )
    )
    parts.append(
        f'<p style="margin:0 0 24px 0;">{email_code("cueapi-worker start --config cueapi-worker.yaml")}</p>'
    )
    parts.append(
        f'<p style="margin:24px 0;">{email_button("View Dashboard", "https://dashboard.cueapi.ai")}</p>'
    )
    return "\n".join(parts)
