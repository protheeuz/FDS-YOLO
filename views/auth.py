import cv2
import numpy as np
from flask import Blueprint, request, jsonify, redirect, session, url_for, render_template, send_file, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
import requests
from sklearn.metrics.pairwise import cosine_similarity
from database import get_db
from deepface import DeepFace
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import os
import json
import bcrypt
import qrcode
import io
from models import User
import random
import string
import logging

auth_bp = Blueprint('auth', __name__)


def generate_unique_code():
    return ''.join(random.choices(string.digits, k=4))

def generate_session_token():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

logging.basicConfig(level=logging.DEBUG)

def encode_face(face_encoding):
    return json.dumps(face_encoding)

def decode_face(stored_encoding):
    return np.array(json.loads(stored_encoding))

def calculate_cosine_similarity(embedding1, embedding2):
    embedding1 = np.array(embedding1).reshape(1, -1)
    embedding2 = np.array(embedding2).reshape(1, -1)
    return cosine_similarity(embedding1, embedding2)[0][0]

def generate_reset_token(user_id):
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return s.dumps(user_id, salt='password-reset-salt')

def verify_reset_token(token, expiration=3600):
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        user_id = s.loads(token, salt='password-reset-salt', max_age=expiration)
    except (SignatureExpired, BadSignature):
        return None
    return user_id

def send_reset_password_email(email, reset_link, name):
    html_content = render_template('email_templates/reset_password_email.html', reset_link=reset_link, name=name)
    message = Mail(
        from_email=current_app.config['SENDGRID_DEFAULT_FROM'],
        to_emails=email,
        subject='Reset Password Kamu',
        html_content=html_content
    )
    try:
        sg = SendGridAPIClient(current_app.config['SENDGRID_API_KEY'])
        response = sg.send(message)
        logging.info(f"Email sent to {email} with status code {response.status_code}")
    except Exception as e:
        logging.error(f"Error sending email: {e}")

############################################################
############### BATAS ROUTES AUTHENTICATION ################
############################################################

@auth_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        nik = request.form['nik']
        
        connection = get_db()
        cursor = connection.cursor()
        
        cursor.execute("SELECT id, email, name FROM users WHERE nik=%s", (nik,))
        user = cursor.fetchone()
        cursor.close()
        
        if user:
            user_id, email, name = user
            reset_token = generate_reset_token(user_id)
            reset_link = url_for('auth.reset_password', token=reset_token, _external=True)
            send_reset_password_email(email, reset_link, name)
            return render_template('auth/forgot_password.html', success='Link reset password telah dikirim ke email Anda.')
        else:
            return render_template('auth/forgot_password.html', error='NIK yang dimasukkan memang belum tersedia.')
    
    return render_template('auth/forgot_password.html')

@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    user_id = verify_reset_token(token)
    if not user_id:
        return render_template('auth/reset_password.html', error='Token reset password tidak valid atau telah kadaluarsa.')

    if request.method == 'POST':
        password = request.form['password']
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        connection = get_db()
        cursor = connection.cursor()
        cursor.execute("UPDATE users SET password=%s WHERE id=%s", (hashed_password, user_id))
        connection.commit()
        cursor.close()

        return redirect(url_for('auth.login'))
    
    return render_template('auth/reset_password.html', token=token)

