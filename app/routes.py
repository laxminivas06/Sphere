from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for, make_response, send_file
from app.models import DB, DATA_DIR
from app.mailer import Mailer
import os
import uuid
import datetime
import io
import csv
import json
import re
import zipfile
from flask import current_app
import qrcode
from fpdf import FPDF
import tempfile
from io import BytesIO
import hmac
import hashlib
import razorpay
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'doc', 'docx'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

api = Blueprint('api', __name__)

@api.route('/clubs/save_member_signature', methods=['POST'])
def api_save_member_signature():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    club_id = data.get('club_id')
    role_type = data.get('role_type') # 'mentor' or 'president'
    signature_data = data.get('signature')
    
    if not all([club_id, role_type, signature_data]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
    club = DB.get_club_by_id(club_id)
    if not club: return jsonify({'success': False, 'message': 'Club not found'}), 404
    
    # Authorization
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    if role_type == 'mentor':
        # Update first mentor
        mentors = club.get('mentors', [])
        if not mentors and club.get('mentor'): mentors = [club['mentor']]
        if not mentors: mentors = [{'name': 'Club Mentor', 'designation': 'Mentor'}]
        mentors[0]['signature'] = signature_data
        club['mentors'] = mentors
    elif role_type == 'president':
        # Update president in office_bearers
        bearers = club.get('office_bearers', [])
        pres = next((ob for ob in bearers if 'president' in ob.get('role', '').lower() or 'president' in ob.get('position', '').lower()), None)
        if not pres:
            pres = {'name': 'Club President', 'role': 'President'}
            bearers.append(pres)
        pres['signature'] = signature_data
        club['office_bearers'] = bearers
    else:
        return jsonify({'success': False, 'message': 'Invalid role type'}), 400
        
    DB.save_club(club)
    return jsonify({'success': True})

@api.route('/user/save_signature', methods=['POST'])
def api_save_signature():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    signature_data = data.get('signature')
    if not signature_data:
        return jsonify({'success': False, 'message': 'No signature data provided'}), 400
    
    admins = DB.get_admins()
    for admin in admins:
        if admin['email'] == user['email']:
            admin['signature'] = signature_data
            break
    
    DB.save_json('admins.json', admins)
    # Update session user as well (excluding the signature to avoid cookie limits)
    session_user = user.copy()
    session_user.pop('signature', None)
    session['user'] = session_user
    
    return jsonify({'success': True})

@api.route('/student/save-achievement', methods=['POST'])
def save_student_achievement():
    user = session.get('user')
    if not user or user.get('role') != 'student':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    club_id = data.get('club_id')
    reg_id = data.get('reg_id')
    note = data.get('note')
    
    if not all([club_id, reg_id]):
        return jsonify({'success': False, 'message': 'Missing fields'}), 400
        
    from app.models import slugify
    club_regs = DB.get_registrations(club_id)
    reg_to_update = next((r for r in club_regs if r.get('id') == reg_id), None)
    
    if reg_to_update:
        event_id = reg_to_update.get('event_id')
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        
        if event:
            event_slug = slugify(event['title'])
            reg_file = os.path.join(DATA_DIR, 'clubs', club_id, event_slug, 'registrations.json')
            
            if os.path.exists(reg_file):
                with open(reg_file, 'r') as f:
                    regs = json.load(f)
                
                for r in regs:
                    if r.get('id') == reg_id:
                        r['achievement_note'] = note
                        break
                
                with open(reg_file, 'w') as f:
                    json.dump(regs, f, indent=4)
                    
                return jsonify({'success': True})
                
    return jsonify({'success': False, 'message': 'Registration not found'}), 404

@api.route('/student/export-portfolio')
def export_student_portfolio():
    user = session.get('user')
    if not user or user.get('role') != 'student':
        return redirect(url_for('login_page'))
    
    roll = user.get('roll_number')
    preview = request.args.get('preview') == 'true'
    all_clubs = DB.get_clubs()
    registrations = []
    
    # Gather data
    from app.models import slugify
    for club in all_clubs:
        club_regs = DB.get_registrations(club['id'])
        for r in club_regs:
            if r.get('roll_number') == roll:
                events = DB.get_events(club['id'])
                event = next((e for e in events if e['id'] == r['event_id']), None)
                if event:
                    r['event_title'] = event.get('title')
                    r['club_name'] = club.get('name')
                    r['date'] = event.get('date')
                    r['category'] = event.get('event_category', 'Club Event').replace('_', ' ').title()
                    registrations.append(r)
    
    registrations.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    # Premium PDF Generation
    try:
        from io import BytesIO
        from flask import send_file
        from PyPDF2 import PdfReader, PdfWriter
        
        class PortfolioPDF(FPDF):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.enclosure_title = None

            def header(self):
                logo_path = os.path.join(current_app.static_folder, 'images', 'toplogo.jpg')
                if os.path.exists(logo_path):
                    try: self.image(logo_path, 10, 5, 190)
                    except: pass
                
                self.set_xy(10, 40)
                if self.enclosure_title:
                    # Predefined box frame for the enclosure content
                    self.set_draw_color(180, 180, 180)
                    self.rect(10, 50, 190, 220)
                    
                    # Small label in the left corner for enclosure title
                    self.set_font('Arial', 'B', 10)
                    self.set_fill_color(32, 43, 129)
                    self.set_text_color(255, 255, 255)
                    self.cell(90, 8, f"  {self.enclosure_title}", 0, 1, 'L', True)
                else:
                    # Main Portfolio Title
                    self.set_font('Arial', 'B', 22)
                    self.set_text_color(32, 43, 129)
                    self.cell(0, 10, 'ACHIEVEMENT PORTFOLIO', 0, 1, 'C')
                    self.set_font('Arial', 'I', 9)
                    self.cell(0, 5, 'Verified Institutional Record of Collegiate Participation', 0, 1, 'C')
                self.ln(5)

            def footer(self):
                self.set_y(-20)
                # Remove redundant footer line on enclosure pages
                if not self.enclosure_title:
                    self.set_draw_color(200, 200, 200)
                    self.line(10, self.get_y(), 200, self.get_y())
                
                self.ln(2)
                self.set_font('Arial', 'B', 8)
                self.set_text_color(100, 100, 100)
                self.cell(60, 10, clean_txt(user.get('name', 'STUDENT')), 0, 0, 'L')
                self.cell(70, 10, f'Page {self.page_no()}', 0, 0, 'C')
                self.cell(60, 10, 'Generated by EventSphere', 0, 0, 'R')

        def clean_txt(t):
            if not t: return ""
            return str(t).encode('latin-1', 'replace').decode('latin-1')

        pdf = PortfolioPDF()
        pdf.alias_nb_pages()
        pdf.add_page()
        
        # Profile Section
        pdf.ln(10)
        start_y = pdf.get_y()
        
        # Student Photo
        photo_path = os.path.join(current_app.static_folder, 'uploads', 'students', roll, user.get('photo', ''))
        if user.get('photo') and os.path.exists(photo_path):
            try: pdf.image(photo_path, 10, start_y, 40, 40)
            except: 
                pdf.set_fill_color(240, 240, 240)
                pdf.rect(10, start_y, 40, 40, 'F')
        else:
            pdf.set_fill_color(240, 240, 240)
            pdf.rect(10, start_y, 40, 40, 'F')
            
        # Fetch club contributions
        is_contributor = False
        club_contributions = []
        user_name = user.get('name', '').lower().strip()
        user_roll = roll.lower().strip()
        for c in DB.get_clubs():
            for ob in c.get('office_bearers', []):
                ob_name = ob.get('name', '').lower().strip()
                ob_roll = ob.get('roll_number', '').lower().strip()
                if (user_roll and ob_roll and user_roll == ob_roll) or (not ob_roll and user_name and ob_name and user_name == ob_name):
                    is_contributor = True
                    # Fetch detailed contribution data from students.json for this club
                    students_db = DB.load_json('students.json')
                    s_rec = next((s for s in students_db if s.get('roll_number', '').lower() == user_roll), None)
                    c_status = 'Present'
                    c_tenure = ''
                    c_events = 0
                    if s_rec and 'contributions' in s_rec:
                        c_data = next((cont for cont in s_rec['contributions'] if cont.get('club_id') == c.get('id')), None)
                        if c_data:
                            c_status = c_data.get('status', 'Present')
                            c_tenure = c_data.get('tenure_year', '')
                            c_events = c_data.get('events_organized', 0)

                    club_contributions.append({
                        'role': ob.get('role', 'Member'),
                        'club_name': c.get('name', 'Club'),
                        'status': c_status,
                        'tenure_year': c_tenure,
                        'events_organized': c_events
                    })
                    break
                    
        # Profile Details
        pdf.set_xy(55, start_y)
        pdf.set_font('Arial', 'B', 18)
        pdf.set_text_color(32, 43, 129)
        
        name_str = clean_txt(user.get('name', '')).upper()
        name_width = pdf.get_string_width(name_str)
        
        # Draw Name
        pdf.cell(name_width + 5, 10, name_str, 0, 0)
        
        # Draw Golden Star Icon beside name if contributor
        if is_contributor:
            star_icon_path = os.path.join(current_app.static_folder, 'images', 'gold_star.png')
            if os.path.exists(star_icon_path):
                # Position for the star icon
                star_x = pdf.get_x()
                star_y = pdf.get_y() + 1 # Slight vertical adjustment
                pdf.image(star_icon_path, star_x, star_y, 7, 7)
            pdf.ln(10)
        else:
            pdf.ln(10)
        
        pdf.set_font('Arial', '', 10)
        pdf.set_text_color(50, 50, 50)
        pdf.set_x(55)
        pdf.cell(0, 6, f"Roll Number: {roll}", 0, 1)
        pdf.set_x(55)
        pdf.cell(0, 6, f"Department: {clean_txt(user.get('department', 'N/A'))}", 0, 1)
        pdf.set_x(55)
        pdf.cell(0, 6, f"Email: {clean_txt(user.get('email', 'N/A'))}", 0, 1)
        pdf.set_x(55)
        pdf.cell(0, 6, f"Academic Year: {user.get('year', 'N/A')} Year", 0, 1)

        pdf.ln(10)

        # Club Contributions Section
        if club_contributions:
            pdf.set_font('Arial', 'B', 14)
            pdf.set_text_color(32, 43, 129)
            pdf.set_fill_color(255, 251, 235) # Light gold background
            pdf.cell(0, 12, '  CLUB LEADERSHIP & CONTRIBUTIONS', 0, 1, 'L', True)
            pdf.ln(4)
            
            for contrib in club_contributions:
                status_val = "Present" if contrib.get('status') == 'Present' else "Former"
                tenure_val = contrib.get('tenure_year', '')
                status_label = f" ({status_val} {tenure_val})" if tenure_val else f" ({status_val})"
                
                pdf.set_font('Arial', 'B', 11)
                pdf.set_text_color(180, 83, 9) # Darker amber
                pdf.cell(0, 7, f" {clean_txt(contrib['role'])} - {clean_txt(contrib['club_name'])}{status_label}", 0, 1)
                
                pdf.set_font('Arial', 'I', 9)
                pdf.set_text_color(100, 100, 100)
                events_organized = contrib.get('events_organized', 0)
                pdf.cell(0, 6, f" Organized {events_organized} Events | Verified Institutional Office Bearer", 0, 1)
                pdf.ln(2)
            pdf.ln(10)
        
        # Participation Timeline
        pdf.set_font('Arial', 'B', 14)
        pdf.set_text_color(32, 43, 129)
        pdf.set_fill_color(245, 247, 255)
        pdf.cell(0, 12, '  PARTICIPATION TIMELINE', 0, 1, 'L', True)
        pdf.ln(5)
        
        for idx, r in enumerate(registrations, 1):
            # Check for page break
            if pdf.get_y() > 240:
                pdf.add_page()
                pdf.ln(10)
                
            pdf.set_fill_color(255, 255, 255)
            
            # Clean Event Block
            pdf.set_font('Arial', 'B', 12)
            pdf.set_text_color(32, 43, 129)
            title = clean_txt(r.get('event_title', 'N/A'))
            pdf.cell(0, 8, f"{idx}. Event Name: {title}", 0, 1, 'L')
            
            pdf.set_font('Arial', '', 10)
            pdf.set_text_color(80, 80, 80)
            details = f"Club: {clean_txt(r.get('club_name'))}  |  Date: {r.get('date')}  |  Category: {r.get('category')}"
            pdf.cell(0, 6, details, 0, 1, 'L')
            
            pdf.set_font('Arial', '', 10)
            pdf.set_text_color(40, 40, 40)
            achieve = clean_txt(r.get('achievement_note', 'No notes added.'))
            # Wrap text for achievements
            pdf.multi_cell(0, 6, f"Learning & Achievements: {achieve}", 0, 'L')
            pdf.ln(5)

        has_docs = any(r.get('supporting_docs') for r in registrations)
        if has_docs:
            pdf.ln(5)
            pdf.set_font('Arial', 'I', 10)
            pdf.set_text_color(100, 100, 100)
            pdf.cell(0, 6, 'Enclosures: Supporting documents copies are attached.', 0, 1, 'L')

        # Signatures section at absolute bottom
        if pdf.get_y() > 245:
            pdf.add_page()
        
        pdf.set_y(255)
        curr_y = 255
        
        # Fetch actual signatures from DB
        all_u = DB.get_admins()
        sigs = {'chief': '', 'principal': '', 'secretary': '', 'ao': '', 'fm': ''}
        for u in all_u:
            r_low = u.get('role', '').lower()
            if 'principal' in r_low: sigs['principal'] = u.get('signature', '')
            if 'secretary' in r_low: sigs['secretary'] = u.get('signature', '')
            if 'chief' in r_low and 'coordinator' in r_low: sigs['chief'] = u.get('signature', '')
            if 'ao' in r_low: sigs['ao'] = u.get('signature', '')
            if 'fm' in r_low or 'finance' in r_low: sigs['fm'] = u.get('signature', '')

        from PIL import Image, ImageOps
        import base64
        
        def draw_sig(data, x, y, is_green=False):
            if not data: return
            try:
                if data.startswith('data:image'):
                    data = data.split(',')[1]
                sig_bytes = base64.b64decode(data)
                img = Image.open(BytesIO(sig_bytes))
                
                if is_green:
                    # Convert to RGBA if not already
                    img = img.convert("RGBA")
                    datas = img.getdata()
                    new_data = []
                    for item in datas:
                        # item is (R, G, B, A)
                        r, g, b, a = item[0], item[1], item[2], item[3]
                        # Calculate luminance to identify white/light background
                        luminance = 0.299*r + 0.587*g + 0.114*b
                        
                        # If pixel is transparent OR light/white (background), make it fully transparent
                        if a == 0 or luminance > 220:
                            new_data.append((255, 255, 255, 0))
                        else:
                            # It's a dark pixel (the signature stroke). 
                            # Convert to institutional green, using original alpha if it was semi-transparent.
                            # For anti-aliased edges on white, we can adjust alpha based on darkness.
                            # The darker the pixel, the more opaque the green.
                            stroke_alpha = int(min(255, a * ((255 - luminance) / 255.0) * 1.5))
                            new_data.append((16, 185, 129, min(255, stroke_alpha)))
                    img.putdata(new_data)
                
                sig_img_io = BytesIO()
                img.save(sig_img_io, format='PNG')
                sig_img_io.seek(0)
                pdf.image(sig_img_io, x+5, y-15, 40, 15)
            except Exception as e:
                pass

        # Draw Signature Block
        sig_roles = [
            ('Chief Coordinator', sigs['chief']),
            ('Principal', sigs['principal']),
            ('Secretary', sigs['secretary'])
        ]
        
        col_w = 190 / len(sig_roles)
        for i, (role_title, sig_data) in enumerate(sig_roles):
            x_pos = 10 + (i * col_w)
            
            # Digital Signature in GREEN
            if not sig_data:
                pdf.set_text_color(16, 185, 129) # Institutional Green
                pdf.set_font('Arial', 'I', 8)
                pdf.set_xy(x_pos, curr_y - 6)
                pdf.cell(col_w, 5, f"Digitally Signed", 0, 0, 'C')
            else:
                draw_sig(sig_data, x_pos, curr_y, is_green=True)
            
            # Signature Line
            pdf.set_draw_color(180, 180, 180)
            pdf.line(x_pos + 10, curr_y, x_pos + col_w - 10, curr_y)
            
            # Names/Titles in BLACK
            pdf.set_text_color(0, 0, 0)
            pdf.set_font('Arial', 'B', 9)
            pdf.set_xy(x_pos, curr_y + 1)
            pdf.cell(col_w, 5, role_title, 0, 0, 'C')

        # Process Enclosures
        pdf_overlays = [] # (fpdf_page_idx, doc_path, doc_page_idx)
        if has_docs:
            for r in registrations:
                docs = r.get('supporting_docs', [])
                for doc in docs:
                    doc_path = os.path.join(current_app.static_folder, 'uploads', 'students', roll, 'documents', r['id'], doc)
                    if os.path.exists(doc_path):
                        title = clean_txt(r.get('event_title'))
                        if doc.lower().endswith(('.png', '.jpg', '.jpeg')):
                            pdf.enclosure_title = f"Enclosure: {title}"
                            pdf.add_page()
                            # Fit image inside container box (x=10, y=50, w=190, h=220) with padding
                            try:
                                from PIL import Image
                                with Image.open(doc_path) as img:
                                    img_w, img_h = img.size
                                    box_w, box_h = 180, 210
                                    img_aspect = img_w / float(img_h)
                                    box_aspect = box_w / float(box_h)
                                    
                                    if img_aspect > box_aspect:
                                        final_w = box_w
                                        final_h = box_w / img_aspect
                                    else:
                                        final_h = box_h
                                        final_w = box_h * img_aspect
                                        
                                    final_x = 15 + (box_w - final_w) / 2
                                    final_y = 55 + (box_h - final_h) / 2
                                    pdf.image(doc_path, final_x, final_y, final_w, final_h)
                            except Exception:
                                pdf.image(doc_path, 15, 60, 180, 0)
                        elif doc.lower().endswith('.pdf'):
                            try:
                                reader_doc = PdfReader(doc_path)
                                for i in range(len(reader_doc.pages)):
                                    pdf.enclosure_title = f"Enclosure: {title} (Part {i+1})"
                                    pdf.add_page()
                                    pdf_overlays.append((len(pdf.pages), doc_path, i))
                            except: pass
        
        # Reset enclosure title
        pdf.enclosure_title = None

        # Output main PDF to buffer
        try: main_out = pdf.output(dest='S')
        except TypeError: main_out = pdf.output('', 'S')
        if isinstance(main_out, str): main_out = main_out.encode('latin-1')
        
        if not pdf_overlays:
            final_buffer = BytesIO(main_out)
        else:
            writer = PdfWriter()
            reader_main = PdfReader(BytesIO(main_out))
            overlay_map = {item[0]: item for item in pdf_overlays}
            
            for i, f_page in enumerate(reader_main.pages, 1):
                if i in overlay_map:
                    _, doc_path, doc_idx = overlay_map[i]
                    try:
                        from PyPDF2 import Transformation
                        reader_doc = PdfReader(doc_path)
                        d_page = reader_doc.pages[doc_idx]
                        
                        # Scale down and translate to center within the frame
                        # This ensures the document is fully visible and properly aligned inside the container
                        d_page.scale_by(0.7)
                        d_page.add_transformation(Transformation().translate(tx=89, ty=93))
                        
                        # Merge frame ON TOP
                        f_page.merge_page(d_page)
                        writer.add_page(f_page)
                    except Exception:
                        writer.add_page(f_page)
                else:
                    writer.add_page(f_page)
                
            final_buffer = BytesIO()
            writer.write(final_buffer)
            
        final_buffer.seek(0)
        
        return send_file(
            final_buffer,
            mimetype='application/pdf',
            as_attachment=(not preview),
            download_name=f'Achievement_Portfolio_{roll}.pdf'
        )

        
        return send_file(
            final_buffer,
            mimetype='application/pdf',
            as_attachment=(not preview),
            download_name=f'Achievement_Portfolio_{roll}.pdf'
        )

        
        return send_file(
            final_buffer,
            mimetype='application/pdf',
            as_attachment=(not preview),
            download_name=f'Achievement_Portfolio_{roll}.pdf'
        )

        
        return send_file(
            final_buffer,
            mimetype='application/pdf',
            as_attachment=(not preview),
            download_name=f'Achievement_Portfolio_{roll}.pdf'
        )
    except Exception as e:
        return f"System Portfolio Error: {str(e)}", 500

@api.route('/get-student-qr/<club_id>/<reg_id>')
def get_student_qr(club_id, reg_id):
    # This route is used by the success page to display the QR code
    return "" # Implementation details truncated for space

@api.route('/student/upload-document', methods=['POST'])
def student_upload_document():
    user = session.get('user')
    if not user or user.get('role') != 'student':
        return redirect(url_for('login_page'))
    
    roll = user.get('roll_number')
    reg_id = request.form.get('reg_id')
    club_id = request.form.get('club_id')
    file = request.files.get('document')
    
    if not all([reg_id, club_id, file]):
        return redirect('/student/history')
        
    allowed_docs = {'png', 'jpg', 'jpeg'}
    if file and '.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_docs:
        filename = secure_filename(file.filename)
        # Create directory: static/uploads/students/[roll]/documents/[reg_id]/
        upload_path = os.path.join(current_app.static_folder, 'uploads', 'students', roll, 'documents', reg_id)
        os.makedirs(upload_path, exist_ok=True)
        
        file.save(os.path.join(upload_path, filename))
        
        # Update registration file
        from app.models import slugify
        club_regs = DB.get_registrations(club_id)
        reg_to_update = next((r for r in club_regs if r.get('id') == reg_id), None)
        
        if reg_to_update:
            event_id = reg_to_update.get('event_id')
            events = DB.get_events(club_id)
            event = next((e for e in events if e['id'] == event_id), None)
            
            if event:
                event_slug = slugify(event['title'])
                reg_file = os.path.join(DATA_DIR, 'clubs', club_id, event_slug, 'registrations.json')
                
                if os.path.exists(reg_file):
                    with open(reg_file, 'r') as f:
                        regs = json.load(f)
                    
                    for r in regs:
                        if r.get('id') == reg_id:
                            if 'supporting_docs' not in r:
                                r['supporting_docs'] = []
                            if filename not in r['supporting_docs']:
                                r['supporting_docs'].append(filename)
                            break
                    
                    with open(reg_file, 'w') as f:
                        json.dump(regs, f, indent=4)
                    
                    return redirect('/student/history')
    
    return redirect('/student/history')

@api.route('/student/remove-document', methods=['POST'])
def student_remove_document():
    user = session.get('user')
    if not user or user.get('role') != 'student':
        return redirect(url_for('login_page'))
    
    roll = user.get('roll_number')
    reg_id = request.form.get('reg_id')
    club_id = request.form.get('club_id')
    doc_name = request.form.get('doc_name')
    
    if not all([reg_id, club_id, doc_name]):
        return redirect('/student/history')
        
    from app.models import slugify
    club_regs = DB.get_registrations(club_id)
    reg_to_update = next((r for r in club_regs if r.get('id') == reg_id), None)
    
    if reg_to_update:
        event_id = reg_to_update.get('event_id')
        events = DB.get_events(club_id)
        event = next((e for e in events if e['id'] == event_id), None)
        
        if event:
            event_slug = slugify(event['title'])
            reg_file = os.path.join(DATA_DIR, 'clubs', club_id, event_slug, 'registrations.json')
            
            if os.path.exists(reg_file):
                with open(reg_file, 'r') as f:
                    regs = json.load(f)
                
                for r in regs:
                    if r.get('id') == reg_id:
                        if 'supporting_docs' in r and doc_name in r['supporting_docs']:
                            r['supporting_docs'].remove(doc_name)
                            # Remove file from disk
                            doc_path = os.path.join(current_app.static_folder, 'uploads', 'students', roll, 'documents', reg_id, doc_name)
                            if os.path.exists(doc_path):
                                os.remove(doc_path)
                        break
                
                with open(reg_file, 'w') as f:
                    json.dump(regs, f, indent=4)
                    
    return redirect('/student/history')
    # We look up the registration and generate a QR based on its ID
    clubs = DB.get_clubs()
    club = next((c for c in clubs if c['id'] == club_id), None)
    if not club: return "Club not found", 404
    
    events = DB.get_events(club_id)
    reg = None
    for event in events:
        regs = DB.get_registrations(club_id, event['id'])
        reg = next((r for r in regs if r['id'] == reg_id), None)
        if reg: break
        
    if not reg: return "Registration not found", 404
    
    # Generate QR containing the user details for attendance
    import qrcode
    from io import BytesIO
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    
    # Construct data string as requested: Name, Roll Number, Department
    qr_data = f"Name: {reg.get('name', 'N/A')}\nRoll: {reg.get('roll_number', 'N/A')}\nDept: {reg.get('department', 'N/A')}\nRegID: {reg_id}"
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    
    download = request.args.get('download') == 'true'
    return send_file(img_io, mimetype='image/png', as_attachment=download, download_name=f"QR_{reg_id}.png")

def is_trusted_club(club_id):
    # A club is trusted if they have at least one event with an approved report
    events = DB.get_events(club_id)
    return any(e.get('report_approved') for e in events)

def generate_qr_image(qr_data: str) -> str:
    """Generate QR PNG to a temp file and return its path."""
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    temp_dir = os.path.join('static', 'temp_qr')
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}.png")
    img.save(temp_path, 'PNG')
    return temp_path

