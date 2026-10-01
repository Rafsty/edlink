#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Edlink Auto Bot
- Multi-account support (otomatis memproses daftar akun)
- Auto login & relogin saat JWT token expired / invalid
- Auto belajar materi (view detail & actions/collect)
- Auto claim semua quest yang berstatus 'R' (Ready)
- Mode Loop untuk dijalankan dengan PM2 / Background service
"""

import os
import sys
import json
import time
import uuid
import argparse

try:
    import requests
except ImportError:
    print("[!] Modul 'requests' belum terinstall. Menginstall 'requests'...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

API_BASE = "https://api.edlink.id/api/v1.4"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://edlink.id",
    "Referer": "https://edlink.id/",
    "X-App-Locale": "en"
}


class EdlinkBot:
    def __init__(self, account_config):
        self.config = account_config
        self.email = self.config.get("email", "")
        self.password = self.config.get("password", "")

        # Buat session file terisolasi untuk tiap akun
        safe_email = "".join(c for c in self.email if c.isalnum() or c in "@._-") or "unknown"
        self.session_path = os.path.join(BASE_DIR, f"session_{safe_email}.json")

        self.token = None
        self.user_data = {}
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.load_session()

    def load_session(self):
        if os.path.exists(self.session_path):
            try:
                with open(self.session_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    saved_user = data.get("user_data", {})
                    if saved_user.get("email") != self.email:
                        self.token = None
                        self.user_data = {}
                        return

                    self.token = data.get("token")
                    self.user_data = saved_user
                    if self.token:
                        self.session.headers["Authorization"] = f"Bearer {self.token}"
            except Exception as e:
                print(f"[!] Gagal membaca session: {e}", flush=True)

    def save_session(self):
        data = {
            "token": self.token,
            "user_data": self.user_data,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(self.session_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def login(self):
        if not self.email or not self.password or self.email == "your_email@example.com":
            print("[ERROR] Email atau password belum diatur dengan benar!", flush=True)
            return False

        print(f"[*] Melakukan login untuk: {self.email}...", flush=True)
        url = f"{API_BASE}/site/login"

        device_info = {
            "secureId": str(uuid.uuid4()),
            "name": "Chrome",
            "manufacture": "Google",
            "model": "Chrome",
            "product": "Chrome",
            "hardware": "Linux",
            "version": "154.0.0.0",
            "regId": None
        }

        payload = {
            "username": "",
            "app_id": "",
            "email": self.email,
            "password": self.password,
            "web_reg_id": None,
            "device": json.dumps(device_info)
        }

        login_headers = dict(DEFAULT_HEADERS)
        login_headers["X-Referer"] = "https://edlink.id/login"

        try:
            resp = self.session.post(url, headers=login_headers, json=payload, timeout=20)
            if resp.status_code != 200:
                print(f"[ERROR] Login gagal. Status: {resp.status_code}, Respon: {resp.text}", flush=True)
                return False

            res_json = resp.json()
            data = res_json.get("data", {})
            if not data:
                app_sys = res_json.get("applicationSystem", {})
                print(f"[ERROR] Login gagal: {app_sys.get('message', 'Email / Password salah')}", flush=True)
                return False

            user_token = data.get("userToken", {}).get("token")
            if not user_token:
                print(f"[ERROR] Token tidak ditemukan dalam respon login: {res_json}", flush=True)
                return False

            self.token = user_token
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            self.user_data = {
                "id": data.get("id"),
                "name": data.get("name"),
                "email": data.get("email"),
                "activeAccount": data.get("activeAccount")
            }
            self.save_session()
            print(f"[SUCCESS] Berhasil login sebagai: {self.user_data.get('name')} (ID: {self.user_data.get('id')})", flush=True)

            # Inisialisasi event login
            try:
                self.session.post(f"{API_BASE}/tracker", json={"action": "view Beranda", "result": 1}, timeout=10)
                self.session.post(f"{API_BASE}/user/profile", timeout=10)
            except Exception:
                pass

            return True

        except Exception as e:
            print(f"[ERROR] Exception saat login: {e}", flush=True)
            return False

    def api_request(self, method, endpoint, **kwargs):
        """Wrapper request dengan auto-relog jika token expired / invalid dan retry jaringan"""
        if not self.token:
            if not self.login():
                return None

        url = f"{API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.request(method, url, timeout=20, **kwargs)

                # 1. Cek status HTTP 401/403
                if resp.status_code in [401, 403]:
                    print("[!] Token expired / tidak valid (HTTP 401/403). Mencoba relogin otomatis...", flush=True)
                    if self.login():
                        continue
                    else:
                        print("[ERROR] Relogin gagal!", flush=True)
                        return None

                # 2. Cek pesan sesi expired ala Edlink
                try:
                    res_json = resp.json()
                    app_sys = res_json.get("applicationSystem", {})
                    code = app_sys.get("code")
                    message = str(app_sys.get("message", "")).lower()

                    if (code not in [0, None]) or ("login kembali" in message) or ("refresh halaman" in message):
                        print(f"[!] Sesi kedaluwarsa dari server ({app_sys.get('message')}). Mencoba relogin otomatis...", flush=True)
                        if self.login():
                            continue
                        else:
                            print("[ERROR] Relogin gagal!", flush=True)
                            return None
                except Exception:
                    pass

                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                print(f"[!] Gangguan koneksi saat request {url} (percobaan {attempt + 1}/{max_retries}): {e}", flush=True)
                time.sleep(2)
            except Exception as e:
                print(f"[ERROR] Request ke {url} gagal: {e}", flush=True)
                return None

        return None

    def get_profile(self):
        """Mendapatkan profil gamification quest poin & rank"""
        resp = self.api_request("GET", "/quest/profile")
        if resp and resp.status_code == 200:
            try:
                data = resp.json().get("data")
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def get_quests(self):
        """Mendapatkan daftar quest harian dan statusnya"""
        resp = self.api_request("GET", "/quest")
        if resp and resp.status_code == 200:
            try:
                data = resp.json().get("data")
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def claim_quest(self, criteria, title=""):
        """Mengklaim reward quest berdasarkan criteria"""
        print(f"[*] Mengklaim quest: {title or criteria} ({criteria})...", flush=True)
        payload = {"criteria": criteria}
        resp = self.api_request("POST", "/quest/claim", json=payload)

        if resp and resp.status_code == 200:
            try:
                data = resp.json().get("data", {})
                points = data.get("points", 0)
                total = data.get("totalPoint", 0)
                print(f"[+] BERHASIL KLAIM! +{points} Poin (Total Poin Sekarang: {total})", flush=True)
                return True
            except Exception:
                print("[+] Quest berhasil diklaim!", flush=True)
                return True
        else:
            msg = resp.text if resp else "Tidak ada respon"
            print(f"[-] Gagal klaim {criteria}: {msg}", flush=True)
            return False

    def claim_all_available_quests(self):
        """Klaim semua quest yang berstatus 'R' (Ready)"""
        print("\n--- [CEK & KLAIM QUEST] ---", flush=True)
        quests = self.get_quests()
        if not quests:
            print("[i] Tidak ada daftar quest ditemukan.", flush=True)
            return 0

        claimed_count = 0
        for q in quests:
            title = q.get("title", "")
            crit = q.get("criteria", "")
            point = q.get("point", 0)
            status = q.get("status")

            status_str = "Siap Diklaim (READY)" if status == "R" else ("Sudah Diklaim" if status == "C" else "Belum Selesai")
            print(f" - [{status_str}] {title} ({crit}) - {point} Poin", flush=True)

            if status == "R":
                time.sleep(1)
                if self.claim_quest(crit, title):
                    claimed_count += 1

        if claimed_count == 0:
            print("[i] Tidak ada quest yang siap diklaim saat ini.", flush=True)
        else:
            print(f"[SUCCESS] Berhasil mengklaim {claimed_count} quest!", flush=True)
        return claimed_count

    def get_academic_classes(self):
        """Mengambil daftar kelas akademik yang diikuti"""
        resp = self.api_request("POST", "/group/academic-lists")
        if resp and resp.status_code == 200:
            try:
                data = resp.json().get("data")
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def get_class_posts(self, group_id):
        """Mengambil postingan materi dari sebuah kelas"""
        payload = {
            "dataProvider": json.dumps({
                "criterion": [
                    {"operator": "EQ", "value": "", "criteria": "type"},
                    {"operator": "LK", "value": "", "criteria": "title,description"}
                ],
                "page": {"count": 0, "current": 1, "limit": 0, "next": 2, "previous": 1, "total": 0},
                "sort": []
            }),
            "group_team_id": None,
            "accept_assessment_team": True
        }
        resp = self.api_request("POST", f"/post/group/{group_id}?comment=true", json=payload)
        if resp and resp.status_code == 200:
            try:
                res_data = resp.json().get("data")
                if isinstance(res_data, dict):
                    posts = res_data.get("data")
                    return posts if isinstance(posts, list) else []
            except Exception:
                return []
        return []

    def learn_material(self, group_id, member_id, material_id, section_id=None, title=""):
        """Simulasi alur belajar materi lengkap"""
        print(f"[*] Belajar materi: '{title}' (ID: {material_id})...", flush=True)

        # 1. Section detail
        if section_id:
            self.api_request("POST", f"/sections/detail/{section_id}", json=None)

        # 2. Material detail
        self.api_request("POST", f"/learningmaterials/detail/{material_id}")

        # 3. Tracker
        self.api_request("POST", "/tracker", json={"action": "view Detail Bahan Ajar Kelas Akademik", "result": 1})

        # 4. Actions Collect
        collect_payload = {
            "category": "learning_material",
            "action": "view_detail_learning_material",
            "reference_id": material_id,
            "u_type": "gm",
            "u_id": member_id,
            "value": None
        }
        resp_collect = self.api_request("POST", "/actions/collect", json=collect_payload)
        if resp_collect and resp_collect.status_code == 200:
            print(f"[+] Sukses submit progress belajar materi '{title}'", flush=True)
            return True
        else:
            print(f"[-] Gagal submit progress materi '{title}'", flush=True)
            return False

    def auto_learn_materials(self):
        """Otomatis mencari materi pada kelas akademik lalu membaca/mempelajarinya"""
        print("\n--- [PROSES BELAJAR MATERI] ---", flush=True)
        classes = self.get_academic_classes()
        if not classes:
            print("[i] Tidak ada kelas akademik ditemukan.", flush=True)
            return

        max_learn = self.config.get("max_materials_to_learn", 5)
        learned_count = 0

        for cls in classes:
            group_id = cls.get("id")
            class_name = cls.get("name")
            member_id = cls.get("memberId")

            print(f"\n[>] Mengecek kelas: {class_name} (Group ID: {group_id}, Member ID: {member_id})", flush=True)
            posts = self.get_class_posts(group_id)

            for post in posts:
                post_id = post.get("id")
                post_title = post.get("title", "")
                section_id = post.get("classScheduleId")

                if "materi" in post_title.lower() or post.get("type") == "M" or section_id:
                    self.learn_material(group_id, member_id, post_id, section_id, post_title)
                    learned_count += 1
                    time.sleep(2)

                    if learned_count >= max_learn:
                        print(f"[i] Batas maksimal materi tercapai ({max_learn} materi).", flush=True)
                        return

        print(f"[SUCCESS] Selesai mempelajari {learned_count} materi.", flush=True)

    def run(self):
        print("\n" + "=" * 60, flush=True)
        print(f" PROSES AKUN: {self.email} ", flush=True)
        print("=" * 60, flush=True)

        if not self.token:
            if not self.login():
                print(f"[FATAL] Gagal login untuk {self.email}. Melewati akun ini.", flush=True)
                return

        profile = self.get_profile()
        cur_point = profile.get("currentPoint", "-")
        cur_rank = profile.get("currentRank", "-")
        print(f"\n[PROFIL AWAL] Poin: {cur_point} | Rank: {cur_rank}", flush=True)

        print("\n--- [STATUS QUEST HARI INI] ---", flush=True)
        quests = self.get_quests()
        read_material_status = None
        for q in quests:
            status = q.get("status")
            status_str = "Siap Diklaim (READY)" if status == "R" else ("Sudah Diklaim (CLAIMED)" if status == "C" else "Belum Selesai (PENDING)")
            print(f" - [{status_str}] {q.get('title')} ({q.get('criteria')}) : {q.get('point')} Poin", flush=True)
            if q.get("criteria") == "read_material":
                read_material_status = status

        if self.config.get("auto_learn", True):
            if read_material_status == "C":
                print("\n[i] Misi 'Mempelajari materi' sudah CLAIMED (C) untuk hari ini.", flush=True)
            else:
                self.auto_learn_materials()

        self.claim_all_available_quests()

        profile_after = self.get_profile()
        new_point = profile_after.get("currentPoint", cur_point)
        new_rank = profile_after.get("currentRank", cur_rank)
        print(f"\n[PROFIL AKHIR] Poin: {new_point} | Rank: {new_rank}", flush=True)
        print(f"[SUCCESS] Selesai memproses akun: {self.email}", flush=True)


def load_all_accounts(config_file):
    if not os.path.exists(config_file):
        print(f"[ERROR] File konfigurasi {config_file} tidak ditemukan!")
        sys.exit(1)

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Mendukung format multi-account: {"accounts": [{...}, {...}]}
    if "accounts" in data and isinstance(data["accounts"], list):
        return data["accounts"]

    # Mendukung format single-account: {"email": "...", "password": "..."}
    return [data]


def main():
    parser = argparse.ArgumentParser(description="Edlink Auto Claim & Learning Bot")
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="Path ke file config.json")
    parser.add_argument("--loop", type=float, default=0, help="Jalankan berkala setiap X jam (misal: --loop 12)")
    args = parser.parse_args()

    while True:
        print("\n" + "#" * 60, flush=True)
        print(f" MULAI PUTARAN BOT - {time.strftime('%Y-%m-%d %H:%M:%S')} ", flush=True)
        print("#" * 60, flush=True)

        accounts = load_all_accounts(args.config)
        print(f"[*] Terdeteksi {len(accounts)} akun untuk diproses.\n", flush=True)

        for idx, acc in enumerate(accounts, 1):
            print(f"\n>>> [Akun {idx}/{len(accounts)}] <<<", flush=True)
            try:
                bot = EdlinkBot(acc)
                bot.run()
            except Exception as e:
                print(f"[ERROR] Gagal memproses akun {acc.get('email')}: {e}", flush=True)

            if idx < len(accounts):
                time.sleep(3)  # Jeda antar akun

        print("\n" + "#" * 60, flush=True)
        print(f" SEMUA AKUN SELESAI DIPROSES - {time.strftime('%Y-%m-%d %H:%M:%S')} ", flush=True)
        print("#" * 60, flush=True)

        if args.loop <= 0:
            break

        sleep_seconds = int(args.loop * 3600)
        print(f"\n[SLEEP] Menunggu {args.loop} jam ({sleep_seconds} detik) hingga putaran berikutnya...", flush=True)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
