import os
import re
import time
import json
import threading
import sys
import ctypes
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    import customtkinter as ctk
    import pystray
    from PIL import Image, ImageTk
    from send2trash import send2trash
    from plyer import notification
except ImportError:
    ctypes.windll.user32.MessageBoxW(
        0,
        "Missing libraries!\n\nRun:\n  pip install customtkinter pystray pillow send2trash plyer",
        "TXT Timer - Error",
        0x10
    )
    sys.exit(1)

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

DESKTOP = Path.home() / "Desktop"
DATA_DIR = Path.home() / "AppData" / "Local" / "TxtTimer"
DATA_FILE = DATA_DIR / "data.json"
LOG_FILE = DATA_DIR / "log.txt"

config = {
    "hard_delete": False,
    "notifications": True,
    "command": "del",
    "scan_interval": 5
}

scheduled = {}
lock = threading.Lock()
running = True
is_active = True
list_widgets = {}

def log(msg):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log(f"CRITICAL CRASH:\n{err_msg}")

sys.excepthook = handle_exception

def send_os_notification(msg):
    if not config.get("notifications", True):
        return
    def _notify():
        try:
            notification.notify(
                title="TXT Timer",
                message=msg,
                app_name="TXT Timer",
                timeout=5
            )
        except Exception as e:
            log(f"NOTIFY ERROR: {e}")
    threading.Thread(target=_notify, daemon=True).start()

def load_data():
    global config
    if not DATA_FILE.exists():
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
            
        if isinstance(raw, dict) and "settings" in raw:
            config.update(raw["settings"])
            tasks = raw.get("scheduled", {})
        else:
            tasks = raw if isinstance(raw, dict) else {}

        now = datetime.now()
        with lock:
            for fname, ts in tasks.items():
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt > now:
                        scheduled[fname] = dt
                        log(f"LOADED '{fname}'")
                    else:
                        execute_deletion(fname)
                except Exception:
                    continue
    except Exception as e:
        log(f"WARN failed to load data, resetting — {e}")

def save_data():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with lock:
            safe_scheduled = {k: v.isoformat() for k, v in scheduled.items()}
            safe_config = dict(config)
            
        data = {
            "settings": safe_config,
            "scheduled": safe_scheduled
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"ERROR saving data — {e}")

def execute_deletion(fname):
    fp = DESKTOP / fname
    if not fp.exists():
        log(f"NOT FOUND '{fname}'")
        return

    try:
        if config.get("hard_delete", False):
            fp.unlink()
            action_str = "permanently deleted"
        else:
            send2trash(str(fp))
            action_str = "moved to recycle bin"
            
        log(f"ACTION: '{fname}' {action_str}")
        send_os_notification(f"'{fname}' has been {action_str}.")
    except Exception as e:
        log(f"DELETE ERROR on '{fname}' — {e}")

def parse_delay(filename):
    try:
        cmd = config.get("command", "del")
        pattern = rf'{re.escape(cmd)}\((\d+(?:[.,]\d+)?)\)'
        m = re.search(pattern, filename, re.IGNORECASE)
        if not m:
            return None
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None

def _remaining_str(dt):
    secs = int((dt - datetime.now()).total_seconds())
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"{secs}s"
    hours = secs // 3600
    minutes = (secs % 3600) // 60
    seconds = secs % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"

def scan_once():
    if not DESKTOP.is_dir():
        return

    try:
        txts = {f.name for f in DESKTOP.glob("*.txt")}
    except Exception as e:
        log(f"DESKTOP READ ERROR: {e}")
        return

    now = datetime.now()
    changed = False

    with lock:
        for fname in txts:
            if fname not in scheduled:
                delay = parse_delay(fname)
                if delay is not None:
                    delete_at = now + timedelta(minutes=delay)
                    scheduled[fname] = delete_at
                    changed = True
                    log(f"QUEUED '{fname}'")

        expired = [f for f, dt in list(scheduled.items()) if now >= dt]
        for fname in expired:
            execute_deletion(fname)
            if fname in scheduled:
                del scheduled[fname]
            changed = True

        orphans = [f for f in list(scheduled.keys()) if f not in txts]
        for fname in orphans:
            log(f"CANCELLED '{fname}'")
            if fname in scheduled:
                del scheduled[fname]
            changed = True

    if changed:
        save_data()

