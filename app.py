#!/usr/bin/env python3
"""
OTP FVX – Premium OTP Sender with Daily Limits
Uses MongoDB as the database.
"""

import os
import hashlib
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, flash
import requests
from dotenv import load_dotenv
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId

load_dotenv()

# ---------- Configuration ----------
class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY not set")

    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError("ADMIN credentials missing")

    MONGODB_URI = os.environ.get('MONGODB_URI')
    MONGODB_DB = os.environ.get('MONGODB_DB', 'otp_fvx')
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI required")

    API_KEYS = [k.strip() for k in os.environ.get('API_KEYS', '').split(',') if k.strip()]
    API_URL = os.environ.get('API_URL')
    DAILY_LIMIT = int(os.environ.get('DAILY_LIMIT', 15))
    RESET_HOUR = 4  # 4:00 AM
    
    # Telegram Support URL
    TELEGRAM_URL = os.environ.get('TELEGRAM_URL', 'https://t.me/vaibhavff570')

app = Flask(__name__)
app.config.from_object(Config)

# ---------- MongoDB Client ----------
mongo_client = MongoClient(app.config['MONGODB_URI'])
db = mongo_client[app.config['MONGODB_DB']]

# Collections
users_col = db['users']
requests_col = db['requests']
rate_limits_col = db['rate_limits']

print("✅ MongoDB connected successfully")

# ---------- Database Helpers ----------

def get_user_by_id(user_id):
    try:
        if isinstance(user_id, str) and ObjectId.is_valid(user_id):
            result = users_col.find_one({'_id': ObjectId(user_id)})
            if result:
                result['id'] = str(result['_id'])
            return result
        else:
            result = users_col.find_one({'id': int(user_id) if isinstance(user_id, str) else user_id})
            if result:
                result['id'] = str(result['_id'])
            return result
    except Exception:
        return None

def get_user_by_username(username):
    result = users_col.find_one({'username': username})
    if result:
        result['id'] = str(result['_id'])
    return result

def get_user_by_email(email):
    result = users_col.find_one({'email': email})
    if result:
        result['id'] = str(result['_id'])
    return result

def create_user(username, email, password_hash):
    last_user = users_col.find_one({}, sort=[('id', -1)])
    next_id = (last_user.get('id', 0) + 1) if last_user else 1
    
    user_data = {
        'id': next_id,
        'username': username,
        'email': email,
        'password_hash': password_hash,
        'daily_limit': app.config['DAILY_LIMIT'],
        'used_today': 0,
        'last_reset': datetime.now(timezone.utc).isoformat(),
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    result = users_col.insert_one(user_data)
    return str(result.inserted_id)

def get_user_daily_usage(user_id):
    """Get user's daily usage and reset if needed"""
    user = get_user_by_id(user_id)
    if not user:
        return None, None
    
    now = datetime.now(timezone.utc)
    reset_time = datetime(
        now.year, now.month, now.day,
        app.config['RESET_HOUR'], 0, 0, 0,
        tzinfo=timezone.utc
    )
    
    # If current time is before reset hour, use yesterday's reset time
    if now < reset_time:
        reset_time = reset_time - timedelta(days=1)
    
    last_reset = user.get('last_reset')
    if last_reset:
        last_reset_dt = datetime.fromisoformat(last_reset.replace('Z', '+00:00'))
        if last_reset_dt < reset_time:
            # Reset daily usage
            users_col.update_one(
                {'_id': ObjectId(user['_id']) if ObjectId.is_valid(user['id']) else {'id': user['id']}},
                {'$set': {'used_today': 0, 'last_reset': reset_time.isoformat()}}
            )
            user['used_today'] = 0
            user['last_reset'] = reset_time.isoformat()
    
    daily_limit = user.get('daily_limit', app.config['DAILY_LIMIT'])
    used_today = user.get('used_today', 0)
    remaining = daily_limit - used_today
    
    return used_today, remaining, daily_limit

def increment_user_usage(user_id):
    """Increment user's daily usage count"""
    try:
        if isinstance(user_id, str) and ObjectId.is_valid(user_id):
            result = users_col.update_one(
                {'_id': ObjectId(user_id)},
                {'$inc': {'used_today': 1}}
            )
        else:
            result = users_col.update_one(
                {'id': int(user_id) if isinstance(user_id, str) else user_id},
                {'$inc': {'used_today': 1}}
            )
        return result.modified_count > 0
    except Exception:
        return False

def update_user_daily_limit(user_id, new_limit):
    """Admin function to update user's daily limit"""
    try:
        if isinstance(user_id, str) and ObjectId.is_valid(user_id):
            result = users_col.update_one(
                {'_id': ObjectId(user_id)},
                {'$set': {'daily_limit': new_limit}}
            )
        else:
            result = users_col.update_one(
                {'id': int(user_id) if isinstance(user_id, str) else user_id},
                {'$set': {'daily_limit': new_limit}}
            )
        return result.modified_count > 0
    except Exception:
        return False

def check_rate_limit(ip):
    now = datetime.now(timezone.utc)
    record = rate_limits_col.find_one({'ip': ip})
    
    if not record:
        reset_time = now + timedelta(hours=1)
        rate_limits_col.insert_one({
            'ip': ip,
            'count': 1,
            'reset_time': reset_time.isoformat()
        })
        return True
    else:
        reset_time = record.get('reset_time')
        if reset_time and datetime.fromisoformat(reset_time.replace('Z', '+00:00')) < now:
            new_reset = now + timedelta(hours=1)
            rate_limits_col.update_one(
                {'ip': ip},
                {'$set': {'count': 1, 'reset_time': new_reset.isoformat()}}
            )
            return True
        else:
            count = record.get('count', 0)
            if count >= 5:
                return False
            else:
                rate_limits_col.update_one(
                    {'ip': ip},
                    {'$inc': {'count': 1}}
                )
                return True

# ---------- Password Helpers ----------
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_val):
    return hash_password(password) == hash_val

# ---------- Upstream API ----------
def send_otp_via_api(email, username=None):
    """
    Send OTP using your API: https://frux-otp-sso-lime.vercel.app/send-otp
    """
    params = {
        "email": email,
        "key": "FRUXOTP"
    }
    if username:
        params["username"] = username
    
    try:
        resp = requests.get(
            app.config['API_URL'],
            params=params,
            timeout=30
        )
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"📤 API Response: {data}")
            
            if data.get('data', {}).get('result') == 0:
                return data, None
            else:
                error_msg = data.get('data', {}).get('error', 'Unknown error')
                return data, error_msg
        else:
            return None, f"Status {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return None, str(e)

# ---------- Decorators ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            flash('Admin access required.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ========== HTML TEMPLATES ==========

