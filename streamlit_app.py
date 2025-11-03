# streamlit_app.py
import os
import time
import random
import queue
import logging
import threading
import json
from typing import List, Dict
import streamlit as st
import streamlit as st
import hashlib
import time



APP_PASSWORD = "aksisinergi123"  # ubah sesuai keinginan
TOKEN_KEY = "aksisinergi_token"

def generate_token(password):
    return hashlib.sha256(password.encode()).hexdigest()

def check_login():
    if TOKEN_KEY in st.session_state:
        return True
    if "authenticated" in st.session_state and st.session_state.authenticated:
        return True
    return False

def login_page():
    st.title("🔐 Login Aksi Sinergi Bot")
    password = st.text_input("Masukkan password", type="password")
    if st.button("Login"):
        if password == APP_PASSWORD:
            st.session_state.authenticated = True
            st.session_state[TOKEN_KEY] = generate_token(password)
            st.success("Login berhasil! Silakan refresh halaman.")
            time.sleep(1)
            st.rerun()
        else:
            st.error("Password salah.")

def require_login():
    if not check_login():
        login_page()
        st.stop()


# instagrapi import (graceful fallback)
try:
    from instagrapi import Client
    from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired, ClientError
except Exception:
    Client = None
    ChallengeRequired = TwoFactorRequired = ClientError = Exception

# ====== Defaults ======
DEFAULT_TARGET = ""
DEFAULT_COMMENTS = ""
DEFAULT_MAX_COMMENTS = 1
LOG_FILENAME = "bot_stealth.log"
ACCOUNTS_FILE = "accounts.json"
SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# ====== Helper: persist akun ke file (harus didefinisikan sebelum dipakai) ======
def load_accounts_from_file():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"Gagal memuat file akun: {e}")
        return []

def save_accounts_to_file(accounts):
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Gagal menyimpan file akun: {e}")

# ====== Ensure session_state keys ======
if "accounts" not in st.session_state:
    st.session_state.accounts: List[Dict[str, str]] = load_accounts_from_file()

if "clients" not in st.session_state:
    st.session_state.clients: Dict[str, object] = {}

if "running" not in st.session_state:
    st.session_state.running = False

if "worker_thread" not in st.session_state:
    st.session_state.worker_thread = None

if "log_lines" not in st.session_state:
    st.session_state.log_lines = []

if "log_queue" not in st.session_state:
    st.session_state.log_queue = queue.Queue()

if "stop_event" not in st.session_state:
    # Event object must live in session_state so UI can set it and thread can read it via config
    st.session_state.stop_event = threading.Event()

# ====== LOGGING ======
logger = logging.getLogger("instagrapi_logger")
logger.setLevel(logging.DEBUG)

# Avoid duplicate handlers on hot reload
def _has_handler_of_type(logger_, typ):
    return any(isinstance(h, typ) for h in logger_.handlers)

if not _has_handler_of_type(logger, logging.FileHandler):
    file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

# handler yang menaruh pesan ke queue (thread-safe)
class StreamQueueHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            st.session_state.log_queue.put_nowait(msg)
        except Exception:
            # jangan meledak kalau queue penuh/dsb
            pass

if not _has_handler_of_type(logger, StreamQueueHandler):
    qhandler = StreamQueueHandler()
    qhandler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(qhandler)

# ====== Helper functions ======
def login_client_for_account(username: str, password: str):
    """
    Login otomatis menggunakan file session jika tersedia.
    Jika session rusak/expired, akan login ulang dan menyimpan session baru.
    """
    if Client is None:
        raise RuntimeError("instagrapi belum terinstall di environment ini.")

    session_file = os.path.join(SESSION_DIR, f"session_{username}.json")

    cl = Client()
    try:
        # Coba load settings dulu
        if os.path.exists(session_file):
            try:
                cl.load_settings(session_file)
                cl.login(username, password)
                logger.info(f"[{username}] Login via session lama berhasil")
                return cl
            except Exception as e:
                logger.warning(f"[{username}] Session lama gagal: {e}. Akan login ulang dan menimpa session.")
                try:
                    os.remove(session_file)
                except Exception:
                    pass

        # login normal dan simpan session
        cl.login(username, password)
        cl.dump_settings(session_file)
        logger.info(f"[{username}] Login baru sukses, session disimpan ke {session_file}")
        return cl

    except TwoFactorRequired:
        logger.error(f"[{username}] TwoFactorRequired - verifikasi manual diperlukan.")
        raise RuntimeError("TwoFactorRequired")
    except ChallengeRequired:
        logger.error(f"[{username}] ChallengeRequired - verifikasi manual diperlukan.")
        raise RuntimeError("ChallengeRequired")
    except ClientError as e:
        logger.error(f"[{username}] ClientError: {e}")
        raise RuntimeError(f"Login gagal: {e}")
    except Exception as e:
        logger.error(f"[{username}] Login error umum: {e}")
        raise RuntimeError(str(e))

