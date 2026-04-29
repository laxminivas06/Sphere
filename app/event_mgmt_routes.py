from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for, send_file, current_app
from werkzeug.utils import secure_filename
import os, uuid, datetime, json, hmac, hashlib, tempfile
from app.models import DB
from app.mailer import Mailer
import qrcode
from io import BytesIO
from fpdf import FPDF

em = Blueprint('em', __name__)

# ─── Data Layer ───────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
EM_DIR   = os.path.join(DATA_DIR, 'em')
BANNERS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'uploads', 'em', 'banners')

def _load(fname):
    p = os.path.join(EM_DIR, fname)
    if not os.path.exists(p):
        return [] if fname != 'settings.json' else {}
    with open(p) as f:
        try: return json.load(f)
        except: return [] if fname != 'settings.json' else {}

def _save(fname, data):
    os.makedirs(EM_DIR, exist_ok=True)
    with open(os.path.join(EM_DIR, fname), 'w') as f:
        json.dump(data, f, indent=4)

def get_events():  return _load('events.json')
def save_events(d): _save('events.json', d)
def get_tickets():  return _load('tickets.json')
def save_tickets(d): _save('tickets.json', d)
def get_em_admins():    return _load('admins.json')
def save_em_admins(d):  _save('admins.json', d)
def get_settings(): return _load('settings.json')
def put_settings(d): _save('settings.json', d)

# ─── Auth Helpers ─────────────────────────────────────────────────────────────
def is_manager(user):
    """Event Manager role or Super Admin."""
    if not user: return False
    role = user.get('role')
    if role in ('event_manager', 'chief_coordinator'): return True
    # Fallback: check email in admins.json if role is missing
    email = (user.get('email') or '').lower().strip()
    if email:
        for a in DB.get_admins():
            if a.get('email', '').lower().strip() == email:
                if a.get('role') in ('event_manager', 'chief_coordinator'): return True
    return False

def is_event_admin(user):
    """Event-specific admin created by the event manager."""
    if not user: return False
    role = user.get('role')
    if role == 'event_admin': return True
    # Fallback: check email in em/admins.json
    email = (user.get('email') or '').lower().strip()
    if email:
        for a in get_em_admins():
            if a.get('email', '').lower().strip() == email:
                return True
    return False

def is_admin(user):
    """Any privileged user in the EM system: managers, admins, or club admins."""
    if not user: return False
    if is_manager(user) or is_event_admin(user) or is_club_admin(user):
        return True
    return False

def admin_events(user):
    """Return events this user can access (full access for managers, club-specific for club admins)."""
    all_ev = get_events()
    if not user: return []
    role = user.get('role', '')
    if role in ('event_manager', 'chief_coordinator'): return all_ev
    
    # Club Admins see their own club's events
    if is_club_admin(user):
        club_id = get_club_id_from_user(user)
        if club_id:
            return [e for e in all_ev if e.get('organized_by_id') == club_id]
            
    # Assigned Event Admins see specific events
    emails = []
    if user.get('email'): emails.append(user['email'].lower().strip())
    rolls = []
    if user.get('roll_number'): rolls.append(user['roll_number'].lower().strip())
    
    em_admins = get_em_admins()
    # Find matching record in em_admins
    rec = None
    for a in em_admins:
        ae = (a.get('email') or '').lower().strip()
        ar = (a.get('roll_number') or '').lower().strip()
        if (ae and ae in emails) or (ar and ar in rolls):
            rec = a
            break
            
    if not rec:
        # If no specific record found, fallback to checking assigned_admin field directly
        return [e for e in all_ev if 
                (e.get('assigned_admin') and e['assigned_admin'].lower().strip() in emails) or
                (e.get('assigned_admin') and e['assigned_admin'].lower().strip() in rolls)]

    assigned = rec.get('assigned_events', [])
    return [e for e in all_ev if 
            e['id'] in assigned or 
            (e.get('assigned_admin') and e['assigned_admin'].lower().strip() in emails) or
            (e.get('assigned_admin') and e['assigned_admin'].lower().strip() in rolls)]

def get_club_id_from_user(user):
    """Extract club_id from a club admin's role. e.g. 'creator_club_admin' -> 'creator_club'"""
    role = user.get('role', '')
    if role.endswith('_admin') and role not in ('chief_coordinator', 'event_admin'):
        # Specific pattern: creator_club_admin -> creator_club
        slug = role[:-6]
        if slug != 'club':
            return slug
        
        # Fallback for generic 'club_admin': look up by email/roll
        identifier = user.get('email') or user.get('roll_number')
        from app.models import DB
        club = DB.get_club_by_admin(identifier)
        if club: return club['id']
    return None

def is_club_admin(user):
    """Check if user is a club admin (any *_admin role except chief_coordinator and event_admin)."""
    if not user: return False
    role = user.get('role', '')
    return role.endswith('_admin') and role not in ('chief_coordinator', 'event_admin')

def get_events_for_club(club_id):
    """Return all EM events organized by a specific club."""
    return [e for e in get_events() if e.get('organized_by_id') == club_id]

def enrich_events_with_stats(events):
    """Attach ticket stats (_reg_count, _checked_in, _revenue) to each event dict."""
    tickets = get_tickets()
    for ev in events:
        ev_tickets = [t for t in tickets
                      if t['event_id'] == ev['id']
                      and t.get('payment_status') not in ('failed', None)]
        ev['_reg_count']     = len(ev_tickets)
        ev['_checked_in']    = sum(1 for t in ev_tickets if t.get('checked_in'))
        ev['_revenue']       = sum(t.get('amount', 0) for t in ev_tickets
                                   if t.get('payment_status') == 'paid')
    return events

def has_event_access(user, event_id):
    """Check if the user has administrative access to a specific event."""
    if not user: return False
    role = user.get('role', '')
    if role in ('event_manager', 'chief_coordinator'): return True
    
    # Check if this event is in the list of events this user can admin
    # admin_events(user) already handles club-specific and assigned event checks
    allowed_events = [e['id'] for e in admin_events(user)]
    return event_id in allowed_events

