import os
import json
from app.database import init_db
from app.models import DB

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')

def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def load_em_json(filename):
    path = os.path.join(DATA_DIR, 'em', filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def migrate():
    print("Initializing Database...")
    init_db()

    print("Migrating Institutions...")
    institutions = load_json('institutions.json')
    for i in institutions:
        DB.save_institution(i)
    
    print("Migrating Students...")
    students = load_json('students.json')
    if isinstance(students, list):
        DB.save_students(students)

    print("Migrating Admins...")
    admins = load_json('admins.json')
    if isinstance(admins, list):
        for a in admins:
            DB.save_admin(a)

    print("Migrating Contacts...")
    contacts = load_json('contacts.json')
    if isinstance(contacts, dict) and contacts:
        DB.save_contacts(contacts)

    print("Migrating Settings...")
    settings = load_json('settings.json')
    if isinstance(settings, dict) and settings:
        DB.save_settings(settings)

    print("Migrating Office Bearer Requests...")
    obr = load_json('office_bearer_requests.json')
    if isinstance(obr, list) and obr:
        DB.update_office_bearer_requests(obr)

    print("Migrating Clubs & Events...")
    clubs_dir = os.path.join(DATA_DIR, 'clubs')
    if os.path.exists(clubs_dir):
        for club_id in os.listdir(clubs_dir):
            cpath = os.path.join(clubs_dir, club_id)
            if not os.path.isdir(cpath): continue
            
            # About
            about_path = os.path.join(cpath, 'about.json')
            if os.path.exists(about_path):
                with open(about_path, 'r', encoding='utf-8') as f:
                    try:
                        club_data = json.load(f)
                        club_data['id'] = club_id
                        DB.save_club(club_data)
                    except: pass
                    
            # Elections
            elec_path = os.path.join(cpath, 'elections.json')
            if os.path.exists(elec_path):
                with open(elec_path, 'r', encoding='utf-8') as f:
                    try:
                        elections = json.load(f)
                        DB.save_elections(club_id, elections)
                    except: pass
                    
            # Events
            for ev_folder in os.listdir(cpath):
                ev_path = os.path.join(cpath, ev_folder)
                if not os.path.isdir(ev_path): continue
                
                info_path = os.path.join(ev_path, 'info.json')
                if os.path.exists(info_path):
                    with open(info_path, 'r', encoding='utf-8') as f:
                        try:
                            ev_data = json.load(f)
                            ev_data['club_id'] = club_id
                            DB.save_event(club_id, ev_data)
                        except: pass
                        
                reg_path = os.path.join(ev_path, 'registrations.json')
                if os.path.exists(reg_path):
                    with open(reg_path, 'r', encoding='utf-8') as f:
                        try:
                            regs = json.load(f)
                            for r in regs:
                                DB.save_registration(club_id, r)
                        except: pass

    print("Migrating Event Management (EM) Data...")
    DB.save_em_events(load_em_json('events.json'))
    DB.save_em_tickets(load_em_json('tickets.json'))
    DB.save_em_admins(load_em_json('admins.json'))
    
    em_settings = load_em_json('settings.json')
    if isinstance(em_settings, dict) and em_settings:
        DB.put_em_settings(em_settings)

    ht = load_em_json('hackathon_teams.json')
    for t in ht: DB.save_hackathon_team(t)
    
    evals = load_em_json('evaluators.json')
    for e in evals: DB.save_evaluator(e)
    
    scores = load_em_json('scores.json')
    for s in scores: DB.save_score(s)
    
    tf = load_em_json('techfests.json')
    for t in tf: DB.save_techfest(t)
    
    tfe = load_em_json('techfest_events.json')
    for e in tfe: DB.save_techfest_event(e)
    
    tfr = load_em_json('techfest_registrations.json')
    for r in tfr: DB.save_techfest_registration(r)
    
    tfd = load_em_json('techfest_depts.json')
    if isinstance(tfd, dict) and tfd:
        DB.save_techfest_departments(tfd)

    print("Migration Complete!")

if __name__ == '__main__':
    migrate()