def scanner_loop():
    while running:
        if is_active:
            try:
                scan_once()
            except Exception as e:
                log(f"SCAN LOOP ERROR: {e}")
                
        interval = max(1, config.get("scan_interval", 5))
        
        for _ in range(interval * 4):
            if not running:
                break
            time.sleep(0.25)

def get_icon_image():
    icon_path = resource_path("txtTimerLogo.ico")
    try:
        return Image.open(icon_path)
    except Exception:
        return Image.new("RGBA", (64, 64), (0, 0, 0, 0))

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

app = ctk.CTk()
app.title("TXT Timer")
app.geometry("900x550")
app.resizable(False, False)
app.configure(fg_color="#4F5268")

app.protocol("WM_DELETE_WINDOW", lambda: app.withdraw())

top_bar = ctk.CTkFrame(app, height=60, fg_color="#18181A", corner_radius=0)
top_bar.pack(fill="x", side="top")
top_bar.pack_propagate(False)

lbl_main_title = ctk.CTkLabel(top_bar, text="TXT Timer Dashboard", font=("Segoe UI", 18, "bold"), text_color="#FFFFFF")
lbl_main_title.pack(side="left", padx=20)

btn_toggle = ctk.CTkButton(top_bar, text="⏹ Stop System", font=("Segoe UI", 13, "bold"), fg_color="#C8102E", hover_color="#990C23", corner_radius=6, height=36, width=120)
btn_toggle.pack(side="right", padx=20)

main_area = ctk.CTkFrame(app, fg_color="transparent")
main_area.pack(fill="both", expand=True, padx=25, pady=25)

col_left = ctk.CTkFrame(main_area, fg_color="transparent", width=350)
col_left.pack(side="left", fill="y")
col_left.pack_propagate(False)

header_left = ctk.CTkFrame(col_left, fg_color="transparent")
header_left.pack(fill="x", pady=(0, 15))
ctk.CTkLabel(header_left, text="⚙", font=("Segoe UI", 16), text_color="#60A5FA").pack(side="left", padx=(0, 8))
ctk.CTkLabel(header_left, text="Configuration", font=("Segoe UI", 15, "bold"), text_color="#FFFFFF").pack(side="left")

card_left = ctk.CTkFrame(col_left, fg_color="#1F2025", corner_radius=12)
card_left.pack(fill="both", expand=True)

container_left = ctk.CTkFrame(card_left, fg_color="transparent")
container_left.pack(fill="both", expand=True, padx=25, pady=25)

row1 = ctk.CTkFrame(container_left, fg_color="transparent")
row1.pack(fill="x", pady=(0, 25))

icon_trash_frame = ctk.CTkFrame(row1, width=36, height=36, corner_radius=8, fg_color="#4A1522")
icon_trash_frame.pack(side="left", padx=(0, 15))
icon_trash_frame.pack_propagate(False)
ctk.CTkLabel(
    icon_trash_frame, text="🗑️", font=("Segoe UI", 14),
    fg_color="transparent", text_color="#F87171"
).place(relx=0.8, rely=0.45, anchor="center")

txt_fr1 = ctk.CTkFrame(row1, fg_color="transparent")
txt_fr1.pack(side="left")
ctk.CTkLabel(txt_fr1, text="Permanent Delete", font=("Segoe UI", 13, "bold"), text_color="#FFFFFF", height=18).pack(anchor="w")
ctk.CTkLabel(txt_fr1, text="Skip Recycle Bin", font=("Segoe UI", 11), text_color="#9CA3AF", height=16).pack(anchor="w")
switch_hard = ctk.CTkSwitch(row1, text="", width=40, progress_color="#4F46E5")
switch_hard.pack(side="right")

row2 = ctk.CTkFrame(container_left, fg_color="transparent")
row2.pack(fill="x", pady=(0, 30))

icon_bell_frame = ctk.CTkFrame(row2, width=36, height=36, corner_radius=8, fg_color="#1E2B4D")
icon_bell_frame.pack(side="left", padx=(0, 15))
icon_bell_frame.pack_propagate(False)
ctk.CTkLabel(
    icon_bell_frame, text="🔔", font=("Segoe UI", 14),
    fg_color="transparent", text_color="#60A5FA"
).place(relx=0.5, rely=0.45, anchor="center")