@auth_bp.route('/check_existing_user', methods=['POST'])
def check_existing_user():
    nik = request.form['nik']
    email = request.form['email']
    
    connection = get_db()
    cursor = connection.cursor()
    
    cursor.execute("SELECT id FROM users WHERE nik=%s OR email=%s", (nik, email))
    existing_user = cursor.fetchone()
    cursor.close()

    if existing_user:
        return jsonify({"status": "gagal", "pesan": "NIK atau Email sudah terdaftar"}), 400
    
    return jsonify({"status": "sukses"})

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        nik = request.form['nik']
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        role = 'karyawan'  
        registration_date = datetime.now()
        unique_code = generate_unique_code()

        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        connection = get_db()
        cursor = connection.cursor()
        
        cursor.execute("SELECT id FROM users WHERE nik=%s OR email=%s", (nik, email))
        existing_user = cursor.fetchone()
        if existing_user:
            cursor.close()
            return jsonify({"status": "gagal", "pesan": "NIK atau Email sudah terdaftar"}), 400

        cursor.execute("INSERT INTO users (nik, name, email, password, registration_date, role, unique_code) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                       (nik, name, email, hashed_password, registration_date, role, unique_code))
        connection.commit()
        user_id = cursor.lastrowid
        cursor.close()

        return jsonify({"status": "sukses", "user_id": user_id})
    return render_template('auth/register.html')

@auth_bp.route('/register_face', methods=['POST'])
def register_face():
    nik = request.form['nik']
    name = request.form['name']
    email = request.form['email']
    password = request.form['password']
    role = 'karyawan'  
    registration_date = datetime.now()
    unique_code = generate_unique_code()
    
    face_image = request.files['face_image']
    npimg = np.frombuffer(face_image.read(), np.uint8)
    img = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

    try:
        result = DeepFace.represent(img, model_name='Facenet', enforce_detection=False)
        face_encoding = result[0]["embedding"]

        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        connection = get_db()
        cursor = connection.cursor()
        cursor.execute("INSERT INTO users (nik, name, email, password, registration_date, role, unique_code) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                       (nik, name, email, hashed_password, registration_date, role, unique_code))
        connection.commit()
        user_id = cursor.lastrowid
        cursor.execute("INSERT INTO faces (user_id, encoding) VALUES (%s, %s)", (user_id, encode_face(face_encoding)))
        connection.commit()
        cursor.close()
        return jsonify({"status": "sukses", "user_id": user_id})
    except Exception as e:
        logging.exception("Terjadi kesalahan saat memproses wajah")
        return jsonify({"status": "gagal", "pesan": "Wajah tidak ditemukan"}), 400

@auth_bp.route('/login', methods=['POST', 'GET'])
def login():
    if request.method == 'POST':
        nik = request.form['nik']
        password = request.form['password']
        esp32_ip = request.form.get('esp32_ip', '192.168.20.184')  # Dapatkan IP ESP32 dari form

        connection = get_db()
        cursor = connection.cursor()
        cursor.execute("SELECT id, password, role FROM users WHERE nik=%s", (nik,))
        user_data = cursor.fetchone()

        if user_data:
            stored_password = user_data[1]
            password_correct = bcrypt.checkpw(password.encode('utf-8'), stored_password.encode('utf-8'))
            current_app.logger.debug(f"Stored password: {stored_password}, Input password: {password}, Password correct: {password_correct}")

            if password_correct:
                user = User.get(user_data[0])
                login_user(user)
                session['user_id'] = user_data[0]
                session['session_token'] = generate_session_token()
                current_app.logger.debug(f'Session after login (username/password): {session.items()}')
                cursor.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user_data[0],))
                connection.commit()

                current_app.logger.debug(f"Attempting to send user_id {user_data[0]} to ESP32 at {esp32_ip}")
                if send_user_id_to_esp32(user_data[0], esp32_ip):
                    check_date = datetime.now().date()
                    cursor.execute("SELECT completed FROM health_checks WHERE user_id = %s AND check_date = %s", (user_data[0], check_date))
                    health_check = cursor.fetchone()
                    cursor.close()

                    if not health_check or not health_check[0]:
                        return jsonify({"status": "health_check_required", "user_id": user_data[0]})

                    return jsonify({"status": "sukses", "user_id": user_data[0]})
                else:
                    current_app.logger.error("Failed to send user_id to ESP32")
                    return jsonify({"status": "gagal", "message": "Failed to send user_id to ESP32"})

        cursor.close()
        return jsonify({"status": "gagal", "message": "NIK atau password salah"})

    return render_template('auth/login.html')

@auth_bp.route('/login_face', methods=['POST'])
def login_face():
    logging.debug("Memulai proses login wajah")
    
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        logging.error("Tidak dapat mengakses kamera")
        return jsonify({"status": "gagal", "pesan": "Tidak dapat mengakses kamera"}), 400

    ret, frame = cap.read()
    cap.release()

    if not ret:
        logging.error("Tidak dapat mengambil frame dari kamera")
        return jsonify({"status": "gagal", "pesan": "Tidak dapat mengambil frame dari kamera"}), 400

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.1, 4)

    if len(faces) == 0:
        logging.error("Tidak ada wajah yang terdeteksi")
        return jsonify({"status": "gagal", "pesan": "Tidak ada wajah yang terdeteksi"}), 400

    try:
        logging.debug("Mengambil representasi wajah")
        result = DeepFace.represent(frame, model_name='Facenet', enforce_detection=False)
        face_encoding = result[0]["embedding"]
        logging.debug(f"Representasi wajah berhasil diambil: {face_encoding}")

        user_id = recognize_face(face_encoding)
        if user_id:
            logging.debug(f"Wajah dikenali, user_id: {user_id}")
            connection = get_db()
            cursor = connection.cursor()
            cursor.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user_id,))
            connection.commit()

            user = User.get(user_id)
            login_user(user)
            
            session['user_id'] = user_id
            session['session_token'] = generate_session_token()
            current_app.logger.debug(f'Session after login (face): {session.items()}')

            current_app.logger.debug(f"Attempting to send user_id {user_id} to ESP32 at 192.168.20.184")
            if send_user_id_to_esp32(user_id, '192.168.20.184'):
                check_date = datetime.now().date()
                cursor.execute("SELECT completed FROM health_checks WHERE user_id = %s AND check_date = %s", (user_id, check_date))
                health_check = cursor.fetchone()
                cursor.close()

                if not health_check or not health_check[0]:
                    return jsonify({"status": "health_check_required", "user_id": user_id})

                return jsonify({"status": "sukses", "user_id": user_id})
            else:
                current_app.logger.error("Failed to send user_id to ESP32")
                return jsonify({"status": "gagal", "message": "Failed to send user_id to ESP32"})

        else:
            logging.error("Wajah tidak dikenali")
            return jsonify({"status": "gagal", "pesan": "Wajah tidak dikenali"}), 401
    except Exception as e:
        logging.exception("Terjadi kesalahan saat memproses wajah")
        return jsonify({"status": "gagal", "pesan": "Tidak ada wajah yang ditemukan"}), 400