def run_buzzer_for_account(cl, username: str, target_post_url: str, comments: List[str], max_comments: int, counters: Dict[str,int],
                           like_delay_min: float, like_delay_max: float, comment_delay_min: float, comment_delay_max: float):
    """Perform like and optional comment for one account. Raises RuntimeError on verif-needed."""
    try:
        pk = cl.media_pk_from_url(target_post_url)
        _ = cl.media_info(pk)
    except ChallengeRequired:
        raise RuntimeError(f"[{username}] Verifikasi IG diperlukan.")
    except Exception as e:
        logger.error(f"[{username}] Gagal ambil media: {e}")
        return

    try:
        cl.media_like(pk)
        logger.info(f"[{username}] Liked media ID {pk}")
        time.sleep(random.uniform(like_delay_min, like_delay_max))

        if counters.get(username, 0) < max_comments:
            komentar = random.choice(comments) if comments else "Nice!"
            cl.media_comment(pk, komentar)
            counters[username] = counters.get(username, 0) + 1
            logger.info(f"[{username}] Commented '{komentar}' ({counters[username]}/{max_comments})")
            time.sleep(random.uniform(comment_delay_min, comment_delay_max))
        else:
            logger.info(f"[{username}] Skip komentar: limit {max_comments} tercapai.")
    except ChallengeRequired:
        raise RuntimeError(f"[{username}] Verifikasi IG diperlukan.")
    except Exception as e:
        logger.error(f"[{username}] Error like/comment: {e}")
        time.sleep(1)

# ====== Background worker (uses snapshot only) ======
def bot_worker(config):
    logger.info("Worker: dimulai.")
    stop_event = config.get("stop_event")
    if stop_event is None:
        logger.error("Worker: stop_event tidak ada di config.")
        return

    client_dict: Dict[str, object] = config.get("client_dict", {}) or {}
    counters = {u: 0 for u in client_dict.keys()}

    try:
        while not stop_event.is_set():
            if not client_dict:
                logger.info("Worker: snapshot client kosong. Berhenti.")
                break

            if all(counters.get(u, 0) >= config["max_comments_per_account"] for u in client_dict.keys()):
                logger.info("Worker: semua akun mencapai limit komentar. Selesai.")
                break

            for username, cl in list(client_dict.items()):
                if stop_event.is_set():
                    break

                if counters.get(username, 0) >= config["max_comments_per_account"]:
                    continue

                try:
                    run_buzzer_for_account(
                        cl,
                        username,
                        config["target_post_url"],
                        config["comments"],
                        config["max_comments_per_account"],
                        counters,
                        config["like_delay_min"], config["like_delay_max"],
                        config["comment_delay_min"], config["comment_delay_max"]
                    )
                except RuntimeError as err:
                    logger.error(f"[{username}] {err}")
                    client_dict.pop(username, None)
                    counters.pop(username, None)
                    continue
                except Exception as e:
                    logger.error(f"[{username}] Error tak terduga: {e}")
                    continue

                delay = random.uniform(config["between_accounts_delay_min"], config["between_accounts_delay_max"])
                slept = 0.0
                while slept < delay and not stop_event.is_set():
                    time.sleep(min(1.0, delay - slept))
                    slept += 1.0

            total_wait = config["loop_wait_seconds"]
            slept = 0.0
            while slept < total_wait and not stop_event.is_set():
                time.sleep(min(1.0, total_wait - slept))
                slept += 1.0

    except Exception as e:
        logger.exception(f"Worker crash: {e}")
    finally:
        # hanya update UI-state di thread utama; di worker thread kita set event flag
        stop_event.set()
        logger.info("Worker: benar-benar berhenti.")

# ====== Streamlit UI ======
require_login()

st.title("Aksi Sinergi IG Bot Stealth")
st.caption("Tambah akun, login, jalankan bot di background, stop (soft), dan lihat log.")

# Sidebar configuration
st.sidebar.header("Konfigurasi cepat")
target_post_url = st.sidebar.text_input("Target post URL", DEFAULT_TARGET)
raw_comments = st.sidebar.text_area("Komentar (pisah koma/newline)", DEFAULT_COMMENTS)
comments = [c.strip() for c in raw_comments.replace("\n", ",").split(",") if c.strip()]
max_comments_per_account = st.sidebar.number_input("Max komentar per akun", min_value=0, value=DEFAULT_MAX_COMMENTS, step=1)

like_delay_min = st.sidebar.number_input("Like delay min (s)", value=5.0)
like_delay_max = st.sidebar.number_input("Like delay max (s)", value=10.0)
comment_delay_min = st.sidebar.number_input("Comment delay min (s)", value=20.0)
comment_delay_max = st.sidebar.number_input("Comment delay max (s)", value=35.0)