BASE_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{{ title }} – OTP FVX</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <link href="https://unpkg.com/aos@2.3.1/dist/aos.css" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #02020F;
            --card-bg: rgba(13, 8, 41, 0.45);
            --border-color: rgba(157, 119, 250, 0.2);
            --glow-color: rgba(168, 85, 247, 0.5);
        }
        html { scroll-behavior: smooth; }
        body {
            font-family: 'Poppins', sans-serif;
            background-color: var(--bg-dark);
            color: #E2E1FF;
            overflow-x: hidden;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        #vanta-bg { position: fixed; width: 100%; height: 100%; top: 0; left: 0; z-index: -1; pointer-events: none; }
        .gradient-text {
            background: linear-gradient(135deg, #A5B4FC, #F472B6, #C084FC);
            background-size: 200% auto;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: text-shimmer 4s linear infinite;
        }
        @keyframes text-shimmer { to { background-position: 200% center; } }
        .glass-card {
            background: var(--card-bg);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border: 1px solid var(--border-color);
            border-radius: 1.25rem;
            transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .glass-card:hover {
            border-color: rgba(157, 119, 250, 0.35);
            box-shadow: 0 12px 40px rgba(139, 92, 246, 0.15);
        }
        .btn-glow {
            background: linear-gradient(90deg, #7C3AED, #4F46E5);
            color: white; border: none; cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 20px rgba(124, 58, 237, 0.3);
            padding: 0.75rem 1.5rem; border-radius: 0.75rem; font-weight: 600;
            display: inline-flex; align-items: center; justify-content: center; gap: 0.5rem;
        }
        .btn-glow:hover { box-shadow: 0 0 25px rgba(167, 139, 250, 0.6); transform: translateY(-1px); }
        .btn-glow:disabled { opacity: 0.6; cursor: not-allowed; transform: none !important; box-shadow: none !important; }
        .form-input {
            background: rgba(10, 5, 36, 0.6);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            color: white; padding: 0.75rem 1rem;
            width: 100%; outline: none; transition: all 0.3s ease;
        }
        .form-input:focus { border-color: var(--glow-color); box-shadow: 0 0 14px rgba(168, 85, 247, 0.4); background: rgba(15, 8, 50, 0.8); }
        
        #toast-container {
            position: fixed; top: 1.5rem; right: 1.5rem;
            z-index: 9999; display: flex; flex-direction: column; gap: 0.75rem;
            max-width: 400px; width: calc(100% - 3rem);
        }
        .toast-item {
            background: rgba(15, 10, 45, 0.85);
            backdrop-filter: blur(12px);
            border-left: 4px solid #8B5CF6;
            border-radius: 0.5rem; padding: 1rem;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4);
            animation: slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            color: #E2E1FF; font-size: 0.9rem;
        }
        @keyframes slideIn { from { transform: translateX(120%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
        .toast-success { border-left-color: #10B981; }
        .toast-error   { border-left-color: #EF4444; }
        .toast-info    { border-left-color: #3B82F6; }
        .toast-warning { border-left-color: #F59E0B; }

        .badge { padding: 0.25rem 0.625rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
        .badge-success { background: rgba(16, 185, 129, 0.15); color: #34D399; border: 1px solid rgba(16, 185, 129, 0.3); }
        .badge-danger  { background: rgba(239, 68, 68, 0.15); color: #F87171; border: 1px solid rgba(239, 68, 68, 0.3); }
        .badge-warning { background: rgba(245, 158, 11, 0.15); color: #FBBF24; border: 1px solid rgba(245, 158, 11, 0.3); }
        .badge-info    { background: rgba(59, 130, 246, 0.15); color: #60A5FA; border: 1px solid rgba(59, 130, 246, 0.3); }
        
        .footer-link {
            color: #6B7280;
            transition: all 0.3s ease;
            text-decoration: none;
        }
        .footer-link:hover {
            color: #A78BFA;
        }
    </style>
</head>
<body class="bg-[#02020F]">
    <div id="vanta-bg"></div>
    <div id="toast-container"></div>

    <nav class="fixed top-0 left-0 w-full z-50 glass-card !rounded-none !border-x-0 !border-t-0 backdrop-blur-md">
        <div class="max-w-7xl mx-auto px-2 sm:px-6 lg:px-8 h-16 flex justify-between items-center">
            <a href="{{ url_for('home') }}" class="text-base sm:text-xl font-bold gradient-text flex items-center gap-1 sm:gap-2 whitespace-nowrap">
                <i class="fa-solid fa-paper-plane text-purple-400 text-sm sm:text-base"></i><span>OTP FVX</span>
            </a>
            <div class="flex items-center gap-1.5 sm:gap-5">
                {% if session.user_id %}
                    <div class="bg-purple-950/40 px-2 py-0.5 sm:px-3 sm:py-1 rounded-lg border border-purple-800/30 text-[10px] sm:text-sm flex items-center gap-1 whitespace-nowrap">
                        <i class="fas fa-clock text-purple-400"></i>
                        <span class="text-gray-300">Today: <strong class="text-white user-usage">{{ user.used_today if user else 0 }}</strong>/<strong class="text-purple-400">{{ user.daily_limit if user else 15 }}</strong></span>
                    </div>
                    <a href="{{ url_for('dashboard') }}" class="text-[11px] sm:text-sm font-medium text-gray-300 hover:text-white transition px-1">Dashboard</a>
                    <a href="{{ url_for('profile') }}" class="text-[11px] sm:text-sm font-medium text-gray-300 hover:text-white transition hidden sm:inline-flex items-center gap-1"><i class="fas fa-user text-purple-400"></i> Profile</a>
                    <a href="{{ url_for('logout') }}" class="text-[11px] sm:text-sm font-medium text-red-400 hover:text-red-300 transition px-1">Logout</a>
                {% else %}
                    <a href="{{ url_for('home') }}" class="text-[11px] sm:text-sm font-medium text-gray-300 hover:text-white transition px-1">Home</a>
                    <a href="{{ url_for('login') }}" class="text-[11px] sm:text-sm font-medium text-gray-300 hover:text-white transition px-1">Login</a>
                    <a href="{{ url_for('register') }}" class="bg-purple-600 hover:bg-purple-500 text-white text-[10px] sm:text-xs font-semibold px-2 py-1 sm:px-3 sm:py-1.5 rounded-lg transition shadow-md whitespace-nowrap">Sign Up</a>
                {% endif %}
                {% if session.admin_logged_in %}
                    <a href="{{ url_for('admin_dashboard') }}" class="text-yellow-400 hover:text-yellow-300 text-[10px] sm:text-xs font-semibold border border-yellow-500/30 px-1.5 py-0.5 rounded bg-yellow-950/20 whitespace-nowrap">Admin</a>
                {% endif %}
            </div>
        </div>
    </nav>

    <main class="flex-grow pt-24 pb-16 px-4 max-w-7xl w-full mx-auto box-border">
        {{ content|safe }}
    </main>

    <footer class="w-full border-t border-purple-950/40 bg-[#02020F]/80 backdrop-blur-md py-6 text-center text-sm text-gray-500 mt-auto">
        <div class="max-w-7xl mx-auto px-4 flex flex-col justify-center items-center gap-5">
            <div class="flex flex-col sm:flex-row justify-between items-center w-full gap-4">
                <p>&copy; 2026 OTP FVX. All rights reserved.</p>
                <div class="flex gap-4 text-xs text-gray-600 flex-wrap justify-center">
                    <a href="{{ url_for('terms') }}" class="footer-link">Terms of Service</a>
                    <a href="{{ url_for('privacy') }}" class="footer-link">Privacy Policy</a>
                    <a href="{{ config.TELEGRAM_URL }}" target="_blank" class="footer-link hover:text-[#0088cc] flex items-center gap-1">
                        <i class="fab fa-telegram-plane"></i> Support
                    </a>
                </div>
            </div>
            
            <a href="{{ config.TELEGRAM_URL }}" target="_blank" class="inline-flex items-center gap-2 text-[#0088cc] hover:text-[#00aaff] font-bold transition bg-[#0088cc]/10 px-5 py-2.5 rounded-xl border border-[#0088cc]/30 hover:bg-[#0088cc]/20 hover:scale-105 transform duration-300">
                <i class="fab fa-telegram-plane text-xl"></i> Contact on Telegram
            </a>
        </div>
    </footer>

    <div id="flash-messages" class="hidden">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="flash-data" data-category="{{ category }}" data-message="{{ message }}"></div>
            {% endfor %}
        {% endwith %}
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
    <script src="https://unpkg.com/aos@2.3.1/dist/aos.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/vanta@latest/dist/vanta.waves.min.js"></script>
    <script>
        AOS.init({ once: true, duration: 800, offset: 40 });

        try {
            VANTA.WAVES({
                el: "#vanta-bg",
                mouseControls: false, touchControls: false, gyroControls: false,
                minHeight: 200.00, minWidth: 200.00, scale: 1.00, scaleMobile: 1.00,
                color: 0x03021a, shininess: 12.00, waveHeight: 8.00, waveSpeed: 0.40, zoom: 1.1
            });
        } catch(e) { console.error("Background animation skipped."); }

        function showToast(message, type = 'success') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast-item toast-${type}`;
            toast.innerHTML = `<div class="flex items-start gap-2">
                <i class="fas ${type === 'success' ? 'fa-circle-check text-green-400' : type === 'error' ? 'fa-circle-xmark text-red-400' : 'fa-circle-info text-blue-400'} mt-0.5"></i>
                <div>${message}</div>
            </div>`;
            container.appendChild(toast);
            setTimeout(() => {
                toast.style.transform = 'translateX(120%)';
                toast.style.opacity = '0';
                toast.style.transition = 'all 0.4s ease';
                setTimeout(() => toast.remove(), 400);
            }, 4000);
        }

        document.querySelectorAll('.flash-data').forEach(el => {
            showToast(el.getAttribute('data-message'), el.getAttribute('data-category'));
        });
    </script>
</body>
</html>
'''

LANDING_CONTENT = '''
<div class="space-y-20 py-6" data-aos="fade-up">
    <div class="text-center max-w-4xl mx-auto space-y-6 pt-4">
        <span class="px-4 py-1.5 rounded-full text-xs font-semibold uppercase tracking-wider bg-purple-950/60 border border-purple-500/30 text-purple-300">
            <i class="fas fa-sparkles mr-1"></i> Fast & Reliable OTP Service
        </span>
        <h1 class="text-4xl sm:text-6xl font-extrabold tracking-tight leading-tight text-white">
            Send OTP Messages <br><span class="gradient-text">To Any Email Instantly</span>
        </h1>
        <p class="text-gray-400 text-lg sm:text-xl max-w-2xl mx-auto leading-relaxed">
            The easiest and most reliable platform to send automated verification messages. Simple setup, zero delays, and perfectly optimized for all mobile screens and computers.
        </p>
        <div class="pt-4 flex justify-center gap-4">
            {% if session.user_id %}
                <a href="{{ url_for('dashboard') }}" class="btn-glow !text-base !px-8 !py-3">Open Dashboard <i class="fas fa-arrow-right ml-1 text-sm"></i></a>
            {% else %}
                <a href="{{ url_for('register') }}" class="btn-glow !text-base !px-8 !py-3">Get Started Free <i class="fas fa-bolt ml-1 text-sm"></i></a>
                <a href="{{ url_for('login') }} " class="glass-card px-6 py-3 rounded-xl hover:bg-purple-900/10 text-sm font-semibold transition flex items-center justify-center border border-purple-800/20">Login</a>
            {% endif %}
        </div>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 max-w-6xl mx-auto text-center">
        <div class="glass-card p-6" data-aos="fade-up" data-aos-delay="100">
            <p class="text-3xl sm:text-4xl font-extrabold text-white">99.99%</p>
            <p class="text-xs sm:text-sm text-purple-300 uppercase font-medium mt-1">Server Uptime</p>
        </div>
        <div class="glass-card p-6" data-aos="fade-up" data-aos-delay="200">
            <p class="text-3xl sm:text-4xl font-extrabold text-white">&lt; 1.5s</p>
            <p class="text-xs sm:text-sm text-purple-300 uppercase font-medium mt-1">Average Delivery</p>
        </div>
        <div class="glass-card p-6" data-aos="fade-up" data-aos-delay="300">
            <p class="text-3xl sm:text-4xl font-extrabold text-white">15</p>
            <p class="text-xs sm:text-sm text-purple-300 uppercase font-medium mt-1">Free OTPs Per Day</p>
        </div>
        <div class="glass-card p-6" data-aos="fade-up" data-aos-delay="400">
            <p class="text-3xl sm:text-4xl font-extrabold text-white">50k+</p>
            <p class="text-xs sm:text-sm text-purple-300 uppercase font-medium mt-1">OTPs Sent Daily</p>
        </div>
    </div>

    <div class="space-y-12 max-w-6xl mx-auto">
        <div class="text-center space-y-2">
            <h2 class="text-2xl sm:text-3xl font-bold text-white">Why Choose OTP FVX?</h2>
            <p class="text-gray-400 text-sm max-w-md mx-auto">Built for ease, transparency, and complete control over your messaging.</p>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="glass-card p-6 space-y-4">
                <div class="w-12 h-12 bg-purple-500/10 border border-purple-500/30 rounded-xl flex items-center justify-center text-purple-400 text-xl">
                    <i class="fas fa-gauge-high"></i>
                </div>
                <h3 class="text-lg font-bold text-white">Intuitive Dashboard</h3>
                <p class="text-gray-400 text-sm leading-relaxed">
                    Manage all your OTP activities from a clean, real‑time dashboard. Send messages and track every action in one place.
                </p>
            </div>
            <div class="glass-card p-6 space-y-4">
                <div class="w-12 h-12 bg-pink-500/10 border border-pink-500/30 rounded-xl flex items-center justify-center text-pink-400 text-xl">
                    <i class="fas fa-clock"></i>
                </div>
                <h3 class="text-lg font-bold text-white">Daily Free OTPs</h3>
                <p class="text-gray-400 text-sm leading-relaxed">
                    Every user gets <strong class="text-purple-400">15 free OTPs per day</strong>. Resets daily at 4:00 AM. No credit system needed!
                </p>
            </div>
            <div class="glass-card p-6 space-y-4">
                <div class="w-12 h-12 bg-indigo-500/10 border border-indigo-500/30 rounded-xl flex items-center justify-center text-indigo-400 text-xl">
                    <i class="fas fa-clock-rotate-left"></i>
                </div>
                <h3 class="text-lg font-bold text-white">Full Transaction History</h3>
                <p class="text-gray-400 text-sm leading-relaxed">
                    Every OTP request is logged – success or failure – so you can review past activity and keep a complete record.
                </p>
            </div>
        </div>
    </div>
</div>
'''

DASHBOARD_CONTENT = '''
<div class="max-w-2xl mx-auto" data-aos="fade-up">
    <div class="glass-card p-4 mb-6 flex flex-col sm:flex-row justify-between items-center gap-4">
        <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-full bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
                <i class="fas fa-clock"></i>
            </div>
            <div>
                <p class="text-xs text-gray-400">Today's Usage</p>
                <p class="text-lg font-bold text-white"><span class="user-usage">{{ used_today }}</span>/<span class="text-purple-400">{{ daily_limit }}</span></p>
                <p class="text-[10px] text-gray-500">Resets at 4:00 AM daily</p>
            </div>
        </div>
        <div class="bg-purple-950/30 px-3 py-1.5 rounded-lg border border-purple-800/30">
            <span class="text-xs text-gray-300">Remaining: <strong class="text-green-400">{{ remaining }}</strong></span>
        </div>
    </div>

    <div class="glass-card p-6 sm:p-8">
        <div class="text-center mb-6">
            <div class="w-14 h-14 rounded-full bg-purple-500/10 border border-purple-500/30 flex items-center justify-center mx-auto mb-3">
                <i class="fas fa-paper-plane text-xl text-purple-400"></i>
            </div>
            <h2 class="text-xl sm:text-2xl font-bold text-white">Send OTP</h2>
            <p class="text-xs text-gray-400 mt-1">Each OTP uses 1 of your daily limit. Free users get 15 per day!</p>
        </div>

        <form id="otpDispatchForm" class="space-y-4">
            <div>
                <label class="block text-xs font-medium text-purple-300 mb-1">Recipient Email Address</label>
                <div class="relative">
                    <i class="fas fa-envelope absolute left-4 top-3.5 text-gray-500 text-sm"></i>
                    <input type="email" name="email" class="form-input pl-11" placeholder="example@gmail.com" required>
                </div>
            </div>
            <div>
                <label class="block text-xs font-medium text-purple-300 mb-1">Username <span class="text-gray-500">(Optional)</span></label>
                <div class="relative">
                    <i class="fas fa-user absolute left-4 top-3.5 text-gray-500 text-sm"></i>
                    <input type="text" name="username" class="form-input pl-11" placeholder="Enter username if required">
                </div>
            </div>
            {% if remaining > 0 %}
            <button type="submit" class="btn-glow w-full justify-center !py-3 mt-2 text-xs font-bold uppercase tracking-wider">
                Send OTP Now
            </button>
            {% else %}
            <button type="button" class="btn-glow w-full justify-center !py-3 mt-2 text-xs font-bold uppercase tracking-wider opacity-50 cursor-not-allowed" disabled>
                Daily Limit Reached
            </button>
            <p class="text-xs text-center text-yellow-400 mt-2">⏳ Wait until 4:00 AM for reset</p>
            {% endif %}
        </form>
    </div>
</div>

<script>
document.getElementById('otpDispatchForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    const btn = this.querySelector('button[type="submit"]');
    const prevMarkup = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-circle-notch fa-spin mr-2"></i>Sending...';

    try {
        const response = await fetch("{{ url_for('send_otp') }}", {
            method: 'POST',
            body: new FormData(this)
        });
        const result = await response.json();
        if (result.success) {
            showToast(result.message, 'success');
            document.querySelectorAll('.user-usage').forEach(el => el.textContent = result.used_today);
            this.reset();
            // Update remaining
            const remainingSpan = document.querySelector('.text-green-400');
            if (remainingSpan) {
                const currentRemaining = parseInt(remainingSpan.textContent);
                remainingSpan.textContent = currentRemaining - 1;
            }
            // Disable button if limit reached
            if (result.remaining <= 0) {
                btn.disabled = true;
                btn.innerHTML = 'Daily Limit Reached';
                btn.classList.add('opacity-50', 'cursor-not-allowed');
            } else {
                btn.disabled = false;
                btn.innerHTML = prevMarkup;
            }
        } else {
            showToast(result.message, 'error');
            btn.disabled = false;
            btn.innerHTML = prevMarkup;
        }
    } catch(err) {
        showToast('Network error. Please check your connection.', 'error');
        btn.disabled = false;
        btn.innerHTML = prevMarkup;
    }
});
</script>
'''

LOGIN_CONTENT = '''
<div class="flex items-center justify-center min-h-[60vh]" data-aos="fade-up">
    <div class="glass-card p-6 sm:p-8 w-full max-w-md">
        <div class="text-center mb-6">
            <h3 class="text-2xl font-bold text-white">Login to OTP FVX</h3>
            <p class="text-xs text-gray-400 mt-1">Enter your registered email and password below</p>
        </div>

        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Email Address</label>
                <input type="email" name="email" class="form-input" placeholder="example@gmail.com" required>
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Password</label>
                <input type="password" name="password" class="form-input" placeholder="••••••••" required>
            </div>
            <button type="submit" class="btn-glow w-full justify-center !py-3 mt-2">
                Login
            </button>
        </form>

        <p class="text-center text-xs text-gray-400 mt-5">
            Don't have an account? <a href="{{ url_for('register') }}" class="text-purple-400 hover:underline font-medium">Create one here</a>
        </p>
    </div>
</div>
'''

REGISTER_CONTENT = '''
<div class="flex items-center justify-center min-h-[65vh]" data-aos="fade-up">
    <div class="glass-card p-6 sm:p-8 w-full max-w-md">
        <div class="text-center mb-6">
            <h3 class="text-2xl font-bold text-white">Create Free Account</h3>
            <p class="text-xs text-gray-400 mt-1">Get <strong class="text-green-400 font-semibold">15 free OTPs daily</strong> – resets at 4:00 AM!</p>
        </div>

        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Username</label>
                <input type="text" name="username" class="form-input" placeholder="john_doe" required>
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Email Address</label>
                <input type="email" name="email" class="form-input" placeholder="example@gmail.com" required>
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Password</label>
                <input type="password" name="password" class="form-input" placeholder="Minimum 6 characters" minlength="6" required>
            </div>
            <button type="submit" class="btn-glow w-full justify-center !py-3 mt-2">
                Register Account
            </button>
        </form>

        <p class="text-center text-xs text-gray-400 mt-5">
            Already have an account? <a href="{{ url_for('login') }}" class="text-purple-400 hover:underline font-medium">Login here</a>
        </p>
    </div>
</div>
'''

PROFILE_CONTENT = '''
<div class="grid grid-cols-1 lg:grid-cols-3 gap-6" data-aos="fade-up">
    <div class="lg:col-span-1 space-y-6">
        <div class="glass-card p-6">
            <h2 class="text-xl font-bold text-white mb-4"><i class="fas fa-id-card mr-1 text-purple-400"></i> Profile Details</h2>
            <div class="space-y-3 text-xs sm:text-sm">
                <div class="border-b border-purple-950/40 pb-2">
                    <span class="text-gray-400 block text-[11px] uppercase tracking-wider">Username</span>
                    <strong class="text-white">{{ user.username }}</strong>
                </div>
                <div class="border-b border-purple-950/40 pb-2">
                    <span class="text-gray-400 block text-[11px] uppercase tracking-wider">Email Address</span>
                    <strong class="text-white">{{ user.email }}</strong>
                </div>
                <div class="border-b border-purple-950/40 pb-2">
                    <span class="text-gray-400 block text-[11px] uppercase tracking-wider">Daily Limit</span>
                    <strong class="text-purple-400">{{ user.daily_limit }} OTPs</strong>
                </div>
                <div class="border-b border-purple-950/40 pb-2">
                    <span class="text-gray-400 block text-[11px] uppercase tracking-wider">Used Today</span>
                    <strong class="text-white user-usage">{{ user.used_today }}</strong>
                </div>
                <div>
                    <span class="text-gray-400 block text-[11px] uppercase tracking-wider">Joined Date</span>
                    <strong class="text-white">{{ user.created_at }}</strong>
                </div>
            </div>
        </div>
    </div>

    <div class="lg:col-span-2">
        <div class="glass-card p-6 h-full flex flex-col">
            <h3 class="text-lg font-bold text-white mb-4"><i class="fas fa-clock-rotate-left mr-1 text-purple-400"></i> OTP History (Last 20)</h3>
            <div class="overflow-x-auto flex-grow">
                <table class="w-full text-xs sm:text-sm text-left">
                    <thead class="text-[11px] uppercase tracking-wider bg-purple-950/40 text-purple-300 border-b border-purple-900/30">
                        <tr>
                            <th class="px-4 py-3">No.</th>
                            <th class="px-4 py-3">Recipient Email</th>
                            <th class="px-4 py-3">Status</th>
                            <th class="px-4 py-3">Date & Time</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-purple-950/30">
                        {% for req in requests %}
                        <tr class="hover:bg-purple-900/5 transition">
                            <td class="px-4 py-3 text-gray-500 font-mono">#{{ loop.index }}</td>
                            <td class="px-4 py-3 font-medium text-white">{{ req.email }}</td>
                            <td class="px-4 py-3">
                                <span class="badge {{ 'badge-success' if req.success else 'badge-danger' }}">
                                    {{ 'SENT' if req.success else 'FAILED' }}
                                </span>
                            </td>
                            <td class="px-4 py-3 text-gray-400 font-mono text-[11px]">{{ req.timestamp }}</td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="4" class="px-4 py-8 text-center text-gray-500 text-xs uppercase">No OTPs sent yet.</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</div>
'''

ADMIN_DASHBOARD_CONTENT = '''
<div class="space-y-6" data-aos="fade-up">
    <div class="flex justify-between items-center border-b border-purple-900/20 pb-4">
        <h2 class="text-2xl sm:text-3xl font-bold text-white"><i class="fas fa-gears mr-1 text-yellow-400"></i> Admin Dashboard</h2>
        <a href="{{ url_for('admin_logout') }}" class="text-xs text-red-400 hover:underline">Logout</a>
    </div>

    <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="glass-card p-4">
            <span class="text-gray-400 text-[10px] uppercase tracking-wider block">Total Users</span>
            <p class="text-2xl font-bold text-white mt-1">{{ total_users }}</p>
        </div>
        <div class="glass-card p-4">
            <span class="text-gray-400 text-[10px] uppercase tracking-wider block">Successful OTPs</span>
            <p class="text-2xl font-bold text-green-400 mt-1">{{ success_otp }}</p>
        </div>
        <div class="glass-card p-4">
            <span class="text-gray-400 text-[10px] uppercase tracking-wider block">Failed OTPs</span>
            <p class="text-2xl font-bold text-red-400 mt-1">{{ failed_otp }}</p>
        </div>
        <div class="glass-card p-4">
            <span class="text-gray-400 text-[10px] uppercase tracking-wider block">Default Daily Limit</span>
            <p class="text-2xl font-bold text-yellow-400 mt-1">{{ default_limit }}</p>
        </div>
    </div>

    <div class="glass-card p-6">
        <h3 class="text-base font-bold text-white mb-3"><i class="fas fa-users text-purple-400 mr-1"></i> Manage Users</h3>
        <p class="text-xs text-gray-400 mb-4">Grant extra OTPs per day to specific users</p>
        <div class="overflow-x-auto">
            <table class="w-full text-xs text-left">
                <thead class="bg-purple-950/40 text-purple-300 uppercase tracking-wider text-[10px]">
                    <tr>
                        <th class="px-4 py-3">ID</th>
                        <th class="px-4 py-3">Username</th>
                        <th class="px-4 py-3">Email</th>
                        <th class="px-4 py-3">Daily Limit</th>
                        <th class="px-4 py-3">Used Today</th>
                        <th class="px-4 py-3">Action</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-purple-950/30">
                    {% for user in users %}
                    <tr class="hover:bg-purple-900/5 transition">
                        <td class="px-4 py-3 text-gray-500 font-mono">#{{ user.id }}</td>
                        <td class="px-4 py-3 font-medium text-white">{{ user.username }}</td>
                        <td class="px-4 py-3 text-gray-300">{{ user.email }}</td>
                        <td class="px-4 py-3 font-bold text-purple-400">{{ user.daily_limit }}</td>
                        <td class="px-4 py-3">
                            <span class="badge {{ 'badge-warning' if user.used_today >= user.daily_limit else 'badge-info' }}">
                                {{ user.used_today }}/{{ user.daily_limit }}
                            </span>
                        </td>
                        <td class="px-4 py-3">
                            <form method="POST" action="{{ url_for('admin_update_limit') }}" class="flex items-center gap-2">
                                <input type="hidden" name="user_id" value="{{ user.id }}">
                                <input type="number" name="new_limit" value="{{ user.daily_limit }}" class="form-input !py-1 !px-2 w-20 text-center" min="1" required>
                                <button type="submit" class="bg-purple-600 hover:bg-purple-500 text-white font-semibold py-1 px-3 rounded text-xs transition">Update</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="glass-card p-6">
        <h3 class="text-base font-bold text-white mb-3"><i class="fas fa-list text-purple-400 mr-1"></i> Recent Logs (Top 50)</h3>
        <div class="overflow-x-auto">
            <table class="w-full text-xs text-left">
                <thead class="bg-purple-950/40 text-purple-300 uppercase tracking-wider text-[10px]">
                    <tr>
                        <th class="px-4 py-3">ID</th>
                        <th class="px-4 py-3">User</th>
                        <th class="px-4 py-3">Email</th>
                        <th class="px-4 py-3">Status</th>
                        <th class="px-4 py-3">Error</th>
                        <th class="px-4 py-3">Date</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-purple-950/30 text-gray-300">
                    {% for log in logs %}
                    <tr class="hover:bg-purple-900/5 transition">
                        <td class="px-4 py-3 text-gray-500 font-mono">#{{ log.id }}</td>
                        <td class="px-4 py-3 font-medium text-purple-200">{{ log.username or 'Guest' }}</td>
                        <td class="px-4 py-3 text-white">{{ log.email }}</td>
                        <td class="px-4 py-3">
                            <span class="badge {{ 'badge-success' if log.success else 'badge-danger' }}">
                                {{ 'OK' if log.success else 'FAIL' }}
                            </span>
                        </td>
                        <td class="px-4 py-3 text-red-400 max-w-[100px] truncate">{{ log.error_message or '-' }}</td>
                        <td class="px-4 py-3 text-gray-400 font-mono text-[11px]">{{ log.timestamp }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
'''

ADMIN_LOGIN_CONTENT = '''
<div class="flex items-center justify-center min-h-[60vh]" data-aos="fade-up">
    <div class="glass-card p-6 sm:p-8 w-full max-w-md">
        <div class="text-center mb-6">
            <h3 class="text-xl font-bold text-yellow-400"><i class="fas fa-user-shield"></i> Admin Login</h3>
            <p class="text-xs text-gray-400 mt-1">Enter admin credentials</p>
        </div>

        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Username</label>
                <input type="text" name="username" class="form-input" placeholder="admin" required>
            </div>
            <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-purple-300 mb-1">Password</label>
                <input type="password" name="password" class="form-input" placeholder="••••••••" required>
            </div>
            <button type="submit" class="btn-glow w-full justify-center !py-3 mt-2" style="background: linear-gradient(90deg, #D97706, #B45309);">
                Enter Dashboard
            </button>
        </form>
    </div>
</div>
'''

TERMS_CONTENT = '''
<div class="max-w-4xl mx-auto" data-aos="fade-up">
    <div class="glass-card p-6 sm:p-10">
        <h1 class="text-3xl sm:text-4xl font-bold text-white mb-6 text-center">
            <i class="fas fa-file-contract text-purple-400 mr-3"></i>Terms of Service
        </h1>
        <div class="prose prose-invert max-w-none text-gray-300 space-y-6">
            <p class="text-sm text-gray-400 border-b border-purple-900/30 pb-4">Last Updated: January 2026</p>
            
            <section>
                <h2 class="text-xl font-semibold text-white mt-6">1. Acceptance of Terms</h2>
                <p>By using OTP FVX, you agree to these Terms of Service. If you do not agree, please do not use our services.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">2. Description of Service</h2>
                <p>OTP FVX provides a platform for sending One-Time Password (OTP) messages via email. The service is provided on an "as-is" basis with daily usage limits.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">3. User Accounts</h2>
                <ul class="list-disc pl-6 space-y-2">
                    <li>You must provide accurate registration information.</li>
                    <li>You are responsible for maintaining account security.</li>
                    <li>Each user gets <strong class="text-purple-400">15 free OTPs per day</strong>, resetting at 4:00 AM UTC.</li>
                    <li>Accounts are non-transferable.</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">4. Acceptable Use Policy</h2>
                <p>You agree NOT to use OTP FVX for:</p>
                <ul class="list-disc pl-6 space-y-2">
                    <li>Spamming or unsolicited messaging.</li>
                    <li>Phishing, fraud, or illegal activities.</li>
                    <li>Harassment or abuse of any individual or organization.</li>
                    <li>Violating any applicable laws or regulations.</li>
                </ul>
                <p class="mt-3 text-yellow-400">⚠️ Violation may result in immediate account termination without refund.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">5. Service Availability</h2>
                <p>We strive for 99.99% uptime but do not guarantee uninterrupted service. Maintenance or technical issues may occur.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">6. Data Collection & Privacy</h2>
                <p>We collect minimal data required to provide the service. Please refer to our <a href="{{ url_for('privacy') }}" class="text-purple-400 hover:underline">Privacy Policy</a> for details.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">7. Limitation of Liability</h2>
                <p>OTP FVX is provided "as is" without warranties. We are not liable for any damages arising from the use of our service, including but not limited to message delivery failures or data loss.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">8. Changes to Terms</h2>
                <p>We may update these Terms at any time. Continued use of the service constitutes acceptance of the updated Terms.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">9. Contact</h2>
                <p>For questions or concerns, please contact us via our <a href="{{ config.TELEGRAM_URL }}" target="_blank" class="text-purple-400 hover:underline">Telegram Support</a>.</p>
            </section>

            <div class="bg-purple-950/30 border border-purple-800/30 rounded-xl p-4 mt-6 text-center">
                <p class="text-sm text-gray-400">By using OTP FVX, you acknowledge that you have read and agree to these Terms of Service.</p>
            </div>
        </div>
    </div>
</div>
'''

PRIVACY_CONTENT = '''
<div class="max-w-4xl mx-auto" data-aos="fade-up">
    <div class="glass-card p-6 sm:p-10">
        <h1 class="text-3xl sm:text-4xl font-bold text-white mb-6 text-center">
            <i class="fas fa-shield-halved text-purple-400 mr-3"></i>Privacy Policy
        </h1>
        <div class="prose prose-invert max-w-none text-gray-300 space-y-6">
            <p class="text-sm text-gray-400 border-b border-purple-900/30 pb-4">Last Updated: January 2026</p>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">1. Information We Collect</h2>
                <p>We collect minimal data to provide our OTP service:</p>
                <ul class="list-disc pl-6 space-y-2">
                    <li><strong>Account Information:</strong> Username, email address, and hashed password.</li>
                    <li><strong>Usage Data:</strong> OTP requests sent, timestamps, success/failure status, and IP addresses for security.</li>
                    <li><strong>Device Information:</strong> User-agent strings and basic browser details.</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">2. How We Use Your Data</h2>
                <ul class="list-disc pl-6 space-y-2">
                    <li><strong>Service Delivery:</strong> To send OTP messages to requested email addresses.</li>
                    <li><strong>Account Management:</strong> To authenticate users and track daily usage limits.</li>
                    <li><strong>Security:</strong> To prevent abuse, enforce rate limits, and investigate fraudulent activity.</li>
                    <li><strong>Improvement:</strong> To analyze usage patterns and enhance service quality.</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">3. Data Storage & Security</h2>
                <ul class="list-disc pl-6 space-y-2">
                    <li>Data is stored in a secure MongoDB database.</li>
                    <li>Passwords are hashed using SHA-256 (never stored in plaintext).</li>
                    <li>We employ industry-standard security practices to protect your data.</li>
                    <li>OTP logs are retained for service operations and troubleshooting.</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">4. Data Sharing</h2>
                <p>We do <strong>not</strong> sell or share your personal data with third parties, except:</p>
                <ul class="list-disc pl-6 space-y-2">
                    <li>When required by law or legal process.</li>
                    <li>With your explicit consent.</li>
                    <li>To the upstream OTP API service (email and username are passed for sending OTPs).</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">5. Your Rights</h2>
                <ul class="list-disc pl-6 space-y-2">
                    <li><strong>Access:</strong> You can view your data via the Profile page.</li>
                    <li><strong>Correction:</strong> Update your account information anytime.</li>
                    <li><strong>Deletion:</strong> Contact us to request account deletion.</li>
                    <li><strong>Data Export:</strong> Request a copy of your data by contacting support.</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">6. Cookies & Tracking</h2>
                <p>We use session cookies to maintain login sessions. We do not use third-party tracking cookies.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">7. Data Retention</h2>
                <ul class="list-disc pl-6 space-y-2">
                    <li><strong>Account Data:</strong> Retained until you request deletion.</li>
                    <li><strong>OTP Logs:</strong> Retained for operational purposes and archived periodically.</li>
                    <li><strong>Rate Limit Data:</strong> Automatically expires after 1 hour.</li>
                </ul>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">8. Children's Privacy</h2>
                <p>OTP FVX is not intended for children under 13. We do not knowingly collect data from minors.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">9. Changes to Privacy Policy</h2>
                <p>We may update this Privacy Policy periodically. Check this page for updates.</p>
            </section>

            <section>
                <h2 class="text-xl font-semibold text-white mt-6">10. Contact Us</h2>
                <p>If you have any questions about this Privacy Policy, please contact us on <a href="{{ config.TELEGRAM_URL }}" target="_blank" class="text-purple-400 hover:underline">Telegram</a>.</p>
            </section>

            <div class="bg-purple-950/30 border border-purple-800/30 rounded-xl p-4 mt-6 text-center">
                <p class="text-sm text-gray-400">Your privacy matters to us. We are committed to protecting your personal information.</p>
            </div>
        </div>
    </div>
</div>
'''

# ---------- Page Renderer ----------
def render_page(content_template, title="OTP FVX", **context):
    user = None
    if session.get('user_id'):
        if 'user_usage' in session:
            user = {
                'id': session['user_id'],
                'username': session.get('username'),
                'email': session.get('user_email'),
                'daily_limit': session.get('daily_limit', 15),
                'used_today': session.get('user_usage', 0)
            }
        else:
            user = get_user_by_id(session['user_id'])
            if user:
                session['daily_limit'] = user.get('daily_limit', 15)
                session['user_usage'] = user.get('used_today', 0)
                session['username'] = user['username']
                session['user_email'] = user['email']
    content_rendered = render_template_string(content_template, **context)
    return render_template_string(BASE_HTML, title=title, content=content_rendered, user=user)

# ---------- Routes ----------
@app.route('/')
def home():
    return render_page(LANDING_CONTENT, title="Home")

@app.route('/terms')
def terms():
    return render_page(TERMS_CONTENT, title="Terms of Service")

@app.route('/privacy')
def privacy():
    return render_page(PRIVACY_CONTENT, title="Privacy Policy")

@app.route('/dashboard')
@login_required
def dashboard():
    used_today, remaining, daily_limit = get_user_daily_usage(session['user_id'])
    session['user_usage'] = used_today
    session['daily_limit'] = daily_limit
    return render_page(DASHBOARD_CONTENT, title="Dashboard", used_today=used_today, remaining=remaining, daily_limit=daily_limit)

@app.route('/send-otp', methods=['POST'])
@login_required
def send_otp():
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({'success': False, 'message': 'User not found.'}), 401

    # Check daily usage
    used_today, remaining, daily_limit = get_user_daily_usage(session['user_id'])
    if remaining <= 0:
        return jsonify({'success': False, 'message': 'Daily limit reached. Wait until 4:00 AM.'}), 403

    email = request.form.get('email', '').strip()
    username = request.form.get('username', '').strip() or None
    ip = request.remote_addr

    if not email or '@' not in email:
        return jsonify({'success': False, 'message': 'Invalid email.'}), 400

    result, error = send_otp_via_api(email, username)
    
    if error:
        success = 0
        error_msg = error
    else:
        if result and result.get('data', {}).get('result') == 0:
            success = 1
            error_msg = None
        else:
            success = 0
            error_msg = result.get('data', {}).get('error', 'Unknown error')

    if success == 1:
        increment_user_usage(session['user_id'])
        used_today, remaining, daily_limit = get_user_daily_usage(session['user_id'])
        session['user_usage'] = used_today

    # Log the request
    log_data = {
        'user_id': session['user_id'],
        'email': email,
        'username': username,
        'success': success,
        'error_message': error_msg,
        'ip_address': ip,
        'user_agent': request.headers.get('User-Agent'),
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    requests_col.insert_one(log_data)

    if success == 1:
        return jsonify({
            'success': True, 
            'message': '✅ OTP sent successfully! Check your email.',
            'used_today': used_today,
            'remaining': remaining - 1
        })
    else:
        return jsonify({
            'success': False, 
            'message': f"❌ {error_msg}",
            'used_today': used_today,
            'remaining': remaining
        })

@app.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not email or not password:
            flash('All fields required.', 'error')
            return render_page(REGISTER_CONTENT, title="Register")
        if len(password) < 6:
            flash('Password too short.', 'error')
            return render_page(REGISTER_CONTENT, title="Register")
        if get_user_by_username(username):
            flash('Username taken.', 'error')
            return render_page(REGISTER_CONTENT, title="Register")
        if get_user_by_email(email):
            flash('Email registered.', 'error')
            return render_page(REGISTER_CONTENT, title="Register")
        hashed = hash_password(password)
        user_id = create_user(username, email, hashed)
        if user_id:
            flash('Account created! Please log in.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Registration error.', 'error')
    return render_page(REGISTER_CONTENT, title="Register")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not email or not password:
            flash('Please fill all fields.', 'error')
            return render_page(LOGIN_CONTENT, title="Login")
        user = get_user_by_email(email)
        if not user or not verify_password(password, user['password_hash']):
            flash('Invalid credentials.', 'error')
            return render_page(LOGIN_CONTENT, title="Login")
        session['user_id'] = str(user['_id'])
        session['user_usage'] = user.get('used_today', 0)
        session['daily_limit'] = user.get('daily_limit', 15)
        session['username'] = user['username']
        session['user_email'] = user['email']
        flash('Logged in.', 'success')
        return redirect(url_for('dashboard'))
    return render_page(LOGIN_CONTENT, title="Login")

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/profile')
@login_required
def profile():
    user = get_user_by_id(session['user_id'])
    if not user:
        flash('User error.', 'error')
        return redirect(url_for('logout'))
    
    # Update usage
    used_today, remaining, daily_limit = get_user_daily_usage(session['user_id'])
    user['used_today'] = used_today
    user['daily_limit'] = daily_limit
    session['user_usage'] = used_today
    
    cursor = requests_col.find({'user_id': session['user_id']}).sort('timestamp', -1).limit(20)
    requests_logs = []
    for row in cursor:
        ts = row.get('timestamp')
        requests_logs.append({
            'email': row.get('email'),
            'success': row.get('success'),
            'timestamp': ts
        })
    return render_page(PROFILE_CONTENT, title="Profile", user=user, requests=requests_logs)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == app.config['ADMIN_USERNAME'] and password == app.config['ADMIN_PASSWORD']:
            session['admin_logged_in'] = True
            flash('Admin logged in.', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid admin credentials.', 'error')
    return render_page(ADMIN_LOGIN_CONTENT, title="Admin Login")

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('Admin logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    total_users = users_col.count_documents({})
    success_otp = requests_col.count_documents({'success': 1})
    failed_otp = requests_col.count_documents({'success': 0})
    default_limit = app.config['DAILY_LIMIT']

    # Get all users
    users = []
    for user in users_col.find().sort('id', 1):
        users.append({
            'id': user.get('id'),
            'username': user.get('username'),
            'email': user.get('email'),
            'daily_limit': user.get('daily_limit', default_limit),
            'used_today': user.get('used_today', 0)
        })

    logs_pipeline = [
        {'$sort': {'timestamp': -1}},
        {'$limit': 50},
        {'$lookup': {'from': 'users', 'localField': 'user_id', 'foreignField': 'id', 'as': 'user'}},
        {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}}
    ]
    logs = []
    for row in requests_col.aggregate(logs_pipeline):
        ts = row.get('timestamp')
        logs.append({
            'id': str(row['_id'])[:8],
            'email': row.get('email'),
            'success': row.get('success'),
            'error_message': row.get('error_message'),
            'timestamp': ts,
            'username': row.get('user', {}).get('username') if row.get('user') else None
        })

    return render_page(ADMIN_DASHBOARD_CONTENT, title="Admin Dashboard",
                       total_users=total_users, success_otp=success_otp, failed_otp=failed_otp,
                       default_limit=default_limit, users=users, logs=logs)

@app.route('/admin/update-limit', methods=['POST'])
@admin_required
def admin_update_limit():
    user_id = request.form.get('user_id')
    new_limit = request.form.get('new_limit')
    
    if not user_id or not new_limit:
        flash('Missing parameters.', 'error')
        return redirect(url_for('admin_dashboard'))
    
    try:
        new_limit = int(new_limit)
        if new_limit < 1:
            flash('Limit must be at least 1.', 'error')
            return redirect(url_for('admin_dashboard'))
    except ValueError:
        flash('Invalid limit value.', 'error')
        return redirect(url_for('admin_dashboard'))
    
    if update_user_daily_limit(user_id, new_limit):
        flash(f'✅ Daily limit updated to {new_limit}.', 'success')
    else:
        flash('❌ Failed to update limit.', 'error')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/api/send', methods=['POST'])
def api_send():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON required'}), 400
    api_key = data.get('api_key')
    if not api_key or api_key not in app.config['API_KEYS']:
        return jsonify({'error': 'Unauthorized'}), 401
    email = data.get('email', '').strip()
    if not email or '@' not in email:
        return jsonify({'error': 'Invalid email'}), 400
    username = data.get('username', '').strip() or None
    ip = request.remote_addr

    user_id = data.get('user_id')
    if user_id:
        user = get_user_by_id(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        used_today, remaining, daily_limit = get_user_daily_usage(user_id)
        if remaining <= 0:
            return jsonify({'error': 'Daily limit reached'}), 403
    else:
        if not check_rate_limit(ip):
            return jsonify({'error': 'Rate limit exceeded'}), 429

    result, error = send_otp_via_api(email, username)
    if error:
        success = 0
        error_msg = error
    else:
        if result and result.get('data', {}).get('result') == 0:
            success = 1
            error_msg = None
        else:
            success = 0
            error_msg = result.get('data', {}).get('error', 'Unknown error')

    if success == 1 and user_id:
        increment_user_usage(user_id)

    log_data = {
        'user_id': user_id,
        'email': email,
        'username': username,
        'success': success,
        'error_message': error_msg,
        'ip_address': ip,
        'user_agent': request.headers.get('User-Agent'),
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    requests_col.insert_one(log_data)

    if success:
        return jsonify({'status': 'success', 'message': 'OTP sent.'})
    else:
        return jsonify({'status': 'error', 'message': error_msg or 'Failed.'}), 500

# ---------- Main ----------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)