# Keep old name as alias for compatibility with any other callers
generate_qr_attachment = generate_qr_image

def send_registration_email(reg):
    """Rich HTML registration confirmation with QR code attached."""
    if not reg.get('email'):
        return
    # Build a detailed QR (name, roll, dept, reg_id)
    qr_data = (
        f"Name: {reg.get('name','N/A')}\n"
        f"Roll: {reg.get('roll_number','N/A')}\n"
        f"Dept: {reg.get('department','N/A')}\n"
        f"RegID: {reg.get('id','N/A')}"
    )
    qr_path = generate_qr_image(qr_data)
    try:
        Mailer.send_registration_confirmation(reg, qr_image_path=qr_path)
    finally:
        if os.path.exists(qr_path):
            os.remove(qr_path)

def send_verification_email(reg):
    """Rich HTML payment-verified email with QR code attached."""
    if not reg.get('email'):
        return
    qr_data = (
        f"Name: {reg.get('name','N/A')}\n"
        f"Roll: {reg.get('roll_number','N/A')}\n"
        f"Dept: {reg.get('department','N/A')}\n"
        f"RegID: {reg.get('id','N/A')}"
    )
    qr_path = generate_qr_image(qr_data)
    try:
        Mailer.send_payment_verified(reg, qr_image_path=qr_path)
    finally:
        if os.path.exists(qr_path):
            os.remove(qr_path)