between_accounts_delay_min = st.sidebar.number_input("Delay antar akun min (s)", value=30.0)
between_accounts_delay_max = st.sidebar.number_input("Delay antar akun max (s)", value=60.0)

loop_wait_seconds = st.sidebar.number_input("Delay akhir putaran (s)", value=120)

st.sidebar.markdown("---")

# Login actions
if st.sidebar.button("Login All Accounts"):
    success = []
    errors = []
    for acc in list(st.session_state.accounts):
        user, pwd = acc["username"], acc["password"]
        try:
            cl = login_client_for_account(user, pwd)
            st.session_state.clients[user] = cl
            success.append(user)
            logger.info(f"[{user}] Login berhasil (via Login All).")
        except Exception as e:
            errors.append(f"{user}: {e}")
            logger.error(f"[{user}] Login gagal (via Login All): {e}")
    st.sidebar.success(f"Login selesai. Berhasil: {len(success)}. Gagal: {len(errors)}")

if st.sidebar.button("Logout All Accounts"):
    st.session_state.clients = {}
    st.sidebar.info("Semua sesi akun dikeluarkan (logout).")

# Per-account UI in sidebar
st.sidebar.markdown("---")
st.sidebar.write("Akun yang disimpan (belum tentu login):")
for i, acc in enumerate(list(st.session_state.accounts)):
    cols = st.sidebar.columns([3, 1, 1])
    cols[0].write(f"{acc['username']}")
    if cols[1].button("Login", key=f"login_{i}"):
        try:
            cl = login_client_for_account(acc["username"], acc["password"])
            st.session_state.clients[acc['username']] = cl
            st.sidebar.success(f"{acc['username']} login sukses")
        except Exception as e:
            st.sidebar.error(f"{acc['username']} login gagal: {e}")
    if cols[2].button("Hapus", key=f"del_{i}"):
        st.session_state.accounts.pop(i)
        save_accounts_to_file(st.session_state.accounts)
        st.experimental_rerun()

# Main manage accounts
st.markdown("## Manage Accounts")
with st.form("add_account_form", clear_on_submit=True):
    new_u = st.text_input("Username (tambahan)")
    new_p = st.text_input("Password (tambahan)", type="password")
    add_submit = st.form_submit_button("Tambah akun (simpan, tidak otomatis login)")
    if add_submit:
        if new_u and new_p:
            st.session_state.accounts.append({"username": new_u.strip(), "password": new_p.strip()})
            save_accounts_to_file(st.session_state.accounts)
            st.success(f"Akun {new_u} disimpan (belum login).")
        else:
            st.error("Isi username & password")

st.write("Akun tersimpan:", [a["username"] for a in st.session_state.accounts])
st.write("Akun login aktif:", list(st.session_state.clients.keys()))

# Run / Stop controls
col_start, col_stop = st.columns(2)
config = {
    "target_post_url": target_post_url,
    "comments": comments,
    "max_comments_per_account": int(max_comments_per_account),
    "like_delay_min": float(like_delay_min),
    "like_delay_max": float(like_delay_max),
    "comment_delay_min": float(comment_delay_min),
    "comment_delay_max": float(comment_delay_max),
    "between_accounts_delay_min": float(between_accounts_delay_min),
    "between_accounts_delay_max": float(between_accounts_delay_max),
    "loop_wait_seconds": float(loop_wait_seconds)
}

if col_start.button("Start Bot (background)"):
    if not st.session_state.clients:
        st.error("Tidak ada akun login.")
    elif st.session_state.running:
        st.warning("Bot sudah berjalan.")
    else:
        # snapshot clients and pass stop_event explicitly to worker
        config["client_dict"] = dict(st.session_state.clients)
        config["stop_event"] = st.session_state.stop_event
        st.session_state.running = True
        st.session_state.stop_event.clear()
        t = threading.Thread(target=bot_worker, args=(config,), daemon=True)
        st.session_state.worker_thread = t
        t.start()
        st.success("Worker started.")

if col_stop.button("Stop Bot (soft)"):
    if st.session_state.running:
        st.session_state.stop_event.set()
        st.session_state.running = False
        st.success("Stop signal dikirim.")
    else:
        st.info("Worker tidak berjalan.")

# Log viewer
st.markdown("---")
st.markdown("## Live Log")
log_display = st.empty()

def render_logs():
    # Ambil log baru dari queue thread background
    while not st.session_state.log_queue.empty():
        try:
            st.session_state.log_lines.append(st.session_state.log_queue.get_nowait())
        except queue.Empty:
            break

    # Tampilkan hanya 300 baris terakhir
    value = "\n".join(st.session_state.log_lines[-300:])
    log_display.text(value)

render_logs()
if st.button("Refresh Log (manual)"):
    render_logs()

st.markdown("**Catatan:** Stop Bot (soft) memberi sinyal worker berhenti rapi pada cek berikutnya. Worker tidak memodifikasi session_state.clients; UI tetap sebagai sumber kebenaran untuk sesi login.")
