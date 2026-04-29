import json
import os
import re

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')

def slugify(text):
    return re.sub(r'[\W_]+', '_', text.lower()).strip('_')

class DB:
    @staticmethod
    def load_json(filename):
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            return []
        with open(filepath, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []

    @staticmethod
    def save_json(filename, data):
        filepath = os.path.join(DATA_DIR, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)

    @staticmethod
    def get_students():
        return DB.load_json('students.json')

    @staticmethod
    def get_student_by_roll(roll):
        students = DB.get_students()
        return next((s for s in students if s['roll_number'].upper() == roll.upper()), None)

    @staticmethod
    def save_student(student):
        students = DB.get_students()
        roll = student.get('roll_number').upper()
        existing_idx = next((i for i, s in enumerate(students) if s['roll_number'].upper() == roll), None)
        if existing_idx is not None:
            students[existing_idx] = student
        else:
            students.append(student)
        DB.save_json('students.json', students)
        
    @staticmethod
    def save_students(students):
        DB.save_json('students.json', students)

    # Admin Management
    @staticmethod
    def get_admins():
        return DB.load_json('admins.json')

    @staticmethod
    def save_admin(admin):
        admins = DB.get_admins()
        existing = next((i for i, a in enumerate(admins) if a.get('email') == admin.get('email')), None)
        if existing is not None:
            admins[existing] = admin
        else:
            admins.append(admin)
        DB.save_json('admins.json', admins)

    @staticmethod
    def get_admin_by_role(role):
        admins = DB.get_admins()
        return next((a for a in admins if a.get('role') == role), None)

    @staticmethod
    def get_admin_by_email(email):
        admins = DB.get_admins()
        return next((a for a in admins if a.get('email') == email), None)

    # Club Registry (New Nested Structure)
    @staticmethod
    def get_clubs():
        clubs = []
        clubs_dir = os.path.join(DATA_DIR, 'clubs')
        if not os.path.exists(clubs_dir):
            return []
        
        for club_id in os.listdir(clubs_dir):
            about_path = os.path.join(clubs_dir, club_id, 'about.json')
            if os.path.exists(about_path):
                with open(about_path, 'r') as f:
                    try:
                        clubs.append(json.load(f))
                    except:
                        continue
        return clubs

    @staticmethod
    def save_club(club):
        club_id = club['id']
        filepath = os.path.join(DATA_DIR, 'clubs', club_id, 'about.json')
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(club, f, indent=4)

    @staticmethod
    def get_club_by_id(club_id):
        filepath = os.path.join(DATA_DIR, 'clubs', club_id, 'about.json')
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                try:
                    return json.load(f)
                except:
                    return None
        return None

    @staticmethod
    def get_club_by_admin(admin_id):
        clubs = DB.get_clubs()
        return next((c for c in clubs if c.get('admin_roll') == admin_id), None)

    # Partitioned Data - Events (New Nested Structure)
    @staticmethod
    def get_events(club_id=None):
        all_events = []
        clubs_dir = os.path.join(DATA_DIR, 'clubs')
        if not os.path.exists(clubs_dir):
            return []
        for cid in os.listdir(clubs_dir):
            club_path = os.path.join(clubs_dir, cid)
            if not os.path.isdir(club_path): continue
            for item in os.listdir(club_path):
                event_dir = os.path.join(club_path, item)
                if os.path.isdir(event_dir):
                    info_path = os.path.join(event_dir, 'info.json')
                    if os.path.exists(info_path):
                        with open(info_path, 'r') as f:
                            try:
                                ev = json.load(f)
                                if 'club_id' not in ev:
                                    ev['club_id'] = cid
                                
                                # Dynamic Academic Year Enforcement
                                if not ev.get('year'):
                                    import datetime
                                    ts = ev.get('timestamp')
                                    dt = datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now()
                                    if dt.month >= 6:
                                        ev['year'] = f"{dt.year % 100}-{(dt.year + 1) % 100}"
                                    else:
                                        ev['year'] = f"{(dt.year - 1) % 100}-{dt.year % 100}"

                                if club_id:
                                    collabs = ev.get('collaborating_clubs', [])
                                    if isinstance(collabs, str): collabs = [collabs]
                                    if cid == club_id or club_id in collabs:
                                        all_events.append(ev)
                                else:
                                    all_events.append(ev)
                            except:
                                continue
        return all_events

    @staticmethod
    def get_event_by_id(club_id, event_id):
        events = DB.get_events(club_id)
        return next((e for e in events if e.get('id') == event_id), None)

    @staticmethod
    def save_event(club_id, event):
        import shutil
        event_id = event.get('id')
        new_slug = slugify(event['title'])
        club_dir = os.path.join(DATA_DIR, 'clubs', club_id)
        target_dir = os.path.join(club_dir, new_slug)
        
        # Find existing directory for this ID
        existing_dir = None
        if os.path.exists(club_dir):
            for item in os.listdir(club_dir):
                d = os.path.join(club_dir, item)
                if os.path.isdir(d):
                    ipath = os.path.join(d, 'info.json')
                    if os.path.exists(ipath):
                        with open(ipath, 'r') as f:
                            try:
                                if json.load(f).get('id') == event_id:
                                    existing_dir = d
                                    break
                            except: continue
        
        # If folder exists but slug changed, rename it
        if existing_dir and os.path.abspath(existing_dir) != os.path.abspath(target_dir):
            # Move data folder
            shutil.move(existing_dir, target_dir)
            
            # Move static uploads folder if exists
            old_slug = os.path.basename(existing_dir)
            old_upload = os.path.join(os.path.dirname(os.path.dirname(DATA_DIR)), 'static', 'uploads', 'clubs', club_id, 'events', old_slug)
            new_upload = os.path.join(os.path.dirname(os.path.dirname(DATA_DIR)), 'static', 'uploads', 'clubs', club_id, 'events', new_slug)
            if os.path.exists(old_upload) and not os.path.exists(new_upload):
                os.makedirs(os.path.dirname(new_upload), exist_ok=True)
                shutil.move(old_upload, new_upload)

        os.makedirs(target_dir, exist_ok=True)
        
        # Save info.json
        with open(os.path.join(target_dir, 'info.json'), 'w') as f:
            json.dump(event, f, indent=4)
            
        # Ensure registrations.json exists
        reg_file = os.path.join(target_dir, 'registrations.json')
        if not os.path.exists(reg_file):
            with open(reg_file, 'w') as f:
                json.dump([], f)

    @staticmethod
    def update_events(club_id, events):
        for event in events:
            actual_club_id = event.get('club_id', club_id)
            DB.save_event(actual_club_id, event)

    # Partitioned Data - Registrations (New Nested Structure)
    @staticmethod
    def get_registrations(club_id=None, event_id=None):
        if club_id:
            events = DB.get_events(club_id)
            if event_id:
                events = [e for e in events if e['id'] == event_id]
            all_regs = []
            for event in events:
                actual_club = event.get('club_id', club_id)
                event_slug = slugify(event['title'])
                reg_file = os.path.join(DATA_DIR, 'clubs', actual_club, event_slug, 'registrations.json')
                if os.path.exists(reg_file):
                    with open(reg_file, 'r') as f:
                        try:
                            regs = json.load(f)
                            all_regs.extend(regs)
                        except:
                            pass
            return all_regs
        else:
            all_regs = []
            clubs_dir = os.path.join(DATA_DIR, 'clubs')
            if not os.path.exists(clubs_dir): return []
            for cid in os.listdir(clubs_dir):
                club_path = os.path.join(clubs_dir, cid)
                if not os.path.isdir(club_path): continue
                for item in os.listdir(club_path):
                    event_dir = os.path.join(club_path, item)
                    if os.path.isdir(event_dir):
                        reg_path = os.path.join(event_dir, 'registrations.json')
                        if os.path.exists(reg_path):
                            with open(reg_path, 'r') as f:
                                try:
                                    all_regs.extend(json.load(f))
                                except:
                                    pass
            return all_regs

    @staticmethod
    def save_registration(club_id, reg):
        event_id = reg.get('event_id')
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            return
        
        actual_club = event.get('club_id', club_id)
        event_slug = slugify(event['title'])
        reg_file = os.path.join(DATA_DIR, 'clubs', actual_club, event_slug, 'registrations.json')
        
        regs = []
        if os.path.exists(reg_file):
            with open(reg_file, 'r') as f:
                try:
                    regs = json.load(f)
                except:
                    regs = []
        
        regs.append(reg)
        with open(reg_file, 'w') as f:
            json.dump(regs, f, indent=4)

    @staticmethod
    def update_registrations(club_id, regs):
        if not regs: return
        grouped = {}
        for r in regs:
            eid = r.get('event_id')
            if eid not in grouped: grouped[eid] = []
            grouped[eid].append(r)
            
        events = DB.get_events(club_id)
        for eid, event_regs in grouped.items():
            event = next((e for e in events if e['id'] == eid), None)
            if event:
                actual_club = event.get('club_id', club_id)
                event_slug = slugify(event['title'])
                reg_file = os.path.join(DATA_DIR, 'clubs', actual_club, event_slug, 'registrations.json')
                os.makedirs(os.path.dirname(reg_file), exist_ok=True)
                with open(reg_file, 'w') as f:
                    json.dump(event_regs, f, indent=4)

    # Stats for dashboards
    @staticmethod
    def get_club_stats(club_id):
        club = DB.get_club_by_id(club_id)
        if not club: return None
        
        events = DB.get_events(club_id)
        regs = DB.get_registrations(club_id)
        
        # Expenditure fields
        spend_fields = [
            'honoring', 'memento', 'cash', 'refreshment', 'printing',
            'distribution', 'flex', 'sandal', 'sweets', 'chairs',
            'mic', 'internet', 'others'
        ]
        
        total_spent = 0
        completed_events = 0
        total_revenue = 0
        
        for e in events:
            if e.get('report_approved'):
                completed_events += 1
                
            # Spend calculation
            if e.get('actual_expenses'):
                event_spent = int(e.get('actual_expenses', 0)) + int(e.get('extra_expense', 0))
            else:
                event_spent = int(e.get('expenditure', 0)) + int(e.get('extra_expense', 0))
                
            # Collaborative splitting if needed
            collabs = e.get('collaborating_clubs', [])
            if isinstance(collabs, str): collabs = [collabs]
            if collabs:
                event_spent = int(event_spent / (1 + len(collabs)))
                
            total_spent += event_spent
            
            # Revenue calculation
            auto_revenue = int(e.get('revenue', 0))
            event_revenue = auto_revenue + int(e.get('extra_income', 0)) + int(e.get('offline_cash', 0))
            
            if collabs:
                event_revenue = int(event_revenue / (1 + len(collabs)))
                
            total_revenue += event_revenue
                        
        dept_dist = {}
        year_dist = {}
        for r in regs:
            d = r.get('dept') or r.get('user_dept') or 'N/A'
            y = r.get('year') or r.get('user_year') or 'N/A'
            dept_dist[d] = dept_dist.get(d, 0) + 1
            year_dist[y] = year_dist.get(y, 0) + 1

        return {
            "total_events": len(events),
            "completed_events": completed_events,
            "total_registrations": len(regs),
            "total_revenue": total_revenue,
            "total_spent": total_spent,
            "net_balance": total_revenue - total_spent,
            "dept_distribution": dept_dist,
            "year_distribution": year_dist
        }

    @staticmethod
    def get_settings():
        settings = DB.load_json('settings.json')
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def save_settings(settings):
        DB.save_json('settings.json', settings)

    @staticmethod
    def get_global_stats():
        clubs = DB.get_clubs()
        # Simplified:
        club_stats = []
        for c in clubs:
            s = DB.get_club_stats(c['id'])
            if s: club_stats.append(s)
            
        return {
            "total_clubs": len(clubs),
            "total_events": sum(s['total_events'] for s in club_stats),
            "total_revenue": sum(s['total_revenue'] for s in club_stats),
            "total_spending": sum(s['total_spent'] for s in club_stats)
        }

    # Elections Storage
    @staticmethod
    def get_elections(club_id):
        filepath = os.path.join(DATA_DIR, 'clubs', club_id, 'elections.json')
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                try:
                    return json.load(f)
                except:
                    return []
        return []

    @staticmethod
    def save_elections(club_id, elections):
        filepath = os.path.join(DATA_DIR, 'clubs', club_id, 'elections.json')
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(elections, f, indent=4)

    # Global Institutional Contacts
    @staticmethod
    def get_contacts():
        contacts = DB.load_json('contacts.json')
        return contacts if isinstance(contacts, dict) else {}

    @staticmethod
    def save_contacts(contacts):
        DB.save_json('contacts.json', contacts)

    # Office Bearer Requests
    @staticmethod
    def get_office_bearer_requests():
        return DB.load_json('office_bearer_requests.json')

    @staticmethod
    def save_office_bearer_request(req):
        reqs = DB.get_office_bearer_requests()
        reqs.append(req)
        DB.save_json('office_bearer_requests.json', reqs)

    @staticmethod
    def update_office_bearer_requests(reqs):
        DB.save_json('office_bearer_requests.json', reqs)

    # ── Hackathon Teams ───────────────────────────────────────────────────────
    EM_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'em')

    @staticmethod
    def _em_load(fname):
        path = os.path.join(DB.EM_DIR, fname)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            try: return json.load(f)
            except: return []

    @staticmethod
    def _em_save(fname, data):
        os.makedirs(DB.EM_DIR, exist_ok=True)
        with open(os.path.join(DB.EM_DIR, fname), 'w') as f:
            json.dump(data, f, indent=4)

    @staticmethod
    def get_hackathon_teams(event_id=None):
        teams = DB._em_load('hackathon_teams.json')
        if event_id:
            teams = [t for t in teams if t.get('event_id') == event_id]
        return teams

    @staticmethod
    def save_hackathon_team(team):
        teams = DB._em_load('hackathon_teams.json')
        idx = next((i for i, t in enumerate(teams) if t.get('team_id') == team.get('team_id')), None)
        if idx is not None:
            teams[idx] = team
        else:
            teams.append(team)
        DB._em_save('hackathon_teams.json', teams)

    @staticmethod
    def delete_hackathon_team(team_id):
        teams = [t for t in DB._em_load('hackathon_teams.json') if t.get('team_id') != team_id]
        DB._em_save('hackathon_teams.json', teams)

    # ── Evaluators ────────────────────────────────────────────────────────────
    @staticmethod
    def get_evaluators():
        return DB._em_load('evaluators.json')

    @staticmethod
    def save_evaluator(evaluator):
        evaluators = DB.get_evaluators()
        idx = next((i for i, e in enumerate(evaluators) if e.get('id') == evaluator.get('id')), None)
        if idx is not None:
            evaluators[idx] = evaluator
        else:
            evaluators.append(evaluator)
        DB._em_save('evaluators.json', evaluators)

    @staticmethod
    def delete_evaluator(evaluator_id):
        evaluators = [e for e in DB.get_evaluators() if e.get('id') != evaluator_id]
        DB._em_save('evaluators.json', evaluators)

    # ── Scores ────────────────────────────────────────────────────────────────
    @staticmethod
    def get_scores(event_id=None, team_id=None):
        scores = DB._em_load('scores.json')
        if event_id:
            scores = [s for s in scores if s.get('event_id') == event_id]
        if team_id:
            scores = [s for s in scores if s.get('team_id') == team_id]
        return scores

    @staticmethod
    def save_score(score):
        scores = DB._em_load('scores.json')
        # Upsert: replace if same evaluator already scored this team
        idx = next((i for i, s in enumerate(scores)
                    if s.get('event_id') == score.get('event_id')
                    and s.get('team_id') == score.get('team_id')
                    and s.get('evaluator_id') == score.get('evaluator_id')), None)
        if idx is not None:
            scores[idx] = score
        else:
            scores.append(score)
        DB._em_save('scores.json', scores)


    # ── Tech Fest ─────────────────────────────────────────────────────────────
    @staticmethod
    def get_techfests():
        return DB._em_load('techfests.json')

    @staticmethod
    def save_techfest(tf):
        tfs = DB.get_techfests()
        idx = next((i for i, t in enumerate(tfs) if t.get('id') == tf.get('id')), None)
        if idx is not None: tfs[idx] = tf
        else: tfs.append(tf)
        DB._em_save('techfests.json', tfs)

    @staticmethod
    def get_techfest_events(tf_id=None):
        evs = DB._em_load('techfest_events.json')
        if tf_id: evs = [e for e in evs if e.get('techfest_id') == tf_id]
        return evs

    @staticmethod
    def save_techfest_event(ev):
        evs = DB._em_load('techfest_events.json')
        idx = next((i for i, e in enumerate(evs) if e.get('id') == ev.get('id')), None)
        if idx is not None: evs[idx] = ev
        else: evs.append(ev)
        DB._em_save('techfest_events.json', evs)

    @staticmethod
    def delete_techfest_event(ev_id):
        evs = [e for e in DB._em_load('techfest_events.json') if e.get('id') != ev_id]
        DB._em_save('techfest_events.json', evs)

    @staticmethod
    def get_techfest_registrations(tf_id=None):
        regs = DB._em_load('techfest_registrations.json')
        if tf_id: regs = [r for r in regs if r.get('techfest_id') == tf_id]
        return regs

    @staticmethod
    def save_techfest_registration(reg):
        regs = DB._em_load('techfest_registrations.json')
        idx = next((i for i, r in enumerate(regs) if r.get('reg_id') == reg.get('reg_id')), None)
        if idx is not None: regs[idx] = reg
        else: regs.append(reg)
        DB._em_save('techfest_registrations.json', regs)

    @staticmethod
    def get_techfest_departments():
        return DB._em_load('techfest_depts.json') or {"UG": [], "PG": []}

    @staticmethod
    def save_techfest_departments(depts):
        DB._em_save('techfest_depts.json', depts)
    # Institution Management
    @staticmethod
    def get_institutions():
        return DB.load_json('institutions.json')

    @staticmethod
    def save_institution(inst):
        institutions = DB.get_institutions()
        existing = next((i for i, item in enumerate(institutions) if item.get('id') == inst.get('id')), None)
        if existing is not None:
            institutions[existing] = inst
        else:
            institutions.append(inst)
        DB.save_json('institutions.json', institutions)

    @staticmethod
    def get_institution_by_id(inst_id):
        institutions = DB.get_institutions()
        return next((inst for inst in institutions if inst.get('id') == inst_id), None)

    @staticmethod
    def get_institution_by_domain(domain):
        institutions = DB.get_institutions()
        return next((inst for inst in institutions if inst.get('domain') == domain), None)