@api.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@api.route('/login', methods=['POST'])
def api_login():
    data = request.json or {}
    roll = data.get('roll_number', '').strip()
    dob = data.get('dob', '').strip()

    # Fields that should NEVER be sent to the browser or stored in session
    SENSITIVE_FIELDS = {'password', 'dob'}

    def sanitize(user_obj):
        return {k: v for k, v in user_obj.items() if k not in SENSITIVE_FIELDS}

    # 2. Check Admins / Evaluators / Managers in admins.json
    # Filter by current institution for Super Admins
    from flask import session
    current_inst_id = session.get('current_institution_id')
    
    admins = DB.get_admins()
    for admin in admins:
        admin_email = admin.get('email', '').strip().lower()
        admin_roll = admin.get('roll_number', '').strip().lower()
        admin_inst_id = admin.get('institution_id')

        # Match by email or roll_number
        if (admin_email == roll.lower() or admin_roll == roll.lower()) and admin.get('password') == dob:
            # Enforce institutional boundary for non-global admins
            if admin_inst_id and current_inst_id and admin_inst_id != current_inst_id:
                continue # Belongs to a different institution
                
            safe_admin = sanitize(admin)
            # Store in session without the large signature field to avoid cookie size limits
            session_admin = safe_admin.copy()
            session_admin.pop('signature', None)
            session['user'] = session_admin
            return jsonify({'success': True, 'user': safe_admin})

    # 2.5. Check Event Admins
    from app.event_mgmt_routes import get_em_admins
    em_admins = get_em_admins()
    for admin in em_admins:
        admin_email = admin.get('email', '').strip().lower()
        admin_roll = admin.get('roll_number', '').strip().lower()
        if (admin_email == roll.lower() or admin_roll == roll.lower()) and admin.get('password') == dob:
            admin['role'] = 'event_admin'  # Explicitly set role for EM admins
            safe_admin = sanitize(admin)
            session['user'] = safe_admin
            return jsonify({'success': True, 'user': safe_admin})

    # 3. Check Students
    student = DB.get_student_by_roll(roll)
    if student:
        formatted_dob = dob
        if len(dob) == 8 and dob.isdigit():
            formatted_dob = f"{dob[4:8]}-{dob[2:4]}-{dob[0:2]}"
            
        if student.get('dob') == dob or student.get('dob') == formatted_dob:
            student['role'] = 'student'
            safe_student = sanitize(student)
            session['user'] = safe_student
            return jsonify({'success': True, 'user': safe_student})

    return jsonify({'success': False, 'message': 'Invalid credentials. Please try again.'})

