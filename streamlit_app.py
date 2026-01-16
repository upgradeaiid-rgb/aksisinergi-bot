import os
import time
import random
import queue
import logging
import threading
import json
from typing import List, Dict
import streamlit as st
import hashlib

# ================= AUTH CONFIG =================

APP_USERNAME = "admin"
APP_PASSWORD = "aksisinergi123"
TOKEN_KEY = "aksisinergi_token"

def generate_token(password):
	return hashlib.sha256(password.encode()).hexdigest()

def check_login():
	return (
		st.session_state.get("authenticated")
		or (TOKEN_KEY in st.session_state and bool(st.session_state.get(TOKEN_KEY)))
	)

def login_page():
	st.title("🔐 Login Aksi Sinergi Bot")

	username = st.text_input("Username")
	password = st.text_input("Password", type="password")

	if st.button("Login"):
		if username == APP_USERNAME and password == APP_PASSWORD:
			st.session_state.authenticated = True
			st.session_state[TOKEN_KEY] = generate_token(password)
			st.success("Login berhasil! Silakan refresh halaman.")
			time.sleep(1)
			st.rerun()
		else:
			st.error("Username atau password salah.")

def require_login():
	if not check_login():
		login_page()
		st.stop()

# ================= instagrapi import =================

try:
	from instagrapi import Client
	from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired, ClientError
except Exception:
	Client = None
	ChallengeRequired = TwoFactorRequired = ClientError = Exception

LOG_FILENAME = "bot_stealth.log"
ACCOUNTS_FILE = "accounts.json"
SESSION_DIR = "sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

# ================= Helper akun =================

def load_accounts_from_file():
	try:
		with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
			data = json.load(f)
			return data if isinstance(data, list) else []
	except FileNotFoundError:
		return []
	except Exception as e:
		print(f"Gagal load file akun: {e}")
		return []

def save_accounts_to_file(accounts):
	try:
		with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
			json.dump(accounts, f, indent=2, ensure_ascii=False)
	except Exception as e:
		print(f"Gagal simpan file akun: {e}")

# ================= Session state init =================

for key, default in {
	"accounts": load_accounts_from_file(),
	"clients": {},
	"running": False,
	"worker_thread": None,
	"log_lines": [],
	"log_queue": queue.Queue(),
	"stop_event": threading.Event(),
}.items():
	if key not in st.session_state:
		st.session_state[key] = default

# ================= Logging =================

logger = logging.getLogger("instagrapi_logger")
logger.setLevel(logging.DEBUG)

def _has_handler_of_type(logger_obj, typ):
	return any(isinstance(h, typ) for h in logger_obj.handlers)

if not _has_handler_of_type(logger, logging.FileHandler):
	fh = logging.FileHandler(LOG_FILENAME, "a", "utf-8")
	fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
	logger.addHandler(fh)

class StreamQueueHandler(logging.Handler):
	def emit(self, record):
		try:
			msg = self.format(record)
			if "log_queue" in st.session_state:
				try:
					st.session_state.log_queue.put_nowait(msg)
				except Exception:
					pass
		except Exception:
			pass

if not _has_handler_of_type(logger, StreamQueueHandler):
	qh = StreamQueueHandler()
	qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
	logger.addHandler(qh)

# ================= Login function =================

def login_client_for_account(username: str, password: str, proxy: str = None):
	if Client is None:
		raise RuntimeError("instagrapi belum terinstall.")

	session_file = os.path.join(SESSION_DIR, f"session_{username}.json")
	cl = Client(proxy=proxy) if proxy else Client()

	try:
		if os.path.exists(session_file):
			try:
				cl.load_settings(session_file)
				cl.login(username, password)
				logger.info(f"[{username}] Login via session lama berhasil (proxy={proxy})")
				return cl
			except Exception as e:
				logger.warning(f"[{username}] Session lama gagal: {e}")
				try:
					os.remove(session_file)
				except Exception:
					pass

		cl.login(username, password)
		try:
			cl.dump_settings(session_file)
		except Exception:
			logger.warning(f"[{username}] Gagal menyimpan session")
		logger.info(f"[{username}] Login baru sukses (proxy={proxy})")
		return cl

	except TwoFactorRequired:
		raise RuntimeError("TwoFactorRequired")
	except ChallengeRequired:
		raise RuntimeError("ChallengeRequired")
	except ClientError as e:
		raise RuntimeError(f"Login gagal: {e}")
	except Exception as e:
		raise RuntimeError(str(e))

