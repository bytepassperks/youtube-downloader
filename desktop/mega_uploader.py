#!/usr/bin/env python3
"""
MegaUploader - One-click local folder to iDrive S3 uploader with Telegram notification.

Usage:
1. Download files with MegaDownloader Unlimited (or any tool) to a local folder
2. Run this tool, point it to the folder
3. It auto-deletes excluded files, uploads everything to iDrive S3,
   and sends a Telegram notification with your portal link

Features:
- Configurable local folder path (saved for next time)
- Auto-deletes excluded files from all folders/subfolders before upload
- Preserves full folder structure in S3
- Parallel uploads (8 threads)
- Telegram notification with portal link
- Settings saved between runs
"""

import os
import sys
import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import boto3
from botocore.config import Config as BotoConfig

# ============================================================================
# LOGGING
# ============================================================================
LOG_DIR = os.path.join(os.path.expanduser("~"), "MegaTransfer", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"megauploader_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("MegaUploader")
logger.setLevel(logging.DEBUG)

fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(ch)

logger.info(f"Log file: {LOG_FILE}")

# ============================================================================
# SETTINGS
# ============================================================================
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), "MegaTransfer")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "uploader_settings.json")

DEFAULT_SETTINGS = {
    "idrive_endpoint": "https://s3.us-west-2.idrivee2.com",
    "idrive_access_key": "",
    "idrive_secret_key": "",
    "idrive_bucket": "mega-transfers",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "portal_domain": "https://bytecourses.online",
    "local_folder": "",
    "s3_prefix": "",  # Auto-detected from folder name if empty
    "exclude_files": ["edollarearn.com.url", "Upgrade Your Account VIP - edollarearn.com.txt"],
    "upload_threads": 8,
    "delete_after_upload": False,
}


def load_settings():
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
            settings.update(saved)
        except Exception as e:
            logger.warning(f"Could not load settings: {e}")
    return settings


def save_settings(settings):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save settings: {e}")