@api.route('/events/update_details', methods=['POST'])
def update_event_details():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    print('DEBUG FILES:', request.files)
    data = request.form.to_dict()
    event_id = data.get('event_id') or data.get('id')
    all_events = DB.get_events()
    event = next((e for e in all_events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    club_id = event['club_id']
    club = DB.get_club_by_id(club_id)
    
    # Authorization check
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    # Update fields
    for field in ['title', 'venue', 'date', 'time', 'payment_type', 'fee', 'description']:
        if field in data:
            event[field] = data[field]
            
    if 'event_type' in data:
        event['registration_type'] = data['event_type']
    
    # Handle collaborating clubs
    event['collaborating_clubs'] = request.form.getlist('collaborating_clubs')

    # Handle poster upload
    poster = request.files.get('poster')
    if poster and poster.filename:
        if allowed_file(poster.filename):
            from app.models import slugify
            event_slug = slugify(event['title'])
            upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', event_slug, 'posters')
            os.makedirs(upload_dir, exist_ok=True)
            
            # Secure filename and add unique prefix
            filename = secure_filename(poster.filename)
            fn = f"poster_{uuid.uuid4().hex[:8]}_{filename}"
            poster.save(os.path.join(upload_dir, fn))
            event['poster'] = fn

    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/bulk-email', methods=['POST'])
def api_bulk_email():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json or {}
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    subject = data.get('subject')
    content = data.get('content')
    target = data.get('target', 'all') # 'all' or 'verified'
    
    if not all([club_id, event_id, subject, content]):
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
    club = DB.get_club_by_id(club_id)
    if not club: return jsonify({'success': False, 'message': 'Club not found'}), 404
    
    # Permission check
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    regs = DB.get_registrations(club_id, event_id)
    if target == 'verified':
        regs = [r for r in regs if r.get('status') == 'verified' or r.get('payment_status') == 'verified']
        
    sent_count = 0
    for reg in regs:
        email = reg.get('email')
        if email:
            html_message = content.replace('\n', '<br>')
            body = f"Hi {reg.get('name', 'Student')},\n\n{content}"
            html_body = f"""
            <div style="font-family: sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #334155; border-radius: 10px; background: #0f172a; color: #f1f5f9;">
                <h2 style="color: #6366f1;">{event['title']}</h2>
                <p>Hi <strong>{reg.get('name', 'Student')}</strong>,</p>
                <p>{html_message}</p>
                <hr style="border: none; border-top: 1px solid #334155; margin: 20px 0;">
                <p style="font-size: 0.8rem; color: #94a3b8;">Sent via Sphoorthy EventSphere</p>
            </div>
            """
            try:
                Mailer.send_email(email, subject, body, html_body, club_id=club_id)
                sent_count += 1
            except:
                continue
            
    return jsonify({'success': True, 'count': sent_count})

@api.route('/events/create_permission', methods=['POST'])
def create_event_permission():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    print('DEBUG FILES:', request.files)
    data = request.form.to_dict()
    club_id = data.get('club_id')
    if not club_id: return jsonify({'success': False, 'message': 'Club ID required'}), 400
    
    events = DB.get_events(club_id)
    # Block event creation if there are any active (non-completed) events
    active_events = [e for e in events if not e.get('report_approved') and not e.get('deleted') and e.get('event_status') not in ('deleted', 'rejected')]
    if len(active_events) > 0:
        return jsonify({'success': False, 'message': 'You cannot create a new event until the report for your previous event is verified and approved.'}), 403
    
    # Generate unique ID
    event_id = str(uuid.uuid4())
    
    # Default academic year
    now = datetime.datetime.now()
    if now.month >= 6:
        year_str = f"{now.year % 100}-{(now.year + 1) % 100}"
    else:
        year_str = f"{(now.year - 1) % 100}-{now.year % 100}"
        
    # Get club details for auto-filled signatures
    club = DB.get_club_by_id(club_id)
    mentor_name = ""
    mentor_sig = ""
    if club:
        # Try to get from 'mentors' list first
        mentors = club.get('mentors', [])
        if not mentors and club.get('mentor'): mentors = [club['mentor']]
        
        if mentors:
            mentor_name = mentors[0].get('name', '')
            mentor_sig = mentors[0].get('signature', '')
    
    # Get president from office bearers
    president_name = ""
    president_sig = ""
    if club and club.get('office_bearers'):
        pres = next((ob for ob in club['office_bearers'] if 'president' in ob.get('role', '').lower()), None)
        if pres:
            president_name = pres.get('name', '')
            president_sig = pres.get('signature', '')

    new_event = {
        'id': event_id,
        'title': data.get('title', 'Untitled Event'),
        'date': data.get('date', ''),
        'time': data.get('time', ''),
        'venue': data.get('venue', 'TBD'),
        'description': data.get('description', ''),
        'payment_type': data.get('payment_type', 'free'), # free/paid
        'registration_type': data.get('event_type', 'individual'), # individual/team
        'fee': data.get('fee', '0'),
        'club_id': club_id,
        'year': year_str,
        'approved': False,
        'event_finished': False,
        'report_approved': False,
        'event_status': 'pending_principal',
        'approval_status': 'pending_principal',
        'approval_chain': [],
        'proposer_signatures': {
            'club_coordinator': user.get('name', ''),
            'club_members': 'Active Members',
            'club_mentor': mentor_name,
            'mentor_sig': mentor_sig,
            'president': president_name,
            'president_sig': president_sig
        },
        'approver_signatures': {},
        'timestamp': datetime.datetime.now().isoformat(),
        'collaborating_clubs': request.form.getlist('collaborating_clubs')
    }
    
    DB.save_event(club_id, new_event)

    # ── Promotional email blast to past registrants ───────────────────────────
    try:
        club = DB.get_club_by_id(club_id)
        poster_file = request.files.get('poster')
        poster_path = None
        if poster_file and poster_file.filename and allowed_file(poster_file.filename):
            from app.models import slugify
            ev_slug = slugify(new_event['title'])
            poster_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', ev_slug, 'posters')
            os.makedirs(poster_dir, exist_ok=True)
            poster_fn = f"poster_{uuid.uuid4().hex[:8]}_{secure_filename(poster_file.filename)}"
            poster_path = os.path.join(poster_dir, poster_fn)
            poster_file.save(poster_path)
            new_event['poster'] = poster_fn
            DB.save_event(club_id, new_event)

        # Gather unique emails from ALL past registrations of this club
        all_regs = DB.get_registrations(club_id)
        seen = set()
        blast_emails = []
        for r in all_regs:
            em = r.get('email', '')
            if em and em not in seen:
                seen.add(em)
                blast_emails.append(em)

        if blast_emails and club:
            base_url = request.host_url.rstrip('/')
            event_url = f"{base_url}/event/{club_id}/{event_id}"
            Mailer.send_new_event_promo(
                event=new_event,
                club=club,
                recipient_emails=blast_emails,
                event_url=event_url,
                poster_path=poster_path,
            )
    except Exception as _promo_err:
        print(f"[Promo email] Error: {_promo_err}")

    return jsonify({'success': True, 'event_id': event_id})

def _apply_auto_signatures(club, event, user):
    import datetime
    now_str = datetime.datetime.now().isoformat()
    
    if 'approval_chain' not in event:
        event['approval_chain'] = []
    
    # Reset auto-signatures to avoid duplicates
    event['approval_chain'] = [sig for sig in event['approval_chain'] if not sig.get('is_auto')]
    if 'proposer_signatures' not in event:
        event['proposer_signatures'] = {}

    # 1. Mentor Signature
    mentors = club.get('mentors', [])
    if not mentors and club.get('mentor'): mentors = [club['mentor']]
    
    mentor_name = "Club Mentor"
    mentor_sig = ""
    if mentors:
        mentor_name = mentors[0].get('name', '')
        mentor_sig = mentors[0].get('signature', '')

    event['approval_chain'].append({
        'stage': 'club_mentor',
        'role_label': 'Club Mentor',
        'approver_name': mentor_name,
        'approved_at': now_str,
        'signature': mentor_sig or f"Digitally Signed by {mentor_name}",
        'is_auto': True
    })
    event['proposer_signatures']['club_mentor'] = mentor_name
    event['proposer_signatures']['mentor_sig'] = mentor_sig
    
    # 2. Club President / Secretary
    bearers = club.get('office_bearers', [])
    found_bearer = False
    for b in bearers:
        pos = b.get('position', '').lower() or b.get('role', '').lower()
        if pos in ['president', 'secretary']:
            event['approval_chain'].append({
                'stage': f"club_{pos}",
                'role_label': f"Club {b.get('position') or b.get('role')}",
                'approver_name': b['name'],
                'approved_at': now_str,
                'signature': b.get('signature') or f"Digitally Signed by {b['name']}",
                'is_auto': True
            })
            event['proposer_signatures'][pos] = b['name']
            if pos == 'president':
                event['proposer_signatures']['president_sig'] = b.get('signature', '')
            found_bearer = True
    
    if not found_bearer:
        name = user.get('name', 'Club Admin')
        sig = user.get('signature', '')
        if not sig:
            # Fetch from DB since it's not in session
            db_user = DB.get_admin_by_email(user.get('email'))
            if db_user:
                sig = db_user.get('signature', '')
        
        event['approval_chain'].append({
            'stage': 'club_admin',
            'role_label': 'Club Coordinator',
            'approver_name': name,
            'approved_at': now_str,
            'signature': sig or f"Digitally Signed by {name}",
            'is_auto': True
        })
        event['proposer_signatures']['president'] = name # Fallback for template
        event['proposer_signatures']['president_sig'] = sig


@api.route('/events/save_permission', methods=['POST'])
def save_event_permission():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    # BLOCK EDITING AFTER SECRETARY APPROVAL
    # Secretary is the stage before Principal. So pending_principal means Secretary approved.
    if event.get('approval_status') in ['pending_principal', 'approved'] and user.get('role') == 'club_admin':
        return jsonify({'success': False, 'message': 'Letter is locked after institutional verification.'}), 403

    # Update fields and track edits
    allowed_fields = [
        'title', 'date', 'time', 'venue', 'description', 'payment_type', 'event_type', 'fee', 
        'resource_person', 'collaborating_clubs', 'appointment_date', 'transport_receive', 
        'transport_send', 'honoring', 'memento', 'cash', 'refreshment', 'printing', 
        'distribution', 'flex', 'sandal', 'sweets', 'participants', 'chairs', 'mic', 
        'internet', 'others', 'news', 'before', 'after', 'guest_feedback', 
        'student_feedback', 'organizer_feedback', 'date_str'
    ]
    
    has_changes = False
    for key in allowed_fields:
        if key in data and str(data[key]) != str(event.get(key)):
            event[key] = data[key]
            has_changes = True
            
    if has_changes:
        event['is_edited'] = True
            
    # Apply auto-signatures immediately so they appear in the letter view
    club = DB.get_club_by_id(club_id)
    _apply_auto_signatures(club, event, user)
    
    # Auto-submit if not already submitted
    if not event.get('approval_status') and not event.get('event_finished'):
        event['approval_status'] = 'pending_principal'
        event['event_status'] = 'pending_principal'
        event['approved'] = False
        if 'approval_chain' not in event:
            event['approval_chain'] = []

    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/approve', methods=['POST'])
def approve_event():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    action = data.get('action', 'approve') # approve or reject
    
    event = DB.get_event_by_id(club_id, event_id)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    role = user.get('role')
    status = event.get('approval_status')
    
    approval_order = ['chief_coordinator', 'ao', 'fm', 'secretary']
    # Use the user's role directly as the step identifier
    current_role_step = role

    if status != f"pending_{current_role_step}" and not (role == 'chief_coordinator'):
        # Special case: Chief coordinator can approve anything
        if role != 'chief_coordinator':
            return jsonify({'success': False, 'message': f'You are not the current approver. Current status: {status}'}), 403

    if action == 'reject':
        event['approval_status'] = 'rejected'
        event['event_status'] = 'rejected'
        DB.save_event(club_id, event)
        return jsonify({'success': True, 'message': 'Event rejected'})

    # Handle Approval
    if 'approver_signatures' not in event: event['approver_signatures'] = {}
    
    # Fetch signature from DB since it's not in session
    user_sig = user.get('signature')
    if not user_sig:
        db_user = DB.get_admin_by_email(user.get('email'))
        if db_user:
            user_sig = db_user.get('signature')

    event['approver_signatures'][current_role_step] = {
        'name': user.get('name'),
        'timestamp': datetime.datetime.now().isoformat(),
        'signature': user_sig # Capture the signature from DB/session
    }

    # Determine next step
    try:
        current_idx = approval_order.index(current_role_step)
        if current_idx < len(approval_order) - 1:
            next_role = approval_order[current_idx + 1]
            event['approval_status'] = f"pending_{next_role}"
        else:
            # Final approval reached
            event['approval_status'] = 'approved'
            event['approved'] = True
            event['event_status'] = 'active'
    except ValueError:
        # If role not in order, chief coordinator might be forcing approval
        if role == 'chief_coordinator':
            event['approval_status'] = 'approved'
            event['approved'] = True
            event['event_status'] = 'active'
        else:
            return jsonify({'success': False, 'message': 'Invalid approval role'}), 400

    DB.save_event(club_id, event)
    return jsonify({'success': True, 'next_status': event['approval_status']})

@api.route('/events/finish', methods=['POST'])
def finish_event():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
        
    event['event_finished'] = True
    event['event_status'] = 'pending_report'
    DB.save_event(club_id, event)
    return jsonify({'success': True, 'redirect': f'/admin/generate-report/{club_id}/{event_id}'})

@api.route('/events/upload_report', methods=['POST'])
def upload_report():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    club_id = request.form.get('club_id')
    event_id = request.form.get('event_id')
    report_file = request.files.get('report')
    
    if not report_file: return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    # Authorization check
    club = DB.get_club_by_id(club_id)
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    if not allowed_file(report_file.filename) and not report_file.filename.endswith('.pdf'):
        return jsonify({'success': False, 'message': 'Invalid file type. Only PDF and images allowed.'}), 400

    from app.models import slugify
    event_slug = slugify(event['title'])
    upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', event_slug, 'reports')
    os.makedirs(upload_dir, exist_ok=True)
    
    # Secure filename
    filename = f"report_{uuid.uuid4().hex[:8]}_{secure_filename(report_file.filename)}"
    report_file.save(os.path.join(upload_dir, filename))
    
    event['report'] = filename
    event['report_url'] = f"/static/uploads/clubs/{club_id}/events/{event_slug}/reports/{filename}"
    
    # Only initialize workflow and mentor signature on fresh submission
    mentor_sig = request.form.get('mentor_signature')
    if mentor_sig:
        import datetime
        mentor_role = request.form.get('mentor_role') or 'Mentor / Convenor'
        event['report_approved'] = False 
        event['report_workflow_status'] = 'pending_report_chief_coordinator'
        event['report_approvals'] = {
            'mentor': {
                'name': mentor_role,
                'timestamp': datetime.datetime.now().isoformat(),
                'signature': mentor_sig
            },
            'chief_coordinator': None,
            'principal': None
        }
        event['event_status'] = 'pending_report_approval'
    
    DB.save_event(club_id, event)
    # ── Notify chief coordinator via email ─────────────────────────────────────────
    try:
        club_obj = DB.get_club_by_id(club_id)
        # Find chief_coordinator email from admins.json
        admins = DB.get_admins()
        chief_coordinator = next((a for a in admins if a.get('role') == 'chief_coordinator'), None)
        sa_email = chief_coordinator.get('email') if chief_coordinator else None
        if not sa_email:
            # Fallback: check settings.json for a configured chief coordinator email
            settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
            if os.path.exists(settings_path):
                with open(settings_path) as _sf:
                    try:
                        _s = json.load(_sf)
                        sa_email = _s.get('chief_coordinator_email')
                    except Exception:
                        pass
        if sa_email:
            review_url = f"{request.host_url.rstrip('/')}/admin/reports"
            Mailer.send_report_submitted_to_admin(
                event=event,
                club=club_obj or {'name': club_id},
                admin_email=sa_email,
                review_url=review_url,
            )
    except Exception as _re:
        print(f"[Report email] Error notifying chief coordinator: {_re}")

    return jsonify({'success': True})

@api.route('/students/list', methods=['GET'])
def list_students():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

@api.route('/report/approve_stage', methods=['POST'])
def approve_report_stage():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        
    data = request.get_json()
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    event = DB.get_event_by_id(club_id, event_id)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'})
    
    role = user.get('role')
    status = event.get('report_workflow_status')

    # Fetch official's signature from DB to ensure it is correctly captured and not null
    admin_info = DB.get_admin_by_email(user.get('email'))
    admin_sig = admin_info.get('signature') if admin_info else user.get('signature')

    import datetime
    sig_entry = {
        'name': user.get('name'),
        'email': user.get('email'),
        'role': role,
        'timestamp': datetime.datetime.now().isoformat(),
        'signature': admin_sig
    }

    if status == 'pending_report_chief_coordinator' and role == 'chief_coordinator':
        event['report_approvals']['chief_coordinator'] = sig_entry
        event['report_workflow_status'] = 'pending_report_principal'
        msg = "Approved by Chief Coordinator. Awaiting Principal Approval."
    elif status == 'pending_report_principal' and role == 'principal':
        event['report_approvals']['principal'] = sig_entry
        event['report_workflow_status'] = 'finalized'
        event['report_approved'] = True
        event['event_status'] = 'approved'
        event['approval_status'] = 'approved'
        msg = "Report finalized and approved by Principal."
    else:
        return jsonify({'success': False, 'message': f'You are not authorized to approve at this stage ({status})'}), 403
    
    DB.save_event(club_id, event)
    return jsonify({
        'success': True, 
        'message': msg,
        'status': event['report_workflow_status'],
        'sig_entry': sig_entry
    })
        
    page = int(request.args.get('page', 1))
    search = request.args.get('search', '').lower()
    year_filter = request.args.get('year', '').lower()
    
    students = DB.get_students()
    
    if search:
        students = [s for s in students if search in s.get('roll_number', '').lower() or search in s.get('name', '').lower()]
        
    if year_filter:
        students = [s for s in students if s.get('year', '').lower() == year_filter]
        
    # Sort students by roll_number or name
    students.sort(key=lambda x: x.get('roll_number', ''))
    
    per_page = 20
    total = len(students)
    pages = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    
    return jsonify({
        'success': True,
        'students': students[start:end],
        'pages': pages,
        'current_page': page,
        'total': total
    })

@api.route('/students/export', methods=['GET'])
def export_students():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return "Unauthorized", 403
        
    year_filter = request.args.get('year', '').lower()
    students = DB.get_students()
    
    if year_filter:
        students = [s for s in students if s.get('year', '').lower() == year_filter]
        
    students.sort(key=lambda x: x.get('roll_number', ''))
    
    import io, csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Roll Number', 'Name', 'Department', 'Year', 'Email', 'Phone', 'DOB'])
    
    for s in students:
        writer.writerow([
            s.get('roll_number', ''),
            s.get('name', ''),
            s.get('department', ''),
            s.get('year', ''),
            s.get('email', ''),
            s.get('phone', ''),
            s.get('dob', '')
        ])
        
    response = make_response(output.getvalue())
    filename_year = year_filter if year_filter else "all"
    response.headers["Content-Disposition"] = f"attachment; filename=students_{filename_year}.csv"
    response.headers["Content-type"] = "text/csv; charset=utf-8"
    return response

@api.route('/students/leaderboard_api', methods=['GET'])
def get_students_leaderboard_api():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    year_filter = request.args.get('year', '').lower()
    students = DB.get_students()
    
    if year_filter:
        students = [s for s in students if s.get('year', '').lower() == year_filter]
        
    # Count verified attendances
    all_regs = []
    for club in DB.get_clubs():
        all_regs.extend(DB.get_registrations(club['id']))
        
    # Add EM tickets as well
    from app.event_mgmt_routes import get_tickets
    all_regs.extend(get_tickets())
    
    attendance_counts = {}
    for r in all_regs:
        identifier = r.get('roll_number') or r.get('email')
        if not identifier: continue
        identifier = identifier.strip().lower()
        attendance_counts[identifier] = attendance_counts.get(identifier, 0) + 1
            
    leaderboard = []
    for s in students:
        roll = s.get('roll_number', '').lower()
        count = attendance_counts.get(roll, 0)
        if count > 0:
            s['attended_events'] = count
            leaderboard.append(s)
            
    leaderboard.sort(key=lambda x: x.get('attended_events', 0), reverse=True)
    top_students = leaderboard[:50] # Send top 50
    
    return jsonify({
        'success': True,
        'leaderboard': top_students
    })

@api.route('/students/upload', methods=['POST'])
def upload_students_csv():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'})
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'})
        
    try:
        content = file.read().decode('utf-8').splitlines()
        import csv
        reader = list(csv.DictReader(content))
        
        students = DB.get_students()
        added_count = 0
        updated_count = 0
        
        # Validation Pass
        for i, row in enumerate(reader, start=1):
            roll = row.get('roll_number') or row.get('Roll Number') or row.get('reg') or row.get('Reg')
            name = row.get('name') or row.get('Name') or row.get('student name') or row.get('Student Name')
            dept = row.get('department') or row.get('Department')
            year = row.get('year') or row.get('Year')
            dob = row.get('dob') or row.get('DOB') or row.get('Date of Birth')
            
            if not roll or not str(roll).strip(): return jsonify({'success': False, 'message': f'Row {i}: Missing mandatory field "Roll Number/Reg"'})
            if not name or not str(name).strip(): return jsonify({'success': False, 'message': f'Row {i} (Roll {roll}): Missing mandatory field "Student Name"'})
            if not dept or not str(dept).strip(): return jsonify({'success': False, 'message': f'Row {i} (Roll {roll}): Missing mandatory field "Department"'})
            if not year or not str(year).strip(): return jsonify({'success': False, 'message': f'Row {i} (Roll {roll}): Missing mandatory field "Year"'})
            if not dob or not str(dob).strip(): return jsonify({'success': False, 'message': f'Row {i} (Roll {roll}): Missing mandatory field "DOB"'})
        
        for row in reader:
            roll = str(row.get('roll_number') or row.get('Roll Number') or row.get('reg') or row.get('Reg')).strip().upper()
            name = str(row.get('name') or row.get('Name') or row.get('student name') or row.get('Student Name')).strip()
            dept = str(row.get('department') or row.get('Department')).strip()
            year = str(row.get('year') or row.get('Year')).strip()
            dob = str(row.get('dob') or row.get('DOB') or row.get('Date of Birth')).strip()
            email = str(row.get('email') or row.get('Email') or '').strip()
            phone = str(row.get('phone') or row.get('Phone') or '').strip()
            
            existing = next((s for s in students if s['roll_number'].upper() == roll), None)
            if existing:
                existing['name'] = name
                existing['department'] = dept
                existing['year'] = year
                existing['dob'] = dob
                if email: existing['email'] = email
                if phone: existing['phone'] = phone
                updated_count += 1
            else:
                students.append({
                    'roll_number': roll,
                    'name': name,
                    'department': dept,
                    'year': year,
                    'dob': dob,
                    'email': email,
                    'phone': phone,
                    'photo': None,
                    'contributions': []
                })
                added_count += 1
                
        DB.save_students(students)
        return jsonify({'success': True, 'message': f'Imported! Added: {added_count}, Updated: {updated_count}'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@api.route('/students/promote', methods=['POST'])
def promote_students():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    promotion_rules = data.get('promotion_rules', {}) # e.g. {"1st": "2nd", "2nd": "3rd", "3rd": "4th", "4th": "Alumni"}
    detained_rolls = data.get('detained_rolls', [])
    delete_detained = data.get('delete_detained', False)

    students = DB.get_students()
    updated_count = 0
    deleted_count = 0

    new_students = []
    
    for s in students:
        roll = s.get('roll_number', '').upper()
        current_year = s.get('year', '')
        
        if roll in detained_rolls:
            if delete_detained:
                deleted_count += 1
                continue # Skip adding to new_students
            # Else, they are detained but not deleted, so year remains same
            new_students.append(s)
            continue
            
        if current_year in promotion_rules:
            s['year'] = promotion_rules[current_year]
            updated_count += 1
            
        new_students.append(s)

    DB.save_students(new_students)
    return jsonify({
        'success': True,
        'message': f'Promoted: {updated_count}, Deleted: {deleted_count}'
    })

@api.route('/contacts/update', methods=['POST'])
def update_contacts():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.json
    contacts = data.get('contacts', {})
    DB.save_contacts(contacts)
    return jsonify({'success': True})

@api.route('/admin/student-lookup', methods=['GET'])
def admin_student_lookup():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    roll = request.args.get('roll', '').strip().upper()
    if not roll:
        return jsonify({'success': False, 'message': 'Roll number required'}), 400
    
    student = DB.get_student_by_roll(roll)
    if not student:
        return jsonify({'success': False, 'message': 'Student not found'}), 404
    
    # Find all clubs this student is part of
    clubs = DB.get_clubs()
    member_of = []
    for c in clubs:
        bearers = c.get('office_bearers', [])
        for b in bearers:
            if b.get('roll_number', '').upper() == roll:
                member_of.append({
                    'id': c.get('id'),
                    'name': c.get('name'),
                    'role': b.get('role')
                })
                break
    
    return jsonify({
        'success': True,
        'student': {
            'name': student.get('name'),
            'phone': student.get('phone'),
            'year': student.get('year'),
            'department': student.get('department'),
            'roll_number': student.get('roll_number')
        },
        'member_of': member_of,
        'club_count': len(member_of)
    })

@api.route('/clubs/update', methods=['POST'])
def update_club():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.form.to_dict()
    club_id = data.get('id')
    club = DB.get_club_by_id(club_id)
    if not club: return jsonify({'success': False, 'message': 'Club not found'}), 404
    
    # Authorization check
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    # Update fields
    if 'about' in data: club['about'] = data['about']
    if 'mission' in data: club['mission'] = data['mission']
    if 'vision' in data: club['vision'] = data['vision']

    # Initialize missing fields
    if 'logo' not in club: club['logo'] = ''
    if 'cover_image' not in club: club['cover_image'] = ''
    if 'gallery' not in club: club['gallery'] = []
    if 'mentors' not in club: club['mentors'] = []
    if 'office_bearers' not in club: club['office_bearers'] = []

    # Handle logo upload
    logo = request.files.get('logo')
    if logo and logo.filename:
        if allowed_file(logo.filename):
            upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'details')
            os.makedirs(upload_dir, exist_ok=True)
            fn = f"logo_{uuid.uuid4().hex[:8]}_{secure_filename(logo.filename)}"
            logo.save(os.path.join(upload_dir, fn))
            club['logo'] = fn

    # Handle cover image upload
    cover = request.files.get('cover_image')
    if cover and cover.filename:
        if allowed_file(cover.filename):
            upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'details')
            os.makedirs(upload_dir, exist_ok=True)
            fn = f"cover_{uuid.uuid4().hex[:8]}_{secure_filename(cover.filename)}"
            cover.save(os.path.join(upload_dir, fn))
            club['cover_image'] = fn

    # Handle gallery image removal
    remove_img = data.get('remove_gallery_image')
    if remove_img and 'gallery' in club:
        club['gallery'] = [img for img in club['gallery'] if img != remove_img]
        
    # Handle new gallery images
    new_images = request.files.getlist('gallery')
    if new_images:
        upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'gallery')
        os.makedirs(upload_dir, exist_ok=True)
        for img in new_images:
            if img and img.filename and allowed_file(img.filename):
                fn = f"gallery_{uuid.uuid4().hex[:8]}_{secure_filename(img.filename)}"
                img.save(os.path.join(upload_dir, fn))
                club['gallery'].append(fn)

    # Handle Mentors - Only if mentor_names is in request.form
    if 'mentor_names' in request.form:
        mentor_names = request.form.getlist('mentor_names')
        mentor_roles = request.form.getlist('mentor_roles')
        mentor_positions = request.form.getlist('mentor_positions')
        existing_mentor_photos = request.form.getlist('existing_mentor_photos')
        existing_mentor_sigs = request.form.getlist('existing_mentor_sigs')
        
        mentors = []
        upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'mentors')
        os.makedirs(upload_dir, exist_ok=True)
        
        main_mentor_count = 0
        for i in range(len(mentor_names)):
            name = mentor_names[i]
            if not name.strip(): continue # Skip empty rows
            
            current_photo = existing_mentor_photos[i] if i < len(existing_mentor_photos) else None
            current_sig = existing_mentor_sigs[i] if i < len(existing_mentor_sigs) else None
            pos = mentor_positions[i] if i < len(mentor_positions) else 'Co-Mentor'
            
            if pos == 'Mentor':
                main_mentor_count += 1
                if main_mentor_count > 1:
                    return jsonify({'success': False, 'message': 'Only one main Mentor allowed per club'}), 400
            else:
                current_sig = None

            # Handle photo upload
            photo_file = request.files.get(f'mentor_photo_{i}')
            if photo_file and photo_file.filename and allowed_file(photo_file.filename):
                fn = f"mentor_{uuid.uuid4().hex[:8]}_{secure_filename(photo_file.filename)}"
                photo_file.save(os.path.join(upload_dir, fn))
                current_photo = fn
            
            # Handle signature file
            if pos == 'Mentor':
                sig_file = request.files.get(f'mentor_sig_file_{i}')
                if sig_file and sig_file.filename and allowed_file(sig_file.filename):
                    sfn = f"mentor_sig_{uuid.uuid4().hex[:8]}_{secure_filename(sig_file.filename)}"
                    sig_file.save(os.path.join(upload_dir, sfn))
                    current_sig = f"/static/uploads/clubs/{club_id}/mentors/{sfn}"

            mentors.append({
                'name': name,
                'designation': mentor_roles[i] if i < len(mentor_roles) else '',
                'position': pos,
                'photo': current_photo,
                'signature': current_sig
            })
        
        club['mentors'] = mentors
        # Backward compatibility: sync singular mentor field
        if mentors:
            main = next((m for m in mentors if m['position'] == 'Mentor'), mentors[0])
            club['mentor'] = {'name': main['name'], 'designation': main['designation']}
        else:
            club['mentor'] = {'name': '', 'designation': ''}

    # Handle office bearers - Only if bearer_rolls is in request.form
    if 'bearer_rolls' in request.form:
        bearer_rolls = request.form.getlist('bearer_rolls')
        bearer_names = request.form.getlist('bearer_names')
        bearer_roles = request.form.getlist('bearer_roles')
        bearer_phones = request.form.getlist('bearer_phones')
        bearer_years = request.form.getlist('bearer_years')
        bearer_depts = request.form.getlist('bearer_depts')
        bearer_tenures = request.form.getlist('bearer_tenure')
        existing_bearer_photos = request.form.getlist('existing_bearer_photos')
        
        bearers = []
        # Validate membership limit (max 2 clubs)
        all_clubs = DB.get_clubs()
        for roll in bearer_rolls:
            if not roll.strip(): continue
            count = 0
            other_clubs = []
            for c in all_clubs:
                if c['id'] == club_id: continue # Skip current club
                if any(b.get('roll_number', '').upper() == roll.strip().upper() for b in c.get('office_bearers', [])):
                    count += 1
                    other_clubs.append(c['name'])
            
            if count >= 2:
                return jsonify({
                    'success': False, 
                    'message': f"Student {roll} is already a member of 2 clubs ({', '.join(other_clubs)}). Maximum 2 clubs allowed."
                }), 400

        upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'office_bearers')
        os.makedirs(upload_dir, exist_ok=True)
        
        all_events = DB.get_events()
        club_events_count = len([e for e in all_events if e.get('club_id') == club_id])
        
        students = DB.load_json('students.json')
        active_rolls = [r.strip().lower() for r in bearer_rolls if r.strip()]
        
        # Mark existing contributions
        for s in students:
            if 'contributions' in s:
                for c_item in s['contributions']:
                    if c_item.get('club_id') == club_id:
                        s_roll = s.get('roll_number', '').strip().lower()
                        if s_roll and s_roll in active_rolls:
                            c_item['status'] = 'Present'
                        else:
                            c_item['status'] = 'Former'

        for i in range(len(bearer_rolls)):
            if not bearer_rolls[i].strip(): continue
            
            current_photo = existing_bearer_photos[i] if i < len(existing_bearer_photos) else None
            photo_file = request.files.get(f'bearer_photo_{i}')
            if photo_file and photo_file.filename and allowed_file(photo_file.filename):
                fn = f"bearer_{uuid.uuid4().hex[:8]}_{secure_filename(photo_file.filename)}"
                photo_file.save(os.path.join(upload_dir, fn))
                current_photo = fn

            b_roll = bearer_rolls[i].strip()
            b_name = bearer_names[i] if i < len(bearer_names) else 'Unknown'
            b_role = bearer_roles[i] if i < len(bearer_roles) else 'Member'
            b_tenure = bearer_tenures[i] if i < len(bearer_tenures) else ''

            bearers.append({
                'roll_number': b_roll,
                'name': b_name,
                'role': b_role,
                'phone': bearer_phones[i] if i < len(bearer_phones) else '',
                'year': bearer_years[i] if i < len(bearer_years) else '',
                'department': bearer_depts[i] if i < len(bearer_depts) else '',
                'photo': current_photo,
                'tenure_year': b_tenure,
                'events_organized': club_events_count
            })
            
            if b_roll:
                for s in students:
                    if s.get('roll_number', '').lower() == b_roll.lower():
                        if 'contributions' not in s: s['contributions'] = []
                        existing = next((cx for cx in s['contributions'] if cx.get('club_id') == club_id), None)
                        if existing:
                            existing['position'] = b_role
                            existing['status'] = 'Present'
                            existing['tenure_year'] = b_tenure
                            existing['events_organized'] = club_events_count
                        else:
                            s['contributions'].append({
                                'club_id': club_id,
                                'club_name': club.get('name'),
                                'position': b_role,
                                'status': 'Present',
                                'tenure_year': b_tenure,
                                'events_organized': club_events_count
                            })
                        break
        DB.save_json('students.json', students)
        club['office_bearers'] = bearers

    DB.save_club(club)
    return jsonify({'success': True})