# ================= Worker =================

def run_buzzer_for_account(cl, username, target_post_url, comments, max_comments, counters,
						   like_min, like_max, comment_min, comment_max):
	try:
		pk = cl.media_pk_from_url(target_post_url)
		cl.media_info(pk)
	except Exception as e:
		logger.error(f"[{username}] Gagal ambil media: {e}")
		return

	try:
		cl.media_like(pk)
		logger.info(f"[{username}] Like sukses.")
		time.sleep(random.uniform(like_min, like_max))

		if counters.get(username, 0) < max_comments:
			komentar = random.choice(comments) if comments else "Nice!"
			cl.media_comment(pk, komentar)
			counters[username] = counters.get(username, 0) + 1
			logger.info(f"[{username}] Komentar: '{komentar}'")
			time.sleep(random.uniform(comment_min, comment_max))
	except Exception as e:
		logger.error(f"[{username}] Error like/comment: {e}")

def bot_worker(config):
	stop_event = config["stop_event"]
	client_dict = config["client_dict"]
	counters = {u: 0 for u in client_dict.keys()}
	logger.info("Worker mulai.")
	try:
		while not stop_event.is_set():
			for username, cl in list(client_dict.items()):
				if stop_event.is_set():
					break
				if counters.get(username, 0) >= config["max_comments_per_account"]:
					continue
				run_buzzer_for_account(
					cl, username, config["target_post_url"], config["comments"],
					config["max_comments_per_account"], counters,
					config["like_delay_min"], config["like_delay_max"],
					config["comment_delay_min"], config["comment_delay_max"]
				)
				delay = random.uniform(
					config["between_accounts_delay_min"],
					config["between_accounts_delay_max"]
				)
				time.sleep(delay)
			time.sleep(config["loop_wait_seconds"])
	finally:
		stop_event.set()
		logger.info("Worker berhenti.")

# ================= UI =================

require_login()
st.title("Aksi Sinergi IG Bot Stealth")
st.caption("Support proxy per akun untuk IP berbeda.")

# (SELURUH UI DI BAWAH INI TIDAK DIUBAH)

# ========== UI ==========

require_login()
st.title("Aksi Sinergi IG Bot Stealth")
st.caption("Support proxy per akun untuk IP berbeda.")

st.sidebar.header("Konfigurasi cepat")
target_post_url = st.sidebar.text_input("Target post URL")
raw_comments = st.sidebar.text_area("Komentar (pisah koma/newline)")
comments = [c.strip() for c in raw_comments.replace("\n", ",").split(",") if c.strip()]
max_comments_per_account = st.sidebar.number_input("Max komentar per akun", 0, 100, 1)
like_delay_min = st.sidebar.number_input("Like delay min (s)", 1.0, 9999.0, 5.0)
like_delay_max = st.sidebar.number_input("Like delay max (s)", 1.0, 9999.0, 10.0)
comment_delay_min = st.sidebar.number_input("Comment delay min (s)", 1.0, 9999.0, 20.0)
comment_delay_max = st.sidebar.number_input("Comment delay max (s)", 1.0, 9999.0, 35.0)
between_accounts_delay_min = st.sidebar.number_input("Delay antar akun min (s)", 1.0, 9999.0, 30.0)
between_accounts_delay_max = st.sidebar.number_input("Delay antar akun max (s)", 1.0, 9999.0, 60.0)
loop_wait_seconds = st.sidebar.number_input("Delay akhir putaran (s)", 1.0, 9999.0, 120.0)