@auth_bp.route('/login_qr', methods=['POST'])
def login_qr():
    data = request.get_json()
    qr_code = data.get('qr_code')
    user_code = data.get('user_code')
    
    logging.debug(f'Received qr_code: {qr_code}, user_code: {user_code}')
    
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute("SELECT id, unique_code FROM users WHERE nik=%s", (qr_code,))
    user = cursor.fetchone()
    
    if user:
        logging.debug(f'User found: {user}')
        if user_code == user[1]:
            user_id = user[0]
            cursor.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user_id,))
            connection.commit()

            user = User.get(user_id)
            login_user(user)
            
            session['user_id'] = user_id
            session['session_token'] = generate_session_token()
            current_app.logger.debug(f'Session after login (QR): {session.items()}')

            current_app.logger.debug(f"Attempting to send user_id {user_id} to ESP32 at 192.168.20.184")
            if send_user_id_to_esp32(user_id, '192.168.20.184'):
                check_date = datetime.now().date()
                cursor.execute("SELECT completed FROM health_checks WHERE user_id = %s AND check_date = %s", (user_id, check_date))
                health_check = cursor.fetchone()
                cursor.close()

                if not health_check or not health_check[0]:
                    return jsonify({"status": "health_check_required", "user_id": user_id})

                return jsonify({"status": "sukses", "user_id": user_id})
            else:
                current_app.logger.error("Failed to send user_id to ESP32")
                return jsonify({"status": "gagal", "message": "Failed to send user_id to ESP32"})
        else:
            logging.debug('Invalid unique code')
            cursor.close()
            return jsonify({"status": "gagal", "pesan": "Kode unik tidak valid"}), 401
    else:
        logging.debug('Invalid QR code')
        cursor.close()
        return jsonify({"status": "gagal", "pesan": "QR Code tidak valid"}), 401


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/generate_qr', methods=['GET'])
def generate_qr():
    nik = request.args.get('nik')
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute("SELECT unique_code FROM users WHERE nik=%s", (nik,))
    user = cursor.fetchone()
    cursor.close()

    if user:
        unique_code = user[0]
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(unique_code)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        buf = io.BytesIO()
        img.save(buf)
        buf.seek(0)

        return send_file(buf, mimetype='image/png')
    else:
        return jsonify({"status": "gagal", "pesan": "NIK tidak ditemukan"}), 404

@auth_bp.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    connection = get_db()
    cursor = connection.cursor()

    try:
        cursor.execute("DELETE FROM faces WHERE user_id=%s", (user_id,))
        cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
        connection.commit()
        cursor.close()
        
        return jsonify({"status": "sukses", "pesan": "User berhasil dihapus"})
    except Exception as e:
        connection.rollback()
        cursor.close()
        return jsonify({"status": "gagal", "pesan": str(e)}), 500 

def recognize_face(face_encoding):
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute("SELECT user_id, encoding FROM faces")
    rows = cursor.fetchall()
    cursor.close()

    for row in rows:
        user_id, stored_encoding = row
        stored_encoding = decode_face(stored_encoding)
        similarity = calculate_cosine_similarity(face_encoding, stored_encoding)
        if similarity > 0.9:  
            return user_id
    return None

def send_user_id_to_esp32(user_id, esp32_ip):
    try:
        payload = json.dumps({'user_id': user_id})
        headers = {'Content-Type': 'application/json'}
        current_app.logger.debug(f"Sending payload to ESP32: {payload}")
        current_app.logger.debug(f"Headers: {headers}")
        response = requests.post(f'http://{esp32_ip}/set_user_id', data=payload, headers=headers)
        current_app.logger.debug(f"Response from ESP32: {response.status_code}, {response.text}")
        if response.status_code == 200:
            response_json = response.json()
            if response_json.get("status") == "sukses":
                return True
            else:
                current_app.logger.error(f"Failed to set user_id on ESP32: {response_json}")
                return False
        else:
            current_app.logger.error(f"Gagal mengirimkan data user ke Mikrokontroller: {response.status_code}")
            return False
    except Exception as e:
        current_app.logger.error(f"Error sending user_id to ESP32: {e}")
        return False
    
@auth_bp.route('/send_session_token', methods=['POST'])
@login_required
def send_session_token():
    data = request.get_json()
    esp32_ip = data.get('esp32_ip')
    session_token = session.get('session_token')
    current_app.logger.debug(f'Sending session_token {session_token} to ESP32 at {esp32_ip}')

    response = requests.post(f'http://{esp32_ip}/set_session_token', json={'session_token': session_token})

    if response.status_code == 200:
        return jsonify({"status": "sukses"})
    else:
        return jsonify({"status": "gagal"}), 500