@api.route('/clubs/create', methods=['POST'])
def create_club():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    name = data.get('name')
    features = data.get('features', {})
    admin_data = data.get('admin', {})
    
    if not name or not admin_data.get('email'):
        return jsonify({'success': False, 'message': 'Name and Admin Email are required'}), 400
        
    from app.models import slugify
    club_id = slugify(name)
    
    # Check if club already exists
    if DB.get_club_by_id(club_id):
        club_id = f"{club_id}_{uuid.uuid4().hex[:4]}"
        
    new_club = {
        'id': club_id,
        'name': name,
        'features': features,
        'admin_roll': admin_data.get('email'), # Use email as identifier in about.json
        'about': '',
        'mission': '',
        'vision': '',
        'mentor': {'name': '', 'designation': ''},
        'office_bearers': [],
        'gallery': []
    }
    
    DB.save_club(new_club)
    
    # Create Admin
    admin_user = {
        'name': admin_data.get('name'),
        'email': admin_data.get('email'),
        'password': admin_data.get('password'),
        'phone': admin_data.get('phone'),
        'role': 'club_admin' # General role, specific access checked by email/roll
    }
    DB.save_admin(admin_user)
    
    return jsonify({'success': True, 'club_id': club_id})

@api.route('/clubs/update_config', methods=['POST'])
def update_club_config():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        
    data = request.json
    club_id = data.get('id')
    features = data.get('features', {})
    admin_data = data.get('admin', {})
    
    club = DB.get_club_by_id(club_id)
    if not club:
        return jsonify({'success': False, 'message': 'Club not found'}), 404
        
    # Update club info
    club['name'] = data.get('name') or club['name']
    club['features'] = features
    
    # If admin email changed, we need to handle that, but for now let's keep it simple
    old_admin_email = club.get('admin_roll')
    new_admin_email = admin_data.get('email')
    
    club['admin_roll'] = new_admin_email
    DB.save_club(club)
    
    # Update Admin
    admins = DB.get_admins()
    admin_user = next((a for a in admins if a.get('email') == old_admin_email), None)
    
    if admin_user:
        admin_user['name'] = admin_data.get('name') or admin_user['name']
        admin_user['email'] = new_admin_email
        if admin_data.get('password'):
            admin_user['password'] = admin_data.get('password')
        admin_user['phone'] = admin_data.get('phone') or admin_user.get('phone')
    else:
        # Create new if didn't exist
        admin_user = {
            'name': admin_data.get('name'),
            'email': new_admin_email,
            'password': admin_data.get('password'),
            'phone': admin_data.get('phone'),
            'role': 'club_admin'
        }
        admins.append(admin_user)
        
    DB.save_json('admins.json', admins)
    
    return jsonify({'success': True})