# ============================================================================
# UPLOADER ENGINE
# ============================================================================
class Uploader:
    def __init__(self, settings, log_callback=None, progress_callback=None):
        self.settings = settings
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.s3_client = None
        self._cancelled = False
        self._total_uploaded = 0
        self._total_files = 0
        self._files_done = 0
        self._lock = threading.Lock()

    def log(self, msg):
        logger.info(msg)
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                pass

    def _init_s3(self):
        self.s3_client = boto3.client(
            's3',
            endpoint_url=self.settings['idrive_endpoint'],
            aws_access_key_id=self.settings['idrive_access_key'],
            aws_secret_access_key=self.settings['idrive_secret_key'],
            config=BotoConfig(
                signature_version='s3v4',
                connect_timeout=30,
                read_timeout=300,
                retries={'max_attempts': 3, 'mode': 'adaptive'},
                max_pool_connections=20,
            )
        )
        self.log("S3 client initialized")

    def _delete_excluded_files(self, folder_path):
        """Delete excluded files from folder and all subfolders."""
        exclude_list = self.settings.get('exclude_files', [])
        if not exclude_list:
            return 0

        deleted = 0
        for root, dirs, files in os.walk(folder_path):
            for fname in files:
                if fname in exclude_list:
                    fpath = os.path.join(root, fname)
                    try:
                        os.remove(fpath)
                        rel = os.path.relpath(fpath, folder_path)
                        self.log(f"  [DELETE] {rel}")
                        deleted += 1
                    except Exception as e:
                        self.log(f"  [WARN] Could not delete {fname}: {e}")
        return deleted

    def _collect_files(self, folder_path):
        """Collect all files to upload with their relative paths."""
        files = []
        for root, dirs, filenames in os.walk(folder_path):
            for fname in filenames:
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, folder_path)
                size = os.path.getsize(fpath)
                files.append({
                    'local_path': fpath,
                    'rel_path': rel_path.replace('\\', '/'),  # Normalize to forward slashes
                    'size': size,
                })
        # Sort largest first for better parallelism
        files.sort(key=lambda f: f['size'], reverse=True)
        return files

    def _get_uploaded_files(self, s3_prefix):
        """Get set of already-uploaded files in S3 bucket."""
        uploaded = set()
        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(
                Bucket=self.settings['idrive_bucket'],
                Prefix=s3_prefix + '/'
            ):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    # Remove prefix to get relative path
                    rel = key[len(s3_prefix) + 1:]
                    if rel:
                        uploaded.add(rel)
        except Exception as e:
            self.log(f"  Could not check existing S3 files: {e}")
        return uploaded

    def _upload_part_with_retry(self, s3_key, upload_id, part_num, data, rel_path, max_retries=3):
        """Upload a single multipart part with retry logic."""
        for attempt in range(max_retries):
            try:
                part = self.s3_client.upload_part(
                    Bucket=self.settings['idrive_bucket'],
                    Key=s3_key, UploadId=upload_id,
                    PartNumber=part_num, Body=data)
                return part
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 5 * (attempt + 1)
                    self.log(f"  [{rel_path}] Part {part_num} failed (attempt {attempt+1}), retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    raise

    def _upload_file(self, local_path, s3_key, file_size, file_idx):
        """Upload a single file to S3 with retry logic."""
        if self._cancelled:
            return False

        rel_path = s3_key.split('/', 1)[1] if '/' in s3_key else s3_key
        size_mb = file_size / 1024 / 1024
        max_file_retries = 3

        for file_attempt in range(max_file_retries):
            if self._cancelled:
                return False

            try:
                start = time.time()

                if file_size > 16 * 1024 * 1024:  # >16 MB: multipart
                    chunk_size = 16 * 1024 * 1024  # 16 MB parts (smaller for iDrive compatibility)
                    mpu = self.s3_client.create_multipart_upload(
                        Bucket=self.settings['idrive_bucket'], Key=s3_key)
                    upload_id = mpu['UploadId']

                    try:
                        parts = []
                        part_num = 1
                        uploaded_bytes = 0

                        with open(local_path, 'rb') as f:
                            while True:
                                if self._cancelled:
                                    self.s3_client.abort_multipart_upload(
                                        Bucket=self.settings['idrive_bucket'],
                                        Key=s3_key, UploadId=upload_id)
                                    return False

                                data = f.read(chunk_size)
                                if not data:
                                    break

                                part = self._upload_part_with_retry(
                                    s3_key, upload_id, part_num, data, rel_path)
                                parts.append({'ETag': part['ETag'], 'PartNumber': part_num})
                                uploaded_bytes += len(data)
                                part_num += 1

                                pct = uploaded_bytes * 100 // file_size
                                elapsed = time.time() - start
                                speed = uploaded_bytes / elapsed if elapsed > 0 else 0
                                self.log(f"  [{file_idx}] {rel_path}: {pct}% ({speed/1024/1024:.1f} MB/s)")

                        self.s3_client.complete_multipart_upload(
                            Bucket=self.settings['idrive_bucket'], Key=s3_key,
                            UploadId=upload_id,
                            MultipartUpload={'Parts': parts})

                    except Exception:
                        try:
                            self.s3_client.abort_multipart_upload(
                                Bucket=self.settings['idrive_bucket'],
                                Key=s3_key, UploadId=upload_id)
                        except Exception:
                            pass
                        raise

                else:  # Small file: direct upload with retry
                    with open(local_path, 'rb') as f:
                        file_data = f.read()
                    self.s3_client.put_object(
                        Bucket=self.settings['idrive_bucket'],
                        Key=s3_key, Body=file_data)

                elapsed = time.time() - start
                speed = file_size / elapsed if elapsed > 0 else 0

                with self._lock:
                    self._files_done += 1
                    self._total_uploaded += file_size
                    done = self._files_done
                    total = self._total_files

                self.log(f"  [{done}/{total}] {rel_path} ({size_mb:.1f} MB, {speed/1024/1024:.1f} MB/s)")

                if self.progress_callback:
                    self.progress_callback(done, total, rel_path)

                return True

            except Exception as e:
                if file_attempt < max_file_retries - 1:
                    wait_time = 10 * (file_attempt + 1)
                    self.log(f"  [RETRY] {rel_path}: attempt {file_attempt+1} failed, retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    self.log(f"  [FAIL] {rel_path}: {e} (after {max_file_retries} attempts)")
                    return False

        return False

    def run(self, folder_path, s3_prefix=None):
        """Main upload flow."""
        if not os.path.isdir(folder_path):
            self.log(f"ERROR: Folder not found: {folder_path}")
            return False

        # Auto-detect S3 prefix from folder name
        if not s3_prefix:
            s3_prefix = os.path.basename(folder_path.rstrip('/\\'))
        if not s3_prefix:
            s3_prefix = "upload"

        self.log(f"{'='*60}")
        self.log(f"  MegaUploader - Local to iDrive S3")
        self.log(f"{'='*60}")
        self.log(f"Source: {folder_path}")
        self.log(f"Destination: s3://{self.settings['idrive_bucket']}/{s3_prefix}/")
        self.log("")

        # Step 1: Delete excluded files
        self.log("Step 1: Deleting excluded files...")
        exclude_list = self.settings.get('exclude_files', [])
        self.log(f"  Exclude list: {exclude_list}")
        deleted = self._delete_excluded_files(folder_path)
        self.log(f"  Deleted {deleted} excluded files")
        self.log("")

        # Step 2: Collect files
        self.log("Step 2: Scanning files...")
        files = self._collect_files(folder_path)
        total_size = sum(f['size'] for f in files)
        self.log(f"  Found {len(files)} files ({total_size/1024/1024:.0f} MB)")
        self.log("")

        if not files:
            self.log("No files to upload!")
            return True

        # Step 3: Init S3
        self.log("Step 3: Connecting to iDrive S3...")
        try:
            self._init_s3()
        except Exception as e:
            self.log(f"ERROR: Could not connect to S3: {e}")
            return False

        # Step 4: Check already uploaded
        self.log("Step 4: Checking already uploaded files...")
        uploaded = self._get_uploaded_files(s3_prefix)
        to_upload = [f for f in files if f['rel_path'] not in uploaded]
        skipped = len(files) - len(to_upload)
        if skipped > 0:
            self.log(f"  Skipping {skipped} already-uploaded files")
        upload_size = sum(f['size'] for f in to_upload)
        self.log(f"  Uploading {len(to_upload)} files ({upload_size/1024/1024:.0f} MB)")
        self.log("")

        if not to_upload:
            self.log("All files already uploaded!")
            self._send_telegram(s3_prefix, len(files), len(files),
                               total_size, 0, 0, [])
            return True

        # Step 5: Upload
        self.log(f"Step 5: Uploading ({self.settings.get('upload_threads', 8)} threads)...")
        self._total_files = len(to_upload)
        self._files_done = 0
        self._total_uploaded = 0
        start_time = time.time()

        failed_files = []
        threads = self.settings.get('upload_threads', 8)

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {}
            for idx, f in enumerate(to_upload):
                s3_key = f"{s3_prefix}/{f['rel_path']}"
                future = executor.submit(
                    self._upload_file, f['local_path'], s3_key,
                    f['size'], idx + 1)
                futures[future] = f

            for future in as_completed(futures):
                f = futures[future]
                try:
                    if not future.result():
                        failed_files.append(f['rel_path'])
                except Exception as e:
                    self.log(f"  [ERROR] {f['rel_path']}: {e}")
                    failed_files.append(f['rel_path'])

        total_time = time.time() - start_time
        avg_speed = self._total_uploaded / total_time if total_time > 0 else 0
        completed = len(to_upload) - len(failed_files)

        self.log("")
        self.log(f"{'='*60}")
        self.log(f"  Upload Complete!")
        self.log(f"{'='*60}")
        self.log(f"Files: {completed}/{len(to_upload)} uploaded ({skipped} skipped)")
        self.log(f"Size: {self._total_uploaded/1024/1024:.0f} MB")
        self.log(f"Time: {total_time:.0f}s @ {avg_speed/1024/1024:.1f} MB/s avg")
        if failed_files:
            self.log(f"Failed: {len(failed_files)} files")
            for ff in failed_files[:10]:
                self.log(f"  - {ff}")

        # Step 6: Telegram
        self.log("")
        self.log("Step 6: Sending Telegram notification...")
        self._send_telegram(s3_prefix, completed + skipped, len(files),
                           total_size, total_time, avg_speed, failed_files)

        self.log("")
        self.log("Done!")
        return len(failed_files) == 0

    def _send_telegram(self, s3_prefix, completed, total, total_size,
                       total_time, avg_speed, failed_files):
        bot_token = self.settings.get('telegram_bot_token', '')
        chat_id = self.settings.get('telegram_chat_id', '')
        portal_domain = self.settings.get('portal_domain', '').rstrip('/')

        if not bot_token or not chat_id:
            self.log("  Telegram not configured, skipping")
            return

        portal_url = f"{portal_domain}/content/{s3_prefix}"
        status = "\u2705" if not failed_files else "\u26a0\ufe0f"
        msg = (
            f"{status} <b>Upload Complete</b>\n\n"
            f"<b>{s3_prefix}</b>\n"
            f"Files: {completed}/{total}\n"
            f"Size: {total_size/1024/1024:.0f} MB\n"
        )
        if total_time > 0:
            msg += f"Time: {total_time:.0f}s @ {avg_speed/1024/1024:.1f} MB/s\n"
        if failed_files:
            msg += f"Failed: {len(failed_files)} files\n"
        msg += f"\n\U0001F517 <a href=\"{portal_url}\">View in Portal</a>"

        try:
            tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = requests.post(tg_url, json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=15)
            if resp.ok:
                self.log("  Telegram notification sent!")
            else:
                self.log(f"  Telegram error: {resp.text}")
        except Exception as e:
            self.log(f"  Telegram failed: {e}")

    def cancel(self):
        self._cancelled = True
        self.log("Cancelling...")


# ============================================================================
# GUI
# ============================================================================
def _init_gui():
    """Try to import CustomTkinter, fall back to tkinter."""
    try:
        import customtkinter as ctk
        return ctk, True
    except ImportError:
        pass
    try:
        import tkinter as tk
        return tk, False
    except ImportError:
        return None, False


class MegaUploaderApp:
    def __init__(self):
        self.settings = load_settings()
        self.uploader = None
        self._running = False

        gui_mod, self.has_ctk = _init_gui()
        if gui_mod is None:
            logger.error("No GUI available, running in CLI mode")
            self._run_cli()
            return

        self.gui = gui_mod
        if self.has_ctk:
            self.gui.set_appearance_mode("dark")
            self.gui.set_default_color_theme("blue")
            self.root = self.gui.CTk()
        else:
            self.root = self.gui.Tk()

        self.root.title("MegaUploader - Local to iDrive S3")
        self.root.geometry("700x600")
        self._build_ui()

    def _build_ui(self):
        if self.has_ctk:
            self._build_ctk_ui()
        else:
            self._build_tk_ui()

    def _build_ctk_ui(self):
        ctk = self.gui
        main = ctk.CTkFrame(self.root)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # Title
        ctk.CTkLabel(main, text="MegaUploader", font=("Arial", 20, "bold")).pack(pady=(10, 5))
        ctk.CTkLabel(main, text="Upload local files to iDrive S3 + Telegram notification",
                     font=("Arial", 12)).pack(pady=(0, 10))

        # Settings frame
        settings_frame = ctk.CTkFrame(main)
        settings_frame.pack(fill="x", padx=10, pady=5)

        row = 0
        self.entries = {}

        fields = [
            ("Local Folder", "local_folder", True),
            ("S3 Prefix (auto from folder name)", "s3_prefix", False),
            ("iDrive Endpoint", "idrive_endpoint", False),
            ("iDrive Access Key", "idrive_access_key", False),
            ("iDrive Secret Key", "idrive_secret_key", False),
            ("iDrive Bucket", "idrive_bucket", False),
            ("Telegram Bot Token", "telegram_bot_token", False),
            ("Telegram Chat ID", "telegram_chat_id", False),
            ("Portal Domain", "portal_domain", False),
            ("Exclude Files (comma-sep)", "exclude_files_str", False),
        ]

        for label, key, has_browse in fields:
            ctk.CTkLabel(settings_frame, text=label, anchor="w").grid(
                row=row, column=0, sticky="w", padx=5, pady=2)

            entry = ctk.CTkEntry(settings_frame, width=350)
            entry.grid(row=row, column=1, sticky="ew", padx=5, pady=2)

            if key == "exclude_files_str":
                val = ", ".join(self.settings.get('exclude_files', []))
            else:
                val = str(self.settings.get(key, ''))
            entry.insert(0, val)
            self.entries[key] = entry

            if has_browse:
                btn = ctk.CTkButton(settings_frame, text="Browse", width=70,
                                    command=self._browse_folder)
                btn.grid(row=row, column=2, padx=5, pady=2)

            row += 1

        settings_frame.columnconfigure(1, weight=1)

        # Buttons
        btn_frame = ctk.CTkFrame(main)
        btn_frame.pack(fill="x", padx=10, pady=10)

        self.start_btn = ctk.CTkButton(btn_frame, text="Upload to iDrive",
                                       command=self._start_upload,
                                       font=("Arial", 14, "bold"),
                                       height=40, fg_color="green")
        self.start_btn.pack(side="left", padx=5, expand=True, fill="x")

        self.cancel_btn = ctk.CTkButton(btn_frame, text="Cancel",
                                        command=self._cancel_upload,
                                        height=40, fg_color="red",
                                        state="disabled")
        self.cancel_btn.pack(side="left", padx=5)

        # Progress
        self.progress_label = ctk.CTkLabel(main, text="Ready", font=("Arial", 12))
        self.progress_label.pack(pady=5)

        self.progress_bar = ctk.CTkProgressBar(main)
        self.progress_bar.pack(fill="x", padx=10, pady=5)
        self.progress_bar.set(0)

        # Log
        self.log_text = ctk.CTkTextbox(main, height=200)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=5)

    def _build_tk_ui(self):
        tk = self.gui
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        tk.Label(main, text="MegaUploader", font=("Arial", 16, "bold")).pack(pady=5)

        settings_frame = tk.LabelFrame(main, text="Settings")
        settings_frame.pack(fill="x", padx=5, pady=5)

        self.entries = {}
        fields = [
            ("Local Folder", "local_folder", True),
            ("S3 Prefix", "s3_prefix", False),
            ("iDrive Endpoint", "idrive_endpoint", False),
            ("iDrive Access Key", "idrive_access_key", False),
            ("iDrive Secret Key", "idrive_secret_key", False),
            ("iDrive Bucket", "idrive_bucket", False),
            ("Telegram Bot Token", "telegram_bot_token", False),
            ("Telegram Chat ID", "telegram_chat_id", False),
            ("Portal Domain", "portal_domain", False),
            ("Exclude Files", "exclude_files_str", False),
        ]

        for row, (label, key, has_browse) in enumerate(fields):
            tk.Label(settings_frame, text=label, anchor="w").grid(
                row=row, column=0, sticky="w", padx=5, pady=1)
            entry = tk.Entry(settings_frame, width=50)
            entry.grid(row=row, column=1, sticky="ew", padx=5, pady=1)
            if key == "exclude_files_str":
                val = ", ".join(self.settings.get('exclude_files', []))
            else:
                val = str(self.settings.get(key, ''))
            entry.insert(0, val)
            self.entries[key] = entry

            if has_browse:
                btn = tk.Button(settings_frame, text="Browse",
                                command=self._browse_folder)
                btn.grid(row=row, column=2, padx=5, pady=1)

        settings_frame.columnconfigure(1, weight=1)

        btn_frame = tk.Frame(main)
        btn_frame.pack(fill="x", padx=5, pady=5)

        self.start_btn = tk.Button(btn_frame, text="Upload to iDrive",
                                   command=self._start_upload, bg="green", fg="white")
        self.start_btn.pack(side="left", padx=5, expand=True, fill="x")

        self.cancel_btn = tk.Button(btn_frame, text="Cancel",
                                    command=self._cancel_upload, bg="red", fg="white",
                                    state="disabled")
        self.cancel_btn.pack(side="left", padx=5)

        self.progress_label = tk.Label(main, text="Ready")
        self.progress_label.pack(pady=3)

        log_frame = tk.Frame(main)
        log_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _browse_folder(self):
        try:
            from tkinter import filedialog
            folder = filedialog.askdirectory(title="Select folder to upload")
            if folder:
                entry = self.entries.get('local_folder')
                if entry:
                    if self.has_ctk:
                        entry.delete(0, 'end')
                    else:
                        entry.delete(0, 'end')
                    entry.insert(0, folder)
        except Exception as e:
            logger.error(f"Browse error: {e}")

    def _save_current_settings(self):
        """Read settings from UI entries and save."""
        for key, entry in self.entries.items():
            val = entry.get().strip()
            if key == "exclude_files_str":
                self.settings['exclude_files'] = [
                    f.strip() for f in val.split(',') if f.strip()
                ]
            else:
                self.settings[key] = val
        save_settings(self.settings)
        self._log("Settings saved!")

    def _log(self, msg):
        """Add message to log textbox."""
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        line = f"{timestamp} {msg}\n"

        def update():
            try:
                if self.has_ctk:
                    self.log_text.insert("end", line)
                    self.log_text.see("end")
                else:
                    self.log_text.insert("end", line)
                    self.log_text.see("end")
            except Exception:
                pass

        try:
            self.root.after(0, update)
        except Exception:
            pass

    def _update_progress(self, done, total, name):
        def update():
            try:
                pct = done / total if total > 0 else 0
                if self.has_ctk:
                    self.progress_bar.set(pct)
                    self.progress_label.configure(text=f"{done}/{total} files ({pct*100:.0f}%)")
                else:
                    self.progress_label.configure(text=f"{done}/{total} files ({pct*100:.0f}%)")
            except Exception:
                pass

        try:
            self.root.after(0, update)
        except Exception:
            pass

    def _start_upload(self):
        if self._running:
            return

        self._save_current_settings()

        folder = self.settings.get('local_folder', '').strip()
        if not folder:
            self._log("ERROR: Please select a local folder to upload!")
            return

        if not os.path.isdir(folder):
            self._log(f"ERROR: Folder not found: {folder}")
            return

        if not self.settings.get('idrive_access_key') or not self.settings.get('idrive_secret_key'):
            self._log("ERROR: iDrive access key and secret key required!")
            return

        self._running = True
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        s3_prefix = self.settings.get('s3_prefix', '').strip()

        def run_upload():
            try:
                self.uploader = Uploader(
                    self.settings,
                    log_callback=self._log,
                    progress_callback=self._update_progress
                )
                self.uploader.run(folder, s3_prefix or None)
            except Exception as e:
                self._log(f"ERROR: {e}")
            finally:
                self._running = False
                try:
                    self.root.after(0, lambda: self.start_btn.configure(state="normal"))
                    self.root.after(0, lambda: self.cancel_btn.configure(state="disabled"))
                    self.root.after(0, lambda: self.progress_label.configure(text="Done!"))
                except Exception:
                    pass

        t = threading.Thread(target=run_upload, daemon=True)
        t.start()

    def _cancel_upload(self):
        if self.uploader:
            self.uploader.cancel()
            self._log("Cancelling upload...")

    def _run_cli(self):
        """CLI mode when no GUI is available."""
        print("MegaUploader - CLI Mode")
        print("=" * 40)

        folder = input("Local folder path: ").strip()
        if not folder or not os.path.isdir(folder):
            print(f"ERROR: Invalid folder: {folder}")
            return

        s3_prefix = input(f"S3 prefix (Enter for '{os.path.basename(folder)}'): ").strip()

        uploader = Uploader(self.settings, log_callback=print)
        uploader.run(folder, s3_prefix or None)

    def run(self):
        if hasattr(self, 'root'):
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
            self.root.mainloop()

    def _on_close(self):
        if self.uploader:
            self.uploader.cancel()
        self.root.destroy()


# ============================================================================
# MAIN
# ============================================================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        settings = load_settings()
        folder = sys.argv[2] if len(sys.argv) > 2 else input("Folder: ").strip()
        prefix = sys.argv[3] if len(sys.argv) > 3 else None
        uploader = Uploader(settings, log_callback=print)
        success = uploader.run(folder, prefix)
        sys.exit(0 if success else 1)
    else:
        app = MegaUploaderApp()
        app.run()