txt_fr2 = ctk.CTkFrame(row2, fg_color="transparent")
txt_fr2.pack(side="left")
ctk.CTkLabel(txt_fr2, text="OS Notifications", font=("Segoe UI", 13, "bold"), text_color="#FFFFFF", height=18).pack(anchor="w")
ctk.CTkLabel(txt_fr2, text="Enable system alerts", font=("Segoe UI", 11), text_color="#9CA3AF", height=16).pack(anchor="w")
switch_notify = ctk.CTkSwitch(row2, text="", width=40, progress_color="#4F46E5")
switch_notify.pack(side="right")


row3 = ctk.CTkFrame(container_left, fg_color="transparent")
row3.pack(fill="x", pady=(0, 10))
ctk.CTkLabel(row3, text=">_", font=("Consolas", 14, "bold"), text_color="#9CA3AF").pack(side="left", padx=(0, 8))
ctk.CTkLabel(row3, text="Trigger Command", font=("Segoe UI", 13, "bold"), text_color="#FFFFFF").pack(side="left")

entry_cmd = ctk.CTkEntry(container_left, fg_color="#15161A", border_color="#374151", text_color="#FFFFFF", font=("Segoe UI", 13), height=36)
entry_cmd.pack(fill="x", pady=(0, 25))

row4 = ctk.CTkFrame(container_left, fg_color="transparent")
row4.pack(fill="x", pady=(0, 10))
ctk.CTkLabel(row4, text="〰", font=("Segoe UI", 14, "bold"), text_color="#9CA3AF").pack(side="left", padx=(0, 8))
ctk.CTkLabel(row4, text="Scan Interval (s)", font=("Segoe UI", 13, "bold"), text_color="#FFFFFF").pack(side="left")

entry_interval = ctk.CTkEntry(container_left, fg_color="#15161A", border_color="#374151", text_color="#FFFFFF", font=("Segoe UI", 13), height=36)
entry_interval.pack(fill="x")

col_right = ctk.CTkFrame(main_area, fg_color="transparent")
col_right.pack(side="right", fill="both", expand=True, padx=(30, 0))

header_right = ctk.CTkFrame(col_right, fg_color="transparent")
header_right.pack(fill="x", pady=(0, 15))
ctk.CTkLabel(header_right, text="🕒", font=("Segoe UI", 16), text_color="#60A5FA").pack(side="left", padx=(0, 8))
ctk.CTkLabel(header_right, text="Active Countdown List", font=("Segoe UI", 15, "bold"), text_color="#FFFFFF").pack(side="left")
badge_items = ctk.CTkLabel(header_right, text="0 Items", font=("Segoe UI", 11, "bold"), fg_color="#18181A", text_color="#60A5FA", corner_radius=10, width=60, height=24)
badge_items.pack(side="right")

card_right = ctk.CTkFrame(col_right, fg_color="#1F2025", corner_radius=12)
card_right.pack(fill="both", expand=True)

list_header = ctk.CTkFrame(card_right, fg_color="transparent", height=40)
list_header.pack(fill="x", padx=25, pady=(15, 0))
ctk.CTkLabel(list_header, text="FILE NAME", font=("Segoe UI", 11, "bold"), text_color="#9CA3AF").pack(side="left")
ctk.CTkLabel(list_header, text="TIME REMAINING", font=("Segoe UI", 11, "bold"), text_color="#9CA3AF").pack(side="right")

divider = ctk.CTkFrame(card_right, fg_color="#374151", height=1)
divider.pack(fill="x", padx=10, pady=(5, 0))

frame_empty = ctk.CTkFrame(card_right, fg_color="transparent")
lbl_empty_icon = ctk.CTkLabel(frame_empty, text="📭", font=("Segoe UI", 48))
lbl_empty_title = ctk.CTkLabel(frame_empty, text="No active countdowns", font=("Segoe UI", 14, "bold"), text_color="#FFFFFF")
lbl_empty_sub = ctk.CTkLabel(frame_empty, text="Files waiting for deletion will appear here.", font=("Segoe UI", 12), text_color="#9CA3AF")
lbl_empty_icon.pack(pady=(60, 10))
lbl_empty_title.pack(pady=(0, 5))
lbl_empty_sub.pack()

scroll_list = ctk.CTkScrollableFrame(card_right, fg_color="transparent", bg_color="transparent")

