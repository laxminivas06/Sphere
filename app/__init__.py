from flask import Flask, session, render_template, redirect, url_for
from flask_wtf.csrf import CSRFProtect
from app.routes import api
from app.event_mgmt_routes import em
from app.models import DB
import os, json
import datetime as _dt

def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    csrf = CSRFProtect(app)
    # Use environment variable for secret key in production; fallback for development only
    app.secret_key = os.environ.get('SECRET_KEY', 'supersecretkeyucef-change-in-production')
    app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB max upload size
    app.register_blueprint(api, url_prefix='/api')
    app.register_blueprint(em, url_prefix='/em')

    # Make datetime.now() available in all Jinja2 templates
    app.jinja_env.globals['now'] = _dt.datetime.now

    from app.models import slugify
    @app.template_filter('slugify')
    def jinja_slugify(text):
        return slugify(text)

    # Helper to get club by ID easily in templates or routes
    @app.context_processor
    def utility_processor():
        def get_club(club_id):
            return DB.get_club_by_id(club_id)
        return dict(get_club=get_club)

    @app.context_processor
    def institution_processor():
        from flask import request, session
        # 1. Check if an institution is selected in session (useful for localhost/dev)
        inst_id = session.get('current_institution_id')
        inst = None
        if inst_id:
            inst = DB.get_institution_by_id(inst_id)
            
        # 2. Otherwise check domain
        if not inst:
            domain = request.host.split(':')[0]
            inst = DB.get_institution_by_domain(domain)
            
        # 3. Fallback to first institution
        if not inst:
            institutions = DB.get_institutions()
            if institutions:
                inst = institutions[0]
        
        return dict(institution=inst)

    @app.route('/select-institution/<inst_id>')
    def select_institution(inst_id):
        session['current_institution_id'] = inst_id
        return redirect(url_for('home'))

    @app.route('/')
    def home():
        user = session.get('user')
        clubs = DB.get_clubs()
        all_events = [e for e in DB.get_events() if e.get('approved')]
        ongoing = [e for e in all_events if not e.get('event_finished')]
        completed = [e for e in all_events if e.get('event_finished')]
        
        # Enrich standard events with club names and correct poster URLs
        from app.models import slugify
        for e in ongoing + completed:
            c = DB.get_club_by_id(e['club_id'])
            e['club_name'] = c['name'] if c else "Unknown Club"
            
            # Resolve poster path robustly
            poster_url = ""
            if e.get('poster'):
                poster_url = f"/static/uploads/clubs/{e['club_id']}/events/{slugify(e['title'])}/posters/{e['poster']}"
            e['poster_url'] = poster_url
        
        active_elections = []
        for c in clubs:
            elections = DB.get_elections(c['id'])
            for el in elections:
                if el.get('status') in ['nominations_open', 'voting_started']:
                    active_elections.append({"club": c, "election": el})

        from app.event_mgmt_routes import get_events as get_em_events
        all_em_events = get_em_events()
        # Active EM events
        active_em = [e for e in all_em_events if e.get('status') == 'active']
        college_events = [e for e in active_em if not e.get('allow_external')]
        inter_college_events = [e for e in active_em if e.get('allow_external')]
        
        # Completed EM events (those that aren't active/draft)
        completed_em = [e for e in all_em_events if e.get('status') in ('completed', 'finished')]
        completed.extend(completed_em)

        # Enrich EM events with banner_url
        for e in active_em + completed_em:
            if e.get('banner'):
                e['banner_url'] = f"/static/uploads/em/banners/{e['banner']}"
            else:
                e['banner_url'] = "https://images.unsplash.com/photo-1540575467063-178a50c2df87?auto=format&fit=crop&w=600"

        # Calculate Statistics for Analytics
        clubs_count = len(clubs)
        active_events_count = len(active_em) + len(ongoing)
        
        # Count Participations
        participations_count = 0
        # 1. EM Tickets
        em_tickets = DB._em_load('tickets.json')
        participations_count += len(em_tickets)
        # 2. Hackathon Members
        ht_teams = DB._em_load('hackathon_teams.json')
        for t in ht_teams:
            participations_count += 1 # Leader
            participations_count += len(t.get('members', []))
        # 3. Techfest
        tf_regs = DB._em_load('techfest_registrations.json')
        participations_count += len(tf_regs)
        # 4. Standard Club Events
        clubs_dir = os.path.join(app.root_path, '..', 'data', 'clubs')
        if os.path.exists(clubs_dir):
            for cid in os.listdir(clubs_dir):
                cdir = os.path.join(clubs_dir, cid)
                if os.path.isdir(cdir):
                    for edir in os.listdir(cdir):
                        reg_file = os.path.join(cdir, edir, 'registrations.json')
                        if os.path.exists(reg_file):
                            try:
                                with open(reg_file) as f:
                                    participations_count += len(json.load(f))
                            except: pass

        stats = {
            "clubs": clubs_count,
            "events": active_events_count,
            "participations": participations_count
        }

        return render_template('index.html', user=user, clubs=clubs, ongoing=ongoing, completed=completed, 
                               active_elections=active_elections, college_events=college_events, 
                               inter_college_events=inter_college_events, stats=stats)

    @app.route('/login')
    def login_page():
        return render_template('login.html')

    @app.route('/club/<club_id>')
    def club_page(club_id):
        user = session.get('user')
        club = DB.get_club_by_id(club_id)
        all_events = [e for e in DB.get_events(club_id) if e.get('approved')]
        ongoing = [e for e in all_events if not e.get('report_approved')]
        completed = [e for e in all_events if e.get('report_approved')]
        
        elections = DB.get_elections(club_id)
        active_elections = [el for el in elections if el.get('status') in ['nominations_open', 'voting_started']]
        
        return render_template('club.html', user=user, club=club, events=all_events, ongoing=ongoing, completed=completed, active_elections=active_elections)

    @app.route('/event/<club_id>/<event_id>')
    def event_page(club_id, event_id):
        user = session.get('user')
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        if not event or event.get('deleted'): return "Event not found or has been moved to history.", 404
        club = DB.get_club_by_id(club_id)
        is_registered = False
        if user:
            # For club admins/super admins they don't register, but wait, a student could be registered.
            # We assume user['roll_number'] or user['email'] exists. To match registrations, we prefer roll_number or email.
            identifier = user.get('roll_number') or user.get('email')
            regs = DB.get_registrations(club_id)
            is_registered = any(r['event_id'] == event_id and (r.get('roll_number') == identifier or r.get('email') == identifier) for r in regs)

        all_clubs = DB.get_clubs()
        return render_template('event.html', user=user, event=event, club=club, is_registered=is_registered, all_clubs=all_clubs)

    @app.route('/register/<club_id>/<event_id>')
    def register_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        event = next((e for e in DB.get_events(club_id) if e['id'] == event_id), None)
        club = DB.get_club_by_id(club_id)
        
        from app.models import slugify
        event_slug = slugify(event['title']) if event else "unknown"
        
        # Fetch teams for this event
        regs = DB.get_registrations(club_id)
        teams = [{"team_id": r.get('team_id'), "team_name": r.get('team_name')} for r in regs if r.get('event_id') == event_id and r.get('team_role') == 'leader']
        return render_template('register.html', user=user, event=event, club=club, club_id=club_id, teams=teams, event_slug=event_slug)

    @app.route('/success/<club_id>/<reg_id>')
    def success_page(club_id, reg_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        
        identifier = user.get('roll_number') or user.get('email')
        regs = DB.get_registrations(club_id)
        reg = next((r for r in regs if r['id'] == reg_id and (r.get('roll_number') == identifier or r.get('email') == identifier)), None)
        
        if not reg: return "Registration not found", 404
        
        return render_template('success.html', user=user, reg=reg)

    @app.route('/admin')
    def admin_dashboard():
        user = session.get('user')
        if not user:
            return redirect(url_for('login_page'))
        role = user.get('role', '')
        # Evaluators get their own dedicated dashboard
        if role == 'evaluator':
            return redirect('/em/evaluator/dashboard')
        # Event Manager & Event Admins go to the separate EM dashboard
        if role in ('event_manager', 'event_admin'):
            return redirect('/em/dashboard')
        # Allow super_admin or any club-specific admin (role ends with '_admin' and not super)
        if role != 'super_admin' and role != 'club_admin' and not role.endswith('_admin'):
            return "Unauthorized", 403
            
        if user['role'] == 'super_admin':
            stats = DB.get_global_stats()
            clubs = DB.get_clubs()
            # Enrich club data with admin info for the management UI
            admins = DB.load_json('admins.json')
            for c in clubs:
                admin_email = c.get('admin_roll')
                admin = next((a for a in admins if a.get('email') == admin_email), None)
                if admin:
                    c['admin_phone'] = admin.get('phone', '')
                    c['admin_name'] = admin.get('name', '')
            
            all_events = DB.get_events()
            return render_template('admin_super.html', user=user, stats=stats, clubs=clubs, events=all_events)
        else:
            # Club admin uses email as identifier
            identifier = user.get('email') or user.get('roll_number')
            club = DB.get_club_by_admin(identifier)
            if not club: return "No club assigned", 404
            events = DB.get_events(club['id'])
            regs = DB.get_registrations(club['id'])
            stats = DB.get_club_stats(club['id'])
            
            # Compute actual finances for club admin view
            for event in events:
                if event.get('actual_expenses'):
                    event['computed_spend'] = int(event.get('actual_expenses', 0)) + int(event.get('extra_expense', 0))
                else:
                    event['computed_spend'] = int(event.get('expenditure', 0)) + int(event.get('extra_expense', 0))
                
                auto_revenue = int(event.get('revenue', 0))
                event['computed_revenue'] = auto_revenue + int(event.get('extra_income', 0)) + int(event.get('offline_cash', 0))
            all_clubs = DB.get_clubs()
            reqs = DB.get_office_bearer_requests()
            club_reqs = [r for r in reqs if r.get('club_id') == club['id']]

            # Fetch EM events organized by this club (for Event Hub tab)
            from app.event_mgmt_routes import get_events_for_club, get_em_admins, get_tickets, enrich_events_with_stats
            club_em_events = enrich_events_with_stats(get_events_for_club(club['id']))
            em_tickets = get_tickets()
            em_admins  = get_em_admins()
            # Total EM stats for this club
            em_stats = {
                'total_events':    len(club_em_events),
                'total_regs':      sum(e.get('_reg_count', 0) for e in club_em_events),
                'total_revenue':   sum(e.get('_revenue', 0)   for e in club_em_events),
                'total_checked_in': sum(e.get('_checked_in', 0) for e in club_em_events),
            }
            return render_template('admin_club.html',
                user=user, club=club, events=events, registrations=regs,
                stats=stats, all_clubs=all_clubs, ob_requests=club_reqs,
                club_em_events=club_em_events, em_tickets=em_tickets,
                em_admins=em_admins, em_stats=em_stats)

    @app.route('/admin/contacts')
    def admin_contacts_page():
        user = session.get('user')
        if not user or user['role'] != 'super_admin':
            return redirect(url_for('login_page'))
        contacts = DB.get_contacts()
        return render_template('admin_contacts.html', user=user, contacts=contacts)

    @app.route('/admin/bulk-email/<club_id>')
    def bulk_email_page(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
        events = DB.get_events(club_id)
        return render_template('admin_bulk_email.html', user=user, club=club, events=events)

    @app.route('/admin/events/<club_id>')
    def club_events_page(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
        events = DB.get_events(club_id)
        all_clubs = DB.get_clubs()
        return render_template('admin_club_events.html', user=user, club=club, events=events, all_clubs=all_clubs)

    @app.route('/admin/finance/<club_id>')
    def club_finance_page_standalone(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
        events = DB.get_events(club_id)
        for event in events:
            if event.get('actual_expenses'):
                event['computed_spend'] = int(event.get('actual_expenses', 0)) + int(event.get('extra_expense', 0))
            else:
                event['computed_spend'] = int(event.get('expenditure', 0)) + int(event.get('extra_expense', 0))
            auto_revenue = int(event.get('revenue', 0))
            event['computed_revenue'] = auto_revenue + int(event.get('extra_income', 0)) + int(event.get('offline_cash', 0))
        return render_template('admin_club_finance.html', user=user, club=club, events=events)

    @app.route('/admin/identity/<club_id>')
    def club_identity_page(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
        club_reqs = [r for r in DB.get_office_bearer_requests() if r.get('club_id') == club_id]
        return render_template('admin_club_identity.html', user=user, club=club, ob_requests=club_reqs)


    @app.route('/scanner')
    def scanner_page():
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_admin(identifier)
        if not club: return "Unauthorized. Only Club Admins can access scanner.", 403
        return render_template('scanner.html', user=user, club=club)


    @app.route('/events/permission_letter/<club_id>/<event_id>')
    def permission_letter_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        
        if not club or not event: return "Not found", 404
        
        is_trusted = any(e.get('report_approved') for e in events)
        
        import datetime
        date_str = datetime.datetime.now().strftime("%d-%m-%Y")
        
        return render_template('permission_letter.html', 
            club_id=club_id,
            event_id=event_id,
            club_code=club['name'][:3].upper() if club['name'] else "xxx",
            year=event.get('year', datetime.datetime.now().year),
            auto_id=event.get('auto_id', '001'),
            date_str=date_str,
            club_name=club['name'],
            program_title=event.get('title', ''),
            program_date_place=f"{event.get('date', '')} {event.get('time', '')} - {event.get('venue', '')}",
            resource_person=event.get('resource_person', '-'),
            appointment_date=event.get('appointment_date', '-'),
            transport_receive=event.get('transport_receive', '-'),
            transport_send=event.get('transport_send', '-'),
            honoring=event.get('honoring', '-'),
            memento=event.get('memento', '-'),
            cash=event.get('cash', '-'),
            refreshment=event.get('refreshment', '-'),
            printing=event.get('printing', '-'),
            distribution=event.get('distribution', '-'),
            flex=event.get('flex', '-'),
            sandal=event.get('sandal', '-'),
            sweets=event.get('sweets', '-'),
            event=event,
            students=event.get('participants', '-'),
            chairs=event.get('chairs', '-'),
            mic=event.get('mic', '-'),
            internet=event.get('internet', '-'),
            others=event.get('others', '-'),
            news=event.get('news', '-'),
            before=event.get('before', '-'),
            after=event.get('after', '-'),
            guest_feedback=event.get('guest_feedback', '-'),
            student_feedback=event.get('student_feedback', '-'),
            organizer_feedback=event.get('organizer_feedback', '-'),
            coordinators=event.get('coordinators', ''),
            description=event.get('description', ''),
            mentor_name=club.get('mentor', {}).get('name', 'N/A'),
            is_trusted=is_trusted
        )

    @app.route('/admin/club/<club_id>')
    def super_admin_club_detail(club_id):
        user = session.get('user')
        if not user:
            return redirect(url_for('login_page'))
        
        club = DB.get_club_by_id(club_id)
        if not club:
            return "Club not found", 404

        # If not super admin, check if this is the club's own admin
        if user.get('role') != 'super_admin':
            identifier = user.get('email') or user.get('roll_number')
            if club.get('admin_roll') == identifier or club.get('admin_email') == identifier:
                # Redirect club admin to their dashboard (browser will preserve any #hash)
                return redirect(url_for('admin_dashboard'))
            return "Unauthorized", 403
        # If authorized club admin, the redirect above already happened.
        # If we are here, the user is a super_admin.
        
        events = DB.get_events(club_id)
        regs   = DB.get_registrations(club_id)

        # Compute budget spend and revenue
        total_spend = 0
        total_revenue = 0
        for event in events:
            # Spend calculation
            if event.get('actual_expenses'):
                spend = int(event.get('actual_expenses', 0)) + int(event.get('extra_expense', 0))
            else:
                # If actual expenses not provided, fallback to initial estimated expenditure
                spend = int(event.get('expenditure', 0)) + int(event.get('extra_expense', 0))
            
            event['computed_spend'] = spend
            total_spend += spend
            
            # Revenue calculation
            auto_revenue = int(event.get('revenue', 0))
            rev = auto_revenue + int(event.get('extra_income', 0)) + int(event.get('offline_cash', 0))
            event['computed_revenue'] = rev
            total_revenue += rev

        approved_count   = sum(1 for e in events if e.get('approved') or e.get('event_status') == 'approved')
        pending_reports  = sum(1 for e in events if e.get('report') and not e.get('report_approved'))

        return render_template('admin_super_club_detail.html',
            user=user, club=club, events=events, registrations=regs,
            total_spend=total_spend, total_revenue=total_revenue,
            approved_count=approved_count, pending_reports_count=pending_reports
        )

    @app.route('/admin/report/<club_id>/<event_id>')
    def admin_report_viewer(club_id, event_id):
        user = session.get('user')
        if not user or user['role'] != 'super_admin':
            return redirect(url_for('login_page'))

        club   = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event  = next((e for e in events if e['id'] == event_id), None)

        if not event or not event.get('report'):
            return "Report not found", 404

        from app.models import slugify
        event_slug      = slugify(event['title'])
        report_filename = event['report']
        report_url      = f"/static/uploads/clubs/{club_id}/events/{event_slug}/reports/{report_filename}"
        report_ext      = report_filename.rsplit('.', 1)[-1].lower()

        return render_template('admin_report_viewer.html',
            user=user, event=event, club=club,
            report_url=report_url, report_ext=report_ext,
            report_filename=report_filename,
            club_id=club_id
        )

    @app.route('/admin/event/setup/<club_id>/<event_id>')
    def event_setup_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        all_clubs = DB.get_clubs()
        return render_template('admin_event_setup.html', user=user, club=club, event=event, all_clubs=all_clubs)

    @app.route('/admin/event/registrations/<club_id>/<event_id>')
    def event_registrations_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        regs = [r for r in DB.get_registrations(club_id) if r['event_id'] == event_id]
        return render_template('admin_event_registrations.html', user=user, club=club, event=event, registrations=regs)

    @app.route('/admin/event/attendance/<club_id>/<event_id>')
    def event_attendance_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        regs = [r for r in DB.get_registrations(club_id) if r['event_id'] == event_id and r.get('payment_verified')]
        
        # Group by department
        grouped_attendance = {}
        for r in regs:
            dept = r.get('department', 'N/A')
            if dept not in grouped_attendance: grouped_attendance[dept] = []
            grouped_attendance[dept].append(r)
            
        return render_template('admin_event_attendance.html', user=user, club=club, event=event, grouped_attendance=grouped_attendance)

    @app.route('/admin/event/feedback/<club_id>/<event_id>')
    def event_feedback_mgmt_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        # Fetch results
        from app.routes import api
        # Mocking the result fetch logic here for template
        feedbacks = [] # In real app, fetch from registrations or separate feedback store
        return render_template('admin_event_feedback.html', user=user, club=club, event=event)

    @app.route('/admin/event/finance/<club_id>/<event_id>')
    def event_finance_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        
        # Automatic revenue calculation
        regs = [r for r in DB.get_registrations(club_id) if r['event_id'] == event_id and r.get('payment_verified')]
        auto_revenue = sum(int(event.get('fee', 0)) for r in regs) if event.get('payment_type') == 'paid' else 0
        
        return render_template('admin_event_finance.html', user=user, club=club, event=event, auto_revenue=auto_revenue)

    @app.route('/admin/elections/<club_id>')
    def admin_elections_page(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        # Validate admin access
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
        
        elections = DB.get_elections(club_id)
        return render_template('admin_elections.html', user=user, club=club, elections=elections)

    @app.route('/elections/nominate/<club_id>/<election_id>')
    def election_nominate_page(club_id, election_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        elections = DB.get_elections(club_id)
        election = next((el for el in elections if el['id'] == election_id), None)
        if not election or election.get('status') != 'nominations_open': return "Nominations are not open", 400
        return render_template('election_nominate.html', user=user, club=club, election=election)

    @app.route('/elections/vote/<club_id>/<election_id>')
    def election_vote_page(club_id, election_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        elections = DB.get_elections(club_id)
        election = next((el for el in elections if el['id'] == election_id), None)
        if not election or election.get('status') != 'voting_started': return "Voting is not currently active", 400
        return render_template('election_vote.html', user=user, club=club, election=election)

    @app.route('/elections/results/<club_id>/<election_id>')
    def election_results_page(club_id, election_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        club = DB.get_club_by_id(club_id)
        elections = DB.get_elections(club_id)
        election = next((el for el in elections if el['id'] == election_id), None)
        if not election or election.get('status') != 'results_published': return "Results not published yet", 400
        return render_template('election_results.html', user=user, club=club, election=election)

    @app.route('/admin/generate-report/<club_id>/<event_id>')
    def generate_report_page(club_id, event_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        
        club = DB.get_club_by_id(club_id)
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        if not event: return "Event not found", 404
        
        # Calculate stats for the report
        regs = [r for r in DB.get_registrations(club_id) if r['event_id'] == event_id]
        verified_regs = [r for r in regs if r.get('payment_verified') or event.get('payment_type') == 'free']
        
        # Count participants (excluding those with 'faculty' or similar if we wanted, but let's be general)
        student_count = len(verified_regs)
        
        # In a real app we might have a list of faculty who attended. 
        # For now let's just pass the data we have.
        
        return render_template('report_generator.html', 
            user=user, 
            club=club, 
            event=event, 
            student_count=student_count
        )

    @app.route('/admin/reports/<club_id>')
    def admin_reports_page(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
            
        events = DB.get_events(club_id)
        elections = DB.get_elections(club_id)

        # Enrich events with computed financial fields (same logic as club admin dashboard)
        for event in events:
            if event.get('actual_expenses'):
                event['computed_spend'] = int(event.get('actual_expenses', 0)) + int(event.get('extra_expense', 0))
            else:
                event['computed_spend'] = int(event.get('expenditure', 0)) + int(event.get('extra_expense', 0))
            auto_revenue = int(event.get('revenue', 0))
            event['computed_revenue'] = auto_revenue + int(event.get('extra_income', 0)) + int(event.get('offline_cash', 0))

        return render_template('admin_academic_reports.html', user=user, club=club, events=events, elections=elections)

    # ── SUPER ADMIN: Institutional Event Registry ──────────────────────────────
    @app.route('/super/registry')
    def super_registry_page():
        user = session.get('user')
        if not user or user.get('role') != 'super_admin':
            return redirect(url_for('login_page'))

        import datetime
        now = datetime.datetime.now()
        # Academic year logic: Starts in June (month 6)
        if now.month >= 6:
            current_year_str = f"{now.year % 100}-{(now.year + 1) % 100}"
        else:
            current_year_str = f"{(now.year - 1) % 100}-{now.year % 100}"

        clubs = DB.get_clubs()
        all_events_by_club = {}
        all_elections_by_club = {}
        for c in clubs:
            # Filter events by the calculated dynamic year
            club_events = DB.get_events(c['id'])
            all_events_by_club[c['id']] = [e for e in club_events if e.get('year') == current_year_str and not e.get('deleted')]
            
            # Filter elections (assuming they also have a 'year' field, or we use createdAt)
            club_elections = DB.get_elections(c['id'])
            all_elections_by_club[c['id']] = [el for el in club_elections if el.get('year') == current_year_str]

        return render_template(
            'super_registry.html',
            user=user,
            clubs=clubs,
            current_year_str=current_year_str,
            all_events_by_club=all_events_by_club,
            all_elections_by_club=all_elections_by_club,
            now=now
        )

    # ── SUPER ADMIN: Club Annual Reports ──────────────────────────────────────
    @app.route('/super/registry/<club_id>')
    def super_club_registry_page(club_id):
        user = session.get('user')
        if not user or user.get('role') != 'super_admin':
            return redirect(url_for('login_page'))

        club = DB.get_club_by_id(club_id)
        if not club: return "Club not found", 404

        events    = DB.get_events(club_id)
        elections = DB.get_elections(club_id)

        import datetime
        current_year = datetime.datetime.now().year

        # Collect all distinct academic years from events + elections
        years = set()
        
        # Add current year if not present
        now = datetime.datetime.now()
        if now.month >= 6:
            current_year_str = f"{now.year % 100}-{(now.year + 1) % 100}"
        else:
            current_year_str = f"{(now.year - 1) % 100}-{now.year % 100}"
        years.add(current_year_str)

        for ev in events:
            y = ev.get('year')
            if y: years.add(str(y))
        for el in elections:
            y = el.get('year')
            if y: years.add(str(y))

        years = sorted(list(years), reverse=True)

        return render_template(
            'super_club_registry.html',
            user=user,
            club=club,
            events=events,
            elections=elections,
            years=years,
            current_year=current_year_str
        )

    # ── SUPER ADMIN: Approvals ──────────────────────────────────────────────────
    @app.route('/super/approvals')
    def super_approvals_page():
        user = session.get('user')
        if not user or user.get('role') != 'super_admin':
            return redirect(url_for('login_page'))
        events = DB.get_events()
        ob_requests = DB.get_office_bearer_requests()
        return render_template('super_approvals.html', user=user, events=events, ob_requests=ob_requests)

    # ── SUPER ADMIN: Master Database ─────────────────────────────────────────────
    @app.route('/super/master')
    def super_master_page():
        user = session.get('user')
        if not user or user.get('role') != 'super_admin':
            return redirect(url_for('login_page'))
        return render_template('super_master_db.html', user=user)

    # ── SUPER ADMIN: Global Settings ─────────────────────────────────────────────
    @app.route('/super/settings')
    def super_settings_page():
        user = session.get('user')
        if not user or user.get('role') != 'super_admin':
            return redirect(url_for('login_page'))
        return render_template('super_settings.html', user=user)

    # ── SUPER ADMIN: Global Leaderboard ──────────────────────────────────────────
    @app.route('/super/leaderboard')
    def super_leaderboard_page():
        user = session.get('user')
        if not user or user.get('role') != 'super_admin':
            return redirect(url_for('login_page'))
            
        # Get all students and calculate their total event attendance
        students = DB.get_students()
        all_regs = []
        for club in DB.get_clubs():
            all_regs.extend(DB.get_registrations(club['id']))
            
        # Add EM tickets as well
        from app.event_mgmt_routes import get_tickets
        all_regs.extend(get_tickets())
        
        # Count verified attendances
        attendance_counts = {}
        for r in all_regs:
            # Include all registrations so the leaderboard isn't empty initially
            identifier = r.get('roll_number') or r.get('email')
            if not identifier: continue
            identifier = identifier.strip().lower()
            attendance_counts[identifier] = attendance_counts.get(identifier, 0) + 1
                
        # Attach counts to students and sort
        leaderboard = []
        for s in students:
            roll = s.get('roll_number', '').lower()
            count = attendance_counts.get(roll, 0)
            if count > 0:
                s['attended_events'] = count
                leaderboard.append(s)
                
        # Sort by attended events descending
        leaderboard.sort(key=lambda x: x.get('attended_events', 0), reverse=True)
        # Get Top 20
        top_students = leaderboard[:20]
        
        return render_template('super_leaderboard.html', user=user, students=top_students)

    # ── CLUB ADMIN: Club Leaderboard ─────────────────────────────────────────────
    @app.route('/admin/leaderboard/<club_id>')
    def club_leaderboard_page(club_id):
        user = session.get('user')
        if not user: return redirect(url_for('login_page'))
        identifier = user.get('email') or user.get('roll_number')
        club = DB.get_club_by_id(club_id)
        if not club: return "Not found", 404
        if user.get('role') != 'super_admin' and club.get('admin_roll') != identifier:
            return "Unauthorized", 403
            
        regs = DB.get_registrations(club_id)
        events = DB.get_events(club_id)
        
        # Aggregate by student
        student_stats = {}
        for r in regs:
            # Include all registrations
            roll = r.get('roll_number')
            if not roll: continue
            roll = roll.upper()
            
            if roll not in student_stats:
                student_stats[roll] = {
                    'roll_number': roll,
                    'name': r.get('name', 'Unknown'),
                    'department': r.get('department', '-'),
                    'events_attended': 0,
                    'amount_spent': 0
                }
            
            student_stats[roll]['events_attended'] += 1
            
            # Find event fee if paid and payment is verified (or just add fee to show potential spend)
            event = next((e for e in events if e['id'] == r.get('event_id')), None)
            if event and event.get('payment_type') == 'paid' and r.get('payment_verified'):
                try:
                    fee = int(event.get('fee', 0))
                    student_stats[roll]['amount_spent'] += fee
                except: pass
                    
        # Sort by attended descending
        leaderboard = list(student_stats.values())
        leaderboard.sort(key=lambda x: x['events_attended'], reverse=True)
        top_students = leaderboard[:20]
        
        return render_template('admin_club_leaderboard.html', user=user, club=club, students=top_students)

    # ── STUDENT: History & Dashboard ─────────────────────────────────────────────
    @app.route('/student/history')
    def student_history():
        user = session.get('user')
        if not user or user.get('role') != 'student':
            return redirect(url_for('login_page'))
        
        roll = user.get('roll_number')
        all_clubs = DB.get_clubs()
        all_regs = []
        
        # 1. Standard Club Registrations
        for club in all_clubs:
            club_regs = DB.get_registrations(club['id'])
            for r in club_regs:
                if r.get('roll_number') == roll:
                    events = DB.get_events(club['id'])
                    event = next((e for e in events if e['id'] == r['event_id']), None)
                    if event:
                        r['event_title'] = event.get('title')
                        r['club_name'] = club.get('name')
                        r['fee'] = event.get('fee', '0')
                        r['date'] = event.get('date')
                        r['type'] = 'Club Event'
                    all_regs.append(r)
        
        # 2. Event Management (EM) Registrations
        from app.event_mgmt_routes import get_tickets as get_em_tickets, get_events as get_em_events
        em_tickets = get_em_tickets()
        em_events = get_em_events()
        for t in em_tickets:
            if t.get('roll_number') == roll:
                ev = next((e for e in em_events if e['id'] == t['event_id']), None)
                if ev:
                    t['event_title'] = ev.get('title')
                    t['club_name'] = "Signature Event"
                    t['fee'] = ev.get('ticket_price', '0')
                    t['date'] = ev.get('date')
                    t['type'] = ev.get('event_category', 'Special').replace('_', ' ').title()
                    # Map EM fields to match standard regs for the template
                    t['payment_verified'] = True # EM tickets are usually paid or confirmed
                all_regs.append(t)
        
        # Sort by date
        all_regs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        # Calculate stats
        total_spent = 0
        for r in all_regs:
            try: total_spent += int(r.get('fee', 0))
            except: pass
            
        attended_count = len([r for r in all_regs if r.get('payment_verified')])
        unique_clubs = len(set(r.get('club_id') for r in all_regs))
        
        return render_template('student_history.html', user=user, registrations=all_regs, stats={
            'total_spent': total_spent,
            'attended_count': attended_count,
            'unique_clubs': unique_clubs
        })

    # ── NAVIGATION: Separate Pages ─────────────────────────────────────────────
    @app.route('/events/ongoing')
    def ongoing_events_page():
        user = session.get('user')
        all_events = [e for e in DB.get_events() if e.get('approved') and not e.get('event_finished')]
        
        from app.event_mgmt_routes import get_events as get_em_events
        active_em = [e for e in get_em_events() if e.get('status') == 'active']
        
        # Convert EM events to match standard structure for unified display if needed
        # Or just pass both
        return render_template('ongoing_events.html', user=user, events=all_events, em_events=active_em)

    @app.route('/clubs/all')
    def all_clubs_page():
        user = session.get('user')
        clubs = DB.get_clubs()
        return render_template('all_clubs.html', user=user, clubs=clubs)

    @app.route('/events/archive')
    def archive_page():
        user = session.get('user')
        all_events = [e for e in DB.get_events() if e.get('event_finished')]
        
        from app.event_mgmt_routes import get_events as get_em_events
        completed_em = [e for e in get_em_events() if e.get('status') in ('completed', 'finished')]
        
        return render_template('archive.html', user=user, events=all_events, em_events=completed_em)

    @app.route('/student/profile')
    def student_profile():
        user = session.get('user')
        if not user or user.get('role') != 'student':
            return redirect(url_for('login_page'))
        
        # Refresh student data from DB to ensure it's up to date
        student = DB.get_student_by_roll(user.get('roll_number'))
        if student:
            student['role'] = 'student'
            session['user'] = student
            
        return render_template('student_profile.html', user=student)

    @app.route('/student/profile/edit')
    def student_profile_edit():
        user = session.get('user')
        if not user or user.get('role') != 'student':
            return redirect(url_for('login_page'))
        
        student = DB.get_student_by_roll(user.get('roll_number'))
        if student:
            student['role'] = 'student'
            session['user'] = student
            
        return render_template('student_profile_edit.html', user=student)

    return app

