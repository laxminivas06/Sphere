"""
database.py — PostgreSQL connection pool and strict schema initialisation.

Reads DATABASE_URL from environment (or .env file via python-dotenv).
All JSON/JSONB columns have been removed in favor of strict explicit columns.
"""

import os
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/eventsphere")

_pool = None

def _get_pool():
    global _pool
    if _pool is None:
        _pool = pg_pool.ThreadedConnectionPool(1, 20, DATABASE_URL)
    return _pool

def get_db():
    return _get_pool().getconn()

def close_db(conn):
    if _pool and conn:
        _pool.putconn(conn)

class cursor:
    def __init__(self, dict_cursor=True):
        self.dict_cursor = dict_cursor
        self._conn = None
        self._cur = None

    def __enter__(self):
        self._conn = get_db()
        cursor_factory = psycopg2.extras.RealDictCursor if self.dict_cursor else None
        self._cur = self._conn.cursor(cursor_factory=cursor_factory)
        return self._cur

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            if self._cur:
                self._cur.close()
            if self._conn:
                close_db(self._conn)

def init_db():
    """Create all tables with explicit columns."""
    with cursor(dict_cursor=False) as cur:
        # Core Tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS students (
                roll_number VARCHAR(100) PRIMARY KEY,
                name VARCHAR(255),
                email VARCHAR(255),
                phone VARCHAR(50),
                role VARCHAR(50),
                dob VARCHAR(50),
                department VARCHAR(100),
                year VARCHAR(50),
                class VARCHAR(50),
                photo TEXT,
                password TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS student_contributions (
                id SERIAL PRIMARY KEY,
                roll_number VARCHAR(100),
                club_id VARCHAR(100),
                role VARCHAR(100),
                status VARCHAR(50),
                tenure_year VARCHAR(50),
                events_organized INTEGER
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                email VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255),
                phone VARCHAR(50),
                role VARCHAR(50),
                password TEXT,
                signature TEXT
            );
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS institutions (
                id VARCHAR(100) PRIMARY KEY,
                name VARCHAR(255),
                domain VARCHAR(255),
                logo TEXT,
                tagline TEXT,
                address TEXT
            );
        """)

        # Clubs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clubs (
                id VARCHAR(100) PRIMARY KEY,
                admin_roll VARCHAR(100),
                name VARCHAR(255),
                about TEXT,
                mission TEXT,
                vision TEXT,
                logo TEXT,
                cover_image TEXT,
                mentor_name VARCHAR(255),
                mentor_signature TEXT,
                features TEXT[],
                gallery TEXT[]
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS club_office_bearers (
                id SERIAL PRIMARY KEY,
                club_id VARCHAR(100),
                name VARCHAR(255),
                roll_number VARCHAR(100),
                role VARCHAR(100),
                photo TEXT
            );
        """)

        # Events
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id VARCHAR(100) PRIMARY KEY,
                club_id VARCHAR(100),
                title VARCHAR(255),
                date VARCHAR(50),
                time VARCHAR(50),
                venue VARCHAR(255),
                description TEXT,
                poster TEXT,
                event_status VARCHAR(50),
                payment_type VARCHAR(50),
                registration_type VARCHAR(50),
                fee NUMERIC,
                cash NUMERIC,
                flex NUMERIC,
                printing NUMERIC,
                distribution NUMERIC,
                memento NUMERIC,
                honoring NUMERIC,
                sandal NUMERIC,
                sweets NUMERIC,
                refreshment NUMERIC,
                chairs VARCHAR(255),
                mic VARCHAR(255),
                internet VARCHAR(255),
                others VARCHAR(255),
                guest_feedback TEXT,
                student_feedback TEXT,
                organizer_feedback TEXT,
                after_info TEXT,
                before_info TEXT,
                news TEXT,
                appointment_date VARCHAR(50),
                resource_person VARCHAR(255),
                transport_receive VARCHAR(255),
                transport_send VARCHAR(255),
                approved BOOLEAN DEFAULT FALSE,
                approval_status VARCHAR(50),
                approval_chain TEXT[],
                approver_signatures TEXT[],
                proposer_signatures TEXT[],
                fully_approved_at VARCHAR(100),
                report TEXT,
                report_url TEXT,
                report_approved BOOLEAN DEFAULT FALSE,
                report_approvals TEXT[],
                report_workflow_status VARCHAR(50),
                collaborating_clubs TEXT[],
                participants TEXT,
                timestamp VARCHAR(100),
                date_str VARCHAR(100),
                year VARCHAR(50),
                event_finished BOOLEAN DEFAULT FALSE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                id SERIAL PRIMARY KEY,
                club_id VARCHAR(100),
                event_id VARCHAR(100),
                email VARCHAR(255),
                name VARCHAR(255),
                roll_number VARCHAR(100),
                phone VARCHAR(50),
                department VARCHAR(100),
                year VARCHAR(50),
                payment_status VARCHAR(50),
                payment_id VARCHAR(255),
                payment_verified BOOLEAN DEFAULT FALSE,
                checked_in BOOLEAN DEFAULT FALSE,
                team_id VARCHAR(100),
                team_name VARCHAR(255),
                team_role VARCHAR(50),
                team_members TEXT[]
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS elections (
                id VARCHAR(100) PRIMARY KEY,
                club_id VARCHAR(100),
                title VARCHAR(255),
                year VARCHAR(50),
                status VARCHAR(50),
                candidates TEXT[],
                voters TEXT[]
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY DEFAULT 1,
                email VARCHAR(255),
                phone VARCHAR(50),
                address TEXT,
                facebook VARCHAR(255),
                instagram VARCHAR(255),
                twitter VARCHAR(255),
                linkedin VARCHAR(255)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                maintenance_mode BOOLEAN DEFAULT FALSE,
                academic_year VARCHAR(50),
                theme VARCHAR(50)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS office_bearer_requests (
                id SERIAL PRIMARY KEY,
                club_id VARCHAR(100),
                roll_number VARCHAR(100),
                name VARCHAR(255),
                role VARCHAR(100),
                status VARCHAR(50),
                reason TEXT
            );
        """)

        # EM Tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS em_events (
                id VARCHAR(100) PRIMARY KEY,
                title VARCHAR(255),
                description TEXT,
                date VARCHAR(50),
                time VARCHAR(50),
                venue VARCHAR(255),
                organized_by VARCHAR(255),
                organized_by_id VARCHAR(100),
                assigned_admin VARCHAR(100),
                status VARCHAR(50),
                banner TEXT,
                max_capacity INTEGER,
                ticket_price NUMERIC,
                event_type VARCHAR(50),
                event_category VARCHAR(50),
                allow_external BOOLEAN DEFAULT FALSE,
                created_by VARCHAR(100),
                created_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS em_tickets (
                ticket_id VARCHAR(100) PRIMARY KEY,
                event_id VARCHAR(100),
                user_id VARCHAR(100),
                user_email VARCHAR(255),
                user_name VARCHAR(255),
                user_phone VARCHAR(50),
                user_roll VARCHAR(100),
                user_dept VARCHAR(100),
                user_year VARCHAR(50),
                college_name VARCHAR(255),
                payment_status VARCHAR(50),
                payment_id VARCHAR(255),
                payment_method VARCHAR(50),
                amount NUMERIC,
                order_id VARCHAR(255),
                checked_in BOOLEAN DEFAULT FALSE,
                checked_in_at VARCHAR(100),
                qr_data TEXT,
                created_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS em_admins (
                id VARCHAR(100) PRIMARY KEY,
                email VARCHAR(255),
                name VARCHAR(255),
                phone VARCHAR(50),
                active_event VARCHAR(100),
                assigned_events TEXT[],
                created_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS em_settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                campus_lat NUMERIC,
                campus_lng NUMERIC,
                campus_radius NUMERIC,
                payment_demo_mode BOOLEAN DEFAULT FALSE,
                razorpay_key_id VARCHAR(255),
                razorpay_key_secret VARCHAR(255)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS hackathon_teams (
                team_id VARCHAR(100) PRIMARY KEY,
                event_id VARCHAR(100),
                team_name VARCHAR(255),
                leader_id VARCHAR(100),
                members TEXT[],
                project_title VARCHAR(255),
                description TEXT,
                github_url VARCHAR(255),
                demo_url VARCHAR(255),
                submission_file TEXT,
                submitted BOOLEAN DEFAULT FALSE,
                submitted_at VARCHAR(100),
                payment_status VARCHAR(50),
                payment_method VARCHAR(50),
                checked_in BOOLEAN DEFAULT FALSE,
                checked_in_at VARCHAR(100),
                qr_data TEXT,
                created_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS evaluators (
                id VARCHAR(100) PRIMARY KEY,
                name VARCHAR(255),
                email VARCHAR(255),
                phone VARCHAR(50),
                assigned_events TEXT[],
                created_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                id SERIAL PRIMARY KEY,
                event_id VARCHAR(100),
                team_id VARCHAR(100),
                evaluator_id VARCHAR(100),
                score_value NUMERIC,
                feedback TEXT,
                created_at VARCHAR(100),
                UNIQUE (event_id, team_id, evaluator_id)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS techfests (
                id VARCHAR(100) PRIMARY KEY,
                name VARCHAR(255),
                year VARCHAR(50),
                month VARCHAR(50),
                status VARCHAR(50),
                allow_multi_participation BOOLEAN DEFAULT FALSE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS techfest_events (
                id VARCHAR(100) PRIMARY KEY,
                techfest_id VARCHAR(100),
                name VARCHAR(255),
                description TEXT,
                sub_event_type VARCHAR(50),
                min_team_size INTEGER,
                max_team_size INTEGER,
                registration_fee NUMERIC,
                payment_type VARCHAR(50),
                requirements TEXT,
                scoring_criteria TEXT,
                status VARCHAR(50),
                updated_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS techfest_registrations (
                reg_id VARCHAR(100) PRIMARY KEY,
                techfest_id VARCHAR(100),
                event_id VARCHAR(100),
                team_name VARCHAR(255),
                leader_id VARCHAR(100),
                members TEXT[],
                payment_status VARCHAR(50),
                created_at VARCHAR(100)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS techfest_departments (
                id INTEGER PRIMARY KEY DEFAULT 1,
                ug_depts TEXT[],
                pg_depts TEXT[]
            );
        """)