@api.route('/office_bearers/request', methods=['POST'])
def request_office_bearer():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    print('DEBUG FILES:', request.files)
    data = request.form.to_dict()
    req = {
        'id': str(uuid.uuid4()),
        'club_id': data.get('club_id'),
        'name': data.get('name'),
        'role': data.get('role'),
        'status': 'pending',
        'timestamp': datetime.datetime.now().isoformat()
    }
    DB.save_office_bearer_request(req)
    return jsonify({'success': True})

@api.route('/events/approve_report/<club_id>/<event_id>', methods=['POST'])
def approve_report(club_id, event_id):
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['report_approved'] = True
    event['event_finished'] = True
    event['event_status'] = 'approved'
    event['approval_status'] = 'approved'
    DB.save_event(club_id, event)

    # ── Notify club admin via email ──────────────────────────────────────────
    try:
        club_obj = DB.get_club_by_id(club_id)
        admin_email = club_obj.get('admin_roll') if club_obj else None
        if admin_email:
            Mailer.send_report_approved_to_club(
                event=event,
                club=club_obj,
                club_admin_email=admin_email,
            )
    except Exception as _ae:
        print(f"[Report approved email] Error: {_ae}")

    return jsonify({'success': True})

@api.route('/admin/approve_finance_unlock', methods=['POST'])
def approve_finance_unlock():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['finance_locked'] = False
    event['finance_unlock_requested'] = False
    event['finance_unlock_approved'] = True
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/save_finance', methods=['POST'])
def save_finance():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['extra_income'] = data.get('extra_income', 0)
    event['extra_expense'] = data.get('extra_expense', 0)
    event['offline_cash'] = data.get('offline_cash', 0)
    event['actual_expenses'] = data.get('actual_expenses', 0)
    event['finance_locked'] = True
    event['finance_unlock_approved'] = False
    
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/update_finance', methods=['POST'])
def update_finance():
    """Club admin finance update — no locking, editable anytime."""
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data     = request.json
    club_id  = data.get('club_id')
    event_id = data.get('event_id')

    events = DB.get_events(club_id)
    event  = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404

    try:
        event['extra_income']    = int(float(data.get('extra_income', 0)))
        event['extra_expense']   = int(float(data.get('extra_expense', 0)))
        event['offline_cash']    = int(float(data.get('offline_cash', 0)))
        event['actual_expenses'] = int(float(data.get('actual_expenses', 0)))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Invalid numeric values'}), 400

    # Recompute helper fields used by Finance Hub table
    auto_revenue = int(event.get('revenue', 0))
    event['computed_revenue'] = auto_revenue + event['extra_income'] + event['offline_cash']
    event['computed_spend']   = event['actual_expenses'] + event['extra_expense']

    DB.save_event(club_id, event)
    return jsonify({
        'success': True,
        'computed_revenue': event['computed_revenue'],
        'computed_spend':   event['computed_spend']
    })

@api.route('/events/request_finance_unlock', methods=['POST'])
def request_finance_unlock():
    user = session.get('user')
    if not user: return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['finance_unlock_requested'] = True
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/approve/<club_id>/<event_id>', methods=['POST'])
def approve_event_structure(club_id, event_id):
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['approved'] = True
    event['event_status'] = 'approved'
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/reject/<club_id>/<event_id>', methods=['POST'])
def reject_event_structure(club_id, event_id):
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['approved'] = False
    event['event_status'] = 'pending'
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/approve_deletion/<club_id>/<event_id>', methods=['POST'])
def approve_event_deletion(club_id, event_id):
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    # Simple soft delete or status change
    event['event_status'] = 'deleted'
    event['deletion_approved'] = True
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/events/reject_deletion/<club_id>/<event_id>', methods=['POST'])
def reject_event_deletion(club_id, event_id):
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event: return jsonify({'success': False, 'message': 'Event not found'}), 404
    
    event['deletion_requested'] = False
    DB.save_event(club_id, event)
    return jsonify({'success': True})

@api.route('/office_bearers/action', methods=['POST'])
def action_bearer_request():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json
    req_id = data.get('id')
    action = data.get('action') # 'approve' or 'reject'
    
    requests = DB.load_json('office_bearer_requests.json')
    req = next((r for r in requests if r['id'] == req_id), None)
    if not req: return jsonify({'success': False, 'message': 'Request not found'}), 404
    
    if action == 'approve':
        req['status'] = 'approved'
        # Also add to the club's bearers list
        club = DB.get_club_by_id(req['club_id'])
        if club:
            if 'office_bearers' not in club: club['office_bearers'] = []
            club['office_bearers'].append({
                'name': req['name'],
                'role': req['role'],
                'phone': '',
                'photo': None
            })
            DB.save_club(club)
    else:
        req['status'] = 'rejected'
        
    DB.save_json('office_bearer_requests.json', requests)
    return jsonify({'success': True})

@api.route('/clubs/<club_id>/download_annual_zip/<year>', methods=['GET'])
def download_annual_zip(club_id, year):
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return "Unauthorized", 403
    
    events = DB.get_events(club_id)
    yr_events = [e for e in events if str(e.get('year')) == str(year)]
    
    if not yr_events:
        return "No data found for this year", 404
        
    memory_file = io.BytesIO()
    from app.models import slugify
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for ev in yr_events:
            ev_slug = slugify(ev['title'])
            
            # Report
            if ev.get('report'):
                report_path = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', ev_slug, 'reports', ev['report'])
                if os.path.exists(report_path):
                    zf.write(report_path, arcname=f"{year}/{ev_slug}/Report_{ev['report']}")
            
            # Poster
            if ev.get('poster'):
                poster_path = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', ev_slug, 'posters', ev['poster'])
                if os.path.exists(poster_path):
                    zf.write(poster_path, arcname=f"{year}/{ev_slug}/Poster_{ev['poster']}")
                    
    memory_file.seek(0)
    club = DB.get_club_by_id(club_id)
    club_name = club.get('name', club_id) if club else club_id
    # Clean club name for filename
    safe_name = "".join([c if c.isalnum() else "_" for c in club_name])
    
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{safe_name}_Annual_Reports_{year}.zip"
    )

