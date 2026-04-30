import re
import json
import os
from werkzeug.security import generate_password_hash, check_password_hash
from app.database import cursor

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
EM_DIR = os.path.join(DATA_DIR, 'em')

def slugify(text):
    return re.sub(r'[\W_]+', '_', text.lower()).strip('_')

def _row(r):
    return dict(r) if r else None

def _num(val):
    if not val: return 0
    try: return int(float(val))
    except (ValueError, TypeError): return 0

def _flatten_members(lst):
    if not lst: return []
    out = []
    for item in lst:
        if isinstance(item, dict): out.append(str(item.get('name', item.get('email', str(item)))))
        else: out.append(str(item))
    return out

class DB:
    @staticmethod
    def hash_password(password):
        return generate_password_hash(password)

    @staticmethod
    def verify_password(stored_val, provided_val):
        if not stored_val or not provided_val: return False
        if stored_val.startswith(('pbkdf2:sha256:', 'scrypt:', 'argon2:')):
            return check_password_hash(stored_val, provided_val)
        return stored_val == provided_val

    # ─── Students ─────────────────────────────────────────────────────────────
    @staticmethod
    def get_students():
        with cursor() as cur:
            cur.execute("SELECT * FROM students;")
            students = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM student_contributions;")
            conts = [dict(r) for r in cur.fetchall()]
            c_map = {}
            for c in conts:
                r = c['roll_number']
                if r not in c_map: c_map[r] = []
                c_clean = dict(c)
                c_clean.pop('id', None)
                c_clean.pop('roll_number', None)
                c_map[r].append(c_clean)
            for s in students:
                s['contributions'] = c_map.get(s['roll_number'], [])
            return students

    @staticmethod
    def get_student_by_roll(roll):
        with cursor() as cur:
            cur.execute("SELECT * FROM students WHERE UPPER(roll_number) = UPPER(%s);", (roll,))
            s = _row(cur.fetchone())
            if s:
                cur.execute("SELECT * FROM student_contributions WHERE UPPER(roll_number) = UPPER(%s);", (roll,))
                conts = []
                for r in cur.fetchall():
                    d = dict(r)
                    d.pop('id', None)
                    d.pop('roll_number', None)
                    conts.append(d)
                s['contributions'] = conts
            return s

    @staticmethod
    def save_student(student):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO students (roll_number, name, email, phone, role, dob, department, year, class, photo, password)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (roll_number) DO UPDATE SET
                    name=EXCLUDED.name, email=EXCLUDED.email, phone=EXCLUDED.phone, role=EXCLUDED.role,
                    dob=EXCLUDED.dob, department=EXCLUDED.department, year=EXCLUDED.year, class=EXCLUDED.class,
                    photo=EXCLUDED.photo, password=EXCLUDED.password;
            """, (
                student.get('roll_number'), student.get('name'), student.get('email'), student.get('phone'),
                student.get('role'), student.get('dob'), student.get('department'), student.get('year'),
                student.get('class'), student.get('photo'), student.get('password')
            ))
            
            cur.execute("DELETE FROM student_contributions WHERE roll_number = %s;", (student.get('roll_number'),))
            conts = student.get('contributions', [])
            for c in conts:
                cur.execute("""
                    INSERT INTO student_contributions (roll_number, club_id, role, status, tenure_year, events_organized)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (
                    student.get('roll_number'), c.get('club_id'), c.get('role'),
                    c.get('status'), c.get('tenure_year'), c.get('events_organized')
                ))

    @staticmethod
    def save_students(students):
        for s in students: DB.save_student(s)

    # ─── Admins ───────────────────────────────────────────────────────────────
    @staticmethod
    def get_admins():
        with cursor() as cur:
            cur.execute("SELECT * FROM admins;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_admin(admin):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO admins (email, name, phone, role, password, signature)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    name=EXCLUDED.name, phone=EXCLUDED.phone, role=EXCLUDED.role,
                    password=EXCLUDED.password, signature=EXCLUDED.signature;
            """, (
                admin.get('email'), admin.get('name'), admin.get('phone'),
                admin.get('role'), admin.get('password'), admin.get('signature')
            ))

    @staticmethod
    def get_admin_by_role(role):
        with cursor() as cur:
            cur.execute("SELECT * FROM admins WHERE role = %s LIMIT 1;", (role,))
            return _row(cur.fetchone())

    @staticmethod
    def get_admin_by_email(email):
        with cursor() as cur:
            cur.execute("SELECT * FROM admins WHERE email = %s;", (email,))
            return _row(cur.fetchone())

    # ─── Institutions ─────────────────────────────────────────────────────────
    @staticmethod
    def get_institutions():
        with cursor() as cur:
            cur.execute("SELECT * FROM institutions;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_institution(inst):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO institutions (id, name, domain, logo, tagline, address)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name, domain=EXCLUDED.domain, logo=EXCLUDED.logo,
                    tagline=EXCLUDED.tagline, address=EXCLUDED.address;
            """, (
                inst.get('id'), inst.get('name'), inst.get('domain'),
                inst.get('logo'), inst.get('tagline'), inst.get('address')
            ))

    @staticmethod
    def get_institution_by_id(inst_id):
        with cursor() as cur:
            cur.execute("SELECT * FROM institutions WHERE id = %s;", (inst_id,))
            return _row(cur.fetchone())

    @staticmethod
    def get_institution_by_domain(domain):
        with cursor() as cur:
            cur.execute("SELECT * FROM institutions WHERE domain = %s LIMIT 1;", (domain,))
            return _row(cur.fetchone())

    # ─── Clubs ────────────────────────────────────────────────────────────────
    @staticmethod
    def _assemble_club(c, obs):
        d = dict(c)
        d['office_bearers'] = [dict(o) for o in obs if o['club_id'] == d['id']]
        if 'features' not in d or not d['features']: d['features'] = []
        if 'gallery' not in d or not d['gallery']: d['gallery'] = []
        if d.get('mentor_name'):
            d['mentor'] = {'name': d.pop('mentor_name'), 'signature': d.pop('mentor_signature', '')}
        else:
            d['mentor'] = {}
        return d

    @staticmethod
    def get_clubs():
        with cursor() as cur:
            cur.execute("SELECT * FROM clubs;")
            clubs = cur.fetchall()
            cur.execute("SELECT * FROM club_office_bearers;")
            obs = cur.fetchall()
            return [DB._assemble_club(c, obs) for c in clubs]

    @staticmethod
    def save_club(club):
        with cursor(dict_cursor=False) as cur:
            mentor = club.get('mentor', {})
            cur.execute("""
                INSERT INTO clubs (id, admin_roll, name, about, mission, vision, logo, cover_image, mentor_name, mentor_signature, features, gallery)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    admin_roll=EXCLUDED.admin_roll, name=EXCLUDED.name, about=EXCLUDED.about, mission=EXCLUDED.mission,
                    vision=EXCLUDED.vision, logo=EXCLUDED.logo, cover_image=EXCLUDED.cover_image,
                    mentor_name=EXCLUDED.mentor_name, mentor_signature=EXCLUDED.mentor_signature,
                    features=EXCLUDED.features, gallery=EXCLUDED.gallery;
            """, (
                club.get('id'), club.get('admin_roll'), club.get('name'), club.get('about'),
                club.get('mission'), club.get('vision'), club.get('logo'), club.get('cover_image'),
                mentor.get('name'), mentor.get('signature'), club.get('features', []), club.get('gallery', [])
            ))
            
            cur.execute("DELETE FROM club_office_bearers WHERE club_id = %s;", (club.get('id'),))
            obs = club.get('office_bearers', [])
            for o in obs:
                cur.execute("""
                    INSERT INTO club_office_bearers (club_id, name, roll_number, role, photo)
                    VALUES (%s, %s, %s, %s, %s);
                """, (club.get('id'), o.get('name'), o.get('roll_number'), o.get('role'), o.get('photo')))

    @staticmethod
    def get_club_by_id(club_id):
        with cursor() as cur:
            cur.execute("SELECT * FROM clubs WHERE id = %s;", (club_id,))
            c = cur.fetchone()
            if not c: return None
            cur.execute("SELECT * FROM club_office_bearers WHERE club_id = %s;", (club_id,))
            obs = cur.fetchall()
            return DB._assemble_club(c, obs)

    @staticmethod
    def get_club_by_admin(admin_id):
        with cursor() as cur:
            cur.execute("SELECT * FROM clubs WHERE admin_roll = %s LIMIT 1;", (admin_id,))
            c = cur.fetchone()
            if not c: return None
            cur.execute("SELECT * FROM club_office_bearers WHERE club_id = %s;", (c['id'],))
            obs = cur.fetchall()
            return DB._assemble_club(c, obs)

    # ─── Events ───────────────────────────────────────────────────────────────
    @staticmethod
    def _assemble_event(e):
        d = dict(e)
        if not d.get('year'):
            import datetime
            ts = d.get('timestamp')
            try: dt = datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now()
            except: dt = datetime.datetime.now()
            if dt.month >= 6: d['year'] = f"{dt.year % 100}-{(dt.year + 1) % 100}"
            else: d['year'] = f"{(dt.year - 1) % 100}-{dt.year % 100}"
        return d

    @staticmethod
    def get_events(club_id=None):
        with cursor() as cur:
            if club_id:
                cur.execute("SELECT * FROM events WHERE club_id = %s OR %s = ANY(collaborating_clubs);", (club_id, club_id))
            else:
                cur.execute("SELECT * FROM events;")
            return [DB._assemble_event(r) for r in cur.fetchall()]

    @staticmethod
    def get_event_by_id(club_id, event_id):
        events = DB.get_events(club_id)
        return next((e for e in events if e.get('id') == event_id), None)

    @staticmethod
    def save_event(club_id, event):
        actual_club_id = event.get('club_id', club_id)
        if 'club_id' not in event: event['club_id'] = actual_club_id
        
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO events (
                    id, club_id, title, date, time, venue, description, poster, event_status, payment_type, registration_type,
                    fee, cash, flex, printing, distribution, memento, honoring, sandal, sweets, refreshment,
                    chairs, mic, internet, others, guest_feedback, student_feedback, organizer_feedback,
                    after_info, before_info, news, appointment_date, resource_person, transport_receive, transport_send,
                    approved, approval_status, approval_chain, approver_signatures, proposer_signatures, fully_approved_at,
                    report, report_url, report_approved, report_approvals, report_workflow_status,
                    collaborating_clubs, participants, timestamp, date_str, year, event_finished
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                ) ON CONFLICT (id) DO UPDATE SET
                    club_id=EXCLUDED.club_id, title=EXCLUDED.title, date=EXCLUDED.date, time=EXCLUDED.time, venue=EXCLUDED.venue,
                    description=EXCLUDED.description, poster=EXCLUDED.poster, event_status=EXCLUDED.event_status,
                    payment_type=EXCLUDED.payment_type, registration_type=EXCLUDED.registration_type, fee=EXCLUDED.fee, cash=EXCLUDED.cash,
                    flex=EXCLUDED.flex, printing=EXCLUDED.printing, distribution=EXCLUDED.distribution, memento=EXCLUDED.memento,
                    honoring=EXCLUDED.honoring, sandal=EXCLUDED.sandal, sweets=EXCLUDED.sweets, refreshment=EXCLUDED.refreshment,
                    chairs=EXCLUDED.chairs, mic=EXCLUDED.mic, internet=EXCLUDED.internet, others=EXCLUDED.others,
                    guest_feedback=EXCLUDED.guest_feedback, student_feedback=EXCLUDED.student_feedback, organizer_feedback=EXCLUDED.organizer_feedback,
                    after_info=EXCLUDED.after_info, before_info=EXCLUDED.before_info, news=EXCLUDED.news,
                    appointment_date=EXCLUDED.appointment_date, resource_person=EXCLUDED.resource_person,
                    transport_receive=EXCLUDED.transport_receive, transport_send=EXCLUDED.transport_send,
                    approved=EXCLUDED.approved, approval_status=EXCLUDED.approval_status, approval_chain=EXCLUDED.approval_chain,
                    approver_signatures=EXCLUDED.approver_signatures, proposer_signatures=EXCLUDED.proposer_signatures,
                    fully_approved_at=EXCLUDED.fully_approved_at, report=EXCLUDED.report, report_url=EXCLUDED.report_url,
                    report_approved=EXCLUDED.report_approved, report_approvals=EXCLUDED.report_approvals, report_workflow_status=EXCLUDED.report_workflow_status,
                    collaborating_clubs=EXCLUDED.collaborating_clubs, participants=EXCLUDED.participants, timestamp=EXCLUDED.timestamp,
                    date_str=EXCLUDED.date_str, year=EXCLUDED.year, event_finished=EXCLUDED.event_finished;
            """, (
                event.get('id'), event.get('club_id'), event.get('title'), event.get('date'), event.get('time'), event.get('venue'),
                event.get('description'), event.get('poster'), event.get('event_status'), event.get('payment_type'), event.get('registration_type'),
                _num(event.get('fee')), _num(event.get('cash')), _num(event.get('flex')), _num(event.get('printing')), _num(event.get('distribution')),
                _num(event.get('memento')), _num(event.get('honoring')), _num(event.get('sandal')), _num(event.get('sweets')), _num(event.get('refreshment')),
                event.get('chairs'), event.get('mic'), event.get('internet'), event.get('others'),
                event.get('guest_feedback'), event.get('student_feedback'), event.get('organizer_feedback'),
                event.get('after_info') or event.get('after'), event.get('before_info') or event.get('before'), event.get('news'),
                event.get('appointment_date'), event.get('resource_person'), event.get('transport_receive'), event.get('transport_send'),
                event.get('approved', False), event.get('approval_status'), event.get('approval_chain', []),
                event.get('approver_signatures', []), event.get('proposer_signatures', []), event.get('fully_approved_at'),
                event.get('report'), event.get('report_url'), event.get('report_approved', False), event.get('report_approvals', []),
                event.get('report_workflow_status'), event.get('collaborating_clubs', []), event.get('participants'),
                event.get('timestamp'), event.get('date_str'), event.get('year'), event.get('event_finished', False)
            ))

    @staticmethod
    def update_events(club_id, events):
        for e in events: DB.save_event(club_id, e)

    # ─── Registrations ────────────────────────────────────────────────────────
    @staticmethod
    def get_registrations(club_id=None, event_id=None):
        with cursor() as cur:
            if club_id and event_id:
                cur.execute("SELECT * FROM registrations WHERE club_id = %s AND event_id = %s;", (club_id, event_id))
            elif club_id:
                cur.execute("SELECT * FROM registrations WHERE club_id = %s;", (club_id,))
            else:
                cur.execute("SELECT * FROM registrations;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_registration(club_id, reg):
        events = DB.get_events(club_id)
        event = next((e for e in events if e.get('id') == reg.get('event_id')), None)
        actual_club = event.get('club_id', club_id) if event else club_id
        
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO registrations (
                    club_id, event_id, email, name, roll_number, phone, department, year,
                    payment_status, payment_id, payment_verified, checked_in, team_id, team_name, team_role, team_members
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (
                actual_club, reg.get('event_id'), reg.get('email'), reg.get('name'), reg.get('roll_number'),
                reg.get('phone'), reg.get('department'), reg.get('year'), reg.get('payment_status'),
                reg.get('payment_id'), reg.get('payment_verified', False), reg.get('checked_in', False),
                reg.get('team_id'), reg.get('team_name'), reg.get('team_role'), _flatten_members(reg.get('team_members', []))
            ))

    @staticmethod
    def update_event_registrations(club_id, event_id, regs):
        with cursor(dict_cursor=False) as cur:
            cur.execute("DELETE FROM registrations WHERE event_id = %s;", (event_id,))
        for r in regs: DB.save_registration(club_id, r)

    @staticmethod
    def update_registrations(club_id, regs):
        if not regs: return
        grouped = {}
        for r in regs:
            eid = r.get('event_id')
            if eid not in grouped: grouped[eid] = []
            grouped[eid].append(r)
        for eid, event_regs in grouped.items():
            DB.update_event_registrations(club_id, eid, event_regs)

    @staticmethod
    def get_club_stats(club_id):
        club = DB.get_club_by_id(club_id)
        if not club: return None
        events = DB.get_events(club_id)
        regs = DB.get_registrations(club_id)
        total_spent = 0
        completed_events = 0
        total_revenue = 0
        for e in events:
            if e.get('report_approved'): completed_events += 1
            event_spent = int(e.get('cash') or 0) + int(e.get('printing') or 0)
            collabs = e.get('collaborating_clubs', [])
            if collabs: event_spent = int(event_spent / (1 + len(collabs)))
            total_spent += event_spent
            event_revenue = int(e.get('fee') or 0)
            if collabs: event_revenue = int(event_revenue / (1 + len(collabs)))
            total_revenue += event_revenue
            
        dept_dist = {}
        year_dist = {}
        for r in regs:
            d = r.get('department') or 'N/A'
            y = r.get('year') or 'N/A'
            dept_dist[d] = dept_dist.get(d, 0) + 1
            year_dist[y] = year_dist.get(y, 0) + 1

        return {
            "total_events": len(events), "completed_events": completed_events,
            "total_registrations": len(regs), "total_revenue": total_revenue,
            "total_spent": total_spent, "net_balance": total_revenue - total_spent,
            "dept_distribution": dept_dist, "year_distribution": year_dist
        }

    @staticmethod
    def get_global_stats():
        clubs = DB.get_clubs()
        club_stats = [s for c in clubs if (s := DB.get_club_stats(c['id']))]
        return {
            "total_clubs": len(clubs),
            "total_events": sum(s['total_events'] for s in club_stats),
            "total_revenue": sum(s['total_revenue'] for s in club_stats),
            "total_spending": sum(s['total_spent'] for s in club_stats)
        }

    # ─── Settings, Contacts & Elections ───────────────────────────────────────
    @staticmethod
    def get_settings():
        with cursor() as cur:
            cur.execute("SELECT * FROM settings WHERE id = 1;")
            r = cur.fetchone()
            return dict(r) if r else {}

    @staticmethod
    def save_settings(settings):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO settings (id, maintenance_mode, academic_year, theme) VALUES (1, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET maintenance_mode=EXCLUDED.maintenance_mode, academic_year=EXCLUDED.academic_year, theme=EXCLUDED.theme;
            """, (settings.get('maintenance_mode', False), settings.get('academic_year'), settings.get('theme')))

    @staticmethod
    def get_contacts():
        with cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE id = 1;")
            r = cur.fetchone()
            return dict(r) if r else {}

    @staticmethod
    def save_contacts(contacts):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO contacts (id, email, phone, address, facebook, instagram, twitter, linkedin)
                VALUES (1, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    email=EXCLUDED.email, phone=EXCLUDED.phone, address=EXCLUDED.address,
                    facebook=EXCLUDED.facebook, instagram=EXCLUDED.instagram, twitter=EXCLUDED.twitter, linkedin=EXCLUDED.linkedin;
            """, (
                contacts.get('email'), contacts.get('phone'), contacts.get('address'),
                contacts.get('facebook'), contacts.get('instagram'), contacts.get('twitter'), contacts.get('linkedin')
            ))

    @staticmethod
    def get_elections(club_id):
        with cursor() as cur:
            cur.execute("SELECT * FROM elections WHERE club_id = %s;", (club_id,))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_elections(club_id, elections):
        with cursor(dict_cursor=False) as cur:
            cur.execute("DELETE FROM elections WHERE club_id = %s;", (club_id,))
            for e in elections:
                cur.execute("INSERT INTO elections (id, club_id, title, year, status, candidates, voters) VALUES (%s, %s, %s, %s, %s, %s, %s);",
                    (e.get('id'), club_id, e.get('title'), e.get('year'), e.get('status'), e.get('candidates', []), e.get('voters', [])))

    @staticmethod
    def get_office_bearer_requests():
        with cursor() as cur:
            cur.execute("SELECT * FROM office_bearer_requests;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_office_bearer_request(req):
        with cursor(dict_cursor=False) as cur:
            cur.execute("INSERT INTO office_bearer_requests (club_id, roll_number, name, role, status, reason) VALUES (%s, %s, %s, %s, %s, %s);",
                (req.get('club_id'), req.get('roll_number'), req.get('name'), req.get('role'), req.get('status'), req.get('reason')))

    @staticmethod
    def update_office_bearer_requests(reqs):
        with cursor(dict_cursor=False) as cur:
            cur.execute("TRUNCATE office_bearer_requests RESTART IDENTITY;")
            for r in reqs:
                cur.execute("INSERT INTO office_bearer_requests (club_id, roll_number, name, role, status, reason) VALUES (%s, %s, %s, %s, %s, %s);",
                    (r.get('club_id'), r.get('roll_number'), r.get('name'), r.get('role'), r.get('status'), r.get('reason')))

    # ─── EM Modules ───────────────────────────────────────────────────────────
    @staticmethod
    def get_em_events():
        with cursor() as cur:
            cur.execute("SELECT * FROM em_events;")
            return [dict(r) for r in cur.fetchall()]
            
    @staticmethod
    def save_em_events(events):
        with cursor(dict_cursor=False) as cur:
            for e in events:
                cur.execute("""
                    INSERT INTO em_events (id, title, description, date, time, venue, organized_by, organized_by_id, assigned_admin, status, banner, max_capacity, ticket_price, event_type, event_category, allow_external, created_by, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, description=EXCLUDED.description, date=EXCLUDED.date, time=EXCLUDED.time, venue=EXCLUDED.venue, organized_by=EXCLUDED.organized_by, organized_by_id=EXCLUDED.organized_by_id, assigned_admin=EXCLUDED.assigned_admin, status=EXCLUDED.status, banner=EXCLUDED.banner, max_capacity=EXCLUDED.max_capacity, ticket_price=EXCLUDED.ticket_price, event_type=EXCLUDED.event_type, event_category=EXCLUDED.event_category, allow_external=EXCLUDED.allow_external, created_by=EXCLUDED.created_by, created_at=EXCLUDED.created_at;
                """, (
                    e.get('id'), e.get('title'), e.get('description'), e.get('date'), e.get('time'), e.get('venue'),
                    e.get('organized_by'), e.get('organized_by_id'), e.get('assigned_admin'), e.get('status'), e.get('banner'),
                    _num(e.get('max_capacity')), _num(e.get('ticket_price')), e.get('event_type'), e.get('event_category'), e.get('allow_external', False),
                    e.get('created_by'), e.get('created_at')
                ))

    @staticmethod
    def get_em_tickets():
        with cursor() as cur:
            cur.execute("SELECT * FROM em_tickets;")
            return [dict(r) for r in cur.fetchall()]
            
    @staticmethod
    def save_em_tickets(tickets):
        with cursor(dict_cursor=False) as cur:
            for t in tickets:
                cur.execute("""
                    INSERT INTO em_tickets (ticket_id, event_id, user_id, user_email, user_name, user_phone, user_roll, user_dept, user_year, college_name, payment_status, payment_id, payment_method, amount, order_id, checked_in, checked_in_at, qr_data, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ticket_id) DO UPDATE SET event_id=EXCLUDED.event_id, user_id=EXCLUDED.user_id, user_email=EXCLUDED.user_email, user_name=EXCLUDED.user_name, user_phone=EXCLUDED.user_phone, user_roll=EXCLUDED.user_roll, user_dept=EXCLUDED.user_dept, user_year=EXCLUDED.user_year, college_name=EXCLUDED.college_name, payment_status=EXCLUDED.payment_status, payment_id=EXCLUDED.payment_id, payment_method=EXCLUDED.payment_method, amount=EXCLUDED.amount, order_id=EXCLUDED.order_id, checked_in=EXCLUDED.checked_in, checked_in_at=EXCLUDED.checked_in_at, qr_data=EXCLUDED.qr_data;
                """, (
                    t.get('ticket_id'), t.get('event_id'), t.get('user_id'), t.get('user_email'), t.get('user_name'),
                    t.get('user_phone'), t.get('user_roll'), t.get('user_dept'), t.get('user_year'), t.get('college_name'),
                    t.get('payment_status'), t.get('payment_id'), t.get('payment_method'), _num(t.get('amount')), t.get('order_id'),
                    t.get('checked_in', False), t.get('checked_in_at'), t.get('qr_data'), t.get('created_at')
                ))

    @staticmethod
    def get_em_admins():
        with cursor() as cur:
            cur.execute("SELECT * FROM em_admins;")
            return [dict(r) for r in cur.fetchall()]
            
    @staticmethod
    def save_em_admins(admins):
        with cursor(dict_cursor=False) as cur:
            for a in admins:
                cur.execute("""
                    INSERT INTO em_admins (id, email, name, phone, active_event, assigned_events, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name, phone=EXCLUDED.phone, active_event=EXCLUDED.active_event, assigned_events=EXCLUDED.assigned_events;
                """, (a.get('id'), a.get('email'), a.get('name'), a.get('phone'), a.get('active_event'), a.get('assigned_events', []), a.get('created_at')))

    @staticmethod
    def get_em_settings():
        with cursor() as cur:
            cur.execute("SELECT * FROM em_settings WHERE id = 1;")
            r = cur.fetchone()
            return dict(r) if r else {}
        
    @staticmethod
    def put_em_settings(d):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO em_settings (id, campus_lat, campus_lng, campus_radius, payment_demo_mode, razorpay_key_id, razorpay_key_secret)
                VALUES (1, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET campus_lat=EXCLUDED.campus_lat, campus_lng=EXCLUDED.campus_lng, campus_radius=EXCLUDED.campus_radius, payment_demo_mode=EXCLUDED.payment_demo_mode, razorpay_key_id=EXCLUDED.razorpay_key_id, razorpay_key_secret=EXCLUDED.razorpay_key_secret;
            """, (d.get('campus_lat'), d.get('campus_lng'), d.get('campus_radius'), d.get('payment_demo_mode', False), d.get('razorpay_key_id'), d.get('razorpay_key_secret')))

    @staticmethod
    def get_hackathon_teams(event_id=None):
        with cursor() as cur:
            if event_id: cur.execute("SELECT * FROM hackathon_teams WHERE event_id = %s;", (event_id,))
            else: cur.execute("SELECT * FROM hackathon_teams;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_hackathon_team(team):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO hackathon_teams (team_id, event_id, team_name, leader_id, members, project_title, description, github_url, demo_url, submission_file, submitted, submitted_at, payment_status, payment_method, checked_in, checked_in_at, qr_data, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (team_id) DO UPDATE SET event_id=EXCLUDED.event_id, team_name=EXCLUDED.team_name, leader_id=EXCLUDED.leader_id, members=EXCLUDED.members, project_title=EXCLUDED.project_title, description=EXCLUDED.description, github_url=EXCLUDED.github_url, demo_url=EXCLUDED.demo_url, submission_file=EXCLUDED.submission_file, submitted=EXCLUDED.submitted, submitted_at=EXCLUDED.submitted_at, payment_status=EXCLUDED.payment_status, payment_method=EXCLUDED.payment_method, checked_in=EXCLUDED.checked_in, checked_in_at=EXCLUDED.checked_in_at, qr_data=EXCLUDED.qr_data;
            """, (
                team.get('team_id'), team.get('event_id'), team.get('team_name'), team.get('leader_id'), _flatten_members(team.get('members', [])),
                team.get('project_title'), team.get('description'), team.get('github_url'), team.get('demo_url'),
                team.get('submission_file'), team.get('submitted', False), team.get('submitted_at'),
                team.get('payment_status'), team.get('payment_method'), team.get('checked_in', False),
                team.get('checked_in_at'), team.get('qr_data'), team.get('created_at')
            ))

    @staticmethod
    def delete_hackathon_team(team_id):
        with cursor(dict_cursor=False) as cur: cur.execute("DELETE FROM hackathon_teams WHERE team_id = %s;", (team_id,))

    @staticmethod
    def get_evaluators():
        with cursor() as cur:
            cur.execute("SELECT * FROM evaluators;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_evaluator(ev):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO evaluators (id, name, email, phone, assigned_events, created_at) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, email=EXCLUDED.email, phone=EXCLUDED.phone, assigned_events=EXCLUDED.assigned_events;
            """, (ev.get('id'), ev.get('name'), ev.get('email'), ev.get('phone'), ev.get('assigned_events', []), ev.get('created_at')))

    @staticmethod
    def delete_evaluator(ev_id):
        with cursor(dict_cursor=False) as cur: cur.execute("DELETE FROM evaluators WHERE id = %s;", (ev_id,))

    @staticmethod
    def get_scores(event_id=None, team_id=None):
        with cursor() as cur:
            if event_id and team_id: cur.execute("SELECT * FROM scores WHERE event_id = %s AND team_id = %s;", (event_id, team_id))
            elif event_id: cur.execute("SELECT * FROM scores WHERE event_id = %s;", (event_id,))
            elif team_id: cur.execute("SELECT * FROM scores WHERE team_id = %s;", (team_id,))
            else: cur.execute("SELECT * FROM scores;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_score(score):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO scores (event_id, team_id, evaluator_id, score_value, feedback, created_at) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_id, team_id, evaluator_id) DO UPDATE SET score_value=EXCLUDED.score_value, feedback=EXCLUDED.feedback;
            """, (score.get('event_id'), score.get('team_id'), score.get('evaluator_id'), _num(score.get('score_value')), score.get('feedback'), score.get('created_at')))

    # ─── Tech Fest ────────────────────────────────────────────────────────────
    @staticmethod
    def get_techfests():
        with cursor() as cur:
            cur.execute("SELECT * FROM techfests;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_techfest(tf):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO techfests (id, name, year, month, status, allow_multi_participation) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, year=EXCLUDED.year, month=EXCLUDED.month, status=EXCLUDED.status, allow_multi_participation=EXCLUDED.allow_multi_participation;
            """, (tf.get('id'), tf.get('name'), tf.get('year'), tf.get('month'), tf.get('status'), tf.get('allow_multi_participation', False)))

    @staticmethod
    def get_techfest_events(tf_id=None):
        with cursor() as cur:
            if tf_id: cur.execute("SELECT * FROM techfest_events WHERE techfest_id = %s;", (tf_id,))
            else: cur.execute("SELECT * FROM techfest_events;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_techfest_event(ev):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO techfest_events (id, techfest_id, name, description, sub_event_type, min_team_size, max_team_size, registration_fee, payment_type, requirements, scoring_criteria, status, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET techfest_id=EXCLUDED.techfest_id, name=EXCLUDED.name, description=EXCLUDED.description, sub_event_type=EXCLUDED.sub_event_type, min_team_size=EXCLUDED.min_team_size, max_team_size=EXCLUDED.max_team_size, registration_fee=EXCLUDED.registration_fee, payment_type=EXCLUDED.payment_type, requirements=EXCLUDED.requirements, scoring_criteria=EXCLUDED.scoring_criteria, status=EXCLUDED.status, updated_at=EXCLUDED.updated_at;
            """, (
                ev.get('id'), ev.get('techfest_id'), ev.get('name'), ev.get('description'), ev.get('sub_event_type'),
                _num(ev.get('min_team_size')), _num(ev.get('max_team_size')), _num(ev.get('registration_fee')), ev.get('payment_type'),
                ev.get('requirements'), ev.get('scoring_criteria'), ev.get('status'), ev.get('updated_at')
            ))

    @staticmethod
    def delete_techfest_event(ev_id):
        with cursor(dict_cursor=False) as cur: cur.execute("DELETE FROM techfest_events WHERE id = %s;", (ev_id,))

    @staticmethod
    def get_techfest_registrations(tf_id=None):
        with cursor() as cur:
            if tf_id: cur.execute("SELECT * FROM techfest_registrations WHERE techfest_id = %s;", (tf_id,))
            else: cur.execute("SELECT * FROM techfest_registrations;")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def save_techfest_registration(reg):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO techfest_registrations (reg_id, techfest_id, event_id, team_name, leader_id, members, payment_status, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (reg_id) DO UPDATE SET techfest_id=EXCLUDED.techfest_id, event_id=EXCLUDED.event_id, team_name=EXCLUDED.team_name, leader_id=EXCLUDED.leader_id, members=EXCLUDED.members, payment_status=EXCLUDED.payment_status;
            """, (
                reg.get('reg_id'), reg.get('techfest_id'), reg.get('event_id'), reg.get('team_name'),
                reg.get('leader_id'), _flatten_members(reg.get('members', [])), reg.get('payment_status'), reg.get('created_at')
            ))

    @staticmethod
    def get_techfest_departments():
        with cursor() as cur:
            cur.execute("SELECT * FROM techfest_departments WHERE id = 1;")
            r = cur.fetchone()
            if r:
                return {'UG': r.get('ug_depts', []), 'PG': r.get('pg_depts', [])}
            return {'UG': [], 'PG': []}

    @staticmethod
    def save_techfest_departments(depts):
        with cursor(dict_cursor=False) as cur:
            cur.execute("""
                INSERT INTO techfest_departments (id, ug_depts, pg_depts) VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET ug_depts=EXCLUDED.ug_depts, pg_depts=EXCLUDED.pg_depts;
            """, (depts.get('UG', []), depts.get('PG', [])))

    # ─── Adapters to maintain legacy file-based API logic ─────────────────────
    @staticmethod
    def load_json(filename):
        if filename == 'students.json': return DB.get_students()
        if filename == 'admins.json': return DB.get_admins()
        if filename == 'institutions.json': return DB.get_institutions()
        if filename == 'settings.json': return DB.get_settings()
        if filename == 'contacts.json': return DB.get_contacts()
        if filename == 'office_bearer_requests.json': return DB.get_office_bearer_requests()
        return []

    @staticmethod
    def save_json(filename, data):
        if filename == 'students.json': DB.save_students(data)
        elif filename == 'admins.json': 
            for a in data: DB.save_admin(a)
        elif filename == 'institutions.json': 
            for i in data: DB.save_institution(i)
        elif filename == 'settings.json': DB.save_settings(data)
        elif filename == 'contacts.json': DB.save_contacts(data)
        elif filename == 'office_bearer_requests.json': DB.update_office_bearer_requests(data)

    @staticmethod
    def _em_load(filename):
        if filename == 'events.json': return DB.get_em_events()
        if filename == 'tickets.json': return DB.get_em_tickets()
        if filename == 'admins.json': return DB.get_em_admins()
        if filename == 'settings.json': return DB.get_em_settings()
        if filename == 'hackathon_teams.json': return DB.get_hackathon_teams()
        if filename == 'evaluators.json': return DB.get_evaluators()
        if filename == 'scores.json': return DB.get_scores()
        if filename == 'techfests.json': return DB.get_techfests()
        if filename == 'techfest_events.json': return DB.get_techfest_events()
        if filename == 'techfest_registrations.json': return DB.get_techfest_registrations()
        if filename == 'techfest_depts.json': return DB.get_techfest_departments()
        return []

    @staticmethod
    def _em_save(filename, data):
        if filename == 'events.json': DB.save_em_events(data)
        elif filename == 'tickets.json': DB.save_em_tickets(data)
        elif filename == 'admins.json': DB.save_em_admins(data)
        elif filename == 'settings.json': DB.put_em_settings(data)
        elif filename == 'hackathon_teams.json': 
            from app.database import cursor
            with cursor(dict_cursor=False) as cur: cur.execute("TRUNCATE hackathon_teams RESTART IDENTITY CASCADE;")
            for t in data: DB.save_hackathon_team(t)
        elif filename == 'evaluators.json': 
            from app.database import cursor
            with cursor(dict_cursor=False) as cur: cur.execute("TRUNCATE evaluators CASCADE;")
            for e in data: DB.save_evaluator(e)
        elif filename == 'scores.json': 
            from app.database import cursor
            with cursor(dict_cursor=False) as cur: cur.execute("TRUNCATE scores RESTART IDENTITY CASCADE;")
            for s in data: DB.save_score(s)
        elif filename == 'techfests.json': 
            from app.database import cursor
            with cursor(dict_cursor=False) as cur: cur.execute("TRUNCATE techfests CASCADE;")
            for t in data: DB.save_techfest(t)
        elif filename == 'techfest_events.json': 
            from app.database import cursor
            with cursor(dict_cursor=False) as cur: cur.execute("TRUNCATE techfest_events CASCADE;")
            for t in data: DB.save_techfest_event(t)
        elif filename == 'techfest_registrations.json': 
            from app.database import cursor
            with cursor(dict_cursor=False) as cur: cur.execute("TRUNCATE techfest_registrations CASCADE;")
            for t in data: DB.save_techfest_registration(t)
        elif filename == 'techfest_depts.json': DB.save_techfest_departments(data)