st.sidebar.markdown("---")

if st.sidebar.button("Login All Accounts"):
	success, errors = [], []
	for acc in st.session_state.accounts:
		user, pwd, proxy = acc["username"], acc["password"], acc.get("proxy")
		try:
			cl = login_client_for_account(user, pwd, proxy)
			st.session_state.clients[user] = cl
			success.append(user)
		except Exception as e:
			errors.append(f"{user}: {e}")
	st.sidebar.success(f"Login selesai. Berhasil: {len(success)}. Gagal: {len(errors)}")

if st.sidebar.button("Logout All Accounts"):
	st.session_state.clients.clear()
	st.sidebar.info("Semua akun logout.")

st.sidebar.markdown("---")
st.sidebar.write("Akun tersimpan:")
for i, acc in enumerate(st.session_state.accounts):
	cols = st.sidebar.columns([3, 1, 1])
	proxy_show = acc.get("proxy") or "none"
	cols[0].write(f"{acc['username']} ({proxy_show})")
	if cols[1].button("Login", key=f"login_{i}"):
		try:
			cl = login_client_for_account(acc["username"], acc["password"], acc.get("proxy"))
			st.session_state.clients[acc["username"]] = cl
			st.sidebar.success(f"{acc['username']} login sukses")
		except Exception as e:
			st.sidebar.error(f"{acc['username']} gagal: {e}")
	if cols[2].button("Hapus", key=f"del_{i}"):
		st.session_state.accounts.pop(i)
		save_accounts_to_file(st.session_state.accounts)
		st.rerun()

# Tambah akun baru (dengan proxy)

st.markdown("## Manage Accounts")
with st.form("add_account_form", clear_on_submit=True):
	new_u = st.text_input("Username")
	new_p = st.text_input("Password", type="password")
	new_proxy = st.text_input("Proxy (optional) — format: [http://user:pass@ip:port](http://user:pass@ip:port)")
	add_submit = st.form_submit_button("Tambah akun")
	if add_submit:
		if new_u and new_p:
			st.session_state.accounts.append({
				"username": new_u.strip(),
				"password": new_p.strip(),
				"proxy": new_proxy.strip() if new_proxy.strip() else None
			})
			save_accounts_to_file(st.session_state.accounts)
			st.success(f"Akun {new_u} disimpan.")
		else:
			st.error("Isi username & password")

st.write("Akun tersimpan:", [a["username"] for a in st.session_state.accounts])
st.write("Akun login aktif:", list(st.session_state.clients.keys()))

# Start/stop bot

col1, col2 = st.columns(2)
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
if col1.button("Start Bot"):
	if not st.session_state.clients:
		st.error("Tidak ada akun login.")
	elif st.session_state.running:
		st.warning("Bot sudah berjalan.")
	else:
		config["client_dict"] = dict(st.session_state.clients)
		config["stop_event"] = st.session_state.stop_event
		st.session_state.stop_event.clear()
		st.session_state.running = True
		t = threading.Thread(target=bot_worker, args=(config,), daemon=True)
		st.session_state.worker_thread = t
		t.start()
		st.success("Worker mulai.")

if col2.button("Stop Bot"):
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
while not st.session_state.log_queue.empty():
	st.session_state.log_lines.append(st.session_state.log_queue.get_nowait())
log_display.text("\n".join(st.session_state.log_lines[-300:]))
if st.button("Refresh Log"):
	while not st.session_state.log_queue.empty():
		st.session_state.log_lines.append(st.session_state.log_queue.get_nowait())
	log_display.text("\n".join(st.session_state.log_lines[-300:]))
st.markdown("Stop Bot (soft) akan menghentikan worker secara aman tanpa kehilangan sesi.")