@api.route('/settings')
def get_global_settings():
    # Load from em/settings.json as it's the central place for EM settings
    settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            try: settings = json.load(f)
            except: pass
    return jsonify({'success': True, 'settings': settings})

@api.route('/settings/update', methods=['POST'])
def update_global_settings():
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.json or {}
    settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            try: settings = json.load(f)
            except: pass
            
    settings.update(data)
    
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=4)
        
    return jsonify({'success': True})


# ── SMTP Management APIs ───────────────────────────────────────────────────────

@api.route('/smtp/update', methods=['POST'])
def update_smtp_settings():
    """Update global SMTP settings (chief coordinator) or per-club SMTP (club admin)."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.json or {}
    scope = data.get('scope', 'global')  # 'global' or 'club'
    club_id = data.get('club_id')

    smtp_email    = data.get('smtp_email', '').strip()
    smtp_password = data.get('smtp_password', '').strip()
    smtp_server   = data.get('smtp_server', 'smtp.gmail.com').strip()
    smtp_port     = int(data.get('smtp_port', 587))

    if scope == 'club':
        # Club admin updating their own SMTP
        if not club_id:
            return jsonify({'success': False, 'message': 'club_id required'}), 400
        club = DB.get_club_by_id(club_id)
        if not club:
            return jsonify({'success': False, 'message': 'Club not found'}), 404
        identifier = user.get('email') or user.get('roll_number')
        if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        club['smtp_config'] = {
            'server': smtp_server, 'port': smtp_port,
            'user': smtp_email,   'password': smtp_password,
        }
        DB.save_club(club)
    else:
        # Chief coordinator updating global SMTP
        if user.get('role') != 'chief_coordinator':
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        settings = {}
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                try: settings = json.load(f)
                except: pass
        settings['smtp_server']   = smtp_server
        settings['smtp_port']     = smtp_port
        settings['smtp_user']     = smtp_email
        settings['smtp_email']    = smtp_email
        settings['smtp_password'] = smtp_password
        with open(settings_path, 'w') as f:
            json.dump(settings, f, indent=4)

    return jsonify({'success': True, 'message': 'SMTP settings saved.'})


@api.route('/smtp/test', methods=['POST'])
def test_smtp():
    """Test the SMTP connection and optionally send a test email."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.json or {}
    club_id  = data.get('club_id')
    to_email = data.get('to_email') or (user.get('email') or '')
    ok, msg = Mailer.test_smtp(club_id=club_id, to_email=to_email)
    return jsonify({'success': ok, 'message': msg})


@api.route('/smtp/get', methods=['GET'])
def get_smtp_settings_api():
    """Return current SMTP config (masked password)."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    club_id = request.args.get('club_id')
    scope   = request.args.get('scope', 'global')

    if scope == 'club' and club_id:
        club = DB.get_club_by_id(club_id)
        cfg  = club.get('smtp_config', {}) if club else {}
        return jsonify({
            'success': True,
            'smtp_server':   cfg.get('server', 'smtp.gmail.com'),
            'smtp_port':     cfg.get('port', 587),
            'smtp_email':    cfg.get('user', ''),
            'has_password':  bool(cfg.get('password')),
        })
    else:
        settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
        settings = {}
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                try: settings = json.load(f)
                except: pass
        return jsonify({
            'success': True,
            'smtp_server':  settings.get('smtp_server', 'smtp.gmail.com'),
            'smtp_port':    settings.get('smtp_port', 587),
            'smtp_email':   settings.get('smtp_email', settings.get('smtp_user', '')),
            'has_password': bool(settings.get('smtp_password')),
            'chief_coordinator_email': settings.get('chief_coordinator_email', ''),
        })


@api.route('/smtp/bulk-update-clubs', methods=['POST'])
def bulk_update_clubs_smtp():
    """Chief coordinator pushes one SMTP config to ALL clubs."""
    user = session.get('user')
    if not user or user.get('role') != 'chief_coordinator':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    data = request.json or {}
    smtp_email    = data.get('smtp_email', '').strip()
    smtp_password = data.get('smtp_password', '').strip()
    smtp_server   = data.get('smtp_server', 'smtp.gmail.com').strip()
    smtp_port     = int(data.get('smtp_port', 587))
    if not smtp_email or not smtp_password:
        return jsonify({'success': False, 'message': 'Email and password required'}), 400
    clubs = DB.get_clubs()
    for club in clubs:
        club['smtp_config'] = {
            'server': smtp_server, 'port': smtp_port,
            'user': smtp_email,   'password': smtp_password,
        }
        DB.save_club(club)
    
    return jsonify({'success': True, 'message': f'SMTP pushed to {len(clubs)} clubs.'})

# ── RAZORPAY INTEGRATION (CENTRALIZED) ────────────────────────────────────────

@api.route('/payment/create-order', methods=['POST'])
def api_create_order():
    data = request.json or {}
    club_id = data.get('club_id')
    event_id = data.get('event_id')
    
    if not club_id or not event_id:
        return jsonify({'success': False, 'message': 'Missing club or event ID'}), 400
        
    event = next((e for e in DB.get_events(club_id) if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404
        
    try:
        amount = int(float(event.get('fee', 0)) * 100)
    except:
        return jsonify({'success': False, 'message': 'Invalid event fee'}), 400
        
    if amount <= 0:
        return jsonify({'success': False, 'message': 'Free events do not require payment order'}), 400

    # Load centralized credentials
    settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            try: settings = json.load(f)
            except: pass
            
    key_id = settings.get('razorpay_key_id')
    key_secret = settings.get('razorpay_key_secret')
    
    if not key_id or not key_secret:
        return jsonify({'success': False, 'message': 'Razorpay is not configured by the institution.'}), 500
        
    client = razorpay.Client(auth=(key_id, key_secret))
    
    order_data = {
        'amount': amount,
        'currency': 'INR',
        'payment_capture': 1,
        'notes': {
            'club_id': club_id,
            'event_id': event_id,
            'event_title': event.get('title', 'Event')
        }
    }
    
    try:
        order = client.order.create(data=order_data)
        return jsonify({
            'success': True,
            'order_id': order['id'],
            'amount': amount,
            'key_id': key_id
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api.route('/register', methods=['POST'])
def api_register():
    try:
        user = session.get('user')
        if not user:
            return jsonify({'success': False, 'message': 'Authentication required.'}), 401
            
        # This route handles both free and paid (verified) registrations
        data = request.json or {}
        club_id = data.get('club_id')
        event_id = data.get('event_id')
        roll_number = data.get('roll_number')
        
        # SECURITY: Verify that the roll number in the request matches the logged-in student
        if user.get('role') == 'student' and user.get('roll_number') != roll_number:
            return jsonify({'success': False, 'message': 'You can only register yourself.'}), 403
            
        if not club_id:
            return jsonify({'success': False, 'message': 'Registration failed: Club ID is missing.'}), 400
        if not event_id:
            return jsonify({'success': False, 'message': 'Registration failed: Event ID is missing.'}), 400
            
        event = DB.get_event_by_id(club_id, event_id)
        if not event:
            return jsonify({'success': False, 'message': 'Registration failed: Event not found.'}), 404

        # Check if already registered
        regs = DB.get_registrations(club_id)
        if any(r['event_id'] == event_id and r.get('roll_number') == roll_number for r in regs):
            return jsonify({'success': False, 'message': 'You are already registered for this event.'}), 400

        # Payment Verification logic
        is_paid = event.get('payment_type') == 'paid'
        reg_type = data.get('reg_type', 'individual')
        team_role = data.get('team_role')
        team_id = data.get('team_id')
        
        payment_details = data.get('payment_details')
        
        # Team Member check: if they are joining an existing team, we must verify the team exists
        if reg_type == 'team' and team_role == 'member':
            if not team_id:
                return jsonify({'success': False, 'message': 'Team ID is required to join a team.'}), 400
            
            # Verify team exists for this event
            team_leader = next((r for r in regs if r['event_id'] == event_id and r.get('team_id') == team_id and r.get('team_role') == 'leader'), None)
            if not team_leader:
                return jsonify({'success': False, 'message': 'The specified team does not exist for this event.'}), 404
            
            # If team leader has paid, members don't pay (Institutional logic)
            is_paid = False 
        
        if is_paid:
            if not payment_details:
                return jsonify({'success': False, 'message': 'Payment details are required for this paid event.'}), 400
                
            # Verify Razorpay signature
            settings_path = os.path.join(DATA_DIR, 'em', 'settings.json')
            settings = {}
            if os.path.exists(settings_path):
                with open(settings_path) as f:
                    try: settings = json.load(f)
                    except: pass
            
            key_id = settings.get('razorpay_key_id')
            key_secret = settings.get('razorpay_key_secret')
            if not key_id or not key_secret:
                return jsonify({'success': False, 'message': 'Institutional Razorpay is not configured.'}), 500
                
            params_dict = {
                'razorpay_order_id': payment_details.get('razorpay_order_id'),
                'razorpay_payment_id': payment_details.get('razorpay_payment_id'),
                'razorpay_signature': payment_details.get('razorpay_signature')
            }
            
            if not all(params_dict.values()):
                return jsonify({'success': False, 'message': 'Incomplete payment confirmation received.'}), 400

            client = razorpay.Client(auth=(key_id, key_secret))
            try:
                client.utility.verify_payment_signature(params_dict)
            except Exception as sig_err:
                return jsonify({'success': False, 'message': 'Payment signature verification failed.'}), 400

        # Create registration
        reg_id = str(uuid.uuid4())
        reg = {
            'id': reg_id,
            'event_id': event_id,
            'event_title': event.get('title'),
            'club_id': club_id,
            'name': data.get('name'),
            'email': data.get('email'),
            'phone': data.get('phone'),
            'roll_number': roll_number,
            'department': data.get('branch') or data.get('department'),
            'year': data.get('year'),
            'reg_type': reg_type,
            'team_role': team_role,
            'team_name': data.get('team_name'),
            'team_id': team_id,
            'timestamp': datetime.datetime.now().isoformat(),
            'payment_verified': True if not is_paid or payment_details else False,
            'qr_code': reg_id
        }
        
        if payment_details:
            reg['payment_id'] = payment_details.get('razorpay_payment_id')
            reg['order_id'] = payment_details.get('razorpay_order_id')

        DB.save_registration(club_id, reg)
        
        # Send email
        try:
            send_registration_email(reg)
        except Exception as e:
            print(f"Email failure: {e}")
            pass # Don't fail the whole registration if email fails
            
        return jsonify({'success': True, 'reg_id': reg_id})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'Server Error: {str(e)}'}), 500


@api.route('/scan-qr', methods=['POST'])
def api_scan_qr():
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Authentication required.'}), 401
    
    data = request.json or {}
    qr_text = data.get('qr_code', '')
    
    # Parse RegID from multi-line format if needed
    reg_id = None
    if "RegID: " in qr_text:
        try:
            reg_id = qr_text.split("RegID: ")[1].strip().split('\n')[0]
        except:
            reg_id = None
    else:
        reg_id = qr_text.strip()
        
    if not reg_id:
        return jsonify({'success': False, 'message': 'Invalid QR code format.'}), 400
        
    # Find registration
    clubs = DB.get_clubs()
    found_reg = None
    found_club_id = None
    
    for club in clubs:
        regs = DB.get_registrations(club['id'])
        found_reg = next((r for r in regs if r['id'] == reg_id), None)
        if found_reg:
            found_club_id = club['id']
            break
            
    if not found_reg:
        return jsonify({'success': False, 'message': 'Registration not found.'}), 404
        
    if found_reg.get('attended'):
        return jsonify({
            'success': False, 
            'message': f"Attendance already marked for {found_reg.get('name')} at {found_reg.get('attended_at')}"
        }), 400
        
    # Mark as attended
    found_reg['attended'] = True
    found_reg['attended_at'] = datetime.datetime.now().isoformat()
    
    # Save back to DB
    event_id = found_reg.get('event_id')
    all_event_regs = [r for r in DB.get_registrations(found_club_id) if r['event_id'] == event_id]
    for i, r in enumerate(all_event_regs):
        if r['id'] == reg_id:
            all_event_regs[i] = found_reg
            break
            
    DB.update_registrations(found_club_id, all_event_regs)
    
    return jsonify({
        'success': True, 
        'message': f"Attendance marked for {found_reg.get('name')} ({found_reg.get('roll_number')})"
    })


@api.route('/student/update_profile', methods=['POST'])
def update_student_profile():
    user = session.get('user')
    if not user or user.get('role') != 'student':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    roll = user.get('roll_number')
    student = DB.get_student_by_roll(roll)
    if not student:
        return jsonify({'success': False, 'message': 'Student record not found'}), 404

    if request.content_type and 'multipart/form-data' in request.content_type:
        data = request.form.to_dict()
        if 'contributions' in data:
            import json
            try:
                data['contributions'] = json.loads(data['contributions'])
            except:
                pass
                
        photo = request.files.get('photo')
        if photo and photo.filename:
            upload_dir = os.path.join(current_app.static_folder, 'uploads', 'students', roll)
            os.makedirs(upload_dir, exist_ok=True)
            filename = f"avatar_{uuid.uuid4().hex[:8]}_{photo.filename}"
            photo.save(os.path.join(upload_dir, filename))
            student['photo'] = filename
    else:
        data = request.json or {}
        
    # Update allowed fields
    if 'name' in data: student['name'] = data['name']
    if 'email' in data: student['email'] = data['email']
    if 'phone' in data: student['phone'] = data['phone']
    if 'department' in data: student['department'] = data['department']
    if 'year' in data: student['year'] = data['year']
    if 'class' in data: student['class'] = data['class']
    
    # Achievements / Club Contributions
    if 'contributions' in data:
        student['contributions'] = data['contributions']
        
    DB.save_student(student)
    # Update session
    student['role'] = 'student'
    session['user'] = student
    
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────────────────────
# EVENT LIFECYCLE — Approval Workflow & Document Management
# ─────────────────────────────────────────────────────────────────────────────

APPROVAL_STAGES = [
    {'key': 'pending_principal',         'label': 'Principal',               'next': 'pending_chief_coordinator'},
    {'key': 'pending_chief_coordinator', 'label': 'Chief Coordinator',       'next': 'pending_ao'},
    {'key': 'pending_ao',                'label': 'AO',                      'next': 'pending_fm'},
    {'key': 'pending_fm',                'label': 'FM',                      'next': 'pending_secretary'},
    {'key': 'pending_secretary',         'label': 'Secretary',               'next': 'approved'},
]


def _get_stage_info(status_key):
    return next((s for s in APPROVAL_STAGES if s['key'] == status_key), None)


@api.route('/events/delete/<club_id>/<event_id>', methods=['POST'])
def api_delete_event(club_id, event_id):
    """Soft-delete: marks event as deletion_requested for super-admin approval."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    identifier = user.get('email') or user.get('roll_number')
    club = DB.get_club_by_id(club_id)
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    if user.get('role') == 'chief_coordinator':
        event['deleted'] = True
        event['event_status'] = 'deleted'
    else:
        event['deletion_requested'] = True

    DB.save_event(club_id, event)
    return jsonify({'success': True})