def update_ui_state():
    if is_active:
        btn_toggle.configure(text="⏹ Stop System", fg_color="#C8102E", hover_color="#990C23")
    else:
        btn_toggle.configure(text="▶ Start System", fg_color="#10B981", hover_color="#059669")

def toggle_state():
    global is_active
    is_active = not is_active
    log(f"SYSTEM {'STARTED' if is_active else 'STOPPED'}")
    update_ui_state()

btn_toggle.configure(command=toggle_state)

def save_settings_from_ui(*args):
    config["hard_delete"] = bool(switch_hard.get())
    config["notifications"] = bool(switch_notify.get())
    
    cmd_val = entry_cmd.get().strip()
    if cmd_val:
        config["command"] = cmd_val
        
    try:
        interval_val = int(entry_interval.get().strip())
        config["scan_interval"] = max(1, interval_val)
    except ValueError:
        pass
        
    save_data()

switch_hard.configure(command=save_settings_from_ui)
switch_notify.configure(command=save_settings_from_ui)
entry_cmd.bind("<FocusOut>", save_settings_from_ui)
entry_cmd.bind("<Return>", save_settings_from_ui)
entry_interval.bind("<FocusOut>", save_settings_from_ui)
entry_interval.bind("<Return>", save_settings_from_ui)

def init_ui_values():
    if config.get("hard_delete"): switch_hard.select()
    if config.get("notifications"): switch_notify.select()
    entry_cmd.insert(0, config.get("command", "del"))
    entry_interval.insert(0, str(config.get("scan_interval", 5)))
    update_ui_state()

def refresh_ui_list():
    if not running:
        return

    try:
        with lock:
            items_count = len(scheduled)
            badge_items.configure(text=f"{items_count} Items")

            if items_count == 0:
                scroll_list.pack_forget()
                frame_empty.pack(expand=True, fill="both")
            else:
                frame_empty.pack_forget()
                scroll_list.pack(expand=True, fill="both", padx=15, pady=10)

                current_fnames = list(scheduled.keys())

                for fname, dt in scheduled.items():
                    if is_active:
                        rem = _remaining_str(dt)
                        time_color = "#9CA3AF"
                    else:
                        rem = "⏸ Paused"
                        time_color = "#F59E0B"

                    if fname in list_widgets:
                        list_widgets[fname]['time_lbl'].configure(
                            text=rem, text_color=time_color
                        )
                    else:
                        row = ctk.CTkFrame(scroll_list, fg_color="transparent")
                        row.pack(fill="x", pady=4)
                        n_lbl = ctk.CTkLabel(row, text=fname, font=("Segoe UI", 13), text_color="#FFFFFF")
                        n_lbl.pack(side="left", padx=10)
                        t_lbl = ctk.CTkLabel(row, text=rem, font=("Segoe UI", 13), text_color=time_color)
                        t_lbl.pack(side="right", padx=10)
                        list_widgets[fname] = {'frame': row, 'time_lbl': t_lbl}

                for f in list(list_widgets.keys()):
                    if f not in current_fnames:
                        list_widgets[f]['frame'].destroy()
                        del list_widgets[f]

    except Exception as e:
        log(f"UI REFRESH ERROR: {e}")

    app.after(1000, refresh_ui_list)

def quit_app(icon=None, item=None):
    global running
    running = False
    if icon:
        try:
            icon.stop()
        except Exception:
            pass
    app.quit()
    log("EXIT")

if ctypes.windll.kernel32.CreateMutexW(None, False, "TxtTimer_SingleInstance") and ctypes.windll.kernel32.GetLastError() == 183:
    ctypes.windll.user32.MessageBoxW(0, "TXT Timer is already running.", "TXT Timer", 0x30)
    sys.exit(0)

DATA_DIR.mkdir(parents=True, exist_ok=True)
log("STARTED")
load_data()
init_ui_values()

threading.Thread(target=scanner_loop, daemon=True).start()
app.after(1000, refresh_ui_list)

def setup_tray():
    try:
        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", lambda: app.after(0, app.deiconify), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", quit_app)
        )
        icon_img = get_icon_image()
        icon = pystray.Icon("TxtTimer", icon_img, "TXT Timer", menu)
        icon.run()
    except Exception as e:
        log(f"TRAY ERROR: {e}")

threading.Thread(target=setup_tray, daemon=True).start()

app.mainloop()