with open('app/models.py', 'a', encoding='utf-8') as f:
    f.write('''
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
''')