@api.route('/events/submit_for_approval', methods=['POST'])
def submit_event_for_approval():
    """Club Admin submits event into the approval pipeline."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.json or {}
    club_id = data.get('club_id')
    event_id = data.get('event_id')

    club = DB.get_club_by_id(club_id)
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    event['approval_status'] = 'pending_principal'
    event['event_status'] = 'pending_principal'
    event['approved'] = False
    
    # Final check on auto-signatures during submission
    _apply_auto_signatures(club, event, user)

    DB.save_event(club_id, event)
    return jsonify({'success': True, 'message': 'Event submitted for Principal approval.'})


@api.route('/events/approve_stage/<club_id>/<event_id>', methods=['POST'])
def approve_event_stage(club_id, event_id):
    """Advance the event through one approval stage; called by each approver."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    # Only authorized roles may call this
    APPROVAL_ROLES = ('principal', 'secretary', 'ao', 'fm', 'chief_coordinator')
    role = user.get('role')
    if role not in APPROVAL_ROLES and not role.endswith('_admin'):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.json or {}
    approver_name = data.get('approver_name', user.get('name', 'Unknown'))
    remarks = data.get('remarks', '')

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    current_status = event.get('approval_status', event.get('event_status', ''))
    
    # Generic role check: User must be the intended approver for this stage
    if current_status != 'approved' and not current_status.endswith(role):
        return jsonify({'success': False, 'message': f'Only the {role.upper()} can approve this stage.'}), 403

    stage = _get_stage_info(current_status)
    if not stage:
        return jsonify({'success': False, 'message': f'Event is not in a pending stage (current: {current_status})'}), 400

    # Record digital signature for this stage
    sig_entry = {
        'stage':          current_status,
        'role_label':     stage['label'],
        'approver_name':  approver_name,
        'approved_at':    datetime.datetime.now().isoformat(),
        'signature':      f"Digitally Signed by {approver_name} ({stage['label']})",
        'remarks':        remarks,
    }
    if 'approval_chain' not in event:
        event['approval_chain'] = []
    event['approval_chain'].append(sig_entry)

    # Also update the flatter approver_signatures for legacy template support
    if 'approver_signatures' not in event:
        event['approver_signatures'] = {}
    
    # Fetch signature from DB if not in session
    user_sig = user.get('signature')
    if not user_sig:
        db_user = DB.get_admin_by_email(user.get('email'))
        if db_user:
            user_sig = db_user.get('signature')

    # Extract the role key (e.g., 'principal' from 'pending_principal')
    role_key = current_status.replace('pending_', '')
    event['approver_signatures'][role_key] = {
        'name': approver_name,
        'timestamp': sig_entry['approved_at'],
        'signature': user_sig,
        'remarks': remarks
    }

    next_status = stage['next']
    event['approval_status'] = next_status
    event['event_status'] = next_status

    if next_status == 'approved':
        event['approved'] = True
        event['event_status'] = 'approved'
        event['approval_status'] = 'approved'
        event['fully_approved_at'] = datetime.datetime.now().isoformat()

    DB.save_event(club_id, event)

    return jsonify({
        'success': True,
        'next_status': next_status,
        'message': f'Approved by {stage["label"]}. ' + (
            'Event is now LIVE!' if next_status == 'approved'
            else f'Awaiting {_get_stage_info(next_status)["label"] if _get_stage_info(next_status) else "next approver"}.'
        )
    })


@api.route('/events/reject_stage/<club_id>/<event_id>', methods=['POST'])
def reject_event_stage(club_id, event_id):
    """Reject at any approval stage — sends event back to draft."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.json or {}
    reason = data.get('reason', 'No reason provided')
    approver_name = data.get('approver_name', user.get('name', 'Unknown'))

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    current_status = event.get('approval_status', '')
    stage = _get_stage_info(current_status)

    reject_entry = {
        'stage':         current_status,
        'role_label':    stage['label'] if stage else current_status,
        'approver_name': approver_name,
        'rejected_at':   datetime.datetime.now().isoformat(),
        'reason':        reason,
        'action':        'rejected',
    }
    if 'approval_chain' not in event:
        event['approval_chain'] = []
    event['approval_chain'].append(reject_entry)

    event['approval_status'] = 'rejected'
    event['event_status'] = 'rejected'
    event['approved'] = False
    event['rejection_reason'] = reason

    DB.save_event(club_id, event)
    return jsonify({'success': True, 'message': f'Event rejected by {stage["label"] if stage else "approver"}.'})


@api.route('/events/resubmit', methods=['POST'])
def resubmit_event():
    """Club Admin resubmits a rejected event back to Principal."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.json or {}
    club_id = data.get('club_id')
    event_id = data.get('event_id')

    club = DB.get_club_by_id(club_id)
    identifier = user.get('email') or user.get('roll_number')
    if user.get('role') != 'chief_coordinator' and club.get('admin_roll') != identifier:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    event['approval_status'] = 'pending_principal'
    event['event_status'] = 'pending_principal'
    event['approved'] = False
    event['rejection_reason'] = ''
    # Keep approval_chain history intact — append resubmission marker
    event.setdefault('approval_chain', []).append({
        'stage': 'resubmitted',
        'role_label': 'Club Admin',
        'approver_name': user.get('name', 'Club Admin'),
        'approved_at': datetime.datetime.now().isoformat(),
        'action': 'resubmitted',
    })

    DB.save_event(club_id, event)
    return jsonify({'success': True, 'message': 'Event resubmitted for approval.'})


@api.route('/events/approval_status/<club_id>/<event_id>', methods=['GET'])
def get_approval_status(club_id, event_id):
    """Return current approval chain and status for the event."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    return jsonify({
        'success': True,
        'approval_status': event.get('approval_status', event.get('event_status', 'draft')),
        'approval_chain': event.get('approval_chain', []),
        'approved': event.get('approved', False),
        'rejection_reason': event.get('rejection_reason', ''),
    })


@api.route('/events/pending_approvals', methods=['GET'])
def get_pending_approvals():
    """Return all events pending approval — filtered by stage if query param given."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    stage_filter = request.args.get('stage')  # e.g. 'pending_principal'
    all_events = DB.get_events()
    pending = []
    for e in all_events:
        status = e.get('approval_status', e.get('event_status', ''))
        if stage_filter:
            if status == stage_filter:
                pending.append(e)
        else:
            if status.startswith('pending_') and not e.get('deleted'):
                pending.append(e)

    return jsonify({'success': True, 'events': pending, 'count': len(pending)})


@api.route('/events/documents/<club_id>/<event_id>', methods=['GET'])
def list_event_documents(club_id, event_id):
    """List all documents in an event's document folder."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    from app.models import slugify
    from flask import current_app
    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    event_slug = slugify(event['title'])
    base = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', event_slug)
    docs = {'permission_letter': [], 'reports': [], 'posters': [], 'files': []}

    for folder in docs:
        folder_path = os.path.join(base, folder)
        if os.path.exists(folder_path):
            docs[folder] = sorted(os.listdir(folder_path))

    return jsonify({'success': True, 'documents': docs, 'event_slug': event_slug})


@api.route('/events/upload_document', methods=['POST'])
def upload_event_document():
    """Upload any document (poster, report, file) into the event's document folder."""
    user = session.get('user')
    if not user:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    club_id = request.form.get('club_id')
    event_id = request.form.get('event_id')
    doc_type = request.form.get('doc_type', 'files')  # permission_letter | reports | posters | files
    doc_file = request.files.get('file')

    if not doc_file or not doc_file.filename:
        return jsonify({'success': False, 'message': 'No file provided'}), 400

    events = DB.get_events(club_id)
    event = next((e for e in events if e['id'] == event_id), None)
    if not event:
        return jsonify({'success': False, 'message': 'Event not found'}), 404

    from app.models import slugify
    from flask import current_app
    event_slug = slugify(event['title'])
    upload_dir = os.path.join(current_app.static_folder, 'uploads', 'clubs', club_id, 'events', event_slug, doc_type)
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"{doc_type}_{uuid.uuid4().hex[:8]}_{secure_filename(doc_file.filename)}"
    doc_file.save(os.path.join(upload_dir, filename))

    # Track in event data
    if doc_type == 'reports':
        event['report'] = filename
        event['report_approved'] = False
    elif doc_type == 'posters':
        event['poster'] = filename

    DB.save_event(club_id, event)
    url = f"/static/uploads/clubs/{club_id}/events/{event_slug}/{doc_type}/{filename}"
    return jsonify({'success': True, 'filename': filename, 'url': url})