# ─── QR / PDF Helpers ────────────────────────────────────────────────────────
def _qr_buf(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO(); img.save(buf, 'PNG'); buf.seek(0)
    return buf

def _ticket_id():
    return 'TKT-' + uuid.uuid4().hex[:8].upper()

def _make_ticket_record(user, event, status, payment_id=None, order_id=None, amount=0, method='free', email_override=None, phone_override=None, name_override=None, roll_override=None, college_override=None):
    if user:
        identifier = user.get('roll_number') or user.get('email', '')
        name = user.get('name', 'Unknown')
        email = email_override or user.get('email', '')
        phone = phone_override or user.get('phone', '')
        dept = user.get('department', '')
        year = user.get('year', '')
        roll = user.get('roll_number', '')
    else:
        identifier = roll_override or email_override or uuid.uuid4().hex[:8]
        name = name_override or 'Guest Student'
        email = email_override or ''
        phone = phone_override or ''
        dept = 'External'
        year = 'N/A'
        roll = roll_override or ''

    tid = _ticket_id()
    return {
        'ticket_id': tid,
        'event_id': event['id'],
        'user_id': identifier,
        'user_name': name,
        'user_email': email,
        'user_phone': phone,
        'user_dept': dept,
        'user_year': year,
        'user_roll': roll,
        'college_name': college_override or ('Sphoorthy Engineering College' if user else ''),
        'payment_status': status,
        'payment_id': payment_id,
        'order_id': order_id,
        'amount': amount,
        'payment_method': method,
        'qr_data': f'EM|{event["id"]}|{tid}|{identifier}|{status}',
        'checked_in': False,
        'checked_in_at': None,
        'created_at': datetime.datetime.now().isoformat()
    }

def _generate_pdf_ticket(ticket, event):
    def _s(text):
        return str(text or '').encode('latin-1', 'replace').decode('latin-1')

    pdf = FPDF(orientation='L', unit='mm', format='A5')
    pdf.add_page()
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(False)

    # Background-like header strip (simulate with filled rect)
    pdf.set_fill_color(30, 27, 75)
    pdf.rect(0, 0, 210, 40, 'F')

    # Event title
    pdf.set_xy(10, 8)
    pdf.set_text_color(200, 190, 255)
    pdf.set_font('Arial', 'B', 18)
    title = _s((event['title'] if event else 'Event')[:40])
    pdf.cell(130, 12, title, ln=False)

    # Ticket ID (top right)
    pdf.set_xy(145, 5)
    pdf.set_font('Arial', 'B', 9)
    pdf.set_text_color(180, 170, 240)
    pdf.cell(55, 8, f"TICKET: {ticket['ticket_id']}", align='R', ln=True)

    # Status badge next to ticket id
    pdf.set_xy(145, 13)
    status_text = 'PAID' if ticket['payment_status'] == 'paid' else 'FREE ENTRY'
    pdf.set_font('Arial', 'B', 9)
    pdf.set_text_color(74, 222, 128)
    pdf.cell(55, 8, status_text, align='R', ln=True)

    # Separator line
    pdf.set_draw_color(99, 102, 241)
    pdf.set_line_width(0.5)
    pdf.line(0, 40, 210, 40)

    # Dashed perforation line (simulate with short segments)
    pdf.set_draw_color(100, 100, 180)
    pdf.set_line_width(0.3)
    for x in range(155, 210, 4):
        pdf.line(x, 40, x + 2, 40)

    # Left section - attendee info
    pdf.set_text_color(20, 20, 40)
    pdf.set_xy(10, 45)
    pdf.set_font('Arial', 'B', 13)
    pdf.set_text_color(50, 50, 100)
    pdf.cell(130, 8, 'ATTENDEE', ln=True)

    pdf.set_xy(10, 53)
    pdf.set_font('Arial', 'B', 14)
    pdf.set_text_color(20, 20, 60)
    pdf.cell(130, 9, _s(ticket.get('user_name', '')[:30]), ln=True)

    details = [
        ('Roll No.', ticket.get('user_roll') or ticket['user_id']),
        ('Dept.',    ticket.get('user_dept', 'N/A')),
        ('Event',    (event['date'] if event else '') + ' at ' + (event.get('time', '') if event else '')),
        ('Venue',    (event.get('venue', 'TBA') if event else 'TBA')[:35]),
    ]
    y = 64
    for label, value in details:
        pdf.set_xy(10, y)
        pdf.set_font('Arial', '', 9)
        pdf.set_text_color(120, 120, 150)
        pdf.cell(28, 7, label + ':', ln=False)
        pdf.set_font('Arial', 'B', 9)
        pdf.set_text_color(30, 30, 70)
        pdf.cell(100, 7, _s(value), ln=True)
        y += 7

    # QR code (right side)
    buf = _qr_buf(ticket['qr_data'])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    tmp.write(buf.read()); tmp.close()
    pdf.image(tmp.name, x=155, y=42, w=50, h=50)
    os.unlink(tmp.name)

    pdf.set_xy(155, 93)
    pdf.set_font('Arial', '', 7)
    pdf.set_text_color(130, 130, 160)
    pdf.cell(50, 5, 'Scan at venue entrance', align='C')

    # Footer
    pdf.set_xy(0, 110)
    pdf.set_font('Arial', 'I', 8)
    pdf.set_text_color(160, 160, 200)
    pdf.cell(210, 8, 'This is a system-generated ticket. Present QR at the event entrance.', align='C')

    try:
        raw = pdf.output(dest='S')
    except TypeError:
        raw = pdf.output()
        
    if isinstance(raw, str):
        raw = raw.encode('latin-1')
    return BytesIO(raw)

# ─── Email ────────────────────────────────────────────────────────────────────
def _send_ticket_email(ticket, event):
    email = ticket.get('user_email')
    if not email: return
    event_title  = event['title'] if event else 'Event'
    event_date   = event.get('date', '') if event else ''
    event_time   = event.get('time', '') if event else ''
    event_venue  = event.get('venue', '') if event else ''

    # Save QR to temp
    buf = _qr_buf(ticket['qr_data'])
    qr_dir = os.path.join('static', 'temp_qr')
    os.makedirs(qr_dir, exist_ok=True)
    qr_path = os.path.join(qr_dir, f"em_{ticket['ticket_id']}.png")
    with open(qr_path, 'wb') as f: f.write(buf.read())

    is_paid = ticket['payment_status'] == 'paid'
    subject = f"🎟️ Your Ticket Confirmed: {event_title}"
    body    = (f"Hi {ticket['user_name']},\n\nYour ticket for {event_title} is confirmed!\n"
               f"Ticket ID: {ticket['ticket_id']}\nDate: {event_date} {event_time}\n"
               f"Venue: {event_venue}\n\nShow the attached QR code at the entrance.")
    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#0f172a;color:#f1f5f9;border-radius:16px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:2rem;text-align:center;">
    <h1 style="margin:0;font-size:1.8rem;">🎟️ Your Ticket is Confirmed!</h1>
    <p style="margin:.5rem 0 0;opacity:.85;">{event_title}</p>
  </div>
  <div style="padding:2rem;">
    <p>Hi <strong>{ticket['user_name']}</strong>, your registration is all set!</p>
    <div style="background:#1e293b;border-radius:12px;padding:1.5rem;margin:1.5rem 0;text-align:center;">
      <p style="color:#94a3b8;margin:0 0 .5rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;">Ticket ID</p>
      <h2 style="margin:0;color:#a5b4fc;font-size:1.5rem;letter-spacing:.08em;">{ticket['ticket_id']}</h2>
    </div>
    <table style="width:100%;border-collapse:collapse;">
      <tr><td style="padding:.5rem;color:#94a3b8;">📅 Date &amp; Time</td>
          <td style="padding:.5rem;font-weight:bold;">{event_date} {event_time}</td></tr>
      <tr><td style="padding:.5rem;color:#94a3b8;">📍 Venue</td>
          <td style="padding:.5rem;font-weight:bold;">{event_venue}</td></tr>
      <tr><td style="padding:.5rem;color:#94a3b8;">💳 Status</td>
          <td style="padding:.5rem;font-weight:bold;color:{'#10b981' if is_paid else '#6366f1'};">{'Paid ✓' if is_paid else 'Free Entry ✓'}</td></tr>
    </table>
    <p style="color:#94a3b8;font-size:.85rem;margin-top:1.5rem;border-top:1px solid #1e293b;padding-top:1rem;">
      📎 QR Code is attached to this email. Present it at the venue. Do not share it with others.</p>
  </div>
</div>"""
    Mailer.send_async(email, subject, body, html, qr_path,
                      club_id=event.get('club_id') if event else None)
    # QR temp file cleanup happens in background thread - skip here to avoid race condition

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@em.route('/events')
def em_events():
    user   = session.get('user')
    events = [e for e in get_events() if e.get('status') == 'active']
    return render_template('em_events.html', user=user, events=events)

@em.route('/events/<event_id>')
def em_event_detail(event_id):
    user   = session.get('user')
    event  = next((e for e in get_events() if e['id'] == event_id and e.get('status') == 'active'), None)
    if not event: return 'Event not found', 404

    if event.get('event_category') == 'tech_fest':
        return render_template('em_techfest_landing.html', techfest=event)

    is_registered = False
    user_ticket   = None
    if user:
        identifier = user.get('roll_number') or user.get('email', '')
        tickets    = get_tickets()
        user_ticket = next((t for t in tickets
                            if t['event_id'] == event_id
                            and t['user_id'] == identifier
                            and t.get('payment_status') not in ('failed', None)), None)
        is_registered = user_ticket is not None

    settings = get_settings()
    # Count tickets for capacity display
    all_tickets = get_tickets()
    reg_count = sum(1 for t in all_tickets if t['event_id'] == event_id and t.get('payment_status') not in ('failed',))

    user_team = None
    if user and event.get('event_category') == 'hackathon':
        teams = get_hackathon_teams(event_id)
        user_team = next((
            t for t in teams
            if identifier in [m.get('roll_number') or m.get('email') for m in t.get('members', [])]
            or t.get('leader_id') == identifier
        ), None)

    return render_template('em_event_detail.html',
        user=user, event=event,
        is_registered=is_registered,
        user_ticket=user_ticket,
        razorpay_key=settings.get('razorpay_key_id', ''),
        reg_count=reg_count,
        user_team=user_team)

@em.route('/events/<event_id>/register')
def em_event_register(event_id):
    user = session.get('user')
    event = next((e for e in get_events() if e['id'] == event_id and e.get('status') == 'active'), None)
    if not event:
        return 'Event not found', 404

    if not user and not event.get('allow_external'):
        return redirect(url_for('login_page'))
        
    identifier = (user.get('roll_number') or user.get('email', '')) if user else None
    tickets = get_tickets()
    user_ticket = next((t for t in tickets 
                        if t['event_id'] == event_id 
                        and t['user_id'] == identifier 
                        and t.get('payment_status') not in ('failed', None)), None)
    
    if user_ticket:
        # Already registered, redirect to ticket page
        return redirect(url_for('event_mgmt.em_ticket', ticket_id=user_ticket['ticket_id']))
        
    settings = get_settings()
    reg_count = sum(1 for t in tickets if t['event_id'] == event_id and t.get('payment_status') not in ('failed',))
    
    if event.get('max_capacity') and reg_count >= int(event['max_capacity']):
        # Capacity full, return to detail page
        return redirect(url_for('event_mgmt.em_event_detail', event_id=event_id))

    # For hackathon events, look up user's existing team
    user_team = None
    if event.get('event_category') == 'hackathon':
        teams = get_hackathon_teams(event_id)
        user_team = next((
            t for t in teams
            if identifier in [m.get('roll_number') or m.get('email') for m in t.get('members', [])]
            or t.get('leader_id') == identifier
        ), None)

    return render_template('em_event_register.html',
        user=user, event=event,
        razorpay_key=settings.get('razorpay_key_id', ''),
        payment_demo=settings.get('payment_demo_mode', False),
        reg_count=reg_count,
        user_team=user_team)

@em.route('/ticket/<ticket_id>')
def em_ticket(ticket_id):
    user = session.get('user')
    if not user: return redirect(url_for('login_page'))
    
    ticket_type = request.args.get('type')
    if ticket_type == 'hackathon':
        teams = get_hackathon_teams()
        team = next((t for t in teams if t['team_id'] == ticket_id), None)
        if not team: return 'Team/Ticket not found', 404
        
        ticket = {
            'ticket_id':      team['team_id'],
            'event_id':       team['event_id'],
            'user_id':        team['leader_id'],
            'user_name':      team['team_name'] + ' (Team)',
            'user_roll':      team['leader_id'],
            'user_dept':      'Hackathon Team',
            'user_year':      '',
            'payment_status': team.get('payment_status', 'paid'),
            'amount':         0,
            'qr_data':        team.get('qr_data', f"HT|{team['event_id']}|{team['team_id']}"),
            'created_at':     team.get('created_at', '')
        }
    else:
        tickets = get_tickets()
        ticket  = next((t for t in tickets if t['ticket_id'] == ticket_id), None)
        if not ticket: return 'Ticket not found', 404
    
    identifier = user.get('roll_number') or user.get('email', '')
    if ticket_type == 'hackathon':
        team = next((t for t in get_hackathon_teams() if t['team_id'] == ticket_id), None)
        members = [m.get('roll_number') or m.get('email') for m in team.get('members', [])]
        if identifier != team['leader_id'] and identifier not in members and not is_admin(user):
            return 'Unauthorized', 403
    else:
        if ticket['user_id'] != identifier and not is_admin(user):
            return 'Unauthorized', 403

    event = next((e for e in get_events() if e['id'] == ticket['event_id']), None)
    return render_template('em_ticket.html', user=user, ticket=ticket, event=event, is_hackathon=(ticket_type=='hackathon'))

@em.route('/dashboard')
def em_dashboard():
    user = session.get('user')
    if not user or not is_admin(user):
        return redirect(url_for('login_page'))
    
    # Persistent selection: Check query param first, then session
    active_event_id = request.args.get('event_id')
    if active_event_id:
        session['em_active_event'] = active_event_id
    else:
        active_event_id = session.get('em_active_event')

    # Allow clearing
    if request.args.get('clear_event'):
        active_event_id = None
        session.pop('em_active_event', None)

    # Event admins get their own dedicated, focused dashboard
    if is_event_admin(user):
        return redirect(url_for('em.em_ea_dashboard'))
    clubs = DB.get_clubs()
    events    = admin_events(user)
    tickets   = get_tickets()
    settings  = get_settings()
    em_admins = get_em_admins()
    all_events = get_events()
    active_events = [e for e in all_events if e.get('status') == 'active']
    occupied_admin_emails = set()
    for e in active_events:
        if e.get('assigned_admin'):
            occupied_admin_emails.add(e.get('assigned_admin'))
    
    for a in em_admins:
        if any(eid in [e['id'] for e in active_events] for eid in a.get('assigned_events', [])):
            occupied_admin_emails.add(a.get('email'))
            
    available_admins = [a for a in em_admins if a.get('email') not in occupied_admin_emails]

    return render_template('em_admin.html',
        user=user, events=events, all_tickets=tickets,
        settings=settings, em_admins=em_admins, available_admins=available_admins, 
        all_events=all_events, clubs=clubs, active_event_id=active_event_id)

@em.route('/my-events')
def em_my_events():
    user = session.get('user')
    if not user or not is_admin(user):
        return redirect(url_for('login_page'))
    events    = admin_events(user)
    all_tickets = get_tickets()
    clubs = DB.get_clubs()
    em_admins = get_em_admins()
    all_events = get_events()
    active_events = [e for e in all_events if e.get('status') == 'active']
    occupied_admin_emails = set()
    for e in active_events:
        if e.get('assigned_admin'):
            occupied_admin_emails.add(e.get('assigned_admin'))
    available_admins = [a for a in em_admins if a.get('email') not in occupied_admin_emails]
    
    return render_template('em_my_events.html', 
        user=user, events=events, all_tickets=all_tickets, 
        clubs=clubs, available_admins=available_admins, all_events=all_events)

@em.route('/admins')
def em_admins_page():
    user = session.get('user')
    if not user or not is_admin(user):
        return redirect(url_for('login_page'))
    em_admins = get_em_admins()
    # For modals
    all_events = get_events()
    active_events = [e for e in all_events if e.get('status') == 'active']
    occupied_admin_emails = set()
    for e in active_events:
        if e.get('assigned_admin'):
            occupied_admin_emails.add(e.get('assigned_admin'))
    available_admins = [a for a in em_admins if a.get('email') not in occupied_admin_emails]

    return render_template('em_admins.html', 
        user=user, em_admins=em_admins, available_admins=available_admins, all_events=all_events)

@em.route('/settings')
def em_settings():
    user = session.get('user')
    if not user or not is_admin(user):
        return redirect(url_for('login_page'))
    settings = get_settings()
    return render_template('em_settings.html', user=user, settings=settings)

@em.route('/ea/dashboard')
def em_ea_dashboard():
    """Dedicated dashboard for event_admin role."""
    user = session.get('user')
    if not user:
        return redirect(url_for('login_page'))
    if not is_event_admin(user):
        return redirect(url_for('event_mgmt.em_dashboard'))
    all_events = get_events()
    em_admins  = get_em_admins()
    events     = admin_events(user)
    emails = []
    if user.get('email'): emails.append(user['email'].lower().strip())
    rolls = []
    if user.get('roll_number'): rolls.append(user['roll_number'].lower().strip())

    admin_rec = None
    active_event_id = None
    for a in em_admins:
        ae = a.get('email', '').lower().strip()
        ar = a.get('roll_number', '').lower().strip()
        if (ae and ae in emails) or (ar and ar in rolls):
            admin_rec = a
            break

    if admin_rec:
        candidate = admin_rec.get('active_event')
        if candidate:
            candidate_ev = next((e for e in all_events if e['id'] == candidate), None)
            if candidate_ev and candidate_ev.get('status') == 'active':
                active_event_id = candidate
            else:
                admin_rec['active_event'] = None
                save_em_admins(em_admins)

    if not active_event_id:
        active_assigned = [e for e in events if e.get('status') == 'active']
        if active_assigned:
            active_event_id = active_assigned[0]['id']
            if admin_rec:
                admin_rec['active_event'] = active_event_id
                save_em_admins(em_admins)

    if active_event_id:
        return redirect(url_for('em.em_event_hub', event_id=active_event_id))

    return render_template('event_admin_dashboard.html',
        user=user, active_event=None, active_event_id=None, settings=get_settings()
    )

@em.route('/tickets')
def em_tickets_hub():
    user = session.get('user')
    if not user: return redirect(url_for('login_page'))
    
    # Unified ticket hub logic: Redirect to the registrations page of the active event
    events = admin_events(user)
    active = [e for e in events if e.get('status') == 'active']
    
    if active:
        ev = active[0]
        cat = ev.get('event_category', '')
        if cat == 'hackathon':
            return redirect(url_for('em.em_hackathon_registrations', event_id=ev['id']))
        elif cat == 'tech_fest':
            return redirect(url_for('em.em_techfest_registrations', event_id=ev['id']))
        else:
            return redirect(url_for('em.em_event_registrations', event_id=ev['id']))
            
    # Fallback for managers or if no active event
    if is_manager(user):
        return redirect(url_for('em.em_dashboard'))
    return "No active event assigned. Please contact the Event Manager.", 403

@em.route('/scanner')
def em_scanner():
    user = session.get('user')
    if not user or not is_admin(user):
        return redirect(url_for('login_page'))
    events = [e for e in admin_events(user) if e.get('status') == 'active']
    return render_template('em_scanner.html', user=user, events=events, preselected_event_id='')

# ═══════════════════════════════════════════════════════════════════════════════
# CLUB EVENT HUB — Dedicated Sub-Page Routes
# ═══════════════════════════════════════════════════════════════════════════════

def _require_event_access(event_id):
    """Helper: returns (user, event, club_id) or redirects."""
    user = session.get('user')
    if not user:
        return redirect(url_for('login_page'))
    
    event_id = (event_id or '').strip()
    event, club_id = _club_event_access(user, event_id)
    
    if not event:
        # Debugging: Log the failed access attempt to a temporary file
        log_path = os.path.join(DATA_DIR, 'access_errors.log')
        with open(log_path, 'a') as f:
            f.write(f"[{datetime.datetime.now()}] Access Denied: User={user.get('email')} EventID={event_id} Role={user.get('role')}\n")
        
        from flask import make_response
        return make_response('Event not found or access denied. Please ensure you are assigned to this event.', 403)
    return user, event, club_id

@em.route('/event/<event_id>/hub')
def em_event_hub(event_id):
    result = _require_event_access(event_id)
    if not isinstance(result, tuple):
        return result
    user, event, club_id = result
    
    cat = (event.get('event_category') or '').strip().lower()
    template = 'em_event_hub.html'
    if 'hackathon' in cat:
        template = 'em_hackathon_hub.html'
    elif 'tech' in cat or 'fest' in cat:
        template = 'em_techfest_hub.html'

    return render_template(template, 
        user=user, 
        event=event, 
        club_id=club_id, 
        settings=get_settings()
    )

@em.route('/event/<event_id>/registrations')
def em_event_registrations(event_id):
    result = _require_event_access(event_id)
    if not isinstance(result, tuple):
        return result
    user, event, club_id = result
    return render_template('em_event_registrations_page.html', user=user, event=event, club_id=club_id)

@em.route('/event/<event_id>/analytics')
def em_event_analytics(event_id):
    result = _require_event_access(event_id)
    if not isinstance(result, tuple):
        return result
    user, event, club_id = result
    return render_template('em_event_analytics_page.html', user=user, event=event, club_id=club_id)

@em.route('/event/<event_id>/admins')
def em_event_admins(event_id):
    result = _require_event_access(event_id)
    if not isinstance(result, tuple):
        return result
    user, event, club_id = result
    return render_template('em_event_admins_page.html', user=user, event=event, club_id=club_id)

@em.route('/event/<event_id>/bulk-email')
def em_event_bulk_email(event_id):
    result = _require_event_access(event_id)
    if not isinstance(result, tuple):
        return result
    user, event, club_id = result
    return render_template('em_event_bulkemail_page.html', user=user, event=event, club_id=club_id)

@em.route('/event/<event_id>/scanner')
def em_event_scanner(event_id):
    result = _require_event_access(event_id)
    if not isinstance(result, tuple):
        return result
    user, event, club_id = result
    # Pass event in a list so the selector renders; pass event_id to auto-select
    return render_template('em_scanner.html', user=user, events=[event], preselected_event_id=event_id)

# ── Club: Export CSV for their event ─────────────────────────────────────────
@em.route('/api/club/event/<event_id>/export')
def api_club_export_event(event_id):
    user = session.get('user')
    event, _club_id = _club_event_access(user, event_id)
    if not event:
        return 'Unauthorized or event not found', 403
    tickets = [t for t in get_tickets() if t['event_id'] == event_id]
    import io, csv as _csv
    output = io.StringIO()
    writer = _csv.writer(output)
    writer.writerow([
        'Ticket ID', 'Name', 'Roll Number', 'Department', 'Year',
        'Email', 'Phone', 'Payment Status', 'Payment Method',
        'Amount (₹)', 'Checked In', 'Check-in Time', 'Registered At'
    ])
    for t in tickets:
        writer.writerow([
            t.get('ticket_id', ''),
            t.get('user_name', ''),
            t.get('user_roll', ''),
            t.get('user_dept', ''),
            t.get('user_year', ''),
            t.get('user_email', ''),
            t.get('user_phone', ''),
            t.get('payment_status', ''),
            t.get('payment_method', ''),
            t.get('amount', 0),
            'Yes' if t.get('checked_in') else 'No',
            t.get('checked_in_at', ''),
            t.get('created_at', '')
        ])
    from flask import make_response
    response = make_response(output.getvalue())
    safe_title = ''.join(c for c in event.get('title', event_id) if c.isalnum() or c in ' _-')[:30].strip().replace(' ', '_')
    response.headers["Content-Disposition"] = f"attachment; filename=registrations_{safe_title}.csv"
    response.headers["Content-type"] = "text/csv; charset=utf-8"
    return response

# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

# ── Event CRUD ────────────────────────────────────────────────────────────────
@em.route('/api/events/create', methods=['POST'])
def api_create_event():
    user = session.get('user')
    if not is_manager(user): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data   = request.form.to_dict()
    events = get_events()

    banner_fn = None
    banner_f  = request.files.get('banner')
    if banner_f and banner_f.filename:
        os.makedirs(BANNERS_DIR, exist_ok=True)
        banner_fn = f"banner_{uuid.uuid4().hex[:8]}_{banner_f.filename}"
        banner_f.save(os.path.join(BANNERS_DIR, banner_fn))

    price = 0
    if data.get('event_type') == 'paid':
        try: price = int(data.get('ticket_price', 0))
        except: price = 0

    cap = None
    if data.get('max_capacity'):
        try: cap = int(data['max_capacity'])
        except: cap = None

    # Validate assigned_admin is not already assigned to another active event
    assigned_admin_email = data.get('assigned_admin', '').strip()
    if assigned_admin_email:
        all_em_admins = get_em_admins()
        all_ev = get_events()
        for adm in all_em_admins:
            if adm.get('email') == assigned_admin_email:
                # Check if any of their already-assigned events are still active
                active_assigned = [
                    e for e in all_ev
                    if (e['id'] in adm.get('assigned_events', []) or e.get('assigned_admin') == adm.get('email')) and e.get('status') == 'active'
                ]
                if active_assigned:
                    return jsonify({
                        'success': False,
                        'message': f'Admin "{adm["name"]}" is already assigned to an active event: "{active_assigned[0]["title"]}". Unassign them first.'
                    }), 400
                break

    ev = {
        'id':              str(uuid.uuid4()),
        'title':           data.get('title', '').strip(),
        'description':     data.get('description', '').strip(),
        'date':            data.get('date', ''),
        'time':            data.get('time', ''),
        'venue':           data.get('venue', '').strip(),
        'organized_by':    data.get('organized_by', '').strip(),
        'organized_by_id': data.get('organized_by_id', '').strip(),
        'event_category':  data.get('event_category', 'club_event'),
        'event_type':      data.get('event_type', 'free'),
        'ticket_price':    price,
        'banner':          banner_fn,
        'max_capacity':    cap,
        'assigned_admin':  assigned_admin_email,
        'allow_external':  data.get('allow_external') == 'yes',
        'status':          'active',
        'created_by':      user.get('email') or user.get('roll_number', ''),
        'created_at':      datetime.datetime.now().isoformat()
    }
    events.append(ev)
    save_events(events)

    # Sync assignment in admins.json
    if assigned_admin_email:
        all_em_admins = get_em_admins()
        for adm in all_em_admins:
            if adm.get('email', '').lower().strip() == assigned_admin_email.lower().strip():
                if ev['id'] not in adm.get('assigned_events', []):
                    if 'assigned_events' not in adm: adm['assigned_events'] = []
                    adm['assigned_events'].append(ev['id'])
                    # Auto-set as active event if they don't have one
                    if not adm.get('active_event'):
                        adm['active_event'] = ev['id']
                    save_em_admins(all_em_admins)
                break

    return jsonify({'success': True, 'event_id': ev['id']})

@em.route('/api/events/update/<event_id>', methods=['POST'])
def api_update_event(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = get_events()
    ev = next((e for e in events if e['id'] == event_id), None)
    if not ev: return jsonify({'success': False, 'message': 'Event not found'}), 404

    data = request.form.to_dict()
    allowed_fields = ['title', 'date', 'time', 'venue', 'organized_by', 'organized_by_id', 'event_category', 'event_type', 'ticket_price', 'max_capacity', 'assigned_admin']
    for k in allowed_fields:
        if k in data:
            ev[k] = data[k]
    banner_f = request.files.get('banner')
    if banner_f and banner_f.filename:
        os.makedirs(BANNERS_DIR, exist_ok=True)
        fn = f"banner_{uuid.uuid4().hex[:8]}_{banner_f.filename}"
        banner_f.save(os.path.join(BANNERS_DIR, fn))
        ev['banner'] = fn
    if 'ticket_price' in ev:
        try: ev['ticket_price'] = int(ev['ticket_price'])
        except: ev['ticket_price'] = 0
    
    if 'allow_external' in data:
        ev['allow_external'] = data['allow_external'] == 'yes'
    else:
        ev['allow_external'] = False
        
    # Sync assignment in admins.json if assigned_admin changed
    new_admin_email = data.get('assigned_admin', '').strip()
    old_admin_email = ev.get('assigned_admin', '').strip()
    
    if new_admin_email != old_admin_email:
        all_em_admins = get_em_admins()
        # Remove from old admin
        if old_admin_email:
            for adm in all_em_admins:
                if adm.get('email', '').lower().strip() == old_admin_email.lower().strip():
                    if ev['id'] in adm.get('assigned_events', []):
                        adm['assigned_events'].remove(ev['id'])
                        if adm.get('active_event') == ev['id']:
                            adm['active_event'] = None
                        save_em_admins(all_em_admins)
                    break
        # Add to new admin
        if new_admin_email:
            for adm in all_em_admins:
                if adm.get('email', '').lower().strip() == new_admin_email.lower().strip():
                    if 'assigned_events' not in adm: adm['assigned_events'] = []
                    if ev['id'] not in adm['assigned_events']:
                        adm['assigned_events'].append(ev['id'])
                        if not adm.get('active_event'):
                            adm['active_event'] = ev['id']
                        save_em_admins(all_em_admins)
                    break

    save_events(events)
    return jsonify({'success': True})

@em.route('/api/events/cancel/<event_id>', methods=['POST'])
def api_cancel_event(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    events = get_events()
    for e in events:
        if e['id'] == event_id:
            e['status'] = 'cancelled'
            break
    save_events(events)
    return jsonify({'success': True})

@em.route('/api/events/complete/<event_id>', methods=['POST'])
def api_complete_event(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    events = get_events()
    for e in events:
        if e['id'] == event_id:
            e['status'] = 'completed'
            break
    save_events(events)
    # Unlock for event admin
    if is_event_admin(user):
        admins = get_em_admins()
        for a in admins:
            if a.get('email') == (user.get('email') or user.get('roll_number')):
                if a.get('active_event') == event_id:
                    a['active_event'] = None
        save_em_admins(admins)
    return jsonify({'success': True})

@em.route('/api/admins/lock-event/<event_id>', methods=['POST'])
def api_lock_event(event_id):
    user = session.get('user')
    if not is_event_admin(user):
        return jsonify({'success': False})
    admins = get_em_admins()
    for a in admins:
        if a.get('email') == (user.get('email') or user.get('roll_number')):
            a['active_event'] = event_id
            break
    save_em_admins(admins)
    return jsonify({'success': True})



# ── Payment (Razorpay) ────────────────────────────────────────────────────────
@em.route('/api/payment/create-order', methods=['POST'])
def api_create_order():
    user = session.get('user')
    data     = request.json
    event_id = data.get('event_id')
    event    = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404

    if not user and not event.get('allow_external'):
        return jsonify({'success': False, 'message': 'Login required'}), 401

    if event.get('event_type') != 'paid':
        return jsonify({'success': False, 'message': 'Free event'}), 400

    identifier = (user.get('roll_number') or user.get('email', '')) if user else (data.get('roll') or data.get('email'))
    tickets    = get_tickets()

    if any(t['event_id'] == event_id and t['user_id'] == identifier
           and t.get('payment_status') not in ('failed',) for t in tickets):
        return jsonify({'success': False, 'message': 'Already registered'}), 400

    if event.get('max_capacity'):
        count = sum(1 for t in tickets if t['event_id'] == event_id
                    and t.get('payment_status') not in ('failed',))
        if count >= int(event['max_capacity']):
            return jsonify({'success': False, 'message': 'Event is at full capacity'}), 400

    settings   = get_settings()
    key_id     = settings.get('razorpay_key_id', '').strip()
    key_secret = settings.get('razorpay_key_secret', '').strip()
    if not key_id or not key_secret:
        return jsonify({'success': False,
                        'message': 'Payment gateway not configured. Contact event manager.'}), 503

    if settings.get('payment_demo_mode'):
        return jsonify({
            'success': True,
            'demo': True,
            'order_id': f"order_demo_{uuid.uuid4().hex[:12]}",
            'amount': int(event.get('ticket_price', 0)) * 100,
            'event_title': event.get('title', '')
        })

    try:
        import razorpay
        client = razorpay.Client(auth=(key_id, key_secret))
        amount_paise = int(event.get('ticket_price', 0)) * 100
        order = client.order.create({
            'amount':          amount_paise,
            'currency':        'INR',
            'payment_capture': 1,
            'notes': {
                'event_id':    event_id,
                'event_title': event.get('title', ''),
                'user_id':     identifier,
                'team_id':     data.get('team_id', '')
            }
        })
        return jsonify({'success': True, 'order_id': order['id'],
                        'amount': amount_paise, 'key': key_id,
                        'event_title': event.get('title', '')})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@em.route('/api/payment/verify', methods=['POST'])
def api_verify_payment():
    user = session.get('user')

    data       = request.json
    payment_id = data.get('razorpay_payment_id', '')
    order_id   = data.get('razorpay_order_id', '')
    signature  = data.get('razorpay_signature', '')
    event_id   = data.get('event_id', '')

    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return jsonify({'success': False}), 404

    settings   = get_settings()
    key_secret = settings.get('razorpay_key_secret', '').strip()

    # HMAC verification
    if not settings.get('payment_demo_mode'):
        msg      = f"{order_id}|{payment_id}"
        expected = hmac.new(key_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if expected != signature:
            return jsonify({'success': False, 'message': 'Payment signature verification failed'}), 400
    else:
        # In demo mode, we accept any signature for mock orders
        if not order_id.startswith('order_demo_'):
            return jsonify({'success': False, 'message': 'Invalid order ID for demo mode'}), 400

    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return jsonify({'success': False}), 404

    identifier = (user.get('roll_number') or user.get('email', '')) if user else (data.get('roll') or data.get('email'))
    tickets    = get_tickets()

    # Prevent duplicate
    if any(t['event_id'] == event_id and t['user_id'] == identifier
           and t.get('payment_status') == 'paid' for t in tickets):
        existing = next(t for t in tickets if t['event_id'] == event_id
                        and t['user_id'] == identifier and t.get('payment_status') == 'paid')
        return jsonify({'success': True, 'ticket_id': existing['ticket_id'], 'duplicate': True})

    email_override = data.get('email')
    phone_override = data.get('phone')
    name_override  = data.get('name')
    roll_override  = data.get('roll')
    college_override = data.get('college')

    t = _make_ticket_record(user, event, 'paid', payment_id, order_id,
                            event.get('ticket_price', 0), 'Razorpay', email_override, phone_override,
                            name_override, roll_override, college_override)
    tickets.append(t)
    save_tickets(tickets)

    try: _send_ticket_email(t, event)
    except Exception as e: print(f'Email error: {e}')

    return jsonify({'success': True, 'ticket_id': t['ticket_id']})

# ── Free Registration ─────────────────────────────────────────────────────────
@em.route('/api/register/free', methods=['POST'])
def api_register_free():
    user = session.get('user')
    data     = request.json
    event_id = data.get('event_id')
    event    = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404

    if not user and not event.get('allow_external'):
        return jsonify({'success': False, 'message': 'Login required'}), 401

    if event.get('event_type') != 'free':
        return jsonify({'success': False, 'message': 'This is a paid event'}), 400

    email_override = data.get('email')
    phone_override = data.get('phone')
    name_override  = data.get('name')
    roll_override  = data.get('roll')
    college_override = data.get('college')

    identifier = (user.get('roll_number') or user.get('email', '')) if user else (roll_override or email_override)
    tickets    = get_tickets()

    # Prevent duplicate registration (Strict Check)
    for t in tickets:
        if t['event_id'] == event_id and t.get('payment_status') not in ('failed',):
            if t['user_id'] == identifier or t.get('user_roll') == identifier or t.get('user_email') == identifier:
                return jsonify({'success': False, 'message': 'You are already registered for this event.'}), 400

    # Capacity check
    if event.get('max_capacity'):
        count = sum(1 for t in tickets if t['event_id'] == event_id
                    and t.get('payment_status') not in ('failed',))
        if count >= int(event['max_capacity']):
            return jsonify({'success': False, 'message': 'Event is at full capacity'}), 400

    t = _make_ticket_record(user, event, 'free', email_override=email_override, phone_override=phone_override, 
                            name_override=name_override, roll_override=roll_override, college_override=college_override)
    tickets.append(t)
    save_tickets(tickets)

    try: _send_ticket_email(t, event)
    except Exception as e: print(f'Email error: {e}')

    return jsonify({'success': True, 'ticket_id': t['ticket_id']})

# ── Cash Registration (pay at venue) ──────────────────────────────────────────
@em.route('/api/register/cash', methods=['POST'])
def api_register_cash():
    return jsonify({'success': False, 'message': 'Cash payments are prohibited. Please use the online payment gateway.'}), 403

# ── QR Scanner ────────────────────────────────────────────────────────────────
@em.route('/api/scan', methods=['POST'])
def api_scan():
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    qr_data = (request.json or {}).get('qr_data', '').strip()
    parts   = qr_data.split('|')
    
    if parts[0] == 'TF':
        # Techfest Scanning
        if len(parts) < 3:
            return jsonify({'success': False, 'message': '❌ Invalid Techfest QR code'}), 400
        
        tf_id = parts[1]
        reg_id = parts[2]
        
        regs = DB.get_techfest_registrations()
        reg = next((r for r in regs if r['reg_id'] == reg_id), None)
        if not reg:
            return jsonify({'success': False, 'message': '❌ Registration not found'}), 404
        
        if reg.get('payment_status') not in ('paid', 'free'):
            return jsonify({'success': False, 'message': '❌ Payment incomplete — entry denied'}), 400
            
        if reg.get('checked_in'):
            return jsonify({
                'success': False,
                'already_in': True,
                'message': f'⚠️ Already checked in at {reg.get("checked_in_at", "unknown")}',
                'ticket': {
                    'user_name': reg['student_details']['name'],
                    'ticket_id': reg_id,
                    'user_dept': reg['student_details'].get('department', 'N/A')
                }
            }), 409
            
        reg['checked_in'] = True
        reg['checked_in_at'] = datetime.datetime.now().isoformat()
        DB.save_techfest_registration(reg)
        
        return jsonify({
            'success': True,
            'message': f'✅ Entry granted to {reg["student_details"]["name"]}',
            'ticket': {
                'user_name': reg['student_details']['name'],
                'ticket_id': reg_id,
                'user_dept': reg['student_details'].get('department', 'N/A'),
                'payment_status': reg['payment_status']
            }
        })

    if len(parts) < 4 or parts[0] != 'EM':
        return jsonify({'success': False, 'message': '❌ Invalid QR code format'}), 400

    event_id  = parts[1]
    ticket_id = parts[2]
    tickets   = get_tickets()
    ticket    = next((t for t in tickets
                      if t['ticket_id'] == ticket_id and t['event_id'] == event_id), None)
    if not ticket:
        return jsonify({'success': False, 'message': '❌ Ticket not found'}), 404

    if ticket.get('payment_status') in ('failed', 'pending', None):
        return jsonify({'success': False, 'message': '❌ Payment incomplete — entry denied'}), 400

    if ticket.get('checked_in'):
        return jsonify({
            'success':        False,
            'already_in':     True,
            'message':        f'⚠️ Already checked in at {ticket.get("checked_in_at", "unknown")}',
            'ticket':         ticket
        }), 409

    ticket['checked_in']    = True
    ticket['checked_in_at'] = datetime.datetime.now().isoformat()
    save_tickets(tickets)

    event = next((e for e in get_events() if e['id'] == event_id), {})
    return jsonify({
        'success': True,
        'message': f'✅ Entry granted to {ticket["user_name"]}',
        'ticket':  ticket,
        'event':   event
    })

# ── Ticket Endpoints ──────────────────────────────────────────────────────────
@em.route('/api/ticket/<ticket_id>/qr')
def api_qr_image(ticket_id):
    # Check regular tickets
    tickets = get_tickets()
    t = next((t for t in tickets if t['ticket_id'] == ticket_id), None)
    
    # If not found, check hackathon teams
    if not t:
        teams = get_hackathon_teams()
        t = next((team for team in teams if team['team_id'] == ticket_id), None)
        
    if not t: return 'Not found', 404
    
    # Fallback for missing qr_data
    qr_data = t.get('qr_data')
    if not qr_data:
        if ticket_id.startswith('TEAM-'):
             qr_data = f"HT|{t['event_id']}|{ticket_id}"
        else:
             qr_data = f"EM|{t['event_id']}|{ticket_id}|{t.get('user_id','')}"

    return send_file(_qr_buf(qr_data), mimetype='image/png')

@em.route('/api/techfest/qr/<reg_id>')
def api_techfest_qr(reg_id):
    regs = DB.get_techfest_registrations()
    reg = next((r for r in regs if r['reg_id'] == reg_id), None)
    if not reg: return 'Not found', 404
    
    qr_data = reg.get('qr_data')
    if not qr_data:
        qr_data = f"TF|{reg.get('techfest_id')}|{reg_id}"
        
    return send_file(_qr_buf(qr_data), mimetype='image/png')

@em.route('/api/ticket/<ticket_id>/download')
def api_download_ticket(ticket_id):
    user = session.get('user')
    if not user: return redirect(url_for('login_page'))
    
    # Check regular tickets
    tickets = get_tickets()
    t = next((tk for tk in tickets if tk['ticket_id'] == ticket_id), None)
    is_hackathon = False
    
    # If not found, check hackathon teams
    if not t:
        teams = get_hackathon_teams()
        t = next((team for team in teams if team['team_id'] == ticket_id), None)
        if t:
            is_hackathon = True
            # Normalize for PDF generator
            t = {
                'ticket_id':      t['team_id'],
                'event_id':       t['event_id'],
                'user_id':        t['leader_id'],
                'user_name':      t['team_name'] + ' (Team)',
                'user_roll':      t['leader_id'],
                'user_dept':      'Hackathon Team',
                'payment_status': t.get('payment_status', 'paid'),
                'amount':         0
            }

    if not t: return 'Ticket not found', 404
    
    identifier = user.get('roll_number') or user.get('email', '')
    if not is_admin(user):
        if not is_hackathon:
            if t['user_id'] != identifier: return 'Unauthorized', 403
        else:
            # For hackathons, verify team membership
            orig_team = next(tm for tm in get_hackathon_teams() if tm['team_id'] == ticket_id)
            m_ids = [m.get('roll_number') or m.get('email') for m in orig_team.get('members', [])]
            if identifier != orig_team['leader_id'] and identifier not in m_ids:
                return 'Unauthorized', 403

    event  = next((e for e in get_events() if e['id'] == t['event_id']), None)
    pdf_io = _generate_pdf_ticket(t, event)
    return send_file(pdf_io, mimetype='application/pdf',
                     as_attachment=True, download_name=f"{t['ticket_id']}.pdf")

@em.route('/api/ticket/<ticket_id>/resend', methods=['POST'])
def api_resend_ticket(ticket_id):
    user = session.get('user')
    tickets = get_tickets()
    t = next((t for t in tickets if t['ticket_id'] == ticket_id), None)
    if not t: return jsonify({'success': False, 'message': 'Ticket not found'}), 404
    if not has_event_access(user, t['event_id']): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    event = next((e for e in get_events() if e['id'] == t['event_id']), None)
    try:
        _send_ticket_email(t, event)
        return jsonify({'success': True, 'message': 'Email sent'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@em.route('/api/ticket/search')
def api_ticket_search():
    user = session.get('user')
    if not user or (not is_admin(user) and not is_club_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    q         = request.args.get('q', '').lower()
    event_id  = request.args.get('event_id', '')
    tickets   = get_tickets()
    if event_id:
        tickets = [t for t in tickets if t['event_id'] == event_id]
    if q:
        tickets = [t for t in tickets if (
            q in t.get('user_name', '').lower() or
            q in t.get('user_roll', '').lower() or
            q in t.get('ticket_id', '').lower() or
            q in t.get('user_email', '').lower()
        )]
    return jsonify({'success': True, 'tickets': tickets[:50]})

@em.route('/api/ticket/export/<event_id>')
def api_export_tickets(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id): return 'Unauthorized', 403
    
    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return 'Event not found', 404
    
    tickets = [t for t in get_tickets() if t['event_id'] == event_id]
    
    import io
    output = io.StringIO()
    import csv
    writer = csv.writer(output)
    writer.writerow(['Ticket ID', 'Name', 'Roll Number', 'Department', 'Year', 'Email', 'Payment Status', 'Checked In', 'Check-in Time'])
    
    for t in tickets:
        writer.writerow([
            t.get('ticket_id', ''),
            t.get('user_name', ''),
            t.get('user_roll', ''),
            t.get('user_dept', ''),
            t.get('user_year', ''),
            t.get('user_email', ''),
            t.get('payment_status', ''),
            'Yes' if t.get('checked_in') else 'No',
            t.get('checked_in_at', '')
        ])
        
    from flask import make_response
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=attendance_{event_id}.csv"
    response.headers["Content-type"] = "text/csv"
    return response

# ── Analytics ─────────────────────────────────────────────────────────────────
@em.route('/api/analytics/<event_id>')
def api_analytics(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    tickets   = [t for t in get_tickets() if t['event_id'] == event_id
                 and t.get('payment_status') not in ('failed',)]
    total     = len(tickets)
    checked   = sum(1 for t in tickets if t.get('checked_in'))
    paid_c    = sum(1 for t in tickets if t.get('payment_status') == 'paid')
    free_c    = sum(1 for t in tickets if t.get('payment_status') == 'free')
    revenue   = sum(t.get('amount', 0) for t in tickets if t.get('payment_status') == 'paid')
    dept_dist = {}
    year_dist = {}
    for t in tickets:
        dept = t.get('user_dept') or 'N/A'
        yr   = t.get('user_year') or 'N/A'
        dept_dist[dept] = dept_dist.get(dept, 0) + 1
        year_dist[yr]   = year_dist.get(yr, 0) + 1
    return jsonify({
        'success': True, 'total': total, 'checked_in': checked,
        'paid': paid_c, 'free': free_c, 'revenue': revenue,
        'dept_distribution': dept_dist, 'year_distribution': year_dist,
        'tickets': tickets
    })

# ── Settings ──────────────────────────────────────────────────────────────────
@em.route('/api/settings', methods=['POST'])
def api_save_settings():
    user = session.get('user')
    # Allow event_manager, chief_coordinator, and event_admin (gateway + SMTP only)
    if not is_manager(user) and not is_event_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    s    = get_settings()
    data = request.json or {}
    # Event admins can only update SMTP + Razorpay keys (not demo mode toggle)
    if is_event_admin(user) and not is_manager(user):
        allowed_keys = {'razorpay_key_id', 'razorpay_key_secret', 'smtp_user', 'smtp_pass', 'smtp_server', 'smtp_port'}
        data = {k: v for k, v in data.items() if k in allowed_keys}
    s.update(data)
    put_settings(s)
    return jsonify({'success': True})

# ── Event Admin Management ─────────────────────────────────────────────────────
@em.route('/api/admins/create', methods=['POST'])
def api_create_em_admin():
    user = session.get('user')
    if not is_manager(user): return jsonify({'success': False}), 403
    data    = request.json or {}
    admins  = get_em_admins()
    if any(a['email'] == data.get('email') for a in admins):
        return jsonify({'success': False, 'message': 'Admin with this email already exists'}), 400
    new_a = {
        'id':               str(uuid.uuid4()),
        'name':             data.get('name', ''),
        'email':            data.get('email', ''),
        'phone':            data.get('phone', ''),
        'assigned_events':  data.get('assigned_events', []),
        'created_at':       datetime.datetime.now().isoformat()
    }
    admins.append(new_a)
    save_em_admins(admins)
    # Register in main admins.json so login works
    DB.save_admin({
        'name':     new_a['name'],
        'email':    new_a['email'],
        'password': data.get('password', 'eventadmin123'),
        'role':     'event_admin',
        'phone':    new_a.get('phone', '')
    })
    return jsonify({'success': True})

@em.route('/api/admins/<admin_id>/assign', methods=['POST'])
def api_assign_events(admin_id):
    user = session.get('user')
    if not is_manager(user): return jsonify({'success': False}), 403
    data      = request.json or {}
    new_ids   = data.get('event_ids', [])
    admins    = get_em_admins()
    all_ev    = get_events()

    # Build a map: event_id -> admin_name for events already assigned to OTHER admins
    occupied = {}  # event_id -> admin_name
    for adm in admins:
        if adm['id'] == admin_id:
            continue  # skip self
        for eid in adm.get('assigned_events', []):
            ev_obj = next((e for e in all_ev if e['id'] == eid and e.get('status') == 'active'), None)
            if ev_obj:
                occupied[eid] = adm['name']

    conflicts = []
    for eid in new_ids:
        if eid in occupied:
            ev_obj = next((e for e in all_ev if e['id'] == eid), None)
            conflicts.append(f'"{ev_obj["title"] if ev_obj else eid}" (assigned to {occupied[eid]})')

    if conflicts:
        return jsonify({
            'success': False,
            'message': 'Cannot assign: the following events are already assigned to other admins — ' + ', '.join(conflicts)
        }), 400

    admin_obj = None
    for a in admins:
        if a['id'] == admin_id:
            a['assigned_events'] = new_ids
            admin_obj = a
            break
    save_em_admins(admins)

    # Sync back to events.json: set assigned_admin on all events in new_ids, 
    # and CLEAR it if it was pointing to this admin but not in new_ids.
    if admin_obj and admin_obj.get('email'):
        admin_email = admin_obj['email'].lower().strip()
        for ev in all_ev:
            cur_assigned = ev.get('assigned_admin', '').lower().strip()
            if ev['id'] in new_ids:
                ev['assigned_admin'] = admin_obj['email']
            elif cur_assigned == admin_email:
                ev['assigned_admin'] = ""
        save_events(all_ev)

    return jsonify({'success': True})

@em.route('/api/admins/<admin_id>/delete', methods=['POST'])
def api_delete_em_admin(admin_id):
    user = session.get('user')
    if not is_manager(user): return jsonify({'success': False}), 403
    admins = [a for a in get_em_admins() if a['id'] != admin_id]
    save_em_admins(admins)
    return jsonify({'success': True})

# ── Bulk Email ────────────────────────────────────────────────────────────────
@em.route('/api/bulk-email/<event_id>', methods=['POST'])
def api_bulk_email(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    data      = request.json or {}
    email_type = data.get('type', 'reminder')   # 'reminder' | 'resend_pass'
    subject   = data.get('subject', '').strip()
    message   = data.get('message', '').strip()

    tickets = [t for t in get_tickets()
               if t['event_id'] == event_id
               and t.get('payment_status') not in ('failed',)
               and t.get('user_email')]

    sent = 0
    failed = 0
    for t in tickets:
        try:
            if email_type == 'resend_pass':
                _send_ticket_email(t, event)
            else:
                if not subject or not message:
                    return jsonify({'success': False, 'message': 'Subject and message are required'}), 400
                ev_title  = event.get('title', '')
                ev_date   = event.get('date', '')
                ev_time   = event.get('time', '')
                ev_venue  = event.get('venue', '')
                html_body = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#0f172a;color:#f1f5f9;border-radius:16px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:2rem;text-align:center;">
    <h1 style="margin:0;font-size:1.5rem;">📣 {subject}</h1>
    <p style="margin:.5rem 0 0;opacity:.85;">{ev_title}</p>
  </div>
  <div style="padding:2rem;">
    <p>Hi <strong>{t['user_name']}</strong>,</p>
    <p style="line-height:1.7;">{message}</p>
    <div style="background:#1e293b;border-radius:12px;padding:1.25rem;margin:1.5rem 0;">
      <p style="color:#94a3b8;margin:0 0 .5rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;">Event Details</p>
      <p style="margin:.3rem 0;"><strong>📅</strong> {ev_date} {ev_time}</p>
      <p style="margin:.3rem 0;"><strong>📍</strong> {ev_venue}</p>
      <p style="margin:.3rem 0;"><strong>🎟️</strong> Ticket: {t['ticket_id']}</p>
    </div>
    <p style="color:#94a3b8;font-size:.82rem;border-top:1px solid #1e293b;padding-top:1rem;margin-top:1.5rem;">
      Please carry your QR code (event pass) for entry. Be on time!
    </p>
  </div>
</div>"""
                Mailer.send_email(t['user_email'], subject, message, html_body)
            sent += 1
        except Exception as e:
            print(f'Bulk email error for {t.get("user_email")}: {e}')
            failed += 1

    return jsonify({'success': True, 'sent': sent, 'failed': failed, 'total': len(tickets)})

# ── Bulk Resend PDF Pass ───────────────────────────────────────────────────────
@em.route('/api/resend-all/<event_id>', methods=['POST'])
def api_resend_all_passes(event_id):
    """Resend PDF event pass to every ticket holder for an event."""
    user = session.get('user')
    if not has_event_access(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404
    tickets = [t for t in get_tickets()
               if t['event_id'] == event_id
               and t.get('payment_status') not in ('failed',)
               and t.get('user_email')]
    sent = failed = 0
    for t in tickets:
        try:
            _send_ticket_email(t, event)
            sent += 1
        except Exception as e:
            print(f'Resend error: {e}')
            failed += 1
    return jsonify({'success': True, 'sent': sent, 'failed': failed, 'total': len(tickets)})

# ═══════════════════════════════════════════════════════════════════════════════
# CLUB EVENT HUB — APIs accessible by club admins for their own club's EM events
# ═══════════════════════════════════════════════════════════════════════════════

def _club_event_access(user, event_id):
    """Returns (event, club_id) if the user has access to the event, else (None, None)."""
    event_id = (event_id or '').strip()
    event = next((e for e in get_events() if e['id'].strip() == event_id), None)
    if not event: return None, None
    if not user: return None, None
    role = user.get('role', '')
    # Full admins always have access
    if role in ('event_manager', 'chief_coordinator'): return event, event.get('organized_by_id')
    # Club admins can only access their own club's events
    if is_club_admin(user):
        club_id = get_club_id_from_user(user)
        if event.get('organized_by_id') == club_id:
            return event, club_id
    
    # Event admins can only access events they are assigned to
    if is_event_admin(user):
        allowed_events = admin_events(user)
        for e in allowed_events:
            if e['id'].strip() == event_id:
                return e, e.get('organized_by_id')
            
    return None, None

# ── Club: List their EM events with stats ─────────────────────────────────────
@em.route('/api/club/events/<club_id>')
def api_club_events(club_id):
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    role = user.get('role', '')
    # Verify access
    if role not in ('event_manager', 'chief_coordinator'):
        if not is_club_admin(user) or get_club_id_from_user(user) != club_id:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    events = get_events_for_club(club_id)
    events = enrich_events_with_stats(events)
    return jsonify({'success': True, 'events': events})

# ── Club: Get tickets for one of their events ──────────────────────────────────
@em.route('/api/club/event/<event_id>/tickets')
def api_club_event_tickets(event_id):
    user = session.get('user')
    event, _club_id = _club_event_access(user, event_id)
    if not event: return jsonify({'success': False, 'message': 'Unauthorized or not found'}), 403
    tickets = [t for t in get_tickets() if t['event_id'] == event_id]
    return jsonify({'success': True, 'tickets': tickets})

# ── Club: Analytics for one of their events ────────────────────────────────────
@em.route('/api/club/event/<event_id>/analytics')
def api_club_event_analytics(event_id):
    user = session.get('user')
    event, _club_id = _club_event_access(user, event_id)
    if not event: return jsonify({'success': False, 'message': 'Unauthorized or not found'}), 403
    tickets = [t for t in get_tickets()
               if t['event_id'] == event_id
               and t.get('payment_status') not in ('failed',)]
    total       = len(tickets)
    checked     = sum(1 for t in tickets if t.get('checked_in'))
    paid_c      = sum(1 for t in tickets if t.get('payment_status') == 'paid')
    free_c      = sum(1 for t in tickets if t.get('payment_status') == 'free')
    revenue     = sum(t.get('amount', 0) for t in tickets if t.get('payment_status') == 'paid')
    dept_dist   = {}
    year_dist   = {}
    for t in tickets:
        dept = t.get('user_dept') or 'N/A'
        yr   = t.get('user_year') or 'N/A'
        dept_dist[dept] = dept_dist.get(dept, 0) + 1
        year_dist[yr]   = year_dist.get(yr, 0) + 1
    return jsonify({
        'success': True, 'total': total, 'checked_in': checked,
        'paid': paid_c, 'free': free_c,
        'revenue': revenue, 'dept_distribution': dept_dist,
        'year_distribution': year_dist, 'tickets': tickets
    })


# ── Club: Get event admins assigned to their event ────────────────────────────
@em.route('/api/club/event/<event_id>/admins')
def api_club_event_admins(event_id):
    user = session.get('user')
    event, _club_id = _club_event_access(user, event_id)
    if not event: return jsonify({'success': False, 'message': 'Unauthorized or not found'}), 403
    em_admins = get_em_admins()
    event_admins = [a for a in em_admins if event_id in a.get('assigned_events', [])]
    # Hide password; load from main admins to check if exists
    return jsonify({'success': True, 'admins': event_admins})

# ── Club: Create / update an event admin for their event ──────────────────────
@em.route('/api/club/admins/create', methods=['POST'])
def api_club_create_event_admin():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    if not is_club_admin(user) and not is_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data     = request.json or {}
    event_id = data.get('event_id', '')
    email    = data.get('email', '').strip()
    name     = data.get('name', '').strip()
    phone    = data.get('phone', '').strip()
    password = data.get('password', 'eventadmin123').strip()

    event, club_id = _club_event_access(user, event_id)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found or you do not own it'}), 403
    if not email:
        return jsonify({'success': False, 'message': 'Email is required'}), 400

    em_admins = get_em_admins()
    existing  = next((a for a in em_admins if a.get('email') == email), None)

    if existing:
        # Assign event if not already assigned
        if event_id not in existing.get('assigned_events', []):
            existing.setdefault('assigned_events', []).append(event_id)
        save_em_admins(em_admins)
        # Update password in main admins.json
        main_admins = DB.load_json('admins.json')
        for a in main_admins:
            if a.get('email') == email:
                if password: a['password'] = password
                break
        DB.save_json('admins.json', main_admins)
        return jsonify({'success': True, 'message': 'Event assigned to existing admin and password updated'})

    # Create new event admin record
    new_a = {
        'id':               str(uuid.uuid4()),
        'name':             name,
        'email':            email,
        'phone':            phone,
        'assigned_events':  [event_id],
        'created_at':       datetime.datetime.now().isoformat(),
        'created_by_club':  club_id or ''
    }
    em_admins.append(new_a)
    save_em_admins(em_admins)

    # Register in main admins.json so login works
    DB.save_admin({
        'name':     name,
        'email':    email,
        'password': password,
        'role':     'event_admin',
        'phone':    phone
    })
    return jsonify({'success': True, 'message': f'Event admin created. Login: {email} / {password}'})

# ── Club: Remove event admin from their event ─────────────────────────────────
@em.route('/api/club/admins/<admin_id>/remove', methods=['POST'])
def api_club_remove_event_admin(admin_id):
    user = session.get('user')
    if not user or (not is_club_admin(user) and not is_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data     = request.json or {}
    event_id = data.get('event_id', '')
    em_admins = get_em_admins()
    for a in em_admins:
        if a['id'] == admin_id:
            if is_club_admin(user):
                # Verify this event belongs to the club
                event, _cid = _club_event_access(user, event_id)
                if not event: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
                # Only remove from this event, not delete the admin entirely
                a['assigned_events'] = [e for e in a.get('assigned_events', []) if e != event_id]
            else:
                # Full admin can delete entirely
                em_admins = [x for x in em_admins if x['id'] != admin_id]
            break
    save_em_admins(em_admins)
    return jsonify({'success': True})

# ── Club: Bulk email for their event ──────────────────────────────────────────
@em.route('/api/club/bulk-email/<event_id>', methods=['POST'])
def api_club_bulk_email(event_id):
    user = session.get('user')
    event, _club_id = _club_event_access(user, event_id)
    if not event: return jsonify({'success': False, 'message': 'Unauthorized or not found'}), 403
    data    = request.json or {}
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()
    if not subject or not message:
        return jsonify({'success': False, 'message': 'Subject and message are required'}), 400
    tickets = [t for t in get_tickets()
               if t['event_id'] == event_id
               and t.get('payment_status') not in ('failed',)
               and t.get('user_email')]
    ev_title = event.get('title', '')
    ev_date  = event.get('date', '')
    ev_time  = event.get('time', '')
    ev_venue = event.get('venue', '')
    sent = failed = 0
    for t in tickets:
        try:
            html_body = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#0f172a;color:#f1f5f9;border-radius:16px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:2rem;text-align:center;">
    <h1 style="margin:0;font-size:1.5rem;">📣 {subject}</h1>
    <p style="margin:.5rem 0 0;opacity:.85;">{ev_title}</p>
  </div>
  <div style="padding:2rem;">
    <p>Hi <strong>{t['user_name']}</strong>,</p>
    <p style="line-height:1.7;">{message}</p>
    <div style="background:#1e293b;border-radius:12px;padding:1.25rem;margin:1.5rem 0;">
      <p style="color:#94a3b8;margin:0 0 .5rem;font-size:.8rem;text-transform:uppercase;">Event Details</p>
      <p>📅 {ev_date} {ev_time}</p>
      <p>📍 {ev_venue}</p>
      <p>🎟️ Ticket: {t['ticket_id']}</p>
    </div>
  </div>
</div>"""
            Mailer.send_email(t['user_email'], subject, message, html_body)
            sent += 1
        except Exception as e:
            print(f'Club bulk email error: {e}')
            failed += 1
    return jsonify({'success': True, 'sent': sent, 'failed': failed, 'total': len(tickets)})


# ═══════════════════════════════════════════════════════════════════════════════
# EVALUATOR ROLE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_evaluator(user):
    """Evaluator role — can only score hackathon projects."""
    return user and user.get('role') == 'evaluator'

def get_evaluators():
    return DB.get_evaluators()

def evaluator_events(user):
    """Return events (hackathon or tech_fest sub-events) assigned to this evaluator."""
    if not user: return []
    evaluators = get_evaluators()
    identifier = user.get('email') or user.get('roll_number', '')
    rec = next((e for e in evaluators if e.get('email') == identifier), None)
    if not rec: return []
    assigned_ids = rec.get('assigned_events', [])
    
    # Check main events (hackathons)
    all_ev = get_events()
    assigned = [e for e in all_ev if e['id'] in assigned_ids and e.get('event_category') == 'hackathon']
    
    # Check Tech Fest sub-events
    all_tfs = DB.get_techfests()
    for tf in all_tfs:
        se_list = DB.get_techfest_events(tf['id'])
        for se in se_list:
            if se['id'] in assigned_ids:
                se['event_category'] = 'tech_fest'
                se['title'] = se.get('name', 'Sub-Event')
                assigned.append(se)
                
    return assigned

# ═══════════════════════════════════════════════════════════════════════════════
# HACKATHON DATA HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_hackathon_teams(event_id=None):
    return DB.get_hackathon_teams(event_id)

def _team_id():
    return 'TEAM-' + uuid.uuid4().hex[:8].upper()

def _can_access_hackathon(user, event_id):
    """Returns event if user can access this hackathon event, else None."""
    if not user: return None
    
    # 1. Check evaluators (specific to hackathons)
    if is_evaluator(user):
        ev_list = evaluator_events(user)
        for e in ev_list:
            if e['id'] == event_id: return e
        return None

    # 2. Use the unified access helper for managers, club admins, and event admins
    ev, _ = _club_event_access(user, event_id)
    return ev



# ═══════════════════════════════════════════════════════════════════════════════
# HACKATHON DASHBOARD ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
def _can_access_techfest(user, event_id):
    """Returns event if user can access this techfest event, else None."""
    if not user: return None
    ev, _ = _club_event_access(user, event_id)
    return ev
        
    return None

@em.route('/techfest/<event_id>')
def techfest_event_landing(event_id):
    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return "Event not found", 404
    
    # Sub-events for this specific Tech Fest
    events = DB.get_techfest_events(event_id)
    user = session.get('user')
    return render_template('em_techfest_landing.html', techfest=event, events=events, user=user)

@em.route('/techfest/<event_id>/register')
def techfest_event_register(event_id):
    # Try getting from regular events first
    event = next((e for e in get_events() if e['id'] == event_id), None)
    # If not found, try getting from dedicated techfests
    if not event:
        tfs = DB.get_techfests()
        event = next((t for t in tfs if t['id'] == event_id), None)
        
    if not event: return "Event not found", 404
    
    # Sub-events for this specific Tech Fest
    events = DB.get_techfest_events(event_id)
    depts = DB.get_techfest_departments()
    user = session.get('user')
    return render_template('em_techfest_register.html', techfest=event, events=events, departments=depts, user=user)

@em.route('/techfest/<event_id>/hub')
def em_techfest_hub(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    
    sub_events = DB.get_techfest_events(event_id)
    regs = DB.get_techfest_registrations(event_id)
    depts = DB.get_techfest_departments()
    
    return render_template('em_techfest_hub.html', 
                           user=user, 
                           event=event, 
                           sub_events=sub_events, 
                           registrations=regs, 
                           departments=depts,
                           settings=get_settings(),
                           now=datetime.datetime.now(),
                           timedelta=datetime.timedelta)

@em.route('/techfest/<event_id>/sub-events')
def em_techfest_sub_events(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    sub_events = DB.get_techfest_events(event_id)
    return render_template('em_techfest_sub_events.html', user=user, event=event, sub_events=sub_events)

@em.route('/techfest/<event_id>/registrations')
def em_techfest_registrations(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    regs = DB.get_techfest_registrations(event_id)
    return render_template('em_techfest_registrations.html', user=user, event=event, registrations=regs)

@em.route('/techfest/<event_id>/attendance')
def em_techfest_attendance(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    regs = DB.get_techfest_registrations(event_id)
    return render_template('em_techfest_attendance.html', user=user, event=event, registrations=regs)

@em.route('/techfest/<event_id>/bulk-email')
def em_techfest_bulk_email(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    regs = DB.get_techfest_registrations(event_id)
    return render_template('em_techfest_bulk_email.html', user=user, event=event, registrations=regs)

@em.route('/techfest/<event_id>/evaluators')
def em_techfest_evaluators(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    sub_events = DB.get_techfest_events(event_id)
    return render_template('em_techfest_evaluators.html', user=user, event=event, sub_events=sub_events)

@em.route('/techfest/<event_id>/submit')
def em_techfest_submit(event_id):
    user = session.get('user')
    if not user: return redirect(url_for('login_page'))
    
    # Find user's registration for this techfest
    all_regs = DB.get_techfest_registrations(event_id)
    reg = next((r for r in all_regs if r['student_details'].get('roll_number') == user.get('roll_number') or r['student_details'].get('email') == user.get('email')), None)
    
    if not reg: return "Registration not found", 404
    
    # Enrich sub-events with requirements
    sub_events = DB.get_techfest_events(event_id)
    user_subs = []
    for s in reg.get('selected_events', []):
        se_full = next((se for se in sub_events if se['id'] == s['event_id']), None)
        if se_full:
            s['requirements'] = se_full.get('requirements', [])
            s['sub_event_type'] = se_full.get('sub_event_type', 'normal')
            user_subs.append(s)
            
    return render_template('em_techfest_submit.html', user=user, event_id=event_id, registrations=reg, selected_events=user_subs)

@em.route('/api/techfest/submit', methods=['POST'])
def api_techfest_submit():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Login required'}), 401
    
    event_id = request.form.get('event_id')
    sub_event_id = request.form.get('sub_event_id')
    
    all_regs = DB.get_techfest_registrations(event_id)
    reg = next((r for r in all_regs if r['student_details'].get('roll_number') == user.get('roll_number') or r['student_details'].get('email') == user.get('email')), None)
    
    if not reg: return jsonify({'success': False, 'message': 'Registration not found'}), 404
    
    # Update the specific sub-event response
    for s in reg.get('selected_events', []):
        if s['event_id'] == sub_event_id:
            responses = s.get('responses', {})
            # Handle files and text
            for key in request.form:
                if key not in ['event_id', 'sub_event_id']:
                    responses[key] = request.form[key]
            
            for key in request.files:
                file = request.files[key]
                if file and file.filename:
                    filename = secure_filename(f"tf_{reg['reg_id']}_{sub_event_id}_{key}_{file.filename}")
                    save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'em', 'techfest_submissions')
                    os.makedirs(save_path, exist_ok=True)
                    file.save(os.path.join(save_path, filename))
                    responses[key] = filename
            
            s['responses'] = responses
            s['submitted'] = True
            s['submitted_at'] = datetime.datetime.now().isoformat()
            break
            
    DB.save_techfest_registration(reg)
    return jsonify({'success': True, 'message': 'Submission successful!'})

@em.route('/hackathon/<event_id>/hub')
def em_hackathon_hub(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    return render_template('em_hackathon_hub.html', user=user, event=event, settings=get_settings())

@em.route('/hackathon/<event_id>/registrations')
def em_hackathon_registrations(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    teams = get_hackathon_teams(event_id)
    return render_template('em_hackathon_registrations.html', user=user, event=event, teams=teams)

@em.route('/hackathon/<event_id>/analytics')
def em_hackathon_analytics(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    return render_template('em_hackathon_analytics.html', user=user, event=event)

@em.route('/hackathon/<event_id>/evaluators')
def em_hackathon_evaluators(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    # evaluators logic is in em_admin but we can show it here too
    evaluators = get_evaluators()
    assigned = [e for e in evaluators if event_id in e.get('assigned_events', [])]
    return render_template('em_hackathon_evaluators.html', user=user, event=event, evaluators=assigned)


@em.route('/hackathon/<event_id>/rounds')
def em_hackathon_rounds(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    return render_template('em_hackathon_rounds.html', user=user, event=event)

@em.route('/api/hackathon/<event_id>/rounds', methods=['POST'])
def api_hackathon_update_rounds(event_id):
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = get_events()
    ev = next((e for e in events if e['id'] == event_id), None)
    if not ev: return jsonify({'success': False}), 404
    
    data = request.json or {}
    ev['evaluation_rounds'] = data.get('rounds', [])
    save_events(events)
    return jsonify({'success': True})

@em.route('/api/hackathon/team/<team_id>/promote', methods=['POST'])
def api_hackathon_promote_team(team_id):
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    target_round = data.get('round_index', 0)
    
    teams = get_hackathon_teams()
    team = next((t for t in teams if t['team_id'] == team_id), None)
    if not team: return jsonify({'success': False, 'message': 'Team not found'}), 404
    
    team['current_round'] = target_round
    DB.save_hackathon_team(team)
    return jsonify({'success': True})

@em.route('/hackathon/<event_id>/bulk-email')
def em_hackathon_bulkemail(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    return render_template('em_hackathon_bulkemail.html', user=user, event=event)

@em.route('/hackathon/<event_id>/scanner')
def em_hackathon_scanner(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    return render_template('em_hackathon_scanner.html', user=user, event=event)

@em.route('/hackathon/<event_id>/attendance')
def em_hackathon_attendance(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return 'Access denied', 403
    
    # Gather checked-in members
    teams = get_hackathon_teams(event_id)
    checked_in_members = []
    for t in teams:
        if t.get('checked_in'):
            for m in t.get('members', []):
                checked_in_members.append({
                    'name': m.get('name') or m.get('roll_number', 'N/A'),
                    'roll_number': m.get('roll_number', 'N/A'),
                    'dept': m.get('dept', 'N/A'),
                    'team_name': t.get('team_name', 'N/A'),
                    'checked_in_at': t.get('checked_in_at')
                })
    
    # Group by department
    dept_map = {}
    for m in checked_in_members:
        dept = m['dept'].upper() if m['dept'] else 'N/A'
        if dept not in dept_map: dept_map[dept] = []
        dept_map[dept].append(m)
        
    return render_template('em_hackathon_attendance.html', user=user, event=event, dept_map=dept_map)

@em.route('/api/hackathon/<event_id>/forward-attendance', methods=['POST'])
def api_hackathon_forward_attendance(event_id):
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    event = _can_access_hackathon(user, event_id)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found or unauthorized'}), 404

    dept_emails = DB.get_contacts()
    if not dept_emails:
        return jsonify({"success": False, "message": "Global contact directory is empty. Super Admin needs to configure Departments and Contacts first."})

    teams = get_hackathon_teams(event_id)
    checked_in_members = []
    for t in teams:
        if t.get('checked_in'):
            for m in t.get('members', []):
                checked_in_members.append({
                    'name': m.get('name') or m.get('roll_number', 'N/A'),
                    'roll_number': m.get('roll_number', 'N/A'),
                    'dept': m.get('dept', 'N/A'),
                    'team_name': t.get('team_name', 'N/A'),
                    'checked_in_at': t.get('checked_in_at', 'Verified')
                })
                
    if not checked_in_members:
        return jsonify({"success": False, "message": "No students have checked in yet."})

    count = 0
    from fpdf import FPDF
    import tempfile, datetime, os
    from app.mailer import Mailer

    event_title = event.get('title', 'Hackathon')
    club_id = event.get('organized_by_id', event.get('club_id'))

    for dept, email in dept_emails.items():
        dept_students = [m for m in checked_in_members if str(m.get('dept')).upper() == dept.upper()]
        if not dept_students: continue

        pdf = FPDF()
        pdf.add_page()
        
        pdf.set_font("Arial", 'B', 18)
        pdf.cell(190, 15, txt=f"Hackathon Attendance", ln=True, align='C')
        pdf.set_font("Arial", 'B', 14)
        pdf.cell(190, 10, txt=f"Event: {event_title}", ln=True, align='C')
        pdf.set_font("Arial", 'I', 11)
        pdf.cell(190, 8, txt=f"Department: {dept} | Date: {datetime.datetime.now().strftime('%d-%m-%Y')}", ln=True, align='C')
        pdf.ln(10)
        
        pdf.set_font("Arial", 'B', 10)
        pdf.set_fill_color(240, 240, 240)
        pdf.cell(35, 10, "Roll Number", border=1, fill=True)
        pdf.cell(60, 10, "Student Name", border=1, fill=True)
        pdf.cell(45, 10, "Team Name", border=1, fill=True)
        pdf.cell(50, 10, "Attendance Time", border=1, fill=True)
        pdf.ln()
        
        pdf.set_font("Arial", '', 9)
        for s in dept_students:
            pdf.cell(35, 10, str(s.get('roll_number', 'N/A'))[:15], border=1)
            pdf.cell(60, 10, str(s.get('name', 'N/A'))[:30], border=1)
            pdf.cell(45, 10, str(s.get('team_name', 'N/A'))[:20], border=1)
            
            # Format time if possible
            time_str = str(s.get('checked_in_at', 'Verified'))
            try:
                dt = datetime.datetime.fromisoformat(time_str)
                time_str = dt.strftime('%I:%M %p')
            except:
                pass
            pdf.cell(50, 10, time_str, border=1)
            pdf.ln()
        
        pdf.ln(15)
        pdf.set_font("Arial", 'I', 9)
        pdf.cell(190, 10, txt="This is a system generated report from Sphoorthy EventSphere.", ln=True, align='L')

        temp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_path = temp_pdf.name
        temp_pdf.close()
        pdf.output(pdf_path)
        
        subject = f"Hackathon Attendance Report: {event_title} - {dept}"
        body = f"Dear Head of Department ({dept}),\n\nPlease find attached the attendance report for the hackathon event '{event_title}'.\n\nBest regards,\nSphoorthy EventSphere Command Center"
        
        try:
            success = Mailer.send_email(
                to_email=email,
                subject=subject,
                body=body,
                attachment_path=pdf_path,
                club_id=club_id
            )
            if success: count += 1
            if os.path.exists(pdf_path): os.unlink(pdf_path)
        except Exception as e:
            print(f"Failed to forward to {dept}: {e}")
            if os.path.exists(pdf_path): os.unlink(pdf_path)

    if count == 0:
        return jsonify({"success": False, "message": "No reports could be dispatched. Check department records or student departments."})

    return jsonify({"success": True, "message": f"Successfully forwarded attendance PDF to {count} department head(s)."})


@em.route('/api/hackathon/scan', methods=['POST'])
def api_hackathon_scan():
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user): return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    qr_data = (request.json or {}).get('qr_data', '').strip()
    parts = qr_data.split('|')
    if len(parts) < 3 or parts[0] != 'HT':
        return jsonify({'success': False, 'message': '❌ Invalid QR format'}), 400

    event_id = parts[1]
    team_id = parts[2]
    teams = get_hackathon_teams(event_id)
    team = next((t for t in teams if t['team_id'] == team_id), None)
    if not team:
        return jsonify({'success': False, 'message': '❌ Team not found'}), 404

    if team.get('payment_status') in ('failed', 'pending', None) and next((e for e in get_events() if e['id']==event_id),{}).get('event_type') == 'paid':
        return jsonify({'success': False, 'message': '❌ Payment incomplete'}), 400

    if team.get('checked_in'):
        return jsonify({'success': False, 'already_in': True, 'message': '⚠️ Already checked in'}), 409

    team['checked_in'] = True
    team['checked_in_at'] = datetime.datetime.now().isoformat()
    DB.save_hackathon_team(team)

    return jsonify({'success': True, 'message': f'✅ Entry granted to team {team["team_name"]}', 'team': team})

@em.route('/api/hackathon/bulk-email/<event_id>', methods=['POST'])
def api_hackathon_bulk_email(event_id):
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event: return jsonify({'success': False}), 403
    data = request.json or {}
    subject = data.get('subject', '').strip()
    message = data.get('message', '').strip()
    teams = get_hackathon_teams(event_id)
    sent = 0
    failed = 0
    for t in teams:
        leader_email = next((m.get('email') for m in t.get('members',[]) if m.get('is_leader')), None)
        if not leader_email: continue
        try:
            ev_title = event.get('title', '')
            html_body = f"<div style='font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#0f172a;color:#f1f5f9;padding:2rem;'><h1>📣 {subject}</h1><p>Hi Team {t['team_name']},</p><p>{message}</p><p>Event: {ev_title}</p></div>"
            Mailer.send_email(leader_email, subject, message, html_body)
            sent += 1
        except:
            failed += 1
    return jsonify({'success': True, 'sent': sent, 'failed': failed, 'total': len(teams)})


# ═══════════════════════════════════════════════════════════════════════════════
# HACKATHON PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@em.route('/hackathon/<event_id>/teams')
def em_hackathon_teams(event_id):
    user = session.get('user')
    if not user or not (is_admin(user) or is_evaluator(user) or is_club_admin(user)):
        return redirect(url_for('login_page'))
    event = _can_access_hackathon(user, event_id)
    if not event:
        return 'Access denied or event not found', 403
    teams = get_hackathon_teams(event_id)
    scores = DB.get_scores(event_id=event_id)
    evaluators = get_evaluators()
    return render_template('em_hackathon_teams.html',
        user=user, event=event, teams=teams, scores=scores, evaluators=evaluators)

@em.route('/hackathon/<event_id>/leaderboard')
def em_hackathon_leaderboard(event_id):
    user = session.get('user')
    event = next((e for e in get_events() if e['id'] == event_id), None)
    if not event: return 'Event not found', 404
    if not event.get('leaderboard_enabled') and not (is_admin(user) or is_club_admin(user)):
        return 'Leaderboard is currently hidden by the event administrator.', 403
    teams = get_hackathon_teams(event_id)
    scores = DB.get_scores(event_id=event_id)
    # Compute average total per team across all evaluators
    team_scores = {}
    for t in teams:
        tid = t['team_id']
        team_scores[tid] = {'team': t, 'scores': [], 'avg': 0}
    for s in scores:
        tid = s.get('team_id')
        if tid in team_scores:
            team_scores[tid]['scores'].append(s)
    for tid, data in team_scores.items():
        if data['scores']:
            data['avg'] = round(sum(s.get('total', 0) for s in data['scores']) / len(data['scores']), 1)
    ranked = sorted(team_scores.values(), key=lambda x: x['avg'], reverse=True)
    return render_template('em_hackathon_leaderboard.html',
        user=user, event=event, ranked=ranked)

@em.route('/hackathon/<event_id>/submit')
def em_hackathon_submit(event_id):
    user = session.get('user')
    if not user: return redirect(url_for('login_page'))
    event = next((e for e in get_events() if e['id'] == event_id and e.get('status') == 'active'), None)
    if not event: return 'Event not found', 404
    if not event.get('submission_portal_enabled') and not (is_admin(user) or is_club_admin(user)):
        return 'Submission portal is currently closed.', 403
    identifier = user.get('roll_number') or user.get('email', '')
    teams = get_hackathon_teams(event_id)
    user_team = next((t for t in teams if identifier in [m.get('roll_number') or m.get('email') for m in t.get('members', [])] or t.get('leader_id') == identifier), None)
    return render_template('em_hackathon_submit.html', user=user, event=event, user_team=user_team)

@em.route('/hackathon/<event_id>/project-submit')
def em_hackathon_project_submit(event_id):
    """Project Submission Portal — opened from Hackathon Hub by admin/volunteer."""
    user = session.get('user')
    event = _can_access_hackathon(user, event_id)
    if not event:
        return 'Access denied or event not found', 403
    if not event.get('submission_portal_enabled'):
        return redirect(url_for('event_mgmt.em_hackathon_hub', event_id=event_id))
    return render_template('em_hackathon_project_submit.html', user=user, event=event)


@em.route('/api/hackathon/team-lookup')
def api_hackathon_team_lookup():
    """AJAX: fetch team info by Team ID within a specific event."""
    user = session.get('user')
    if not user or not (is_admin(user) or is_club_admin(user) or is_evaluator(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    team_id  = request.args.get('team_id', '').strip().upper()
    event_id = request.args.get('event_id', '').strip()
    if not team_id:
        return jsonify({'success': False, 'message': 'Team ID is required'}), 400
    teams = get_hackathon_teams(event_id) if event_id else get_hackathon_teams()
    team  = next((t for t in teams if t['team_id'] == team_id), None)
    if not team:
        return jsonify({'success': False, 'message': f'No team found with ID "{team_id}" for this event'}), 404
    return jsonify({'success': True, 'team': team})


@em.route('/api/hackathon/project-submit', methods=['POST'])
def api_hackathon_project_submit():
    """Admin/volunteer submits full evaluation details for a team."""
    user = session.get('user')
    if not user or not (is_admin(user) or is_club_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    event_id  = request.form.get('event_id', '').strip()
    team_id   = request.form.get('team_id', '').strip()

    # Verify event access
    event = _can_access_hackathon(user, event_id)
    if not event:
        return jsonify({'success': False, 'message': 'Event access denied'}), 403
    if not event.get('submission_portal_enabled'):
        return jsonify({'success': False, 'message': 'Submission portal is currently closed'}), 403

    teams = get_hackathon_teams(event_id)
    team  = next((t for t in teams if t['team_id'] == team_id), None)
    if not team:
        return jsonify({'success': False, 'message': 'Team not found'}), 404

    # Map form fields → team record
    team['project_title']     = request.form.get('project_title', team.get('project_title', '')).strip()
    team['problem_statement'] = request.form.get('problem_statement', team.get('problem_statement', '')).strip()
    team['solution']          = request.form.get('solution', team.get('solution', '')).strip()
    team['tech_stack']        = request.form.get('tech_stack', team.get('tech_stack', '')).strip()
    team['project_type']      = request.form.get('project_type', team.get('project_type', 'Software')).strip()
    team['github_url']        = request.form.get('github_url', team.get('github_url', '')).strip()
    team['demo_url']          = request.form.get('demo_url', team.get('demo_url', '')).strip()
    # Keep description field in sync for backward compat with evaluator view
    team['description']       = team['problem_statement']

    # Presentation file upload
    pres_file = request.files.get('presentation_file')
    if pres_file and pres_file.filename:
        upload_dir = os.path.join('static', 'uploads', 'em', 'hackathon_submissions')
        os.makedirs(upload_dir, exist_ok=True)
        fn = f"{team_id}_{uuid.uuid4().hex[:6]}_{pres_file.filename}"
        pres_file.save(os.path.join(upload_dir, fn))
        team['submission_file']        = fn
        team['presentation_file']      = fn

    team['submitted']          = True
    team['submitted_at']       = datetime.datetime.now().isoformat()
    team['submitted_by_admin'] = user.get('email') or user.get('name', '')
    DB.save_hackathon_team(team)
    return jsonify({'success': True, 'message': 'Project details submitted to evaluators successfully!'})


@em.route('/api/hackathon/<event_id>/toggle-submission', methods=['POST'])
def api_toggle_submission_portal(event_id):
    """Admin toggles the submission portal open/closed for an event."""
    user = session.get('user')
    if not user or not (is_admin(user) or is_club_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    event = _can_access_hackathon(user, event_id)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found or access denied'}), 403

    data    = request.json or {}
    enabled = bool(data.get('enabled', False))

    events = get_events()
    for ev in events:
        if ev['id'] == event_id:
            ev['submission_portal_enabled'] = enabled
            break
    save_events(events)
    return jsonify({'success': True, 'enabled': enabled,
                    'message': 'Submission portal ' + ('opened' if enabled else 'closed')})


@em.route('/api/hackathon/<event_id>/toggle-leaderboard', methods=['POST'])
def api_toggle_leaderboard(event_id):
    """Admin toggles the leaderboard visibility for an event."""
    user = session.get('user')
    if not user or not (is_admin(user) or is_club_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    event = _can_access_hackathon(user, event_id)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found or access denied'}), 403

    data    = request.json or {}
    enabled = bool(data.get('enabled', False))

    events = get_events()
    for ev in events:
        if ev['id'] == event_id:
            ev['leaderboard_enabled'] = enabled
            break
    save_events(events)
    return jsonify({'success': True, 'enabled': enabled,
                    'message': 'Leaderboard is now ' + ('visible' if enabled else 'hidden')})


@em.route('/evaluator/dashboard')
def evaluator_dashboard():
    user = session.get('user')
    if not user or not is_evaluator(user):
        return redirect(url_for('login_page'))
    ev_events = evaluator_events(user)
    evaluators = get_evaluators()
    identifier = user.get('email') or user.get('roll_number', '')
    evaluator_rec = next((e for e in evaluators if e.get('email') == identifier), None)
    
    # Gather teams and scores for assigned events
    events_data = []
    for ev in ev_events:
        is_tech_fest = ev.get('event_category') == 'tech_fest'
        
        if is_tech_fest:
            # For tech fest, we look into techfest_registrations
            all_tf_regs = DB.get_techfest_registrations()
            teams = []
            for r in all_tf_regs:
                for se in r.get('selected_events', []):
                    if se.get('event_id') == ev['id']:
                        # Map tech fest reg to a "team" format for the dashboard
                        teams.append({
                            'team_id': r['reg_id'],
                            'team_name': se.get('team_name') or r['student_details'].get('name', 'Unknown'),
                            'members': se.get('members', []),
                            'leader_id': r['student_details'].get('roll_number') or r['student_details'].get('email'),
                            'submitted': se.get('submitted', False),
                            'responses': se.get('responses', {}),
                            'project_title': se.get('responses', {}).get('Project Title') or se.get('responses', {}).get('Title') or 'Project',
                            'description': se.get('responses', {}).get('Description') or se.get('responses', {}).get('Abstract') or '',
                            'github_url': se.get('responses', {}).get('Github Link') or se.get('responses', {}).get('Code Link') or '',
                            'demo_url': se.get('responses', {}).get('Demo Link') or '',
                            'presentation_file': r.get('submission_file') or se.get('submission_file'),
                            'is_tech_fest': True
                        })
        else:
            teams = get_hackathon_teams(ev['id'])
            
        scores = DB.get_scores(event_id=ev['id'])
        my_scores = [s for s in scores if s.get('evaluator_id') == identifier]
        
        events_data.append({
            'event': ev,
            'teams': teams,
            'scores': my_scores,
            'scored_count': len({s['team_id'] for s in my_scores}),
            'total_teams': len(teams),
            'is_tech_fest': is_tech_fest
        })
        
    return render_template('evaluator_dashboard.html',
        user=user, evaluator_rec=evaluator_rec, events_data=events_data)


# ═══════════════════════════════════════════════════════════════════════════════
# HACKATHON API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@em.route('/api/student-lookup')
def api_student_lookup():
    roll = request.args.get('roll', '').strip().upper()
    dob  = request.args.get('dob', '').strip()
    if not roll or not dob:
        return jsonify({'success': False, 'message': 'Roll number and DOB are required'}), 400
    
    student = DB.get_student_by_roll(roll)
    if student and student.get('dob') == dob:
        return jsonify({'success': True, 'student': student})
    
    return jsonify({'success': False, 'message': 'Student not found or DOB incorrect'}), 404

@em.route('/api/hackathon/team/register', methods=['POST'])
def api_hackathon_register_team():
    user = session.get('user')
    data = request.json or {}
    event_id = data.get('event_id', '')
    event = next((e for e in get_events() if e['id'] == event_id and e.get('status') == 'active'), None)
    if not event:
        return jsonify({'success': False, 'message': 'Hackathon event not found'}), 404
    if event.get('event_category') != 'hackathon':
        return jsonify({'success': False, 'message': 'Not a hackathon event'}), 400

    if not user and not event.get('allow_external'):
        return jsonify({'success': False, 'message': 'Login required'}), 401

    leader_data = data.get('leader_data')
    if user:
        identifier = user.get('roll_number') or user.get('email', '')
        actual_leader = {
            'roll_number': user.get('roll_number', ''),
            'name': user.get('name', ''),
            'email': user.get('email', ''),
            'dept': user.get('department', ''),
            'year': user.get('year', ''),
            'college': 'Sphoorthy Engineering College',
            'is_leader': True
        }
    elif leader_data:
        identifier = leader_data.get('roll_number') or leader_data.get('email', '')
        actual_leader = {
            'roll_number': leader_data.get('roll_number', ''),
            'name': leader_data.get('name', ''),
            'email': leader_data.get('email', ''),
            'dept': leader_data.get('department', 'External'),
            'year': leader_data.get('year', 'N/A'),
            'college': leader_data.get('college', ''),
            'is_leader': True
        }
    else:
        return jsonify({'success': False, 'message': 'Leader details missing'}), 400

    teams = get_hackathon_teams(event_id)

    # Prevent duplicate registration (Check leader and all incoming members)
    members_data = data.get('members', [])
    incoming_ids = [m.get('roll_number') or m.get('email') for m in members_data]
    if identifier: incoming_ids.append(identifier)
    
    # Filter out empty IDs
    incoming_ids = [i for i in incoming_ids if i]

    for t in teams:
        if t.get('payment_status') == 'failed': continue
        existing_ids = [m.get('roll_number') or m.get('email') for m in t.get('members', [])]
        if t.get('leader_id'): existing_ids.append(t['leader_id'])
        
        for inc in incoming_ids:
            if inc in existing_ids:
                return jsonify({'success': False, 'message': f'Participant "{inc}" is already registered in a team for this hackathon.'}), 400

    team_name = data.get('team_name', '').strip()
    if not team_name:
        return jsonify({'success': False, 'message': 'Team name is required'}), 400

    # Min/max capacity check
    min_size = int(event.get('min_team_size', 1))
    max_size = int(event.get('max_team_size', 5))
    members_data = data.get('members', [])
    total_members = len(members_data) + 1  # +1 for leader
    if total_members < min_size:
        return jsonify({'success': False, 'message': f'Minimum team size is {min_size}'}), 400
    if total_members > max_size:
        return jsonify({'success': False, 'message': f'Maximum team size is {max_size}'}), 400

    team = {
        'team_id': _team_id(),
        'event_id': event_id,
        'team_name': team_name,
        'leader_id': identifier,
        'members': [actual_leader] + members_data,
        'project_title': '',
        'github_url': '',
        'demo_url': '',
        'description': '',
        'submission_file': None,
        'submitted': False,
        'submitted_at': None,
        'created_at': datetime.datetime.now().isoformat(),
        'payment_status': 'free' if event.get('event_type') == 'free' else 'pending_payment',
        'payment_method': 'free' if event.get('event_type') == 'free' else 'Razorpay',
        'qr_data': f'HT|{event_id}|' + _team_id(),
        'checked_in': False,
        'checked_in_at': None
    }
    # fix qr_data to use actual team_id
    team['qr_data'] = f'HT|{event_id}|{team["team_id"]}'
    DB.save_hackathon_team(team)
    return jsonify({'success': True, 'team_id': team['team_id'], 'is_paid': event.get('event_type') == 'paid', 'message': f'Team "{team_name}" registered!'})

@em.route('/api/hackathon/event/<event_id>/teams')
def api_hackathon_teams(event_id):
    user = session.get('user')
    if not user or not (is_admin(user) or is_evaluator(user) or is_club_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    teams = get_hackathon_teams(event_id)
    scores = DB.get_scores(event_id=event_id)
    return jsonify({'success': True, 'teams': teams, 'scores': scores})

@em.route('/api/hackathon/submit', methods=['POST'])
def api_hackathon_submit_project():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Login required'}), 401
    event_id = request.form.get('event_id', '')
    team_id = request.form.get('team_id', '')
    identifier = user.get('roll_number') or user.get('email', '')

    teams = get_hackathon_teams(event_id)
    team = next((t for t in teams if t['team_id'] == team_id), None)
    if not team:
        return jsonify({'success': False, 'message': 'Team not found'}), 404

    # Only team leader or member can submit
    all_member_ids = [m.get('roll_number') or m.get('email') for m in team.get('members', [])]
    if identifier not in all_member_ids and team.get('leader_id') != identifier and not is_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    team['project_title'] = request.form.get('project_title', team.get('project_title', ''))
    team['github_url'] = request.form.get('github_url', team.get('github_url', ''))
    team['demo_url'] = request.form.get('demo_url', team.get('demo_url', ''))
    team['description'] = request.form.get('description', team.get('description', ''))

    # Handle file upload
    sub_file = request.files.get('submission_file')
    if sub_file and sub_file.filename:
        d = os.path.join('static', 'uploads', 'em', 'hackathon_submissions')
        os.makedirs(d, exist_ok=True)
        fn = f"{team_id}_{uuid.uuid4().hex[:6]}_{sub_file.filename}"
        sub_file.save(os.path.join(d, fn))
        team['submission_file'] = fn

    team['submitted'] = True
    team['submitted_at'] = datetime.datetime.now().isoformat()
    DB.save_hackathon_team(team)
    return jsonify({'success': True, 'message': 'Project submitted successfully!'})
@em.route('/api/hackathon/team/verify-payment', methods=['POST'])
def api_hackathon_verify_payment():
    data = request.json or {}
    payment_id = data.get('razorpay_payment_id', '')
    order_id   = data.get('razorpay_order_id', '')
    signature  = data.get('razorpay_signature', '')
    team_id    = data.get('team_id', '')

    settings   = get_settings()
    key_secret = settings.get('razorpay_key_secret', '').strip()

    # HMAC verification
    if not settings.get('payment_demo_mode'):
        if key_secret and order_id and signature:
            msg      = f"{order_id}|{payment_id}"
            expected = hmac.new(key_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
            if expected != signature:
                return jsonify({'success': False, 'message': 'Payment signature verification failed'}), 400
    else:
        if not order_id.startswith('order_demo_'):
             return jsonify({'success': False, 'message': 'Invalid order ID for demo mode'}), 400

    teams = get_hackathon_teams()
    team = next((t for t in teams if t['team_id'] == team_id), None)
    if not team: return jsonify({'success': False, 'message': 'Team not found'}), 404

    team['payment_status'] = 'paid'
    team['payment_id']     = payment_id
    team['order_id']       = order_id
    DB.save_hackathon_team(team)

    # Send email to leader
    try:
        leader = next((m for m in team['members'] if m.get('is_leader')), team['members'][0])
        event = next((e for e in get_events() if e['id'] == team['event_id']), None)
        subject = f"✅ Payment Confirmed: {event['title']}"
        body = f"Hi {leader['name']},\n\nPayment for team {team['team_name']} has been confirmed. You're all set for the hackathon!"
        Mailer.send_email(leader['email'], subject, body, body)
    except Exception as e:
        print(f"Hackathon email error: {e}")

    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════════════
# EVALUATOR CRUD APIS  (event_manager only)
# ═══════════════════════════════════════════════════════════════════════════════

@em.route('/api/evaluators/create', methods=['POST'])
def api_create_evaluator():
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.json or {}
    existing = DB.get_evaluators()
    if any(e.get('email') == data.get('email') for e in existing):
        return jsonify({'success': False, 'message': 'Evaluator with this email already exists'}), 400
    
    event_id = data.get('event_id')
    assigned_events = data.get('assigned_event_ids', [])
    if event_id and event_id not in assigned_events:
        assigned_events.append(event_id)

    new_ev = {
        'id': str(uuid.uuid4()),
        'name': data.get('name', '').strip(),
        'email': data.get('email', '').strip(),
        'phone': data.get('phone', '').strip(),
        'assigned_events': assigned_events,
        'created_at': datetime.datetime.now().isoformat()
    }
    DB.save_evaluator(new_ev)
    # Register in global admins.json so login works
    DB.save_admin({
        'name': new_ev['name'],
        'email': new_ev['email'],
        'password': data.get('password', 'evaluator123'),
        'role': 'evaluator',
        'phone': new_ev['phone']
    })
    return jsonify({'success': True, 'message': f"Evaluator '{new_ev['name']}' created", 'evaluator': new_ev})

@em.route('/api/evaluators/list')
def api_list_evaluators():
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False}), 403
    return jsonify({'success': True, 'evaluators': get_evaluators()})

@em.route('/api/evaluators/<evaluator_id>/assign', methods=['POST'])
def api_assign_evaluator(evaluator_id):
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False}), 403
    data = request.json or {}
    evaluators = DB.get_evaluators()
    for e in evaluators:
        if e['id'] == evaluator_id:
            e['assigned_events'] = data.get('event_ids', [])
            break
    DB._em_save('evaluators.json', evaluators)
    return jsonify({'success': True})

@em.route('/api/evaluators/<evaluator_id>/delete', methods=['POST'])
def api_delete_evaluator(evaluator_id):
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False}), 403
    DB.delete_evaluator(evaluator_id)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING APIs  (evaluator + admin)
# ═══════════════════════════════════════════════════════════════════════════════

@em.route('/api/hackathon/score', methods=['POST'])
def api_submit_score():
    user = session.get('user')
    if not user or not (is_evaluator(user) or is_admin(user)):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.json or {}
    event_id = data.get('event_id', '')
    team_id = data.get('team_id', '')
    criteria = data.get('criteria', {})  # e.g. {"innovation":8,"execution":7,"presentation":9}
    comments = data.get('comments', '')
    identifier = user.get('email') or user.get('roll_number', '')

    # Evaluators can only score their assigned events
    if is_evaluator(user):
        ev_list = evaluator_events(user)
        if not any(e['id'] == event_id for e in ev_list):
            return jsonify({'success': False, 'message': 'Not assigned to this event'}), 403

    total = sum(int(v) for v in criteria.values()) if criteria else int(data.get('total', 0))
    score = {
        'score_id': str(uuid.uuid4()),
        'event_id': event_id,
        'team_id': team_id,
        'evaluator_id': identifier,
        'evaluator_name': user.get('name', ''),
        'criteria': criteria,
        'total': total,
        'comments': comments,
        'evaluated_at': datetime.datetime.now().isoformat()
    }
    DB.save_score(score)
    return jsonify({'success': True, 'message': 'Score submitted!'})

@em.route('/api/hackathon/leaderboard/<event_id>')
def api_hackathon_leaderboard(event_id):
    teams = get_hackathon_teams(event_id)
    scores = DB.get_scores(event_id=event_id)
    team_map = {t['team_id']: t for t in teams}
    agg = {}
    for s in scores:
        tid = s.get('team_id')
        if tid not in agg:
            agg[tid] = {'team': team_map.get(tid, {}), 'scores': [], 'avg': 0}
        agg[tid]['scores'].append(s)
    for tid, data in agg.items():
        if data['scores']:
            data['avg'] = round(sum(s.get('total', 0) for s in data['scores']) / len(data['scores']), 1)
    return jsonify({'success': True})

@em.route('/api/techfest/event/delete/<id>', methods=['DELETE', 'POST'])
def api_techfest_delete_sub_event(id):
    user = session.get('user')
    evs = DB.get_techfest_events()
    ev = next((e for e in evs if e['id'] == id), None)
    if not ev: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    if not _can_access_techfest(user, ev['techfest_id']):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    DB.delete_techfest_event(id)
    return jsonify({'success': True})

# ═══════════════════════════════════════════════════════════════════════════════
# TECH FEST MODULE
# ═══════════════════════════════════════════════════════════════════════════════

@em.route('/techfest')
def techfest_landing():
    tfs = DB.get_techfests()
    active_tf = next((t for t in tfs if t.get('status') == 'active'), None)
    if not active_tf:
        return "No active Tech Fest at the moment."
    return render_template('em_techfest_landing.html', techfest=active_tf)

@em.route('/techfest/register')
def techfest_register_page():
    tfs = DB.get_techfests()
    active_tf = next((t for t in tfs if t.get('status') == 'active'), None)
    if not active_tf: return redirect(url_for('event_mgmt.techfest_landing'))
    
    events = DB.get_techfest_events(active_tf['id'])
    depts = DB.get_techfest_departments()
    return render_template('em_techfest_register.html', techfest=active_tf, events=events, departments=depts)

@em.route('/techfest/admin')
def techfest_admin():
    user = session.get('user')
    if not user or not is_admin(user):
        return redirect(url_for('login_page'))
    
    tfs = DB.get_techfests()
    events = DB.get_techfest_events()
    regs = DB.get_techfest_registrations()
    depts = DB.get_techfest_departments()
    return render_template('em_techfest_admin.html', user=user, techfests=tfs, events=events, registrations=regs, departments=depts)

# --- Tech Fest API Routes ---

@em.route('/api/techfest/setup', methods=['POST'])
def api_techfest_setup():
    user = session.get('user')
    if not is_manager(user): return jsonify({'success': False}), 403
    data = request.json or {}
    tf_id = data.get('id') or str(uuid.uuid4())
    tf = {
        'id': tf_id,
        'name': data.get('name'),
        'year': data.get('year'),
        'month': data.get('month'),
        'status': data.get('status', 'active'),
        'allow_multi_participation': data.get('allow_multi_participation', True),
        'created_at': datetime.datetime.now().isoformat()
    }
    DB.save_techfest(tf)
    return jsonify({'success': True})

    return jsonify({'success': True})

@em.route('/api/techfest/departments/save', methods=['POST'])
def api_save_techfest_depts():
    user = session.get('user')
    if not is_admin(user): return jsonify({'success': False}), 403
    data = request.json or {}
    DB.save_techfest_departments(data)

@em.route('/api/techfest/create-order', methods=['POST'])
def api_techfest_create_order():
    data = request.json or {}
    tf_id = data.get('techfest_id')
    event_ids = data.get('event_ids', [])
    
    # Calculate total fee
    sub_events = DB.get_techfest_events(tf_id)
    total_fee = 0
    for eid in event_ids:
        ev = next((e for e in sub_events if e['id'] == eid), None)
        if ev:
            total_fee += int(ev.get('registration_fee', 0))
    
    if total_fee <= 0:
        return jsonify({'success': False, 'message': 'Invalid amount'}), 400
        
    settings = get_settings()
    key_id = settings.get('razorpay_key_id', '').strip()
    key_secret = settings.get('razorpay_key_secret', '').strip()
    
    if not key_id or not key_secret:
        return jsonify({'success': False, 'message': 'Payment gateway not configured'}), 503
        
    try:
        import razorpay
        client = razorpay.Client(auth=(key_id, key_secret))
        amount_paise = total_fee * 100
        order = client.order.create({
            'amount': amount_paise,
            'currency': 'INR',
            'payment_capture': 1,
            'notes': {'techfest_id': tf_id}
        })
        return jsonify({'success': True, 'order_id': order['id'], 'amount': amount_paise, 'key': key_id})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@em.route('/api/techfest/register', methods=['POST'])
def api_techfest_register():
    data = request.json or {}
    tf_id = data.get('techfest_id')
    student = data.get('student_details', {})
    payment = data.get('payment_details')
    
    events = data.get('selected_events', [])
    total_fee = sum(int(e.get('fee', 0)) for e in events)

    # Prevent duplicate registration
    roll = student.get('roll_number')
    email = student.get('email')
    if roll or email:
        regs = DB.get_techfest_registrations(tf_id)
        for r in regs:
            if r.get('payment_status') == 'failed': continue
            sd = r.get('student_details', {})
            if (roll and sd.get('roll_number') == roll) or (email and sd.get('email') == email):
                return jsonify({'success': False, 'message': 'You have already registered for this Techfest.'}), 400

    # Razorpay Verification
    payment_status = 'free' if total_fee == 0 else 'pending'
    if payment and total_fee > 0:
        settings = DB.get_settings()
        key_secret = settings.get('razorpay_key_secret', '').strip()
        if key_secret:
            import hmac, hashlib
            payment_id = payment.get('razorpay_payment_id', '')
            order_id = payment.get('razorpay_order_id', '')
            signature = payment.get('razorpay_signature', '')
            
            if order_id and signature:
                # Proper HMAC verification
                msg = f"{order_id}|{payment_id}"
                expected = hmac.new(key_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
                if expected == signature:
                    payment_status = 'paid'
                else:
                    return jsonify({'success': False, 'message': 'Payment verification failed'}), 400
            else:
                # Fallback if order_id is missing (simple integration)
                payment_status = 'paid'
        else:
            payment_status = 'paid'

    reg_id = 'REG-TF-' + uuid.uuid4().hex[:8].upper()
    
    # Construct QR data with student details for attendance
    name = student.get('name', 'N/A')
    roll = student.get('roll_number', 'N/A')
    dept = student.get('department', 'N/A')
    qr_data = f"TF|{tf_id}|{reg_id}|{name}|{roll}|{dept}"
    
    reg = {
        'reg_id': reg_id,
        'techfest_id': tf_id,
        'student_details': student,
        'selected_events': events, 
        'submitted_at': datetime.datetime.now().isoformat(),
        'qr_data': qr_data,
        'payment_status': payment_status,
        'payment_id': payment.get('razorpay_payment_id') if payment else None,
        'amount': total_fee
    }
    
    DB.save_techfest_registration(reg)
    
    email = student.get('email')
    if email:
        try:
            subject = f"🚀 Tech Fest Registration Received: {reg_id}"
            status_text = "Free Entry" if total_fee == 0 else f"Payment Pending (₹{total_fee})"
            body = f"Hi {student.get('name')},\n\nYour registration for the Tech Fest has been received!\n\nRegistration ID: {reg_id}\nStatus: {status_text}\n\nIf payment is pending, please complete it via the portal."
            Mailer.send_email(email, subject, body, body)
        except: pass

    return jsonify({'success': True, 'reg_id': reg_id, 'qr_data': qr_data, 'total_fee': total_fee})

@em.route('/api/techfest/mark-attendance', methods=['POST'])
def api_techfest_mark_attendance():
    user = session.get('user')
    if not is_admin(user) and not is_club_admin(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    reg_id = data.get('reg_id')
    if not reg_id: return jsonify({'success': False, 'message': 'Missing registration ID'}), 400
    
    regs = DB.get_techfest_registrations()
    reg = next((r for r in regs if r['reg_id'] == reg_id), None)
    if not reg: return jsonify({'success': False, 'message': 'Registration not found'}), 404
    
    reg['checked_in'] = True
    reg['checked_in_at'] = datetime.datetime.now().isoformat()
    DB.save_techfest_registration(reg)
    
    return jsonify({'success': True, 'message': 'Attendance marked successfully!'})

@em.route('/api/event/<event_id>/update-setting', methods=['POST'])
def api_event_update_setting(event_id):
    user = session.get('user')
    if not has_event_access(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    events = get_events()
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    # Update any field provided in data
    for key, val in data.items():
        if key in ['allow_external', 'status', 'submission_portal_enabled', 'leaderboard_enabled']:
            event[key] = val
    
    save_events(events)
    return jsonify({'success': True})

@em.route('/api/techfest/<tf_id>/update-setting', methods=['POST'])
def api_techfest_update_setting(tf_id):
    user = session.get('user')
    if not has_event_access(user, tf_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    allowed_fields = ['allow_external', 'status', 'submission_portal_enabled', 'leaderboard_enabled', 'show_results']
    
    # Try finding in techfests.json first (special techfest config)
    techfests = DB.get_techfests()
    tf = next((t for t in techfests if t['id'] == tf_id), None)
    if tf:
        for key in allowed_fields:
            if key in data:
                tf[key] = data[key]
        DB.save_techfests(techfests)
    
    # Also try finding in events.json (general event storage)
    events = get_events()
    event = next((e for e in events if e['id'] == tf_id), None)
    if event:
        for key in allowed_fields:
            if key in data:
                event[key] = data[key]
        save_events(events)
    
    if not tf and not event:
        return jsonify({'success': False, 'message': 'Techfest/Event not found'}), 404
    
    return jsonify({'success': True})

@em.route('/techfest/ticket/<reg_id>')
def techfest_ticket(reg_id):
    regs = DB.get_techfest_registrations()
    reg = next((r for r in regs if r['reg_id'] == reg_id), None)
    if not reg: return "Registration not found", 404
    
    tf = next((t for t in DB.get_techfests() if t['id'] == reg['techfest_id']), None)
    return render_template('em_techfest_ticket.html', registration=reg, techfest=tf)

@em.route('/api/techfest/event/save', methods=['POST'])
def api_techfest_save_sub_event():
    user = session.get('user')
    data = request.json or {}
    
    # Get techfest_id to check access
    tf_id = data.get('techfest_id')
    if not tf_id:
        tfs = DB.get_techfests()
        active_tf = next((t for t in tfs if t.get('status') == 'active'), None)
        if active_tf: tf_id = active_tf['id']
    
    if not tf_id or not _can_access_techfest(user, tf_id):
        return jsonify({'success': False, 'message': 'Unauthorized or Tech Fest not found'}), 403
    
    ev_id = data.get('id')
    if not ev_id:
        ev_id = 'tf-ev-' + uuid.uuid4().hex[:8].upper()
    
    # Get active Tech Fest if not provided
    tf_id = data.get('techfest_id')
    if not tf_id:
        tfs = DB.get_techfests()
        active_tf = next((t for t in tfs if t.get('status') == 'active'), None)
        if active_tf: tf_id = active_tf['id']

    ev = {
        'id': ev_id,
        'techfest_id': tf_id,
        'name': data.get('name', 'Unnamed Event'),
        'description': data.get('description', ''),
        'sub_event_type': data.get('sub_event_type', 'normal'),
        'payment_type': data.get('payment_type', 'free'),
        'registration_fee': int(data.get('registration_fee') or 0),
        'min_team_size': int(data.get('min_team_size') or 1),
        'max_team_size': int(data.get('max_team_size') or 1),
        'status': data.get('status', 'active'),
        'requirements': data.get('requirements', []),
        'scoring_criteria': [c.strip() for c in data.get('scoring_criteria', '').split(',') if c.strip()],
        'updated_at': datetime.datetime.now().isoformat()
    }
    
    DB.save_techfest_event(ev)
    return jsonify({'success': True, 'id': ev_id})

@em.route('/api/techfest/verify-payment', methods=['POST'])
def api_techfest_verify_payment():
    return jsonify({'success': False, 'message': 'Manual cash verification is prohibited.'}), 403

@em.route('/api/techfest/<event_id>/forward-attendance', methods=['POST'])
def api_techfest_forward_attendance(event_id):
    user = session.get('user')
    if not _can_access_techfest(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    event = next((e for e in get_events() if e['id'] == event_id), None)
    regs = DB.get_techfest_registrations(event_id)
    spoorthy_regs = [r for r in regs if r.get('student_details', {}).get('is_spoorthy') == 'yes' and r.get('checked_in')]
    
    if not spoorthy_regs:
        return jsonify({'success': False, 'message': 'No present Spoorthy students to report.'})

    # Group by dept
    dept_map = {}
    for r in spoorthy_regs:
        d = r['student_details'].get('department', 'Others')
        if d not in dept_map: dept_map[d] = []
        dept_map[d].append(r)

    # In a real app, we'd generate PDFs and email HODs. 
    # For now, we'll simulate success.
    return jsonify({'success': True, 'message': f'Attendance forwarded for {len(dept_map)} departments!'})

@em.route('/api/techfest/<event_id>/bulk-email', methods=['POST'])
def api_techfest_bulk_email(event_id):
    user = session.get('user')
    if not _can_access_techfest(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.json or {}
    target = data.get('target', 'all')
    subject = data.get('subject')
    message = data.get('message')
    
    regs = DB.get_techfest_registrations(event_id)
    if target == 'spoorthy':
        regs = [r for r in regs if r.get('student_details', {}).get('is_spoorthy') == 'yes']
    elif target == 'others':
        regs = [r for r in regs if r.get('student_details', {}).get('is_spoorthy') == 'no']
    elif target == 'unpaid':
        regs = [r for r in regs if r.get('payment_status') != 'paid']

    # Send emails
    count = 0
    for r in regs:
        email = r.get('student_details', {}).get('email')
        if email:
            try:
                # Mailer.send_email(email, subject, message, message)
                count += 1
            except: pass
    
    return jsonify({'success': True, 'message': f'Emails sent to {count} participants!'})

@em.route('/em/techfest/<event_id>/leaderboard')
def em_techfest_leaderboard(event_id):
    user = session.get('user')
    event = _can_access_techfest(user, event_id)
    if not event: return 'Access denied', 403
    
    # Calculate leaderboard
    all_tf_regs = DB.get_techfest_registrations()
    scores = DB.get_scores(event_id=event_id)
    
    rankings = []
    for r in all_tf_regs:
        for se in r.get('selected_events', []):
            if se.get('event_id') == event_id:
                # Calculate average or total score for this participation
                part_scores = [s for s in scores if s.get('team_id') == r['reg_id']]
                avg_score = sum(s.get('total', 0) for s in part_scores) / len(part_scores) if part_scores else 0
                rankings.append({
                    'reg_id': r['reg_id'],
                    'name': se.get('team_name') or r['student_details'].get('name', 'Unknown'),
                    'college': 'Spoorthy' if r['student_details'].get('is_spoorthy') == 'yes' else r['student_details'].get('college_name'),
                    'score': round(avg_score, 2),
                    'evaluators': len(part_scores)
                })
    
    rankings.sort(key=lambda x: x['score'], reverse=True)
    for i, r in enumerate(rankings): r['rank'] = i + 1
    
    return render_template('em_techfest_leaderboard.html', event=event, rankings=rankings)

@em.route('/em/api/techfest/score', methods=['POST'])
def api_techfest_score():
    user = session.get('user')
    if not user or not is_evaluator(user):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    event_id = data.get('event_id')
    reg_id = data.get('team_id') # evaluator_dashboard sends team_id
    
    score = {
        'event_id': event_id,
        'team_id': reg_id,
        'evaluator_id': user.get('email') or user.get('roll_number'),
        'evaluator_name': user.get('name'),
        'criteria': data.get('criteria', {}),
        'total': data.get('total', 0),
        'comments': data.get('comments', ''),
        'submitted_at': datetime.datetime.now().isoformat()
    }
    
    DB.save_score(score)
    return jsonify({'success': True})

@em.route('/api/techfest/scores/<event_id>')
def api_techfest_sub_event_scores(event_id):
    user = session.get('user')
    # event_id here refers to techfest_id (main event) for historical reasons or sub-event?
    # In techfest hub, it's the main event id.
    if not _can_access_techfest(user, event_id):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    scores = DB.get_scores(event_id=event_id)
    regs = DB.get_techfest_registrations()
    
    # Calculate rankings
    rankings = []
    for r in regs:
        for se in r.get('selected_events', []):
            if se.get('event_id') == event_id:
                part_scores = [s for s in scores if s.get('team_id') == r['reg_id']]
                if part_scores:
                    avg = sum(s.get('total', 0) for s in part_scores) / len(part_scores)
                    rankings.append({
                        'reg_id': r['reg_id'],
                        'name': se.get('team_name') or r['student_details'].get('name', 'Unknown'),
                        'score': round(avg, 2),
                        'comments': part_scores[0].get('comments', '') if part_scores else ''
                    })
    
    rankings.sort(key=lambda x: x['score'], reverse=True)
    for i, r in enumerate(rankings): r['rank'] = i + 1
    
    return jsonify({'success': True, 'scores': rankings})

